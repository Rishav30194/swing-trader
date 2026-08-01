# Strategy Validation — SMA-200 Regime Overlay

Record of the research that replaced the three-condition signal gate with the
regime overlay, 2026-07-31 → 2026-08-01. Kept so nobody re-runs this search from
scratch, and so the limits of the claim stay attached to the claim.

---

## Why the previous strategy was replaced

The three-condition gate (close > EMA_50, RSI 40–55, MACD bullish crossover) has
no measurable edge.

| test | result |
|---|---|
| full window 2018-11→2026-07, original 8 symbols | CAGR −1.12%, Sharpe −0.08, PF 0.89 |
| broad 58-symbol universe | CAGR −4.61%, Sharpe −0.26 |
| permutation test on trade P&Ls (10,000 shuffles) | **p = 0.6144** |
| train (2018–22) / test (2023–26) Sharpe | −0.50 / +0.38 — last place in both |
| per-symbol | negative or ~zero on 5 of 8 |

This independently corroborates the project's own OOS-2025 finding (p = 0.499).

The fill convention was ruled out as the cause: same-bar-close fills give +0.13%
CAGR versus −1.12% at next-open. Flat either way.

Two live defects were also suppressing signals and are fixed by this rewrite:
`_BARS_LOOKBACK = 90` returned only 61–63 bars, leaving EMA_50 under-converged
(reading up to 1.7% high); and the scan always evaluated the in-progress intraday
bar, so a crossover confirmed at the close was never actionable.

## What was searched, and what failed

Roughly 200 configurations across six families. Everything below was tested with
signals confirmed at bar *t* close, executed at *t+1* open, 5 bps/side.

| family | outcome |
|---|---|
| technical rules (RSI-2, Bollinger, dip-buying, Donchian, MA crossover) | all underperform buy & hold on a universe not selected for past performance |
| cross-sectional / dual momentum | 10/36 parameter cells beat buy & hold; median config loses; edge concentrated in one 7-month stretch |
| sector rotation (11 SPDRs, 12 configs) | best Sharpe 0.80, all below simply holding VOO (0.85) |
| options (real Alpaca bars, 27 SPY cycles) | covered call won 89% of cycles and netted +$57 vs +$29,183 for holding the shares; edge sits inside the bid-ask spread |
| volatility targeting | full-window Sharpe 1.40, but a static control at matched exposure scored **higher** out-of-sample (1.62 vs 1.56) |
| LLM / agentic signals | rejected — see below |

**Survivorship bias was the dominant effect throughout.** The same strategy
scored Sharpe 1.19 on the original 8 symbols, 0.27 on laggards, and 0.17 on
sector ETFs. Any result measured only on the original universe is untrustworthy.

## The adopted design, and its validation

Per-symbol hysteresis band around the 200-day SMA; equal weight; weekly rebalance.

**Matched-exposure control** — the overlay runs ~74% invested, so the fair null is
a portfolio held at a constant 74%, not buy & hold. Figures below are the adopted
configuration (2% band, weekly, 5% drift tolerance), reproducible with
`python validate_oos.py`:

| window | design | CAGR | Sharpe | maxDD | MAR |
|---|---|---|---|---|---|
| full | buy & hold | 39.11% | 1.15 | −50.0% | 0.78 |
| | static 74% | 25.91% | 1.17 | −34.0% | 0.76 |
| | **overlay** | 29.95% | **1.26** | **−27.7%** | **1.08** |
| train | buy & hold | 23.04% | 0.83 | −50.0% | 0.46 |
| | static 65% | 16.23% | 0.83 | −30.6% | 0.53 |
| | **overlay** | 22.76% | **1.00** | **−27.7%** | **0.82** |
| test | buy & hold | 55.30% | 1.57 | −30.7% | 1.80 |
| | static 83% | 40.06% | **1.67** | −24.7% | 1.63 |
| | **overlay** | 38.45% | 1.57 | **−18.0%** | **2.13** |

The overlay beats the matched static control on **drawdown and MAR in all three
windows**. It has no Sharpe advantage in the test half (1.57 vs 1.57 vs 1.67) —
a known limit, not a regression.

Supporting checks:

- **Block bootstrap** — the overlay produced the shallower drawdown in 98.4%
  (full), 95.4% (train) and 99.8% (test) of resamples, and in 98.7%–100% across
  four different universes.
- **Parameter grid** — 12/12 band × cadence cells beat buy & hold on MAR
  (1.04–1.19 vs 0.82).
- **Cost sensitivity** — MAR holds at 100 bps/side, 20× the modelled cost.
- **Cadence** — weekly (Sharpe 1.28) is worth ~96% of daily (1.33) at a third of
  the turnover. This is why there is no intraday scanner.

## Costs, fees and tax

**Trading costs are modelled; tax is not.** The 5 bps/side charged in every
backtest above comfortably covers what you actually pay:

| cost | amount | modelled? |
|---|---|---|
| Alpaca commission (US stocks/ETFs) | $0 | n/a |
| SEC fee (sells only) | ~0.00278% of proceeds | yes, inside 5 bps |
| FINRA TAF (sells only) | $0.000166/share, capped $8.30 | yes, inside 5 bps |
| bid-ask spread on these names | ~1–3 bps | yes, inside 5 bps |
| **capital gains tax** | **see below** | **NO** |

**Every rebalance sell is a taxable event in a taxable account.** Buy-and-hold
realises nothing until you sell, so it defers tax indefinitely — a structural
advantage the pre-tax tables above do not show. Measured over 2018-11 → 2026-07
on a $100,000 base, tracking average cost basis per sleeve:

