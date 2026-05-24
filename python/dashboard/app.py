"""Streamlit dashboard.

Run with: `streamlit run python/dashboard/app.py`

Five views (selectable from the sidebar):
  1. Pool Overview      — TVL, borrows, rates, utilization per reserve
  2. Rate Curves        — kinked IRM visualized with current operating point
  3. Positions          — borrower table sorted by health factor, danger zone highlighted
  4. Risk Engine        — recommended vs on-chain LTV, vol estimate, stress test
  5. Activity           — recent on-chain events (deposits, borrows, liquidations)

The dashboard is read-only. It pulls from the analytics SQLite DB (populated
by the aggregator service) and from live chain reads (cached briefly to keep
the UI snappy).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from python.analytics.db import (
    get_price_series,
    get_recent_events,
    get_recent_positions,
    get_reserve_series,
    session,
)
from python.chain import Client, asset_meta
from python.config import ASSETS
from python.risk_engine.dynamic_ltv import ModelConfig, RiskInputs, recommend, explain
from python.risk_engine.stress_test import AssetParams, Position, stress_test
from python.risk_engine.volatility import vol_from_db


st.set_page_config(page_title="Lending Protocol Dashboard", layout="wide", page_icon="📈")


# ----------------------------------------------------------------------
# Connection (cached)
# ----------------------------------------------------------------------
@st.cache_resource
def get_client() -> Client | None:
    try:
        return Client()
    except Exception as e:
        st.error(f"Could not connect to chain: {e}")
        return None


@st.cache_data(ttl=15)
def get_live_reserves(_c: Client) -> list[dict]:
    out = []
    for addr in _c.get_reserves_list():
        meta = asset_meta(addr)
        if not meta:
            continue
        try:
            r = _c.get_reserve_data(addr)
            price_usd = _c.get_price(addr) / 1e18
        except Exception:
            continue
        dec = 10 ** meta.decimals
        total_supply_native = r.total_supply / dec
        total_borrow_native = r.total_borrow / dec
        out.append({
            "symbol": meta.symbol,
            "address": addr,
            "decimals": meta.decimals,
            "price_usd": price_usd,
            "total_supply": total_supply_native,
            "total_borrow": total_borrow_native,
            "utilization": r.utilization_wad / 1e18,
            "borrow_apr": r.borrow_apr_wad / 1e18,
            "supply_apr": r.supply_apr_wad / 1e18,
            "liquidity_index_ray": r.liquidity_index,
            "borrow_index_ray": r.borrow_index,
        })
    return out


# ----------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------
PAGES = [
    "📊 Pool Overview",
    "📈 Rate Curves",
    "👥 Positions",
    "🛡️ Risk Engine",
    "🗒️ Activity",
]

st.sidebar.title("DeFi Lending")
page = st.sidebar.radio("View", PAGES, label_visibility="collapsed")
st.sidebar.markdown("---")

client = get_client()

if client is None:
    st.warning("Dashboard is running without an on-chain connection. "
               "Some views will only show historical data from the DB.")


# ======================================================================
# Page 1 — Pool Overview
# ======================================================================
def page_overview():
    st.title("📊 Pool Overview")
    if not client:
        st.stop()

    reserves = get_live_reserves(client)
    if not reserves:
        st.info("No reserves found. Did the deploy script run and `.env` get populated?")
        return

    # KPIs (sum across reserves)
    total_tvl = sum(r["total_supply"] * r["price_usd"] for r in reserves)
    total_borrows = sum(r["total_borrow"] * r["price_usd"] for r in reserves)
    avg_util = total_borrows / total_tvl if total_tvl > 0 else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Supplied", f"${total_tvl:,.0f}")
    c2.metric("Total Borrowed", f"${total_borrows:,.0f}")
    c3.metric("Avg Utilization", f"{avg_util:.1%}")
    c4.metric("# Reserves", len(reserves))

    st.markdown("### Per-Reserve Detail")
    df = pd.DataFrame([
        {
            "Asset": r["symbol"],
            "Price (USD)": f"${r['price_usd']:,.2f}",
            "Total Supply": f"{r['total_supply']:,.4f}",
            "Utilization": f"{r['utilization']:.1%}",
            "Borrow APR": f"{r['borrow_apr']:.2%}",
            "Supply APR": f"{r['supply_apr']:.2%}",
        }
        for r in reserves
    ])
    st.dataframe(df, hide_index=True, use_container_width=True)

    # Historical utilization plot (one chart per reserve)
    st.markdown("### Utilization History")
    with session() as conn:
        since = int(time.time()) - 7 * 86400
        for r in reserves:
            rows = get_reserve_series(conn, r["address"], since)
            if not rows:
                continue
            df_hist = pd.DataFrame(rows, columns=["ts", "utilization", "borrow_apr", "supply_apr", "supply", "borrow"])
            df_hist["time"] = pd.to_datetime(df_hist["ts"], unit="s")
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=df_hist["time"], y=df_hist["utilization"] * 100,
                                     name="Utilization (%)", line=dict(color="#3b82f6")))
            fig.add_trace(go.Scatter(x=df_hist["time"], y=df_hist["borrow_apr"] * 100,
                                     name="Borrow APR (%)", yaxis="y2", line=dict(color="#ef4444")))
            fig.update_layout(
                title=f"{r['symbol']}",
                yaxis=dict(title="Utilization %"),
                yaxis2=dict(title="APR %", overlaying="y", side="right"),
                height=300,
                margin=dict(l=40, r=40, t=40, b=20),
            )
            st.plotly_chart(fig, use_container_width=True)


# ======================================================================
# Page 2 — Rate Curves (visualize the kinked IRM)
# ======================================================================
def page_curves():
    st.title("📈 Interest Rate Curves")
    st.markdown(
        "The protocol uses a **two-slope kinked** rate model. Below the kink "
        "utilization, rates rise gently (encouraging borrowing). Above it, "
        "rates rise steeply (discouraging further borrowing and protecting "
        "withdrawal liquidity)."
    )

    if not client:
        st.stop()

    # Read IRM parameters from chain
    try:
        irm_address = client.pool.functions.reserves(
            client.w3.to_checksum_address(client.get_reserves_list()[0])
        ).call()[9]
        irm = client.w3.eth.contract(
            address=client.w3.to_checksum_address(irm_address),
            abi=[
                {"inputs": [], "name": "baseRateWad", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
                {"inputs": [], "name": "slope1Wad", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
                {"inputs": [], "name": "slope2Wad", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
                {"inputs": [], "name": "optimalUtilWad", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
            ],
        )
        base = irm.functions.baseRateWad().call() / 1e18
        slope1 = irm.functions.slope1Wad().call() / 1e18
        slope2 = irm.functions.slope2Wad().call() / 1e18
        kink = irm.functions.optimalUtilWad().call() / 1e18
    except Exception as e:
        st.warning(f"Could not load IRM params from chain: {e}")
        base, slope1, slope2, kink = 0.02, 0.04, 0.75, 0.80

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Base Rate", f"{base:.2%}")
    c2.metric("Slope 1", f"{slope1:.2%}")
    c3.metric("Slope 2", f"{slope2:.2%}")
    c4.metric("Kink", f"{kink:.1%}")

    # Sample the curve
    u = np.linspace(0, 1, 200)
    rates = np.where(
        u <= kink,
        base + (u / kink) * slope1,
        base + slope1 + ((u - kink) / (1 - kink)) * slope2,
    )

    reserves = get_live_reserves(client)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=u * 100, y=rates * 100, name="Borrow APR", line=dict(color="#ef4444", width=3)))
    # supply curve assumes reserve factor = 0.1
    rf = 0.10
    supply_rates = rates * u * (1 - rf)
    fig.add_trace(go.Scatter(x=u * 100, y=supply_rates * 100, name="Supply APR (RF=10%)",
                             line=dict(color="#10b981", width=3)))
    # mark kink
    fig.add_vline(x=kink * 100, line_dash="dash", line_color="gray", annotation_text="Kink")

    # mark current operating points for each reserve
    for r in reserves:
        fig.add_trace(go.Scatter(
            x=[r["utilization"] * 100],
            y=[r["borrow_apr"] * 100],
            mode="markers+text",
            marker=dict(size=14, color="#1e3a8a", symbol="diamond"),
            text=[r["symbol"]],
            textposition="top center",
            name=f"{r['symbol']} (live)",
            showlegend=False,
        ))

    fig.update_layout(
        xaxis_title="Utilization (%)",
        yaxis_title="APR (%)",
        height=480,
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)


# ======================================================================
# Page 3 — Positions
# ======================================================================
def page_positions():
    st.title("👥 Borrower Positions")
    st.caption("Sorted by health factor (ascending — riskiest first).")

    with session() as conn:
        rows = get_recent_positions(conn, limit=200)

    if not rows:
        st.info("No position snapshots yet. Make sure the aggregator service is running.")
        return

    df = pd.DataFrame(rows, columns=["User", "Last Snapshot (ts)", "Collateral (USD)", "Debt (USD)", "HF", "LiqThresh (bps)"])
    df["Last Snapshot"] = pd.to_datetime(df["Last Snapshot (ts)"], unit="s")
    df.drop(columns=["Last Snapshot (ts)"], inplace=True)

    # Mask "infinite" HF (no debt) for display
    df["HF Display"] = df["HF"].apply(lambda v: "∞" if v >= 1e18 else f"{v:.3f}")

    # Risk zone classification
    def zone(hf: float) -> str:
        if hf < 1.0: return "🔴 LIQUIDATABLE"
        if hf < 1.1: return "🟠 Danger"
        if hf < 1.5: return "🟡 Caution"
        return "🟢 Safe"

    df["Zone"] = df["HF"].apply(zone)

    display = df[["User", "Zone", "HF Display", "Collateral (USD)", "Debt (USD)", "Last Snapshot"]].copy()
    display["Collateral (USD)"] = display["Collateral (USD)"].map(lambda x: f"${x:,.2f}")
    display["Debt (USD)"] = display["Debt (USD)"].map(lambda x: f"${x:,.2f}")

    # Top KPIs
    n_liquidatable = (df["HF"] < 1.0).sum()
    n_danger = ((df["HF"] >= 1.0) & (df["HF"] < 1.1)).sum()
    total_debt = df["Debt (USD)"].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("Liquidatable", n_liquidatable, delta=None)
    c2.metric("In Danger Zone", n_danger)
    c3.metric("Total Outstanding Debt", f"${total_debt:,.0f}")

    st.dataframe(display, hide_index=True, use_container_width=True, height=600)


# ======================================================================
# Page 4 — Risk Engine
# ======================================================================
def page_risk():
    st.title("🛡️ Risk Engine")
    st.markdown(
        "Recommends LTV and liquidation threshold per reserve based on "
        "realized volatility, pool concentration, and liquidity depth. "
        "These are **suggestions** — applying them requires admin action."
    )

    if not client:
        st.stop()
    reserves = get_live_reserves(client)
    if not reserves:
        st.info("No reserves loaded.")
        return

    asset_symbols = [r["symbol"] for r in reserves]
    chosen = st.selectbox("Asset", asset_symbols)
    chosen_reserve = next(r for r in reserves if r["symbol"] == chosen)

    # ---- vol & inputs ------------------------------------------------
    with session() as conn:
        vol = vol_from_db(conn, chosen_reserve["address"], lookback_days=30)
        # find largest position for this asset by scanning DB
        # (approximation: use total snapshot data)
        rows = get_recent_positions(conn, limit=500)
    largest = 0.0
    n_borrowers = 0
    for r in rows:
        if r[3] > 0:  # has debt
            n_borrowers += 1
        if r[2] > largest:
            largest = r[2]

    tvl = chosen_reserve["total_supply"] * chosen_reserve["price_usd"]

    inputs = RiskInputs(
        realized_vol_annual=vol.ewma_annual,
        pool_total_supply_usd=tvl,
        largest_position_usd=largest,
        n_borrowers=n_borrowers,
    )
    params = recommend(inputs)

    # KPIs
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Realized Vol (EWMA, annual)", f"{vol.ewma_annual:.1%}",
              help=f"Method: {vol.method_used}, n={vol.n_samples}")
    c2.metric("Recommended LTV", f"{params.ltv_recommended_bps / 100:.1f}%",
              help="Base LTV adjusted for vol, concentration, and liquidity")
    c3.metric("Recommended Liq Threshold", f"{params.liq_threshold_recommended_bps / 100:.1f}%")
    c4.metric("Stress Score", f"{params.stress_score:.0f}/100",
              delta="risky" if params.stress_score > 50 else "ok",
              delta_color="inverse")

    with st.expander("📋 Breakdown"):
        st.code(explain(inputs, params), language=None)

    st.markdown("---")
    st.subheader("Monte Carlo Stress Test")

    horizon = st.slider("Horizon (days)", 1, 30, 7)
    n_paths = st.slider("Paths", 500, 20_000, 5_000, step=500)
    shock_mult = st.slider("Vol multiplier (stress)", 0.5, 5.0, 1.0, 0.25)

    # Build positions from snapshots
    positions: list[Position] = []
    for r in rows:
        user, ts_, coll_usd, debt_usd, hf, liq_thresh = r
        if debt_usd <= 0:
            continue
        # Aggregate: model the user as 1 position with synthetic single-asset
        # collateral & debt at unit price 1 (USD). We then shock by vol of the
        # chosen asset (rough proxy — extend by per-asset tracking).
        positions.append(Position(
            user=user,
            collateral_asset="USD",
            collateral_amount=float(coll_usd),
            debt_asset="USD2",
            debt_amount=float(debt_usd),
            liq_threshold_bps=int(liq_thresh),
        ))

    if not positions:
        st.info("No active borrowers in snapshots yet — stress test skipped.")
        return

    asset_params = {
        "USD": AssetParams(price=1.0, annual_vol=max(vol.ewma_annual, 0.01) * shock_mult, annual_drift=0.0),
        "USD2": AssetParams(price=1.0, annual_vol=0.01, annual_drift=0.0),  # debt asset = stablecoin
    }

    with st.spinner("Simulating..."):
        result = stress_test(positions, asset_params, horizon_days=horizon, n_paths=int(n_paths))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Paths w/ Liquidation", f"{result.pct_paths_with_liquidation:.1%}")
    c2.metric("E[# Liquidations]", f"{result.n_liquidations_mean:.1f}")
    c3.metric("Expected Loss (USD)", f"${result.expected_loss_usd:,.0f}")
    c4.metric("VaR 95% Loss", f"${result.var_95_loss_usd:,.0f}")

    st.caption(
        "Loss = liquidation bonus paid out. Captures protocol-side capital "
        "efficiency drag from liquidations; not bad debt (which would require "
        "modeling collateral price drops below 100% LTV)."
    )


# ======================================================================
# Page 5 — Activity
# ======================================================================
def page_activity():
    st.title("🗒️ Activity")
    kinds = st.multiselect("Filter", ["deposit", "withdraw", "borrow", "repay", "liquidation"],
                            default=["liquidation", "borrow", "deposit"])
    limit = st.slider("Show last N", 20, 1000, 200)

    with session() as conn:
        rows = get_recent_events(conn, limit=limit, kinds=kinds if kinds else None)

    if not rows:
        st.info("No events ingested yet. Make sure the indexer is running.")
        return

    df = pd.DataFrame(rows, columns=["ts", "Kind", "User", "Asset", "Amount", "Tx Hash"])
    df["When"] = pd.to_datetime(df["ts"], unit="s")
    df.drop(columns=["ts"], inplace=True)

    # try to label asset by symbol
    sym_by_addr = {a.address.lower(): a.symbol for a in ASSETS.values()}
    df["Asset"] = df["Asset"].map(lambda a: sym_by_addr.get((a or "").lower(), (a or "")[:10]))

    # human-format amounts based on asset
    def fmt(row):
        if not row["Amount"]:
            return ""
        try:
            n = int(row["Amount"])
        except Exception:
            return row["Amount"]
        sym = row["Asset"]
        dec = {"WETH": 18, "WBTC": 8, "USDC": 6}.get(sym, 18)
        return f"{n / 10**dec:,.4f} {sym}"
    df["Amount"] = df.apply(fmt, axis=1)

    df["Tx Hash"] = df["Tx Hash"].map(lambda h: (h or "")[:10] + "…")
    st.dataframe(df[["When", "Kind", "User", "Asset", "Amount", "Tx Hash"]],
                 hide_index=True, use_container_width=True, height=600)


# ======================================================================
# Dispatcher
# ======================================================================
if page == PAGES[0]:
    page_overview()
elif page == PAGES[1]:
    page_curves()
elif page == PAGES[2]:
    page_positions()
elif page == PAGES[3]:
    page_risk()
elif page == PAGES[4]:
    page_activity()
