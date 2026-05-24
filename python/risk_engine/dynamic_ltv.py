"""Dynamic LTV recommendation engine.

Standard DeFi protocols set LTV and liquidation threshold via governance —
static parameters that don't react to market conditions. They tend to be
conservative (low LTV) because they need to hold up under worst-case vol.
This wastes capital efficiency during calm periods.

This module recommends LTV and liquidation threshold as a function of:

  1. Realized volatility (EWMA, so it reacts to regime changes)
  2. Pool concentration (more concentrated = riskier — bigger positions
     mean liquidation cascades are more likely)
  3. Liquidity depth (lower TVL = harder to liquidate without slippage,
     so we need a fatter buffer)

Formula:
    ltv_recommended = clamp(
        ltv_base * (sigma_target / sigma_observed) ** alpha
                * (1 - concentration_penalty)
                * liquidity_scalar,
        ltv_min, ltv_max
    )

Liquidation threshold is set to ltv_recommended + safety_gap, capped at
ltv_ceiling (the protocol's hard maximum).

A stress score (0-100) is emitted alongside for the dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskInputs:
    realized_vol_annual: float       # e.g., 0.65 = 65% annualized vol
    pool_total_supply_usd: float
    largest_position_usd: float
    n_borrowers: int


@dataclass
class RiskParams:
    ltv_recommended_bps: int
    liq_threshold_recommended_bps: int
    stress_score: float              # 0-100 (higher = riskier)
    notes: str


@dataclass
class ModelConfig:
    ltv_base_bps: int = 7500
    ltv_min_bps: int = 4000
    ltv_max_bps: int = 8500
    sigma_target: float = 0.60       # 60% annualized — calibrated for ETH
    alpha: float = 0.7               # how aggressively LTV reacts to vol
    safety_gap_bps: int = 500
    ltv_ceiling_bps: int = 9000
    concentration_threshold: float = 0.20  # if any one borrower > 20% of pool, penalize
    concentration_max_penalty: float = 0.15
    min_liquidity_usd: float = 100_000
    liquidity_max_penalty: float = 0.20


def recommend(inputs: RiskInputs, cfg: ModelConfig | None = None) -> RiskParams:
    """Compute LTV / liquidation threshold / stress score for a single reserve."""
    cfg = cfg or ModelConfig()

    notes: list[str] = []

    # ---- 1. Volatility scalar ------------------------------------------
    if inputs.realized_vol_annual <= 0:
        vol_scalar = 1.0
        notes.append("no_vol_data")
    else:
        ratio = cfg.sigma_target / inputs.realized_vol_annual
        # power law: higher vol -> lower LTV
        vol_scalar = ratio ** cfg.alpha
        if inputs.realized_vol_annual > cfg.sigma_target * 1.5:
            notes.append(f"high_vol={inputs.realized_vol_annual:.2%}")

    # ---- 2. Concentration penalty --------------------------------------
    if inputs.pool_total_supply_usd > 0:
        concentration = inputs.largest_position_usd / inputs.pool_total_supply_usd
    else:
        concentration = 0
    if concentration > cfg.concentration_threshold:
        # linear penalty: at 100% concentration, hit max penalty
        excess = (concentration - cfg.concentration_threshold) / (1 - cfg.concentration_threshold)
        concentration_penalty = excess * cfg.concentration_max_penalty
        notes.append(f"conc={concentration:.0%}")
    else:
        concentration_penalty = 0.0

    # ---- 3. Liquidity scalar -------------------------------------------
    if inputs.pool_total_supply_usd < cfg.min_liquidity_usd:
        # thin pool, harder to liquidate cleanly
        shortfall = 1 - (inputs.pool_total_supply_usd / cfg.min_liquidity_usd)
        liquidity_scalar = 1 - shortfall * cfg.liquidity_max_penalty
        notes.append("thin_liquidity")
    else:
        liquidity_scalar = 1.0

    # ---- combine -------------------------------------------------------
    ltv_recommended = (
        cfg.ltv_base_bps
        * vol_scalar
        * (1 - concentration_penalty)
        * liquidity_scalar
    )
    ltv_recommended_bps = int(max(cfg.ltv_min_bps, min(cfg.ltv_max_bps, ltv_recommended)))

    liq_threshold_recommended_bps = min(
        cfg.ltv_ceiling_bps,
        ltv_recommended_bps + cfg.safety_gap_bps,
    )

    # ---- stress score (0-100) ------------------------------------------
    # weighted combination of (1) how far LTV moved down from base,
    # (2) concentration, (3) vol level vs target.
    ltv_drop_pct = max(0, (cfg.ltv_base_bps - ltv_recommended_bps) / cfg.ltv_base_bps)
    vol_pressure = min(2.0, inputs.realized_vol_annual / cfg.sigma_target) if cfg.sigma_target > 0 else 0
    stress_score = (
        40 * ltv_drop_pct                          # 0-40 from LTV decrease
        + 30 * min(1.0, concentration)             # 0-30 from concentration
        + 30 * min(1.0, vol_pressure / 2)          # 0-30 from vol pressure
    )

    return RiskParams(
        ltv_recommended_bps=ltv_recommended_bps,
        liq_threshold_recommended_bps=liq_threshold_recommended_bps,
        stress_score=round(stress_score, 2),
        notes=";".join(notes) if notes else "ok",
    )


def explain(inputs: RiskInputs, params: RiskParams, cfg: ModelConfig | None = None) -> str:
    """Human-readable breakdown — useful for dashboard tooltips."""
    cfg = cfg or ModelConfig()
    lines = [
        f"Realized vol (annual):    {inputs.realized_vol_annual:>7.2%}    (target {cfg.sigma_target:.2%})",
        f"Largest position share:   {(inputs.largest_position_usd / inputs.pool_total_supply_usd * 100 if inputs.pool_total_supply_usd else 0):>6.1f}%",
        f"Pool TVL:                 ${inputs.pool_total_supply_usd:>12,.0f}",
        f"# borrowers:              {inputs.n_borrowers}",
        "",
        f"Recommended LTV:          {params.ltv_recommended_bps / 100:>6.1f}%    (base {cfg.ltv_base_bps / 100:.1f}%)",
        f"Recommended liq thresh:   {params.liq_threshold_recommended_bps / 100:>6.1f}%",
        f"Stress score:             {params.stress_score:>6.1f} / 100",
        f"Notes:                    {params.notes}",
    ]
    return "\n".join(lines)
