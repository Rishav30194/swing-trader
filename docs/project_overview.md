# Project Overview — Swing Trader

## Mission Statement

A personal, automated swing-trading application that executes a disciplined
**3-to-5-day hold strategy** on a tight list of high-quality assets. The system
scans for high-probability setups using combined technical indicators and manages
risk strictly. A human approval gate sits in front of every trade entry.

---

## Current Phase

**Phase 1 — Paper Trading (Simulation Mode)**

No real capital is at risk. The system uses Alpaca's paper trading sandbox with
fake money. Live deployment only happens after the system proves itself across
≥ 30 paper trades with acceptable risk-adjusted metrics.

---

## Asset Universe

| Symbol | Type          | Rationale                                      |
|--------|---------------|------------------------------------------------|
| NVDA   | Single stock  | High-volatility, high-liquidity. Best swing candidate. |
| ASML   | Single stock  | High-quality semiconductor equipment. Lower correlation to NVDA. |
| VOO    | ETF           | S&P 500 tracker. Lower volatility, trend-following use case. |
| QQQM   | ETF           | Nasdaq-100. Tech-heavy, moderate volatility.   |

Scanning is limited to these four symbols intentionally. Breadth is the enemy
of a disciplined first system.

---

## Trading Strategy

### Hold Period
3 to 5 calendar days. Positions are force-closed at end of day 5 regardless
of P&L. Discipline over hope.

### Buy Signal — Strict AND Gate
All four conditions must be true simultaneously on the same daily bar:

1. **RSI Cooldown** — RSI(14) < 45. Asset has pulled back; not overbought.
2. **EMA Proximity** — Price is within 2% of the 21-day EMA. Confirms trend
   alignment; rejects assets in free-fall.
3. **MACD Bullish Crossover** — MACD line crossed above signal line on this
   specific bar (was below on the prior bar). Not just "MACD is positive."
4. **Volume Spike** — Current volume ≥ 1.5× the 20-day average volume.
   Confirms institutional participation.

### Risk Management

- **Stop-loss**: ATR(14)-based. Hard stop = entry − (2 × ATR). Adapts to
  each asset's actual volatility. Never a flat percentage.
- **Take-profit**: entry + (3 × ATR). Ensures R:R ratio > 1.5:1.
- **Trailing stop**: Ratchets up as price rises (never moves down). Activates
  after price moves 1× ATR in the trade's favor.
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
`YES` or `NO` reply. No auto-timeout execution in Phase 1.

---

## Phase 4 Live-Capital Gate Criteria (non-negotiable)

Before switching from paper to live trading:
- Minimum 30 completed paper trades
- Sharpe ratio > 0.8
- Maximum drawdown < 15%
- Profitable win rate > 45% (R:R compensates for sub-50% win rates)

---

## Out of Scope (deliberately)

- Crypto trading
- Options or leveraged instruments
- Intraday / scalping strategies
- Multi-account management
- Portfolio rebalancing
