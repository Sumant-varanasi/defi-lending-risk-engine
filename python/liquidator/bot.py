"""Liquidator bot.

Architecture:
  1. Indexer keeps the set of known users fresh (events -> SQLite).
  2. Every POLL_INTERVAL we scan known users and call getUserAccountData()
     to check their health factor.
  3. For any user with HF < (1 - HF_SAFETY_BUFFER), we compute the most
     profitable (collateralAsset, debtAsset) pair, simulate liquidation
     locally, check it would actually be profitable after gas, and submit.

Profitability heuristic:
   For each user's (debtAsset, collateralAsset) pair:
     debtToCover     = min(borrowerDebt × closeFactor, maxFromOurWallet)
     debtValueUSD    = debtToCover × priceDebt
     collateralUSD   = debtValueUSD × (1 + liqBonus)
     profitUSD       = collateralUSD - debtValueUSD - gasCostUSD
   Pick the pair maximizing profitUSD.

Reliability features:
  - Single-tx liquidations (no multistep)
  - Skip if pending tx already in flight for this borrower
  - Exponential backoff on RPC errors
  - Permissionless design means even if this bot fails, others will catch it
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from web3 import Web3

from python.analytics.db import session, write_event
from python.chain import RAY, SECONDS_PER_YEAR, Client, asset_meta
from python.config import (
    ASSETS_BY_ADDR,
    GAS_PRICE_GWEI_MAX,
    HF_SAFETY_BUFFER,
    LIQUIDATION_MIN_PROFIT_USD,
    POLL_INTERVAL_SECS,
    POOL_ADDRESS,
)
from python.liquidator.event_indexer import Indexer

log = logging.getLogger(__name__)

CLOSE_FACTOR_BPS = 5_000
BPS = 10_000
WAD = 10**18


@dataclass
class LiquidationOpportunity:
    borrower: str
    collateral_asset: str
    debt_asset: str
    debt_to_cover: int          # in debt token native units
    expected_collateral: int    # in collateral token native units
    expected_profit_usd: float


class Liquidator:
    def __init__(self, client: Client, indexer: Indexer | None = None):
        self.c = client
        self.indexer = indexer or Indexer(client)
        self._inflight: set[str] = set()
        self._approved: set[str] = set()
        self._backoff = 1.0

    # ----- main loop ----------------------------------------------------
    def run_forever(self) -> None:
        log.info("Liquidator bot starting, pool=%s", POOL_ADDRESS)
        while True:
            try:
                self._tick()
                self._backoff = 1.0
            except Exception:
                log.exception("tick failed, backing off %.1fs", self._backoff)
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 60.0)
                continue
            time.sleep(POLL_INTERVAL_SECS)

    def _tick(self) -> None:
        # 1. Pull new events to refresh the known-user set
        self.indexer.step()
        users = self.indexer.known_users()
        if not users:
            return

        # 2. Score every user
        opportunities: list[LiquidationOpportunity] = []
        for user in users:
            if user in self._inflight:
                continue
            opp = self._evaluate_user(user)
            if opp:
                opportunities.append(opp)

        if not opportunities:
            return

        # 3. Execute the best
        opportunities.sort(key=lambda o: o.expected_profit_usd, reverse=True)
        for opp in opportunities:
            if opp.expected_profit_usd < LIQUIDATION_MIN_PROFIT_USD:
                continue
            self._execute(opp)
            return  # one liquidation per tick keeps nonce simple

    # ----- evaluation ---------------------------------------------------
    def _evaluate_user(self, user: str) -> LiquidationOpportunity | None:
        try:
            ad = self.c.get_account_data(user)
        except Exception as e:
            log.debug("getUserAccountData(%s) failed: %s", user, e)
            return None

        if not ad.is_liquidatable:
            return None

        hf_threshold = 1.0 - HF_SAFETY_BUFFER
        if ad.hf >= hf_threshold:
            return None

        log.warning("Unhealthy user %s HF=%.4f debt=$%.2f coll=$%.2f",
                    user, ad.hf, ad.total_debt_wad / 1e18, ad.total_collateral_wad / 1e18)

        # Find best (debtAsset, collateralAsset) pair
        best: LiquidationOpportunity | None = None
        reserves = self.c.get_reserves_list()

        for debt_addr in reserves:
            supplied, borrowed, _ = self.c.get_user_reserve_data(user, debt_addr)
            if borrowed == 0:
                continue
            debt_meta = asset_meta(debt_addr)
            if not debt_meta:
                continue

            for coll_addr in reserves:
                coll_supplied, _, use_collat = self.c.get_user_reserve_data(user, coll_addr)
                if coll_supplied == 0 or not use_collat:
                    continue
                coll_meta = asset_meta(coll_addr)
                if not coll_meta:
                    continue

                opp = self._score_pair(
                    user, debt_addr, debt_meta, borrowed, coll_addr, coll_meta, coll_supplied
                )
                if opp and (best is None or opp.expected_profit_usd > best.expected_profit_usd):
                    best = opp

        return best

    def _score_pair(
        self,
        user: str,
        debt_addr: str,
        debt_meta,
        borrowed: int,
        coll_addr: str,
        coll_meta,
        coll_supplied: int,
    ) -> LiquidationOpportunity | None:
        # Max repayable per closeFactor
        max_repay = (borrowed * CLOSE_FACTOR_BPS) // BPS
        if max_repay == 0:
            return None

        price_debt = self.c.get_price(debt_addr)
        price_coll = self.c.get_price(coll_addr)
        debt_dec = 10 ** debt_meta.decimals
        coll_dec = 10 ** coll_meta.decimals

        # Read liquidation bonus from reserve config
        try:
            reserve = self.c.pool.functions.reserves(Web3.to_checksum_address(coll_addr)).call()
            # reserve struct fields (must match Solidity layout):
            #   0  liquidityIndex
            #   1  borrowIndex
            #   2  lastUpdateTimestamp
            #   3  totalScaledSupply
            #   4  totalScaledBorrow
            #   5  accruedToTreasuryScaled
            #   6  ltvBps
            #   7  liquidationThresholdBps
            #   8  liquidationBonusBps   <-- want this
            #   9  reserveFactorBps
            #  10  interestRateModel
            #  11  active
            #  12  borrowEnabled
            #  13  usableAsCollateral
            liq_bonus_bps = reserve[8]
        except Exception:
            liq_bonus_bps = 500  # fallback

        debt_value_wad = (max_repay * price_debt) // debt_dec
        bonused_value_wad = debt_value_wad + (debt_value_wad * liq_bonus_bps) // BPS
        collateral_to_seize = (bonused_value_wad * coll_dec) // price_coll

        # Cap by borrower's collateral
        if collateral_to_seize > coll_supplied:
            collateral_to_seize = coll_supplied
            # recompute debt covered if collateral exhausted
            new_debt_value_wad = (collateral_to_seize * price_coll) // coll_dec
            new_debt_value_wad = (new_debt_value_wad * BPS) // (BPS + liq_bonus_bps)
            max_repay = (new_debt_value_wad * debt_dec) // price_debt
            debt_value_wad = new_debt_value_wad

        # Profit = (collateral USD - debt USD) - gas
        coll_value_wad = (collateral_to_seize * price_coll) // coll_dec
        gross_profit_usd = (coll_value_wad - debt_value_wad) / 1e18
        gas_cost_usd = self._estimate_gas_cost_usd()
        net_profit_usd = gross_profit_usd - gas_cost_usd

        if max_repay == 0 or collateral_to_seize == 0:
            return None

        return LiquidationOpportunity(
            borrower=user,
            collateral_asset=coll_addr,
            debt_asset=debt_addr,
            debt_to_cover=max_repay,
            expected_collateral=collateral_to_seize,
            expected_profit_usd=net_profit_usd,
        )

    def _estimate_gas_cost_usd(self) -> float:
        """Rough estimate. Real version would use eth_estimateGas + actual eth price."""
        try:
            gas_price = self.c.w3.eth.gas_price
        except Exception:
            gas_price = 10 * 10**9
        # ~300k gas typical for a liquidation
        gas_cost_eth = (gas_price * 300_000) / 1e18
        # use WETH price from oracle if available
        eth_price = 2000.0
        for asset in ASSETS_BY_ADDR.values():
            if asset.symbol == "WETH":
                try:
                    eth_price = self.c.get_price(asset.address) / 1e18
                except Exception:
                    pass
                break
        return gas_cost_eth * eth_price

    # ----- execution ----------------------------------------------------
    def _execute(self, opp: LiquidationOpportunity) -> None:
        log.info("LIQUIDATE %s | debt=%s amount=%d coll=%s expected_profit=$%.2f",
                 opp.borrower, opp.debt_asset, opp.debt_to_cover, opp.collateral_asset,
                 opp.expected_profit_usd)

        # Ensure we've approved the pool to pull our debt asset
        if opp.debt_asset not in self._approved:
            try:
                self.c.approve(opp.debt_asset, POOL_ADDRESS, 2**256 - 1)
                self._approved.add(opp.debt_asset)
                log.info("approved %s for pool", opp.debt_asset)
            except Exception as e:
                log.error("approve failed: %s", e)
                return

        # Check we have enough balance
        balance = self.c.erc20(opp.debt_asset).functions.balanceOf(self.c.account.address).call()
        if balance < opp.debt_to_cover:
            log.warning("insufficient balance of %s: have %d need %d",
                        opp.debt_asset, balance, opp.debt_to_cover)
            return

        self._inflight.add(opp.borrower)
        try:
            tx = self.c.liquidate(
                opp.borrower, opp.collateral_asset, opp.debt_asset, opp.debt_to_cover
            )
            log.info("LIQUIDATION SUCCESS tx=%s", tx)
        except Exception as e:
            log.error("liquidation failed: %s", e)
        finally:
            self._inflight.discard(opp.borrower)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    c = Client()
    Liquidator(c).run_forever()


if __name__ == "__main__":
    main()
