"""Central configuration loaded from environment variables / .env file.

After running the Foundry deploy script, copy the printed addresses into a `.env`
file at the project root (a `.env.example` is provided).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

ROOT = Path(__file__).parent.parent
ABI_DIR = Path(__file__).parent / "abi"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


# ---- chain ----------------------------------------------------------------
RPC_URL = os.getenv("RPC_URL", "http://127.0.0.1:8545")
CHAIN_ID = int(os.getenv("CHAIN_ID", "31337"))  # Anvil default
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")  # liquidator/admin EOA

# ---- contracts (populate from deploy output) ------------------------------
POOL_ADDRESS = os.getenv("POOL", "")
ORACLE_ADDRESS = os.getenv("ORACLE", "")
IRM_ADDRESS = os.getenv("IRM", "")

# ---- assets ---------------------------------------------------------------
@dataclass(frozen=True)
class Asset:
    symbol: str
    address: str
    decimals: int

ASSETS: dict[str, Asset] = {}
for sym in ("WETH", "USDC", "WBTC"):
    addr = os.getenv(sym, "")
    if addr:
        decimals = {"WETH": 18, "USDC": 6, "WBTC": 8}[sym]
        ASSETS[sym.lower()] = Asset(sym, addr, decimals)
ASSETS_BY_ADDR = {a.address.lower(): a for a in ASSETS.values()}


# ---- operational ----------------------------------------------------------
POLL_INTERVAL_SECS = float(os.getenv("POLL_INTERVAL_SECS", "5"))
LIQUIDATION_MIN_PROFIT_USD = float(os.getenv("LIQUIDATION_MIN_PROFIT_USD", "1.0"))
GAS_PRICE_GWEI_MAX = int(os.getenv("GAS_PRICE_GWEI_MAX", "200"))
HF_SAFETY_BUFFER = float(os.getenv("HF_SAFETY_BUFFER", "0.01"))  # only liquidate if HF < 1 - buffer

DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "protocol.sqlite"))


# ---- ABIs (loaded lazily, expected after `forge build`) -------------------
def load_abi(contract: str) -> list:
    """Load ABI from python/abi/{contract}.json.

    After `forge build`, copy abis with:
        jq '.abi' contracts/out/LendingPool.sol/LendingPool.json \
            > python/abi/LendingPool.json
    """
    path = ABI_DIR / f"{contract}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"ABI for {contract} not found at {path}. "
            "Run `make abi` from project root after `forge build`."
        )
    return json.loads(path.read_text())
