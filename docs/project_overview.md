# Project Overview — Swing Trader

## Mission Statement

A personal, automated portfolio application that holds a fixed basket of
high-quality assets and manages **exposure**, not stock selection. Each sleeve
is in the market only while it is above its own 200-day average; otherwise its
capital sits in cash. A human approval gate sits in front of every increase in
exposure. Reductions are automatic.

The goal is a smoother equity curve, not market outperformance. See
`docs/strategy_validation.md` for what that claim rests on.

---

## Current Phase

**Phase 3 — Paper Trading (restarted 2026-08-01 with the regime overlay)**

The original three-condition signal strategy was retired after testing showed it
had no measurable edge: CAGR −1.12%, Sharpe −0.08, and a permutation test on its
trade P&Ls returning p = 0.6144 — indistinguishable from coin flips. That result
independently reproduced the project's own OOS-2025 finding (p = 0.499).

It was replaced by an SMA-200 regime overlay after a search across roughly 200
configurations spanning technical rules, cross-sectional momentum, sector
rotation, options (on real Alpaca option bars), volatility targeting, and
LLM-based signals. Nothing in that search reliably improved risk-adjusted
return. Exposure management reliably reduced drawdown, so that is what the
system now does.

The paper-trading observation period restarts from zero trades under the new
strategy — the prior sample was collected under a different strategy and does
not carry over.

---

## Asset Universe

| Symbol | Type          | Rationale                                                   |
|--------|---------------|-------------------------------------------------------------|
| NVDA   | Single stock  | High-volatility, high-liquidity.                            |
| ASML   | Single stock  | Semiconductor equipment. Lower correlation to NVDA.         |
| VOO    | ETF           | S&P 500 tracker. Lower volatility, steady uptrends.         |
| QQQM   | ETF           | Nasdaq-100. Tech-heavy, moderate volatility.                |
| MSFT   | Single stock  | Large-cap tech, strong trend structure, high liquidity.     |
| AAPL   | Single stock  | Highest US market liquidity.                                |
| AMD    | Single stock  | High-beta semiconductor, similar profile to NVDA.           |
| TSM    | Single stock  | Semiconductor, non-US, lower NVDA correlation.              |

Eight sleeves, equal weight at 1/8 of equity each.

> **Known bias:** these symbols were chosen partly because they had already
> performed well. Testing showed the same strategies scoring Sharpe 1.19 here and
> 0.17 on a universe not selected for past performance. Do not read backtest
> figures on this universe as forward expectations.

---

## Trading Strategy — SMA-200 Regime Overlay

### Hold period

Indefinite. A sleeve stays invested until its own regime turns off. There is no
day-5 force close, no take-profit, and no per-trade stop-loss.

### Regime rule — per sleeve, hysteresis band

Evaluated weekly on the most recent **completed** daily bar:

| current state | condition                    | new state |
|---------------|------------------------------|-----------|
| held          | close < 0.98 × SMA_200       | exit      |
| flat          | close > 1.02 × SMA_200       | enter     |
| either        | anything between those lines | unchanged |

The band is hysteresis, not a threshold. The same price produces a different
decision depending on whether the sleeve is currently held, which is what keeps
turnover near 7×/yr instead of ~20×/yr when price oscillates around the average.

### Weighting

Equal weight across the full configured universe — 1/8 each. A sleeve that is
off, or that could not be evaluated, leaves its capital **in cash**. Weight is
never redistributed to the remaining sleeves: this strategy reduces risk by
holding less, never by holding fewer things more heavily.

### Risk management

The regime band *is* the risk control. There are no ATR stops, no take-profit
levels, and no trailing stops — those belonged to the retired strategy. The
remaining guardrail is `MAX_POSITION_PCT`, which caps any single sleeve.

### Rebalance cadence

Weekly, after Friday's close. Orders queue to Monday's open, which reproduces
the one-bar execution lag the strategy was validated under. Weekly scored Sharpe
1.28 against daily's 1.33 at a third of the turnover, which is why there is no
intraday scanner.

---

## Human-in-the-Loop Gate

One Telegram message per week carries the full plan: every sleeve's close, its
200-day average, the gap between them, and the resulting orders.

- **Increases in exposure** require an explicit `YES`. A `NO` or a timeout skips
  them.
- **Reductions execute automatically**, without approval, and without depending
  on Telegram being reachable at all.

De-risking must never wait for a human. Risk-taking always must.

---

## Expected Performance — and its limits

Validated over 2018-11 → 2026-07 on the eight-symbol universe:

|                | overlay | buy & hold |
|----------------|---------|------------|
| CAGR           | 27.9%   | 39.1%      |
| Sharpe         | 1.24    | 1.15       |
| Max drawdown   | −25.6%  | −50.0%     |
| MAR            | 1.09    | 0.78       |

**Read this as: roughly two-thirds of the return for half the drawdown.** The
overlay showed *no* out-of-sample Sharpe advantage (test half 1.56 vs 1.57) and
beat buy & hold in only 2 of 9 calendar years — 2018 and 2022, the down years.
The backtest window contains no 2000- or 2008-style bear market, so treat the
absolute figures as optimistic.

If the objective were maximum wealth rather than a tolerable drawdown, buy &
hold with no application at all would be the honest recommendation.

---

## Phase 4 Live-Capital Gate Criteria

Before switching from paper to live trading:
- Minimum 26 weekly rebalances executed (~6 months) with zero unhandled crashes
- Maximum drawdown < 15%
- Live regime transitions match what the backtest produces on the same bars

Win-rate and Sharpe gates are deliberately absent. This strategy does not claim
an edge, so re-testing for one would be measuring noise. The gate is operational
reliability and drawdown behaviour.

---

## Out of Scope (deliberately)

- Crypto trading
- Options or leveraged instruments — tested and rejected; every underlying cheap
  enough to trade at $1,000 has a bid-ask spread wider than the strategy's
  break-even. See `docs/strategy_validation.md`.
- LLM-generated trading signals — tested and rejected on the evidence.
- Intraday / scalping strategies
- Multi-account management
- Re-tuning the strategy toward higher returns. The drawdown reduction is the
  only effect that replicated; optimising against it would be fitting noise.
