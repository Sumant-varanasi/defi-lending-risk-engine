"""Periodic risk engine job.

For each reserve:
  1. Compute realized vol from recent price history (in DB).
  2. Compute pool concentration from current positions.
  3. Call the dynamic LTV recommender.
  4. Write the recommendation to `risk_params` table for the dashboard.

This is a *recommender* — it does NOT push parameter changes on-chain.
That decision belongs to governance (in a real system) or the admin
(in this learning project). The dashboard shows the delta between
on-chain and recommended values.
"""
from __future__ import annotations

import logging
import time

from python.analytics.db import session
from python.chain import Client, asset_meta
from python.config import ASSETS
from python.risk_engine.dynamic_ltv import RiskInputs, recommend
from python.risk_engine.volatility import vol_from_db

log = logging.getLogger(__name__)

RUN_INTERVAL_SECS = 300.0  # every 5 minutes


def run_once(c: Client) -> None:
    ts = c.block_timestamp("latest")
    reserves = c.get_reserves_list()

    with session() as conn:
        for asset_addr in reserves:
            meta = asset_meta(asset_addr)
            sym = meta.symbol if meta else asset_addr[:8]

            # ---- inputs ------------------------------------------------
            vol = vol_from_db(conn, asset_addr, lookback_days=30)

            try:
                r = c.get_reserve_data(asset_addr)
                # convert native units to USD for concentration calc
                price = c.get_price(asset_addr) / 1e18
                dec = meta.decimals if meta else 18
                tvl_usd = (r.total_supply / (10**dec)) * price
            except Exception as e:
                log.warning("[%s] reserve fetch failed: %s", sym, e)
                continue

            # Find largest single-user position
            largest = 0.0
            n_borrowers = 0
            from python.liquidator.event_indexer import Indexer
            idx = Indexer(c)
            for user in idx.known_users():
                try:
                    supplied, borrowed, _ = c.get_user_reserve_data(user, asset_addr)
                except Exception:
                    continue
                if supplied > 0:
                    val = (supplied / (10**dec)) * price
                    if val > largest:
                        largest = val
                if borrowed > 0:
                    n_borrowers += 1

            params = recommend(RiskInputs(
                realized_vol_annual=vol.ewma_annual,
                pool_total_supply_usd=tvl_usd,
                largest_position_usd=largest,
                n_borrowers=n_borrowers,
            ))

            conn.execute(
                """INSERT OR REPLACE INTO risk_params VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    asset_addr.lower(),
                    ts,
                    vol.ewma_annual,
                    params.ltv_recommended_bps,
                    params.liq_threshold_recommended_bps,
                    params.stress_score,
                    params.notes,
                ),
            )

            log.info("[%s] vol=%.2f%% LTV→%d/%dbps stress=%.1f",
                     sym, vol.ewma_annual * 100,
                     params.ltv_recommended_bps,
                     params.liq_threshold_recommended_bps,
                     params.stress_score)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    c = Client()
    log.info("Risk engine starting, interval=%.0fs", RUN_INTERVAL_SECS)
    while True:
        try:
            run_once(c)
        except Exception:
            log.exception("risk engine cycle failed")
        time.sleep(RUN_INTERVAL_SECS)


if __name__ == "__main__":
    main()