| DRIFT_TOLERANCE | orders | realised gains | of which short-term | est. tax¹ |
|---|---|---|---|---|
| 0.1% (original) | 1,727 | $435,740 | $197,746 | $98,978 |
| 1% | 321 | $408,973 | $149,162 | $86,704 |
| **5% (adopted)** | **124** | $426,109 | $110,294 | **$82,666** |
| regime-only | 97 | $269,195 | $9,813 | $42,047 |

¹ 32% short-term / 15% long-term. Your rates differ; state tax is extra.

**This is why `DRIFT_TOLERANCE` defaults to 5%.** Drift trades were 94% of all
orders at the original 0.1% setting and bought nothing: at 5% the backtest
returns a *higher* CAGR (29.95% vs 27.88%) and a *higher* Sharpe (1.26 vs 1.24)
while realising far less short-term gain. Fewer trades was strictly better on
every axis.

### After-tax terminal wealth — this account IS taxable

Confirmed 2026-08-01: this runs in a **general taxable brokerage account**, not
an IRA or Roth. So the numbers below are the ones that actually apply.

Both strategies simulated over 2018-11 → 2026-07 on a $100,000 base, paying tax
out of the portfolio at each year end and liquidating everything on the final
bar (32% short-term / 15% long-term):

| strategy | after annual tax | after final liquidation | vs buy & hold |
|---|---|---|---|
| overlay, drift 0.1% | $520,866 | $504,846 | −53.5% |
| overlay, drift 1% | $541,400 | $520,852 | −52.0% |
| **overlay, drift 5%** | **$610,462** | **$584,121** | **−46.2%** |
| overlay, regime-only | $603,485 | $578,810 | −46.7% |
| **buy & hold** | $1,260,282 | **$1,086,233** | — |

**Over this window the overlay ends roughly half of buy-and-hold's after-tax
wealth.**

**Tax is not the main cause.** Ignoring tax entirely the overlay finishes
$755,139 against buy-and-hold's $1,260,282 — already 40% behind. Tax widens the
gap from −40% to −46%. The overlay's problem in a bull market is that it gives
up return, not that it pays tax; the tax is a secondary 6-point penalty.

**What the overlay bought for that money** — 2022, the only real drawdown in the
window:

| | 2022 return | max drawdown |
|---|---|---|
| overlay | −17.28% | −20.43% |
| buy & hold | −35.18% | −42.74% |

It preserved **17.9 percentage points** of capital in the one bad year.

**The honest trade, stated plainly:** over 2018–2026 in a taxable account this
strategy cost roughly half the terminal wealth to turn a −43% drawdown into
−20%. Whether that is worth it depends entirely on whether the deeper drawdown
would have caused a real behavioural error — selling at the bottom — because a
buy-and-hold investor who capitulates in 2022 does far worse than either line
above. The window contains no 2000- or 2008-style bear, where the overlay's case
is strongest and this gap would narrow substantially.

## Capital allocation

Sizing uses the **strategy's** capital, never the account balance:

    strategy equity = market value of managed sleeves + strategy cash ledger

seeded from `TRADING_CAPITAL`. The paper account holds $100,000; without this the
app would deploy all of it — a 100× over-deployment against a $1,000 intent.
Profits compound (sleeves worth $1,100 → sizes off $1,100); money deposited into
the account but never allocated stays invisible. Account equity and cash act as
ceilings only.

## Limits of the claim — read before changing anything

1. **No out-of-sample Sharpe advantage.** Test half: overlay 1.56, static 1.63,
   buy & hold 1.57. A dead heat. The overlay is not a better *return* engine.
2. **Return is materially lower.** 30.0% vs 39.1% CAGR full-window; it beat
   buy & hold in only 2 of 9 calendar years (2018 and 2022 — the down years).
3. **MAR does not generalise in the test half.** Sectors 0.60 vs buy & hold 1.04;
   laggards 0.24 vs 0.47. The drawdown effect replicates everywhere; the
   risk-adjusted-return effect does not.
4. **The backtest window has no 2000- or 2008-style bear market.** Treat absolute
   figures as optimistic.
5. The original 8 symbols were partly chosen because they had already performed
   well. Their forward returns should not be assumed to match the backtest.

**The one defensible claim: this reduces drawdown, at the cost of proportional
return.** Nothing tested reliably improved risk-adjusted return. If the objective
were maximum wealth, buy & hold with no application would be the honest answer.

## Why LLM / agentic trading was not adopted

- *The Alpha Illusion* (arXiv 2605.16895): testing LLM agents beyond their
  knowledge cutoff drops total returns ~72% and Sharpe ~51%; realistic frictions
  cut one system's Sharpe from 0.43 to 0.22; 35 of 40 system × friction cells go
  unmodelled.
- A meta-review of 19 studies found 2/19 use time-consistent splits, 1/19 models
  transaction costs, 1/19 handles survivorship, 0/19 are reproducible. Those three
  gaps are exactly the checks that flipped every result in this document.
- Lopez-Lira & Tang find GPT-4 headline sentiment does predict returns, but that
  "strategy returns decline as LLM adoption rises."
- **Self-contamination:** any LLM-sentiment backtest run by an assistant whose
  training data covers the test period is contaminated by construction. A clean
  test needs post-cutoff news only.

Endorsed use of LLMs here: auditable information extraction, written portfolio
reviews, log and anomaly explanation — upstream of independent risk control and
execution, never as the decision authority.
