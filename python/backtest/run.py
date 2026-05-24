"""Backtest entry point.

Generates synthetic price data, runs three LTV policies, writes reports
and plots into ``backtest_results/``.

Usage:
    PYTHONPATH=. python3 -m python.backtest.run

Optional arguments via env:
    BACKTEST_N_BORROWERS=200  (default 200)
    BACKTEST_SEED=7           (default 7)
    BACKTEST_OUT=/path/to/dir (default: backtest_results/)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from python.backtest.data import generate_eth_prices, summary_stats
from python.backtest.simulator import (
    run_backtest,
    make_static_aggressive,
    make_static_conservative,
    make_dynamic,
)
from python.backtest.report import (
    plot_price_with_crashes,
    plot_cumulative_liquidations,
    plot_capital_efficiency,
    plot_avg_hf,
    plot_summary_bar,
    plot_dynamic_ltv,
    write_markdown_report,
    build_summary_table,
)


def main() -> int:
    n_borrowers = int(os.environ.get("BACKTEST_N_BORROWERS", 50))
    seed = int(os.environ.get("BACKTEST_SEED", 7))
    out_dir = Path(os.environ.get("BACKTEST_OUT", "backtest_results"))
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] Generating synthetic ETH price data (seed={seed})...")
    prices = generate_eth_prices(seed=seed)
    stats = summary_stats(prices)
    print(f"      {stats['n_days']} days, "
          f"vol={stats['annual_vol_realized']:.1%}, "
          f"return={stats['annual_return_geo']:+.1%}, "
          f"MDD={stats['max_drawdown']:.1%}")

    print(f"\n[2/5] Running Static-Aggressive (80/85) on {n_borrowers} borrowers...")
    res_agg = run_backtest(prices, make_static_aggressive(),
                           n_borrowers=n_borrowers,
                           new_borrowers_per_day=1.0,
                           seed=seed)
    print(f"      liquidations={res_agg.total_liquidations}, "
          f"bonus_paid=${res_agg.total_liquidator_bonus_paid:,.0f}, "
          f"bad_debt=${res_agg.total_bad_debt:,.0f}, "
          f"avg_eff={res_agg.avg_capital_efficiency:.1%}, "
          f"total_borrowers={res_agg.n_total_borrowers}")

    print(f"\n[3/5] Running Static-Conservative (60/65)...")
    res_con = run_backtest(prices, make_static_conservative(),
                           n_borrowers=n_borrowers,
                           new_borrowers_per_day=1.0,
                           seed=seed)
    print(f"      liquidations={res_con.total_liquidations}, "
          f"bonus_paid=${res_con.total_liquidator_bonus_paid:,.0f}, "
          f"bad_debt=${res_con.total_bad_debt:,.0f}, "
          f"avg_eff={res_con.avg_capital_efficiency:.1%}, "
          f"total_borrowers={res_con.n_total_borrowers}")

    print(f"\n[4/5] Running Dynamic (vol-responsive)...")
    res_dyn = run_backtest(prices, make_dynamic(),
                           n_borrowers=n_borrowers,
                           new_borrowers_per_day=1.0,
                           seed=seed)
    print(f"      liquidations={res_dyn.total_liquidations}, "
          f"bonus_paid=${res_dyn.total_liquidator_bonus_paid:,.0f}, "
          f"bad_debt=${res_dyn.total_bad_debt:,.0f}, "
          f"avg_eff={res_dyn.avg_capital_efficiency:.1%}, "
          f"total_borrowers={res_dyn.n_total_borrowers}")

    results = [res_agg, res_con, res_dyn]

    print(f"\n[5/5] Writing plots and reports to {out_dir}/ ...")
    plot_price_with_crashes(prices, out_dir / "price.html")
    plot_cumulative_liquidations(results, out_dir / "cumulative_liquidations.html")
    plot_capital_efficiency(results, out_dir / "capital_efficiency.html")
    plot_avg_hf(results, out_dir / "avg_hf.html")
    plot_summary_bar(results, out_dir / "summary.html")
    plot_dynamic_ltv(prices, res_dyn, out_dir / "dynamic_ltv.html")
    write_markdown_report(results, stats, out_dir / "REPORT.md")

    # Also save the raw price data and summary table as CSVs for further analysis
    prices.to_csv(out_dir / "prices.csv")
    build_summary_table(results).to_csv(out_dir / "summary_table.csv", index=False)
    for r in results:
        slug = r.policy_name.split()[0].lower().replace("-", "_")
        r.history.to_csv(out_dir / f"history_{slug}.csv")

    print("\nDone.")
    print("\nSummary table:")
    print(build_summary_table(results).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
