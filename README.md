# Swing Trader

An automated portfolio application that holds a basket of 8 stocks and sells any one of them that falls below its own 200-day average price, holding cash until it recovers. It rebalances once a week and asks for approval over Telegram before buying anything.

**Status: built, tested, and not running.** It is not deployed anywhere and holds no money.

---

## Read this before using it

**Buying these 8 stocks and leaving them alone beat this app, badly.**

Tested over 2018–2026, starting with $1,000, after taxes:

| | your $1,000 becomes |
|---|---|
| This app | **~$5,800** |
| Just buying the stocks and never touching them | **~$10,900** |

Year by year, the app won **2 years out of 9** — both of them bad years:

| year | this app | just holding |
|---|---|---|
| 2018 | −6.6% | −15.0% |
| 2019 | +43.0% | **+79.7%** |
| 2020 | +47.2% | **+80.4%** |
| 2021 | +46.4% | **+52.6%** |
| 2022 | −22.4% | −41.1% |
| 2023 | +46.9% | **+90.6%** |
| 2024 | +43.3% | **+60.3%** |
| 2025 | +26.0% | **+38.7%** |
| 2026 | +21.9% | **+28.6%** |

### The one thing it is good at

At the worst moment in those 8 years, $1,000 would have shown:

- **Just holding:** $500
- **This app:** $723

That is the entire trade. It spares you about **$220** of frightening-looking loss and costs you about **$5,000** of gains.

That trade only makes sense if a 50% drop would genuinely make you sell everything at the bottom. If you would hold through it, simple investing wins.

### Two caveats that cut both ways

- These 8 stocks were picked partly *because* they had already done well, so the "just holding" column is flattered too.
- This 8-year window contained only one bad year. In a 2008-style crash the app would look considerably better than it does here.

---

## How it works

**The rule, per stock, checked once a week on the most recent completed daily bar:**

| currently | condition | action |
|---|---|---|
| holding it | price drops below 0.98 × its 200-day average | sell it |
| not holding it | price rises above 1.02 × its 200-day average | buy it |
| either | price is between those two lines | do nothing |

The two thresholds are deliberately different. If both were exactly the 200-day average, a price hovering around it would trigger a buy and a sell every other week. The gap between them stops that.

**Money:** the 8 stocks get equal shares of whatever you allocate — 1/8 each. A stock that gets sold leaves its share sitting in cash; the money is never piled into the remaining stocks.

**Timing:** it runs after Friday's close. Orders queue and fill at Monday's open.

**Approval:** one Telegram message a week listing every stock, its price, its 200-day average, and what it plans to do. Buying needs you to reply `YES`. **Selling never asks** — protecting you is automatic and happens even if Telegram is down.

### The universe

NVDA, ASML, VOO, QQQM, MSFT, AAPL, AMD, TSM

---

## Capital

`TRADING_CAPITAL` sets how much the app is allowed to invest. It is **required** — the app refuses to start without it.

This is not your account balance. A brokerage account holding $100,000 with `TRADING_CAPITAL=1000` invests $1,000 and ignores the rest. Profits compound: once the holdings are worth $1,100, it works with $1,100.

The account is assumed to be used only by this app. Stocks you bought yourself are never sold and never counted, but the app warns about them each run. A short position stops the run entirely — this app only ever buys.

---

## Costs and tax

Alpaca charges no commission. The regulatory fees on sales are a fraction of a cent. Neither matters.

**Tax does.** In a normal (non-retirement) brokerage account, every sale creates a taxable gain. Buying and holding creates none until you finally sell.

`DRIFT_TOLERANCE` (default 5%) controls how much small-scale rebalancing the app does, and is really a tax setting. At 0.1% it placed 1,727 orders across the backtest; at 5% it places 124 — and returns slightly *more* money. Fewer trades was better on every measure.

In a retirement account (IRA/Roth) none of this applies and rebalancing is free.

---

## Setup

```bash
git clone https://github.com/Rishav30194/swing-trader.git
cd swing-trader
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create `.env` in the project root (never commit it):

```env
# Alpaca — paper trading
ALPACA_API_KEY=your_paper_key_here
ALPACA_API_SECRET=your_paper_secret_here
ALPACA_PAPER=true

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# How much the app may invest — REQUIRED, and not your account balance
TRADING_CAPITAL=1000

# Universe
SYMBOLS=NVDA,ASML,VOO,QQQM,MSFT,AAPL,AMD,TSM

# Strategy
SMA_BAND=0.02              # sell below 0.98×, buy above 1.02× the 200-day average
BARS_LOOKBACK_DAYS=365     # needs 200+ trading days of history

# Rebalancing
DRIFT_TOLERANCE=0.05       # skip small rebalancing trades; this is a tax setting
MIN_ORDER_NOTIONAL=1.0
REBALANCE_DAY=fri
REBALANCE_HOUR=16
REBALANCE_MINUTE=15
REPLY_TIMEOUT_SECS=14400   # 4h to reply; orders queue to Monday anyway
MAX_POSITION_PCT=0.25      # no single stock above 25%
```

Alpaca paper keys: [alpaca.markets](https://alpaca.markets). Telegram bot: [@BotFather](https://t.me/BotFather).

## Running it

```bash
python backtest.py --benchmark    # test the strategy against simply holding
python validate_oos.py            # stress-test it properly
python main.py                    # run it live (paper)
pytest tests/ -q                  # 249 tests
```

`main.py` rebalances every Friday at 16:15 ET and sends a status message every Saturday at 09:00 ET.

---

## Architecture

```
Alpaca (completed daily bars)
        ↓
indicators.py → portfolio.py          the strategy, pure functions
        ↓
Telegram approval                     buys need YES · sells never ask
        ↓
executor.py                           dollar-amount market orders
        ↓
SQLite + APScheduler                  state, audit log, weekly schedule
```

`backtest.py` calls the same `portfolio.py` functions `main.py` calls, so the tested strategy and the live strategy cannot drift apart.

```
src/
  config.py       loads .env into typed settings
  data.py         fetches bars; drops today's unfinished bar
  indicators.py   the 200-day average
  portfolio.py    THE STRATEGY — decide, size, generate orders
  database.py     SQLite: holdings state, cash ledger, audit log
  notifier.py     Telegram messages and reply handling
  executor.py     Alpaca orders and account state
main.py           weekly scheduler
backtest.py       strategy tester
validate_oos.py   stress tests
```

Detail: [`docs/architecture.md`](docs/architecture.md).

---

## Safety rules

Enforced in code, not by convention:

- No live orders while `ALPACA_PAPER=true`
- No order without validated targets; a stock marked "sell" gets a target of exactly zero
- **Sells run immediately and unconditionally**, including when Telegram is unreachable. Only buys wait for approval
- Position sizes come from allocated capital fetched fresh every run, never a cached or hardcoded figure
- One position per stock, each capped at `MAX_POSITION_PCT`
- A failed order is logged, alerted, and never assumed to have worked — holdings are re-read from Alpaca every run

## Going live

Set `ALPACA_PAPER=false`, swap in live API keys, restart. No code changes.

Orders are dollar-amount, so fractional shares are automatic — on $1,000 each stock gets $125, and ASML at ~$1,600/share resolves to about 0.08 shares.

---

## License

Personal use only. Not financial advice.
