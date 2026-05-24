"""Synthetic price data calibrated to historical ETH characteristics.

Statistical properties targeted (based on public ETH/USD daily returns 2018-2024):

  - Annual drift:            ~+30% (long-term up trend, but high variance YoY)
  - Annual volatility:        70-100% (regime-dependent)
  - Daily return kurtosis:    ~8 (fat-tailed; normal would be 3)
  - Daily return skew:        slightly negative (-0.3 to -0.5)

We use a regime-switching jump-diffusion process:

    dS/S = mu * dt + sigma_t * dW + J_t * dN

where:
  - sigma_t alternates between "calm" (sigma=0.55) and "stress" (sigma=1.20)
    regimes via a two-state hidden Markov chain
  - J_t is a downward jump triggered at specific calibrated event dates
    (March 2020 COVID, May 2021 crash, May 2022 LUNA, Nov 2022 FTX)

The result is a synthetic time series that LOOKS like ETH but is reproducible
(seeded RNG). For real backtests, swap in real CSV data — see `load_real_data()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ETH_DEFAULT_ANNUAL_DRIFT = 0.55      # closer to real ETH's 2018-2024 geometric mean
ETH_CALM_ANNUAL_VOL = 0.55
ETH_STRESS_ANNUAL_VOL = 1.00
TRADING_DAYS = 365


# Historical crash events: (date, return_pct) — applied as 1-day shocks.
# Magnitudes scaled to roughly match what really happened on those days
# (we don't aim for exact match — only realistic stress).
HISTORICAL_CRASHES = [
    ("2020-03-12", -0.40),  # COVID Black Thursday
    ("2020-03-13", -0.08),  # COVID continuation
    ("2021-05-19", -0.22),  # May 2021 China FUD crash
    ("2022-05-09", -0.13),  # LUNA depeg
    ("2022-05-12", -0.15),  # LUNA full collapse
    ("2022-06-13", -0.12),  # Celsius
    ("2022-11-08", -0.15),  # FTX
    ("2022-11-09", -0.08),  # FTX continuation
]

# Rally events — bull runs that lasted weeks; we add a multiplier over windows.
HISTORICAL_RALLIES = [
    # (start_date, end_date, total_log_return_added)
    ("2020-10-01", "2021-04-15", 2.30),    # bull run from $350 to $4000
    ("2021-07-15", "2021-11-08", 1.30),    # second leg to ATH
    ("2023-10-01", "2024-01-01", 0.50),    # late 2023 rally
]


@dataclass
class Bar:
    date: pd.Timestamp
    price: float
    log_return: float
    regime: int  # 0=calm, 1=stress


def generate_eth_prices(
    start: str = "2020-01-01",
    end: str = "2024-01-01",
    start_price: float = 130.0,  # ETH was ~$130 at start of 2020
    seed: int = 42,
    transition_prob_calm_to_stress: float = 0.005,  # ~once per 200 days
    transition_prob_stress_to_calm: float = 0.05,   # stress regimes are ~20 days
) -> pd.DataFrame:
    """Generate a synthetic ETH price series with realistic stylized facts.

    Returns a DataFrame indexed by date with columns:
      - price (USD)
      - log_return
      - regime (0=calm, 1=stress)
    """
    rng = np.random.default_rng(seed)

    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)
    dates = pd.date_range(start_dt, end_dt, freq="D")
    n = len(dates)

    # Markov regime simulation
    regime = np.zeros(n, dtype=np.int8)
    for i in range(1, n):
        if regime[i-1] == 0:
            regime[i] = 1 if rng.random() < transition_prob_calm_to_stress else 0
        else:
            regime[i] = 0 if rng.random() < transition_prob_stress_to_calm else 1

    # Per-day vol from regime
    sigma_daily = np.where(
        regime == 0,
        ETH_CALM_ANNUAL_VOL / np.sqrt(TRADING_DAYS),
        ETH_STRESS_ANNUAL_VOL / np.sqrt(TRADING_DAYS),
    )
    mu_daily = ETH_DEFAULT_ANNUAL_DRIFT / TRADING_DAYS

    # GBM-style log returns
    z = rng.standard_normal(n)
    log_returns = (mu_daily - 0.5 * sigma_daily**2) + sigma_daily * z

    # Inject historical crash events
    date_to_idx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(dates)}
    for crash_date, ret in HISTORICAL_CRASHES:
        if crash_date in date_to_idx:
            idx = date_to_idx[crash_date]
            log_returns[idx] = np.log(1 + ret)
            # mark days around the crash as stress regime
            for j in range(max(0, idx - 2), min(n, idx + 10)):
                regime[j] = 1

    # Inject historical rally events (spread incrementally over window)
    for start, end, total_log in HISTORICAL_RALLIES:
        if start in date_to_idx and end in date_to_idx:
            i0, i1 = date_to_idx[start], date_to_idx[end]
            per_day = total_log / max(1, (i1 - i0))
            log_returns[i0:i1] += per_day

    # First return = 0 (baseline)
    log_returns[0] = 0
    log_prices = np.cumsum(log_returns)
    prices = start_price * np.exp(log_prices)

    return pd.DataFrame({
        "date": dates,
        "price": prices,
        "log_return": log_returns,
        "regime": regime,
    }).set_index("date")


def load_real_data(csv_path: str | Path) -> pd.DataFrame:
    """Load real price data from a CSV.

    Expected columns: date (parseable), close OR price.

    If you have a CryptoCompare/Yahoo Finance/CoinGecko export, drop it at
    `data/eth_history.csv` and the run script will use it automatically.
    """
    df = pd.read_csv(csv_path)
    df.columns = [c.lower() for c in df.columns]
    price_col = "close" if "close" in df.columns else "price"
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")
    df["log_return"] = np.log(df[price_col]).diff().fillna(0)
    df["regime"] = 0
    df = df.rename(columns={price_col: "price"})
    return df[["price", "log_return", "regime"]]


def summary_stats(df: pd.DataFrame) -> dict:
    """Statistical fingerprint of the returns — for sanity-checking calibration."""
    r = df["log_return"].dropna()
    daily_vol = r.std()
    return {
        "n_days": len(r),
        "annual_vol_realized": float(daily_vol * np.sqrt(TRADING_DAYS)),
        "annual_return_geo": float(np.exp(r.mean() * TRADING_DAYS) - 1),
        "min_daily_return": float(r.min()),
        "max_daily_return": float(r.max()),
        "skew": float(r.skew()),
        "kurtosis_excess": float(r.kurtosis()),
        "start_price": float(df["price"].iloc[0]),
        "end_price": float(df["price"].iloc[-1]),
        "max_drawdown": float(_max_drawdown(df["price"])),
        "pct_days_in_stress_regime": float((df["regime"] == 1).mean()),
    }


def _max_drawdown(prices: pd.Series) -> float:
    """Worst peak-to-trough decline as a fraction (negative)."""
    cum = prices / prices.cummax()
    return cum.min() - 1.0


if __name__ == "__main__":
    df = generate_eth_prices()
    stats = summary_stats(df)
    print("Generated synthetic ETH prices:")
    for k, v in stats.items():
        if isinstance(v, float):
            print(f"  {k:30s} {v:>10.4f}")
        else:
            print(f"  {k:30s} {v}")
