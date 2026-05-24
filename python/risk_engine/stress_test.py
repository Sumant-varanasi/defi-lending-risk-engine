"""Monte Carlo stress testing for the lending pool.

Given current positions and asset volatilities, simulate forward price
paths under Geometric Brownian Motion (GBM) and count, for each path, how
much of the pool's collateral falls into the liquidatable zone.

This is intentionally a simple GBM-based model — extensions that would
make the project stronger:
  - jump-diffusion (Merton) to capture flash crashes
  - heston / stochastic vol for fat tails
  - correlated multi-asset paths via Cholesky decomposition (we do this here)
  - regime-switching models calibrated to historical crash periods

Output:
  - Expected loss to the protocol over horizon T (USD)
  - Probability of any liquidation in the horizon
  - Distribution percentiles (P50, P95, P99) of total liquidations

This is for the dashboard's "pool health" panel.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

WAD = 10**18
BPS = 10_000


@dataclass
class Position:
    user: str
    collateral_asset: str   # address (lowercase)
    collateral_amount: float  # in token units (not native)
    debt_asset: str
    debt_amount: float
    liq_threshold_bps: int


@dataclass
class AssetParams:
    """Per-asset stochastic model parameters."""
    price: float
    annual_vol: float
    annual_drift: float = 0.0   # default: martingale


@dataclass
class StressResult:
    horizon_days: int
    n_paths: int
    n_liquidations_mean: float
    n_liquidations_p95: float
    pct_paths_with_liquidation: float
    expected_loss_usd: float
    var_95_loss_usd: float          # 95% Value-at-Risk
    cvar_95_loss_usd: float         # Expected loss conditional on tail


def _correlated_gbm(
    n_paths: int,
    n_steps: int,
    dt: float,
    asset_params: dict[str, AssetParams],
    corr: np.ndarray | None = None,
    rng: np.random.Generator | None = None,
) -> dict[str, np.ndarray]:
    """Simulate correlated GBM paths.

    Returns dict {asset_addr: (n_paths, n_steps+1) array of prices}.
    """
    rng = rng or np.random.default_rng()
    assets = list(asset_params.keys())
    n_assets = len(assets)

    if corr is None:
        corr = np.eye(n_assets)

    # Cholesky for correlated normals
    chol = np.linalg.cholesky(corr)

    # Generate (n_paths, n_steps, n_assets) of correlated standard normals
    z = rng.standard_normal((n_paths, n_steps, n_assets))
    z_corr = z @ chol.T

    paths: dict[str, np.ndarray] = {}
    for i, asset in enumerate(assets):
        p = asset_params[asset]
        sigma = p.annual_vol
        mu = p.annual_drift
        # log-price increments: (mu - 0.5 sigma^2) dt + sigma sqrt(dt) Z
        log_returns = (mu - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * z_corr[:, :, i]
        log_prices = np.cumsum(log_returns, axis=1)
        # prepend zero (t=0 has no return)
        log_prices = np.concatenate([np.zeros((n_paths, 1)), log_prices], axis=1)
        paths[asset] = p.price * np.exp(log_prices)
    return paths


def stress_test(
    positions: list[Position],
    asset_params: dict[str, AssetParams],
    horizon_days: int = 7,
    n_paths: int = 5_000,
    steps_per_day: int = 1,
    asset_corr: np.ndarray | None = None,
    liquidation_bonus_bps: int = 500,
    seed: int | None = 42,
) -> StressResult:
    """Run Monte Carlo across positions.

    For each simulated path, at each time step:
      1. Apply new prices to every position.
      2. Compute HF = (Σ coll * price * liqThresh) / (Σ debt * price)
      3. If HF < 1, mark as liquidated. Compute the loss as
         (debt repaid * liq_bonus) = the bonus paid to liquidators,
         which represents the protocol's bad-debt risk + capital efficiency loss.
    """
    if not positions:
        return StressResult(horizon_days, n_paths, 0, 0, 0.0, 0.0, 0.0, 0.0)

    rng = np.random.default_rng(seed)
    n_steps = horizon_days * steps_per_day
    dt = 1.0 / 365 / steps_per_day

    paths = _correlated_gbm(n_paths, n_steps, dt, asset_params, asset_corr, rng)

    # Pre-stack position data for vectorization
    coll_amts = np.array([p.collateral_amount for p in positions])
    debt_amts = np.array([p.debt_amount for p in positions])
    liq_thresh = np.array([p.liq_threshold_bps / BPS for p in positions])
    coll_assets = [p.collateral_asset for p in positions]
    debt_assets = [p.debt_asset for p in positions]

    n_liquidations_per_path = np.zeros(n_paths, dtype=np.int32)
    loss_per_path = np.zeros(n_paths, dtype=np.float64)
    already_liquidated = np.zeros((n_paths, len(positions)), dtype=bool)

    for t in range(1, n_steps + 1):
        # collateral & debt prices at this step for each position
        # shape (n_paths, n_positions)
        cp = np.stack([paths[a][:, t] for a in coll_assets], axis=1)
        dp = np.stack([paths[a][:, t] for a in debt_assets], axis=1)

        coll_value = cp * coll_amts[None, :] * liq_thresh[None, :]
        debt_value = dp * debt_amts[None, :]

        # HF per (path, position). Avoid division by zero.
        with np.errstate(divide="ignore", invalid="ignore"):
            hf = np.where(debt_value > 0, coll_value / debt_value, np.inf)

        newly_liquidated = (hf < 1.0) & (~already_liquidated)
        already_liquidated |= newly_liquidated

        # Loss = liquidation bonus paid out (50% of debt at close factor) — approximation
        close_factor = 0.5
        bonus_frac = liquidation_bonus_bps / BPS
        debt_being_repaid = debt_value * close_factor
        loss_per_path += np.sum(newly_liquidated * debt_being_repaid * bonus_frac, axis=1)
        n_liquidations_per_path += np.sum(newly_liquidated, axis=1).astype(np.int32)

    # Aggregate stats
    return StressResult(
        horizon_days=horizon_days,
        n_paths=n_paths,
        n_liquidations_mean=float(n_liquidations_per_path.mean()),
        n_liquidations_p95=float(np.percentile(n_liquidations_per_path, 95)),
        pct_paths_with_liquidation=float((n_liquidations_per_path > 0).mean()),
        expected_loss_usd=float(loss_per_path.mean()),
        var_95_loss_usd=float(np.percentile(loss_per_path, 95)),
        cvar_95_loss_usd=float(loss_per_path[loss_per_path >= np.percentile(loss_per_path, 95)].mean())
            if np.any(loss_per_path > 0) else 0.0,
    )
