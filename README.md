# DeFi Lending Protocol with Risk Engine

A learning-scope, Aave/Compound-inspired lending protocol — Solidity contracts
plus a Python off-chain stack (liquidator, risk engine, analytics, dashboard).

**What's here:**
- Multi-asset lending pool with scaled balances and per-second interest indices
- Two-slope kinked interest rate model
- Permissionless liquidations with configurable bonus + 50% close factor
- Mock oracle (replace with Chainlink for production)
- Python liquidator bot with profitability scoring + nonce-safe execution
- Realized & EWMA volatility estimator
- **Dynamic LTV recommender** (the originality piece — see `python/risk_engine/dynamic_ltv.py`)
- Monte Carlo stress tester (correlated GBM)
- Streamlit dashboard with five views

**Project status:** This is a starting skeleton. It compiles and runs locally,
but it is **not** audited, not gas-optimized, and uses a mock oracle. Treat
it as scaffolding to extend during a semester.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                          On-chain (EVM)                             │
│                                                                     │
│   LendingPool ──── InterestRateModel       PriceOracle (mock)       │
│       │                                                             │
│       └──── reserves[asset] ──── user balances (scaled by index)    │
│                                                                     │
└──────────┬─────────────────────────────────────────────────────────┘
           │ RPC (web3.py)
┌──────────▼──────────────────┐  ┌────────────────────────────────┐
│  Event Indexer              │  │  Aggregator (60s)              │
│  - subscribes to events     │  │  - snapshots reserves + users  │
│  - tracks known_users       │  │  - writes to SQLite            │
│  - lags head by N blocks    │  │                                │
└──────────┬──────────────────┘  └──────────┬─────────────────────┘
           │                                │
           ├────────────┬───────────────────┤
           │            │                   │
┌──────────▼────┐  ┌────▼─────────┐  ┌──────▼──────────┐
│  Liquidator   │  │  Risk Engine │  │  Dashboard      │
│  - scans HF   │  │  - vol calc  │  │  - Streamlit    │
│  - prof check │  │  - dyn. LTV  │  │  - 5 views      │
│  - submits tx │  │  - MC stress │  │  - reads DB     │
└───────────────┘  └──────────────┘  └─────────────────┘
```

---

## Prerequisites

- **Foundry** (`curl -L https://foundry.paradigm.xyz | bash; foundryup`)
- **Python 3.11+**
- **`jq`** (for extracting ABIs)

---

## First-time setup

```bash
# 1. Install Python deps
make install

# 2. Compile contracts
make build

# 3. Run tests (should pass)
make test
```

---

## Run it locally (5-terminal demo)

### Terminal 1 — Anvil

```bash
anvil --block-time 2
```

Keep this running. It exposes RPC on `http://127.0.0.1:8545` and prints
ten pre-funded accounts at startup.

### Terminal 2 — Deploy + populate `.env`

```bash
cp .env.example .env          # uses Anvil's account 0 key by default

# Deploy contracts
make deploy-local

# Grep the addresses out of the broadcast log and paste them into .env:
#   POOL=0x...
#   ORACLE=0x...
#   IRM=0x...
#   WETH=0x...
#   USDC=0x...
#   WBTC=0x...

# Extract ABIs for Python
make abi
```

### Terminal 3 — Indexer + Aggregator

```bash
make indexer       # processes events into the DB
# (and in another tab)
make aggregator    # 60-second snapshots of pool state + positions
```

### Terminal 4 — Seed demo positions

```bash
make seed
```

This creates five accounts with varied positions, including one near-danger
borrower (Carol, HF ~1.10) who will become liquidatable if you drop the
ETH price.

### Terminal 5 — Dashboard

```bash
make dashboard
```

Open `http://localhost:8501`. You should see all five views populated.

### Trigger a liquidation (optional)

In yet another terminal:

