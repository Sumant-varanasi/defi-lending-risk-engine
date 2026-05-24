"""On-chain event indexer.

Polls the chain for protocol events, persists them, and maintains the set
of users who have ever interacted with the pool (so the liquidator and
analytics can iterate over them without an O(n_users) on-chain query).

Reorg handling: we lag behind head by CONFIRMATION_BLOCKS. For local Anvil
this is set to 1; on real networks, use 6-12.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Iterable

from web3 import Web3

from python.analytics.db import (
    get_state,
    session,
    set_state,
    write_event,
)
from python.chain import Client

log = logging.getLogger(__name__)

CONFIRMATION_BLOCKS = 1   # Anvil; raise to 6-12 on mainnet
CHUNK_SIZE = 2000          # blocks per get_logs call
KNOWN_USERS_KEY = "known_users"
LAST_BLOCK_KEY = "last_indexed_block"

EVENT_NAMES = ("Deposit", "Withdraw", "Borrow", "Repay", "Liquidation")


@dataclass
class IndexerState:
    last_block: int
    known_users: set[str]


def load_state(conn) -> IndexerState:
    last_block = int(get_state(conn, LAST_BLOCK_KEY, "0") or "0")
    users_json = get_state(conn, KNOWN_USERS_KEY, "[]") or "[]"
    return IndexerState(last_block=last_block, known_users=set(json.loads(users_json)))


def save_state(conn, st: IndexerState) -> None:
    set_state(conn, LAST_BLOCK_KEY, str(st.last_block))
    set_state(conn, KNOWN_USERS_KEY, json.dumps(sorted(st.known_users)))


def _user_from_event(event) -> str | None:
    args = event["args"]
    for key in ("user", "borrower", "liquidator"):
        if key in args:
            return args[key].lower()
    return None


def _classify(event) -> dict:
    """Translate a web3 event log into a row for the events table."""
    args = event["args"]
    kind = event["event"].lower()
    extra = {}
    user = None
    asset = None
    amount = None

    if kind in ("deposit", "withdraw", "repay"):
        user = args["user"].lower()
        asset = args["asset"].lower()
        amount = args["amount"]
    elif kind == "borrow":
        user = args["user"].lower()
        asset = args["asset"].lower()
        amount = args["amount"]
        extra["borrow_index"] = str(args["borrowIndex"])
    elif kind == "liquidation":
        user = args["borrower"].lower()
        asset = args["debtAsset"].lower()
        amount = args["debtRepaid"]
        extra = {
            "liquidator": args["liquidator"].lower(),
            "collateral_asset": args["collateralAsset"].lower(),
            "collateral_seized": str(args["collateralSeized"]),
        }

    return {
        "ts": 0,  # filled in by caller (block ts)
        "block_num": event["blockNumber"],
        "kind": kind,
        "user_addr": user,
        "asset_addr": asset,
        "amount": str(amount) if amount is not None else None,
        "extra_json": json.dumps(extra) if extra else None,
        "tx_hash": event["transactionHash"].hex(),
        "log_index": event["logIndex"],
    }


class Indexer:
    def __init__(self, client: Client):
        self.c = client
        self.events = [getattr(client.pool.events, name) for name in EVENT_NAMES]

    def step(self) -> int:
        """Process new blocks once. Returns number of events ingested."""
        with session() as conn:
            state = load_state(conn)
            head = self.c.latest_block() - CONFIRMATION_BLOCKS
            if head <= state.last_block:
                return 0

            ingested = 0
            from_block = state.last_block + 1
            while from_block <= head:
                to_block = min(from_block + CHUNK_SIZE - 1, head)
                ingested += self._process_range(conn, state, from_block, to_block)
                from_block = to_block + 1
                state.last_block = to_block
                save_state(conn, state)
            return ingested

    def _process_range(self, conn, state: IndexerState, fb: int, tb: int) -> int:
        n = 0
        # cache block timestamps to avoid re-fetching
        ts_cache: dict[int, int] = {}
        for ev_factory in self.events:
            try:
                logs = ev_factory.get_logs(from_block=fb, to_block=tb)
            except TypeError:
                # web3 6.x compat
                logs = ev_factory.get_logs(fromBlock=fb, toBlock=tb)
            for ev in logs:
                row = _classify(ev)
                bn = row["block_num"]
                if bn not in ts_cache:
                    ts_cache[bn] = self.c.block_timestamp(bn)
                row["ts"] = ts_cache[bn]
                write_event(conn, row)

                u = _user_from_event(ev)
                if u:
                    state.known_users.add(u)
                n += 1
        return n

    def run_forever(self, poll_interval: float = 5.0) -> None:
        log.info("Indexer starting")
        while True:
            try:
                n = self.step()
                if n:
                    log.info("indexed %d events", n)
            except Exception as e:
                log.exception("indexer step failed: %s", e)
            time.sleep(poll_interval)

    def known_users(self) -> list[str]:
        with session() as conn:
            return sorted(load_state(conn).known_users)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    c = Client()
    Indexer(c).run_forever()


if __name__ == "__main__":
    main()
