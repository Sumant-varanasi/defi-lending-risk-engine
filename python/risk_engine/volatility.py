"""Realized volatility from price history.

We compute annualized realized volatility from log returns:

    r_t      = ln(P_t / P_{t-1})
    sigma_d  = sqrt(mean(r_t^2))     -- daily vol (zero-mean assumed for short windows)
    sigma_a  = sigma_d * sqrt(365)   -- annualized (continuous; crypto trades 24/7)

We also provide an EWMA estimator (Riskmetrics-style) that reacts faster to
regime changes — useful because static realized vol over a 30-day window
will lag a sudden crash, exactly when LTV adjustment matters most.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

CRYPTO_TRADING_DAYS = 365


@dataclass
class VolEstimate:
    realized_annual: float
    ewma_annual: float
    n_samples: int
    method_used: str


def _daily_resample(timestamps: np.ndarray, prices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Resample irregular tick data to one observation per day (last price)."""
    if len(timestamps) == 0:
        return timestamps, prices
    days = (timestamps - timestamps[0]) // 86400
    # take the last price observed for each day
    unique_days, last_idx = np.unique(days, return_index=False), None
    last_idx = np.array(
        [np.max(np.where(days == d)[0]) for d in unique_days]
    )
    return timestamps[last_idx], prices[last_idx]


def compute_volatility(
    timestamps: list[int] | np.ndarray,
    prices: list[float] | np.ndarray,
    lambda_ewma: float = 0.94,
    min_samples: int = 5,
) -> VolEstimate:
    """Compute annualized vol from price history.

    Args:
        timestamps: unix seconds, sorted ascending
        prices:     spot price at each ts
        lambda_ewma: EWMA decay factor (0.94 is RiskMetrics default for daily)
        min_samples: below this, return 0 vol (insufficient data)

    Returns:
        VolEstimate with both realized and EWMA annualized vols.
    """
    ts = np.asarray(timestamps, dtype=np.int64)
    p = np.asarray(prices, dtype=np.float64)
    if len(p) < min_samples + 1:
        return VolEstimate(0.0, 0.0, len(p), "insufficient_data")

    # Resample to daily
    ts_d, p_d = _daily_resample(ts, p)
    if len(p_d) < min_samples + 1:
        # not enough daily samples — fall back to using raw observations and scaling
        log_rets = np.diff(np.log(p))
        dt_secs = np.diff(ts).astype(np.float64)
        dt_secs[dt_secs <= 0] = 1.0
        # scale each return to daily vol equivalent: r_d = r / sqrt(dt / 1day)
        scaled = log_rets / np.sqrt(dt_secs / 86400.0)
        realized_daily = np.sqrt(np.mean(scaled**2))
        return VolEstimate(
            realized_annual=realized_daily * math.sqrt(CRYPTO_TRADING_DAYS),
            ewma_annual=realized_daily * math.sqrt(CRYPTO_TRADING_DAYS),
            n_samples=len(p),
            method_used="intraday_scaled",
        )

    log_rets = np.diff(np.log(p_d))

    # Realized
    realized_daily = math.sqrt(float(np.mean(log_rets**2)))

    # EWMA: sigma_t^2 = lambda * sigma_{t-1}^2 + (1 - lambda) * r_t^2
    var = float(log_rets[0] ** 2)
    for r in log_rets[1:]:
        var = lambda_ewma * var + (1 - lambda_ewma) * float(r) ** 2
    ewma_daily = math.sqrt(var)

    return VolEstimate(
        realized_annual=realized_daily * math.sqrt(CRYPTO_TRADING_DAYS),
        ewma_annual=ewma_daily * math.sqrt(CRYPTO_TRADING_DAYS),
        n_samples=len(p_d),
        method_used="daily_log_returns",
    )


def vol_from_db(conn, asset_addr: str, lookback_days: int = 30) -> VolEstimate:
    """Convenience: pull from analytics DB and compute vol."""
    import time
    since = int(time.time()) - lookback_days * 86400
    rows = conn.execute(
        "SELECT ts, CAST(price_wad AS REAL) / 1e18 FROM prices WHERE asset_addr = ? AND ts >= ? ORDER BY ts ASC",
        (asset_addr.lower(), since),
    ).fetchall()
    if not rows:
        return VolEstimate(0.0, 0.0, 0, "no_data")
    ts, prices = zip(*rows)
    return compute_volatility(ts, prices)
