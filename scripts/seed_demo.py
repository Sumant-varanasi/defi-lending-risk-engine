"""Demo seeder.

Creates a varied set of positions against a freshly deployed local pool so
the dashboard has something interesting to show:

  - alice: 2 ETH supplied, no debt (safe collateral)
  - bob:   1 ETH supplied, borrowed 1200 USDC (HF ~ 1.33)
  - carol: 1 ETH supplied, borrowed 1450 USDC (HF ~ 1.10, near danger)
  - dave:  0.5 WBTC supplied, borrowed 30k USDC (HF ~ 1.20)
  - eve (supply-side): provides USDC liquidity

Requires:
  - Anvil running with the standard 10 funded accounts
  - Deploy.s.sol has run; .env populated with addresses
  - python deps installed
  - ABIs extracted to python/abi/

Usage:
  python -m scripts.seed_demo
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow running as a script
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web3 import Web3

from python.chain import Client
from python.config import ASSETS, POOL_ADDRESS, RPC_URL

log = logging.getLogger(__name__)

# Anvil's default deterministic private keys (DO NOT USE IN PRODUCTION)
ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # 0
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",  # 1 (alice)
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",  # 2 (bob)
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",  # 3 (carol)
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",  # 4 (dave)
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",  # 5 (eve)
]


def _send(c: Client, fn) -> str:
    return c.send_tx(fn)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    weth = ASSETS["weth"]
    usdc = ASSETS["usdc"]
    wbtc = ASSETS.get("wbtc")

    # Deployer (account 0) handles minting
    deployer = Client(private_key=ANVIL_KEYS[0])

    actors = {
        "alice": Client(private_key=ANVIL_KEYS[1]),
        "bob":   Client(private_key=ANVIL_KEYS[2]),
        "carol": Client(private_key=ANVIL_KEYS[3]),
        "dave":  Client(private_key=ANVIL_KEYS[4]),
        "eve":   Client(private_key=ANVIL_KEYS[5]),
    }

    # ---- mint --------------------------------------------------------
    log.info("Minting tokens to actors")
    for name, actor in actors.items():
        _send(deployer, deployer.erc20(weth.address).functions.mint(actor.account.address, 10 * 10**weth.decimals))
        _send(deployer, deployer.erc20(usdc.address).functions.mint(actor.account.address, 100_000 * 10**usdc.decimals))
        if wbtc:
            _send(deployer, deployer.erc20(wbtc.address).functions.mint(actor.account.address, 5 * 10**wbtc.decimals))

    # ---- eve supplies USDC liquidity --------------------------------
    eve = actors["eve"]
    log.info("Eve approves and deposits 50,000 USDC")
    _send(eve, eve.erc20(usdc.address).functions.approve(POOL_ADDRESS, 2**256 - 1))
    _send(eve, eve.pool.functions.deposit(usdc.address, 50_000 * 10**usdc.decimals))

    # ---- alice supplies 2 ETH (safe, no debt) -----------------------
    alice = actors["alice"]
    log.info("Alice deposits 2 WETH (no debt)")
    _send(alice, alice.erc20(weth.address).functions.approve(POOL_ADDRESS, 2**256 - 1))
    _send(alice, alice.pool.functions.deposit(weth.address, 2 * 10**weth.decimals))

    # ---- bob: 1 ETH → 1200 USDC (HF ~ 1.33) -------------------------
    bob = actors["bob"]
    log.info("Bob deposits 1 WETH and borrows 1200 USDC")
    _send(bob, bob.erc20(weth.address).functions.approve(POOL_ADDRESS, 2**256 - 1))
    _send(bob, bob.pool.functions.deposit(weth.address, 1 * 10**weth.decimals))
    _send(bob, bob.pool.functions.borrow(usdc.address, 1200 * 10**usdc.decimals))

    # ---- carol: 1 ETH → 1450 USDC (HF ~ 1.10) -----------------------
    carol = actors["carol"]
    log.info("Carol deposits 1 WETH and borrows 1450 USDC (near danger)")
    _send(carol, carol.erc20(weth.address).functions.approve(POOL_ADDRESS, 2**256 - 1))
    _send(carol, carol.pool.functions.deposit(weth.address, 1 * 10**weth.decimals))
    _send(carol, carol.pool.functions.borrow(usdc.address, 1450 * 10**usdc.decimals))

    # ---- dave: 0.5 WBTC → 30k USDC ----------------------------------
    if wbtc:
        dave = actors["dave"]
        log.info("Dave deposits 0.5 WBTC and borrows 30,000 USDC")
        _send(dave, dave.erc20(wbtc.address).functions.approve(POOL_ADDRESS, 2**256 - 1))
        _send(dave, dave.pool.functions.deposit(wbtc.address, int(0.5 * 10**wbtc.decimals)))
        _send(dave, dave.pool.functions.borrow(usdc.address, 30_000 * 10**usdc.decimals))

    log.info("Seed complete. Run aggregator + dashboard.")
    print("\nNext steps:")
    print("  make indexer       # in one terminal")
    print("  make aggregator    # in another")
    print("  make dashboard     # in a third")
    print("\nTo demo a liquidation, drop ETH price:")
    print("  cast send $ORACLE \"setPrice(address,uint256)\" $WETH 1500000000000000000000 \\")
    print("    --rpc-url $RPC_URL --private-key $PRIVATE_KEY")
    print("  # then watch the liquidator bot pick up Carol")


if __name__ == "__main__":
    main()