```bash
# Drop ETH from $2000 to $1500
source .env
cast send $ORACLE "setPrice(address,uint256)" $WETH 1500000000000000000000 \
  --rpc-url $RPC_URL --private-key $PRIVATE_KEY

# Start the liquidator
make liquidator
```

Within one polling interval the bot should detect Carol's HF dropping below
1.0 and submit a liquidation. Watch the dashboard's Activity page light up.

---

## Repo layout

```
contracts/
  src/
    LendingPool.sol            # main contract (700+ lines)
    InterestRateModel.sol      # two-slope kinked IRM
    PriceOracle.sol            # mock oracle (admin-settable)
    libraries/WadMath.sol      # fixed-point math
    interfaces/                # IERC20, IPriceOracle, IInterestRateModel
    mocks/MockERC20.sol        # for tests + local deploy
  test/LendingPool.t.sol       # Foundry tests
  script/Deploy.s.sol          # Anvil/testnet deploy

python/
  chain.py                     # web3 client wrapper
  config.py                    # env vars + asset metadata

  liquidator/
    event_indexer.py           # event-driven indexer
    bot.py                     # liquidator main loop

  risk_engine/
    volatility.py              # realized + EWMA vol
    dynamic_ltv.py             # ←  the originality layer
    stress_test.py             # Monte Carlo
    runner.py                  # periodic recommender

  analytics/
    db.py                      # SQLite schema + helpers
    aggregator.py              # state snapshotter

  dashboard/
    app.py                     # 5-view Streamlit app

scripts/
  seed_demo.py                 # seeds positions for the demo
```

---

## Key design decisions

