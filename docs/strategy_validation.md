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

**Matched-exposure control** — the overlay runs ~71% invested, so the fair null is
a portfolio held at a constant 71%, not buy & hold.

| window | design | CAGR | Sharpe | maxDD | MAR |
|---|---|---|---|---|---|
| full | buy & hold | 41.10% | 1.15 | −50.0% | 0.82 |
| | static 72% | 24.85% | 1.17 | −33.2% | 0.75 |
| | **overlay** | 29.95% | **1.26** | **−27.7%** | **1.08** |
| train | buy & hold | 26.32% | 0.83 | −50.0% | 0.53 |
| | static 62% | 14.89% | 0.82 | −29.1% | 0.51 |
| | **overlay** | 22.84% | **1.06** | **−25.6%** | **0.89** |
| test | buy & hold | 56.36% | 1.57 | −30.7% | 1.84 |
| | static 83% | 39.43% | **1.63** | −24.7% | 1.60 |
| | **overlay** | 36.35% | 1.56 | **−17.4%** | **2.10** |

Supporting checks:

- **Block bootstrap, 2,000 resamples per universe** — the overlay produced the
  shallower drawdown in 98.7% (original 8), 100% (broad 58), 99.2% (sectors 11),
  98.9% (laggards 8) of resamples.
- **Parameter grid** — 12/12 band × cadence cells beat buy & hold on MAR
  (1.04–1.19 vs 0.82).
- **Cost sensitivity** — MAR holds at 100 bps/side, 20× the modelled cost.
- **Cadence** — weekly (Sharpe 1.28) is worth ~96% of daily (1.33) at a third of
  the turnover. This is why there is no intraday scanner.

> The matched-exposure table above was measured at the original 0.1% drift
> setting. The adopted 5% setting improves it (CAGR 29.95%, Sharpe 1.26) while
> cutting orders from 1,727 to 124 — see the tax section.

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

**Tax makes the overlay's position versus buy-and-hold worse, not better.** The
drawdown benefit is bought with realised gains that buy-and-hold never incurs.
In a tax-advantaged account (IRA/Roth/401k) rebalancing is tax-free and this
entire section is moot — which materially changes where this strategy belongs.

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
