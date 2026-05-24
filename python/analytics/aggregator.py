"""Periodic snapshotter.

Pulls live state from the chain at a fixed interval and writes it to the
analytics DB. Three kinds of data:

  1. Prices (one row per asset per tick)
  2. Reserve snapshots (utilization, rates, indices)
  3. Position snapshots (one row per known user per tick)

The dashboard reads from these tables; the risk engine reads prices for
volatility estimation.

Why a separate process from the indexer? The indexer is event-driven
(reacts to on-chain activity). The snapshotter is time-driven — it polls
state on a schedule whether or not there's been activity. This matters
for volatility estimation: prices change continuously, but only the
oracle's `setPrice` would emit an event.
"""
from __future__ import annotations

import logging
import time

from python.analytics.db import (
    session,
    write_position,
    write_price,
    write_reserve_snapshot,
)
from python.chain import Client
from python.config import POLL_INTERVAL_SECS
from python.liquidator.event_indexer import Indexer

log = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECS = float(60.0)  # one snapshot per minute is plenty


class Aggregator:
    def __init__(self, client: Client, indexer: Indexer | None = None):
        self.c = client
        self.indexer = indexer or Indexer(client)

    def snapshot_once(self) -> None:
        ts = self.c.block_timestamp("latest")
        reserves = self.c.get_reserves_list()

        with session() as conn:
            # ---- prices ---------------------------------------------
            for asset in reserves:
                try:
                    price = self.c.get_price(asset)
                    write_price(conn, asset, ts, price)
                except Exception as e:
                    log.warning("price fetch failed for %s: %s", asset, e)

            # ---- reserve state --------------------------------------
            for asset in reserves:
                try:
                    r = self.c.get_reserve_data(asset)
                    write_reserve_snapshot(conn, asset, ts, {
                        "total_supply": r.total_supply,
                        "total_borrow": r.total_borrow,
                        "utilization_wad": r.utilization_wad,
                        "borrow_apr_wad": r.borrow_apr_wad,
                        "supply_apr_wad": r.supply_apr_wad,
                        "liquidity_index": r.liquidity_index,
                        "borrow_index": r.borrow_index,
                    })
                except Exception as e:
                    log.warning("reserve snapshot failed for %s: %s", asset, e)

            # ---- positions ------------------------------------------
            users = self.indexer.known_users()
            for user in users:
                try:
                    ad = self.c.get_account_data(user)
                except Exception as e:
                    log.debug("acct data failed for %s: %s", user, e)
                    continue
                if ad.total_debt_wad == 0 and ad.total_collateral_wad == 0:
                    continue
                hf_capped = min(ad.health_factor_wad, 10**36)  # cap "infinite" HF for storage
                write_position(conn, user, ts, {
                    "total_coll_usd_wad": ad.total_collateral_wad,
                    "total_debt_usd_wad": ad.total_debt_wad,
                    "health_factor_wad": hf_capped,
                    "liq_threshold_bps": ad.liq_threshold_bps,
                })

        log.info("snapshot @ ts=%d reserves=%d users=%d", ts, len(reserves), len(users))

    def run_forever(self) -> None:
        log.info("Aggregator starting, interval=%.0fs", SNAPSHOT_INTERVAL_SECS)
        while True:
            try:
                # Refresh known-user set first so we don't miss new participants
                self.indexer.step()
                self.snapshot_once()
            except Exception as e:
                log.exception("snapshot failed: %s", e)
            time.sleep(SNAPSHOT_INTERVAL_SECS)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    c = Client()
    Aggregator(c).run_forever()


if __name__ == "__main__":
    main()
