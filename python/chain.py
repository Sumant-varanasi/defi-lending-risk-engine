"""Thin web3 wrapper used by all off-chain services.

Centralizes RPC connection, ABI loading, contract instantiation, and a
small set of read helpers so callers don't have to think about decoding.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError
from web3.middleware import ExtraDataToPOAMiddleware

from python.config import (
    ASSETS,
    ASSETS_BY_ADDR,
    CHAIN_ID,
    ORACLE_ADDRESS,
    POOL_ADDRESS,
    PRIVATE_KEY,
    RPC_URL,
    load_abi,
)

log = logging.getLogger(__name__)


@dataclass
class AccountData:
    total_collateral_wad: int
    total_debt_wad: int
    available_borrows_wad: int
    liq_threshold_bps: int
    ltv_bps: int
    health_factor_wad: int

    @property
    def is_liquidatable(self) -> bool:
        return self.total_debt_wad > 0 and self.health_factor_wad < 10**18

    @property
    def hf(self) -> float:
        return self.health_factor_wad / 1e18 if self.health_factor_wad < (2**256 - 1) else float("inf")


@dataclass
class ReserveSnapshot:
    asset: str
    liquidity_index: int
    borrow_index: int
    total_supply: int
    total_borrow: int
    utilization_wad: int
    borrow_apr_wad: int  # annualized
    supply_apr_wad: int  # annualized


SECONDS_PER_YEAR = 365 * 24 * 60 * 60
RAY = 10**27


class Client:
    """High-level read/write client for the lending protocol."""

    def __init__(self, rpc_url: str = RPC_URL, private_key: str | None = None):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        # POA middleware is harmless on Anvil; needed for L2s/sidechains
        try:
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        except Exception:
            pass

        if not self.w3.is_connected():
            raise ConnectionError(f"Could not connect to RPC at {rpc_url}")

        self.account = None
        if private_key or PRIVATE_KEY:
            self.account = self.w3.eth.account.from_key(private_key or PRIVATE_KEY)
            log.info("Loaded account %s", self.account.address)

    @cached_property
    def pool(self) -> Contract:
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(POOL_ADDRESS),
            abi=load_abi("LendingPool"),
        )

    @cached_property
    def oracle(self) -> Contract:
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(ORACLE_ADDRESS),
            abi=load_abi("PriceOracle"),
        )

    def erc20(self, address: str) -> Contract:
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address),
            abi=load_abi("MockERC20"),
        )

    # ---- reads --------------------------------------------------------
    def get_reserves_list(self) -> list[str]:
        return [a.lower() for a in self.pool.functions.getReservesList().call()]

    def get_account_data(self, user: str) -> AccountData:
        (coll, debt, avail, liq_t, ltv, hf) = self.pool.functions.getUserAccountData(
            Web3.to_checksum_address(user)
        ).call()
        return AccountData(coll, debt, avail, liq_t, ltv, hf)

    def get_reserve_data(self, asset: str) -> ReserveSnapshot:
        (liq_idx, bor_idx, ts, tb, util, br_ray, sr_ray) = self.pool.functions.getReserveData(
            Web3.to_checksum_address(asset)
        ).call()
        # annualize per-second RAY rates into WAD
        # apr_wad = rate_ray * SECONDS_PER_YEAR / 1e9   (RAY -> WAD)
        borrow_apr = br_ray * SECONDS_PER_YEAR // 10**9
        supply_apr = sr_ray * SECONDS_PER_YEAR // 10**9
        return ReserveSnapshot(asset.lower(), liq_idx, bor_idx, ts, tb, util, borrow_apr, supply_apr)

    def get_price(self, asset: str) -> int:
        return self.oracle.functions.getAssetPrice(Web3.to_checksum_address(asset)).call()

    def get_user_reserve_data(self, user: str, asset: str) -> tuple[int, int, bool]:
        return self.pool.functions.getUserReserveData(
            Web3.to_checksum_address(user),
            Web3.to_checksum_address(asset),
        ).call()

    # ---- writes -------------------------------------------------------
    def send_tx(self, fn, value: int = 0, gas_limit: int | None = None) -> str:
        if self.account is None:
            raise RuntimeError("No private key configured")

        tx = fn.build_transaction(
            {
                "from": self.account.address,
                "nonce": self.w3.eth.get_transaction_count(self.account.address),
                "chainId": CHAIN_ID,
                "value": value,
                "gas": gas_limit or fn.estimate_gas({"from": self.account.address, "value": value}),
                "maxFeePerGas": self.w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self.w3.to_wei(1, "gwei"),
            }
        )
        signed = self.account.sign_transaction(tx)
        # web3.py 7.x renamed `rawTransaction` -> `raw_transaction`
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"Tx reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    def liquidate(
        self,
        borrower: str,
        collateral_asset: str,
        debt_asset: str,
        debt_to_cover: int,
    ) -> str:
        fn = self.pool.functions.liquidate(
            Web3.to_checksum_address(borrower),
            Web3.to_checksum_address(collateral_asset),
            Web3.to_checksum_address(debt_asset),
            debt_to_cover,
        )
        return self.send_tx(fn)

    def approve(self, token: str, spender: str, amount: int) -> str:
        fn = self.erc20(token).functions.approve(Web3.to_checksum_address(spender), amount)
        return self.send_tx(fn)

    # ---- block helpers -----------------------------------------------
    def latest_block(self) -> int:
        return self.w3.eth.block_number

    def block_timestamp(self, block: int | str = "latest") -> int:
        return self.w3.eth.get_block(block).timestamp


def asset_meta(addr: str) -> Any:
    return ASSETS_BY_ADDR.get(addr.lower())
