# Project Overview — Swing Trader

## Mission Statement

A personal, automated swing-trading application that executes a disciplined
**3-to-5-day hold strategy** on a tight list of high-quality assets. The system
scans for high-probability setups using combined technical indicators and manages
risk strictly. A human approval gate sits in front of every trade entry.

---

## Current Phase

**Phase 3 — Paper Trading Automation (in progress)**

Phases 1 (data pipeline), 2 (indicators, signals, backtesting), and Pre-Phase 3
(symbol expansion, data cache, OOS validation tooling) are complete. The system
has a validated strategy edge (Sharpe 1.056, 31 trades, 76% win rate on 2022–2024).
Phase 3 code is complete: database, Telegram notifications, Alpaca order placement,
and the main scheduler loop are all implemented and tested (166 unit tests passing).
The system is now in the paper trading observation period.

---

## Asset Universe

| Symbol | Type          | Rationale                                                   |
|--------|---------------|-------------------------------------------------------------|
| NVDA   | Single stock  | High-volatility, high-liquidity. Strong swing candidate.    |
| ASML   | Single stock  | Semiconductor equipment. Lower correlation to NVDA.         |
| VOO    | ETF           | S&P 500 tracker. Lower volatility, steady uptrends.         |
| QQQM   | ETF           | Nasdaq-100. Tech-heavy, moderate volatility.                |
| MSFT   | Single stock  | Large-cap tech, strong trend structure, high liquidity.     |
| AAPL   | Single stock  | Highest US market liquidity, reliable swing behavior.       |
| AMD    | Single stock  | High-beta semiconductor, similar profile to NVDA.           |
| TSM    | Single stock  | Semiconductor equipment, non-US, lower NVDA correlation.    |

Eight symbols. META removed (negative P&L, stop-loss hits — MACD crossovers
were false signals in choppy conditions). SPY removed (redundant with VOO,
nearly identical signal timing). Target: ~10 signals/year.

---

## Trading Strategy

### Hold Period
3 to 5 calendar days. Positions are force-closed at end of day 5 regardless
of P&L. Discipline over hope.

### Buy Signal — Three-Condition AND Gate

All three conditions must be true simultaneously on the same daily bar:

1. **Trend Filter** — Close > EMA(50). The asset is in a structural uptrend.
   Only buy dips in uptrending assets; never buy falling knives.

2. **Pullback** — 40 ≤ RSI(14) < 55. A mild, controlled dip within the
   uptrend. RSI < 40 suggests a deeper problem; RSI ≥ 55 means no real
   pullback has occurred.

3. **MACD Bullish Crossover** — MACD line crossed above signal line on this
   specific bar (was at or below on the prior bar). Confirms momentum is
   turning back up. Not just "MACD is positive."

> **Why this replaced the original four-condition gate:**
> Backtesting 2022–2024 on all four symbols revealed that the original
> conditions (RSI < 45, price within 2% of EMA_21, MACD crossover, volume
> ≥ 1.5×) produced zero signals. Two structural conflicts: (a) when RSI
> drops below 45, price has already moved more than 2% below EMA_21; (b)
> oversold bounces occur on below-average volume, not spikes. The three-
> condition gate produced 17 signals with a 76% win rate and 5.93 profit
> factor across the same period. Crucially, the EMA_50 trend filter
> blocked all signals during the 2022 bear market automatically.

### Risk Management

- **Stop-loss**: ATR(14)-based. Hard stop = entry − (1.5 × ATR). Adapts to
  each asset's actual volatility. Never a flat percentage.
- **Take-profit**: entry + (2 × ATR). R:R ≥ 1.33:1. Set closer than before
  so it is achievable within the 5-day hold window.
- **Trailing stop**: Ratchets up as price rises (never moves down). Activates
  after price moves 0.5× ATR in the trade's favor. Tighter activation
  locks in profit earlier.
- **Force-close**: Day 5 EOD, regardless of P&L.

### Exit Priority Order
1. Hard stop hit → immediate market sell, no human gate
2. Trailing stop hit → immediate market sell, no human gate
3. Take-profit hit → limit sell at TP price
4. Day 5 EOD → market close

Stops execute without human confirmation by design. Emotion is removed from
the exit.

---

## Human-in-the-Loop Gate

Every **entry** requires explicit human approval via Telegram before the order
is placed. The bot sends an alert with full signal context and waits for a
`YES` or `NO` reply. No auto-timeout execution in Phase 3.

---

## Phase 4 Live-Capital Gate Criteria (non-negotiable)

Before switching from paper to live trading:
- Minimum 20 completed paper trades
- Maximum drawdown < 15%
- Zero unhandled crashes over a 2-week observation period
- Win rate > 60% (backtest showed 76%; anything below 60% in paper trading
  suggests live conditions differ materially from the backtest)

Note: Sharpe > 0.8 gate removed — OOS 2025 validation showed the edge is
regime-dependent (profitable in trending years, flat in choppy/declining years).
Operational reliability is the Phase 3 gate; alpha generation is not re-tested here.

---

## Out of Scope (deliberately)

- Crypto trading
- Options or leveraged instruments
- Intraday / scalping strategies
- Multi-account management
- Portfolio rebalancing