**Scaled balances.** A user's deposit is stored as `scaledBalance = amount /
liquidityIndex` at the time of deposit. Their actual balance at any later
moment is `scaledBalance × liquidityIndex`. This makes interest accrual O(1)
in the number of users — same trick Aave uses.

**Indices in RAY (1e27), values in WAD (1e18).** Indices accumulate over years
so they need extra precision; for everything else, WAD is plenty. The
`WadMath` library has both, plus a Taylor-expansion `compoundFactor` for
per-second compounding.

**Health factor = (Σ collateral × liqThreshold) / Σ debt**, all in USD-WAD.
HF < 1.0 (`1e18`) ⇒ liquidatable. The Solidity does this iteratively over
`reservesList[]`; for production you'd want a per-user reserve bitmap to
skip iterating over assets the user doesn't have.

**Liquidations.** 50% close factor, max 15% bonus. The Solidity rounds in
favor of the protocol when collateral is exhausted (recomputes `actualRepay`
so the liquidator doesn't overpay).

**Mock oracle.** This is the biggest divergence from a production protocol —
real oracles have TWAP fallbacks, multiple data sources, and circuit
breakers. Plug in Chainlink's `AggregatorV3Interface` as the next step.

---

## What to do over the semester (suggested roadmap)

This skeleton implements the standard patterns plus several
upgrades from the v2 round. To strengthen it into a thesis-quality
project, focus on the gaps:

### Contracts (security depth)
- ~~Replace mock oracle with Chainlink~~ ✅ — `ChainlinkPriceOracle.sol`
  is wired with staleness + zero-answer checks. Add TWAP fallback
  (Uniswap V3) and a sequencer-uptime feed for L2.
- ~~Add `flashLoan()`~~ ✅ — implemented. Add ERC-3156 strict
  compatibility and second-level fee tiers if you want.
- Add governance: parameter changes through a timelock + multisig.
- Run Slither + Mythril; work through `Damn Vulnerable DeFi` challenges.
- Differential test against a reference Aave V2 fork using
  `vm.createSelectFork`.
- Per-user reserve bitmap to skip iterating over uninvolved reserves in
  HF computation (Aave V3 pattern). Touches six functions — wait until
  you have the test harness running locally before attempting.

### Risk engine (originality)
- ~~Backtest dynamic vs static LTV~~ ✅ — `python/backtest/` runs three
  policies over 4 years of calibrated synthetic data. Drop a real ETH
  CSV at `data/eth_history.csv` and the loader switches to real prices.
- Tune the dynamic recommender: see backtest finding that the current
  `sigma_target=0.60` is too tight — try 0.80 with `alpha=0.4`.
- Concentration risk pricing as a per-borrower fee (whales pay more):
  currently visible only in off-chain LTV recommendations, not as an
  on-chain fee on borrow rate.
- Time-weighted health factor: persistent near-liquidation borrowers
  pay extra.
- Add jump-diffusion (Merton) to the Monte Carlo stress test for
  fat-tail risk.

### Off-chain (reliability + UX)
- Liquidator: replace `getUserAccountData()` polling with a Multicall3
  batch.
- Add Slack/Discord alerts when stress score crosses thresholds.
- Dashboard: swap Streamlit for Next.js + wagmi + viem, deploy to
  Vercel. Genuinely Week 12 work — the Streamlit app is the right
  scaffold for development.
- Add a "submit governance proposal" button that converts risk
  recommendations into on-chain admin txs.

---

## What's intentionally NOT here (still)

- **eMode / isolated collateral / siloed borrows.** Aave V3 features.
  Each is a several-week project; skip unless capital efficiency is
  your thesis focus.
- **Per-user reserve bitmap.** Touches deposit, withdraw, borrow,
  repay, liquidate, and HF calc — refactor-y. Worth doing once your
  test harness can cover all five paths.
- **Real cross-asset correlation matrix.** The Monte Carlo uses
  assumed correlations; replace with sample covariance from real
  historical returns when you have data access.
- **Comprehensive coverage.** Tests cover happy paths, reverts, fuzz
  properties, and basic invariants. Production-grade would add: more
  invariant handlers, mainnet-fork integration tests, formal
  verification of critical functions (Halmos / Certora).
- **Gas optimization.** Storage reads in HF loops are tolerable for ~5
  reserves; bitmap optimization above is the right fix at scale.

---

## v2 additions in this build

- **Treasury accrual.** `ReserveData.accruedToTreasuryScaled` tracks the
  spread between borrower interest and supplier interest. Admin calls
  `mintToTreasury(asset)` to realize the balance.
- **Flash loans.** `flashLoan(receiver, asset, amount, data)`. ERC-3156-
  inspired callback (`IFlashLoanReceiver.onFlashLoan`). Pool verifies
  balance restoration rather than trusting a return value; fee accrues
  to treasury. Reentrancy lock blocks recursive pool calls during the
  flash window.
- **Chainlink oracle.** `ChainlinkPriceOracle` implements `IPriceOracle`
  via Chainlink `AggregatorV3Interface`. Per-asset staleness windows.
  Reverts on stale, zero, or negative answers.
- **Fuzz and invariant tests.** `LendingPoolFuzz.t.sol` exercises
  property tests (round-trip neutrality, HF math invariance under
  uniform price scaling, debt monotonicity) and a stateful invariant
  handler that exercises deposit/withdraw/borrow/repay/warp/priceshock
  random sequences.
- **LTV backtest.** Compares static-aggressive (80/85), static-
  conservative (60/65), and the dynamic recommender across 4 years of
  synthetic ETH price data with embedded crash events. Output in
  `backtest_results/REPORT.md`, plots in `backtest_results/*.html`.

---

## References to read before extending

- **Compound V2 source** (`Comptroller.sol`, `CToken.sol`) — cleanest
  reference architecture
- **Aave V2 whitepaper** — for the rationale behind risk parameters
- **Liquity** — alternative liquidation design (stability pool)
- **Damn Vulnerable DeFi** — work through 4–5 challenges before deploying
- **OpenZeppelin contracts** — `SafeERC20`, `ReentrancyGuard` (we hand-rolled
  minimal versions; swap in OZ's when you add proper deps)
