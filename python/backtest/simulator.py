"""Backtest engine.

Simulates a population of borrowers over time under different LTV policies.

Setup:
  - A population of N borrowers, each opens a position at sim start with a
    random initial LTV drawn from a Beta distribution skewed toward the
    policy maximum (realistic — most borrowers borrow close to the limit).
  - Each borrower's position is (eth_collateral, usdc_debt).
  - Daily price updates drive HF changes.
  - If HF < 1, the position is liquidated:
      * 50% close factor (Aave-style)
      * 5% liquidation bonus paid to liquidator (capital efficiency loss)
      * Position remains open (smaller) until either fully repaid or further
        liquidations close it out.
  - If price falls so fast that collateral_value < debt_value before
    liquidation, the excess is "bad debt" — pure loss to the protocol.

LTV policies:
  - STATIC_AGGRESSIVE: LTV=80%, liqThresh=85%
  - STATIC_CONSERVATIVE: LTV=60%, liqThresh=65%
  - DYNAMIC: recomputed daily from 30-day EWMA vol via the dynamic_ltv
            recommender. New positions opened during stress periods get the
            tighter LTV; existing positions are NOT re-margined (mirrors what
            a protocol can actually do — you can't retroactively shrink
            someone's loan).

What we measure:
  - Capital efficiency:  Σ debt_outstanding / Σ collateral_supplied  (average)
  - Liquidations:        count + total notional liquidated
  - Liquidator profits:  Σ (bonus × liquidated debt value)
  - Bad debt:            Σ max(0, debt - collateral) at liquidation moments
  - Survival:            % of positions still open at end
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from python.risk_engine.dynamic_ltv import (
    ModelConfig,
    RiskInputs,
    recommend,
)
from python.risk_engine.volatility import compute_volatility


log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Position
# ----------------------------------------------------------------------
@dataclass
class Position:
    """An ETH-collateral / USDC-debt position."""
    borrower_id: int
    eth_amount: float            # collateral (ETH)
    debt_usdc: float             # outstanding debt (USDC, 1:1 with USD)
    opened_day: int
    liq_threshold: float         # locked at open (LTV policies can't retroactively change this for safety)
    closed_day: int | None = None
    bad_debt_usd: float = 0.0    # only set if liquidation couldn't fully cover debt
    n_liquidations: int = 0

    @property
    def is_open(self) -> bool:
        return self.closed_day is None and self.debt_usdc > 1e-6 and self.eth_amount > 1e-9


# ----------------------------------------------------------------------
# Policy
# ----------------------------------------------------------------------
@dataclass
class Policy:
    name: str
    # Either fixed values for static policies...
    fixed_ltv: float | None = None
    fixed_liq_threshold: float | None = None
    # ...or a callable that returns (ltv, liq_threshold) given recent prices
    dynamic_fn: Callable[[pd.Series], tuple[float, float]] | None = None

    def get_params(self, recent_prices: pd.Series) -> tuple[float, float]:
        if self.fixed_ltv is not None:
            return self.fixed_ltv, self.fixed_liq_threshold  # type: ignore
        assert self.dynamic_fn is not None
        return self.dynamic_fn(recent_prices)


# ----------------------------------------------------------------------
# Result
# ----------------------------------------------------------------------
@dataclass
class BacktestResult:
    policy_name: str
    history: pd.DataFrame                # one row per day
    final_positions: list[Position]
    n_total_borrowers: int

    # aggregates
    total_liquidations: int = 0
    total_liquidator_bonus_paid: float = 0.0
    total_bad_debt: float = 0.0
    positions_surviving_pct: float = 0.0
    avg_capital_efficiency: float = 0.0


# ----------------------------------------------------------------------
# Simulator
# ----------------------------------------------------------------------
CLOSE_FACTOR = 0.50
LIQUIDATION_BONUS = 0.05


def _open_positions(
    n: int,
    eth_price_at_open: float,
    ltv_at_open: float,
    liq_threshold: float,
    avg_eth_amount: float,
    rng: np.random.Generator,
) -> list[Position]:
    """Create a cohort of borrowers with realistic LTV distribution.

    LTV is drawn from a Beta(5, 1.5) distribution, scaled to [0.3, 1.0] × ltv_at_open.
    This skews toward borrowers maxing out their LTV, which matches reality.
    """
    positions: list[Position] = []
    ltv_draws = rng.beta(5, 1.5, n)  # roughly skewed left, mean ~0.77
    ltv_draws = 0.3 + 0.7 * ltv_draws  # squash into [0.3, 1.0]

    for i in range(n):
        eth = max(0.01, rng.lognormal(mean=np.log(avg_eth_amount), sigma=0.8))
        target_ltv = ltv_draws[i] * ltv_at_open
        debt_usd = eth * eth_price_at_open * target_ltv
        positions.append(
            Position(
                borrower_id=i,
                eth_amount=eth,
                debt_usdc=debt_usd,
                opened_day=0,
                liq_threshold=liq_threshold,
            )
        )
    return positions


def _liquidate(pos: Position, eth_price: float) -> tuple[float, float, float]:
    """Liquidate (partially) a position at the current price.

    Returns: (eth_seized, debt_repaid, bonus_paid_usd)
    """
    # close-factor portion of debt covered
    debt_to_cover = pos.debt_usdc * CLOSE_FACTOR
    # collateral seized = (debt_to_cover × (1 + bonus)) / price
    coll_value_seized_usd = debt_to_cover * (1 + LIQUIDATION_BONUS)
    eth_seized = coll_value_seized_usd / eth_price

    bad_debt = 0.0
    if eth_seized > pos.eth_amount:
        # Collateral exhausted — partial liquidation
        eth_seized = pos.eth_amount
        actual_coll_value = eth_seized * eth_price
        # Recompute debt repaid working backward from collateral
        debt_to_cover = actual_coll_value / (1 + LIQUIDATION_BONUS)
        # If debt still exceeds collateral after liquidation, bad debt remains
        bad_debt = max(0.0, pos.debt_usdc - debt_to_cover)

    bonus_usd = debt_to_cover * LIQUIDATION_BONUS
    pos.eth_amount -= eth_seized
    pos.debt_usdc -= debt_to_cover
    pos.n_liquidations += 1
    if bad_debt > 0:
        pos.bad_debt_usd += bad_debt
        pos.debt_usdc = 0.0  # write off
        pos.closed_day = pos.opened_day  # mark closed; will be cleaned up
    if pos.debt_usdc < 1e-6 or pos.eth_amount < 1e-9:
        pos.closed_day = pos.opened_day  # will be set properly by caller

    return eth_seized, debt_to_cover, bonus_usd


def run_backtest(
    prices: pd.DataFrame,
    policy: Policy,
    n_borrowers: int = 200,
    avg_eth_per_borrower: float = 2.0,
    new_borrowers_per_day: float = 0.0,  # if >0, simulate arrivals (poisson)
    seed: int = 7,
) -> BacktestResult:
    """Run one backtest of one policy over the given price series."""
    rng = np.random.default_rng(seed)
    log_returns = prices["log_return"].values
    eth_prices = prices["price"].values
    n_days = len(prices)

    # ---- Initial cohort -----------------------------------------------
    initial_prices = prices["price"].iloc[:1]  # for opening params
    initial_ltv, initial_liq = policy.get_params(initial_prices)
    positions = _open_positions(
        n_borrowers, eth_prices[0], initial_ltv, initial_liq, avg_eth_per_borrower, rng
    )

    # ---- Daily simulation --------------------------------------------
    history_rows = []
    cum_liquidations = 0
    cum_bonus = 0.0
    cum_bad_debt = 0.0

    for day in range(n_days):
        eth_price = eth_prices[day]

        # New borrowers (optional)
        if new_borrowers_per_day > 0:
            n_new = rng.poisson(new_borrowers_per_day)
            if n_new > 0:
                recent_prices = prices["price"].iloc[max(0, day - 30): day + 1]
                ltv, liq = policy.get_params(recent_prices)
                new_positions = _open_positions(
                    n_new, eth_price, ltv, liq, avg_eth_per_borrower, rng
                )
                for p in new_positions:
                    p.borrower_id = n_borrowers + len(positions)
                    p.opened_day = day
                positions.extend(new_positions)

        # Liquidation pass
        day_liquidations = 0
        day_bonus = 0.0
        day_bad_debt = 0.0
        for pos in positions:
            if not pos.is_open:
                continue
            coll_value = pos.eth_amount * eth_price
            if pos.debt_usdc == 0:
                continue
            hf = (coll_value * pos.liq_threshold) / pos.debt_usdc
            if hf < 1.0:
                eth_seized, debt_repaid, bonus = _liquidate(pos, eth_price)
                day_liquidations += 1
                day_bonus += bonus
                day_bad_debt += pos.bad_debt_usd if pos.closed_day is not None else 0
                if pos.closed_day is not None:
                    pos.closed_day = day

        cum_liquidations += day_liquidations
        cum_bonus += day_bonus
        cum_bad_debt += day_bad_debt

        # Aggregate state
        open_positions = [p for p in positions if p.is_open]
        n_open = len(open_positions)
        total_coll_usd = sum(p.eth_amount * eth_price for p in open_positions)
        total_debt_usd = sum(p.debt_usdc for p in open_positions)
        capital_eff = (total_debt_usd / total_coll_usd) if total_coll_usd > 0 else 0
        avg_hf = float(np.mean([
            (p.eth_amount * eth_price * p.liq_threshold) / p.debt_usdc
            for p in open_positions if p.debt_usdc > 0
        ])) if open_positions else 0.0

        history_rows.append({
            "day": day,
            "date": prices.index[day],
            "eth_price": eth_price,
            "n_open": n_open,
            "total_coll_usd": total_coll_usd,
            "total_debt_usd": total_debt_usd,
            "capital_efficiency": capital_eff,
            "avg_hf": avg_hf,
            "day_liquidations": day_liquidations,
            "cum_liquidations": cum_liquidations,
            "day_bonus_paid": day_bonus,
            "cum_bonus_paid": cum_bonus,
            "cum_bad_debt": cum_bad_debt,
        })

    hist = pd.DataFrame(history_rows).set_index("date")

    final_open = [p for p in positions if p.is_open]

    return BacktestResult(
        policy_name=policy.name,
        history=hist,
        final_positions=positions,
        n_total_borrowers=len(positions),
        total_liquidations=cum_liquidations,
        total_liquidator_bonus_paid=cum_bonus,
        total_bad_debt=cum_bad_debt,
        positions_surviving_pct=len(final_open) / max(1, len(positions)),
        avg_capital_efficiency=float(hist["capital_efficiency"].mean()),
    )


# ----------------------------------------------------------------------
# Policy factory helpers
# ----------------------------------------------------------------------
def make_static_aggressive() -> Policy:
    return Policy(name="Static-Aggressive (80/85)", fixed_ltv=0.80, fixed_liq_threshold=0.85)


def make_static_conservative() -> Policy:
    return Policy(name="Static-Conservative (60/65)", fixed_ltv=0.60, fixed_liq_threshold=0.65)


def make_dynamic(cfg: ModelConfig | None = None) -> Policy:
    cfg = cfg or ModelConfig()

    def dynamic_fn(recent_prices: pd.Series) -> tuple[float, float]:
        if len(recent_prices) < 5:
            # Cold start: use base
            return cfg.ltv_base_bps / 10_000, (cfg.ltv_base_bps + cfg.safety_gap_bps) / 10_000

        # Compute EWMA vol from recent prices
        ts = (recent_prices.index.astype(np.int64) // 10**9).values
        vol = compute_volatility(ts.tolist(), recent_prices.values.tolist())

        # We can't easily pass pool TVL/concentration here without simulating it,
        # so this is the "pure vol-responsive" version of the dynamic policy.
        inputs = RiskInputs(
            realized_vol_annual=vol.ewma_annual,
            pool_total_supply_usd=1e7,
            largest_position_usd=1e5,
            n_borrowers=100,
        )
        params = recommend(inputs, cfg)
        return params.ltv_recommended_bps / 10_000, params.liq_threshold_recommended_bps / 10_000

    return Policy(name="Dynamic (vol-responsive)", dynamic_fn=dynamic_fn)
