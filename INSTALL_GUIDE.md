# BTC Scalp Dashboard v2 — Installation Guide (Windows)

## Step 1: Open Command Prompt

Press **Win + R**, type `cmd`, press **Enter**.

---

## Step 2: Verify Python is installed

Type this and press Enter:

```
python --version
```

You should see something like `Python 3.13.x` or `Python 3.14.x`.

If you see an error ("not recognized"), reinstall Python from https://www.python.org/downloads/ — make sure to check **"Add Python to PATH"** during installation.

---

## Step 3: Download the project

Type these commands one at a time, pressing Enter after each:

```
cd %USERPROFILE%\Desktop
git clone https://github.com/elendiaar/btc-scalp-dashboard.git
cd btc-scalp-dashboard
```

If you see `'git' is not recognized`, download and install Git from https://git-scm.com/download/win — then close and reopen Command Prompt and try again.

**Alternative (no Git):** Go to https://github.com/elendiaar/btc-scalp-dashboard, click the green **Code** button, then **Download ZIP**. Extract the ZIP to your Desktop. Then in Command Prompt:

```
cd %USERPROFILE%\Desktop\btc-scalp-dashboard-master
```

---

## Step 4: Install dependencies

Type this and press Enter:

```
pip install -r requirements.txt
```

Wait until it finishes. You should see "Successfully installed" messages for: fastapi, uvicorn, httpx, numpy, websockets, python-dotenv.

---

## Step 5: Configure the exchange

Copy the example config file:

```
copy .env.example .env
```

The default config uses **binance.com** which works in Romania (and most countries outside the US). No changes needed unless you want to adjust something.

**What you can configure** (open `.env` in Notepad to edit):

| Setting | Default | What it does |
|---|---|---|
| `EXCHANGE` | `binance.com` | Data source. Use `binance.us` if you are in the United States |
| `SYMBOL` | `btcusdt` | Trading pair |
| `PORT` | `5000` | Local server port |
| `FEE_RATE` | `0.001` | Taker fee rate (0.1%) — adjust to match your Binance fee tier |
| `SLIPPAGE_RATE` | `0.0005` | Estimated slippage (0.05%) |

> **Note:** No API keys are needed. The dashboard uses only public Binance endpoints.

---

## Step 6: Start the dashboard

Type this and press Enter:

```
python server.py
```

You should see output like:

```
[Startup] Fetching initial data via REST...
  Loaded 100 5m candles
  Loaded 100 1m candles
  BTC price: $XX,XXX.XX
[Startup] Starting WebSocket streams...
[Startup] All systems running.
INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)
```

The server fetches initial candle data via REST, then opens real-time WebSocket streams for trades, order book, and klines.

---

## Step 7: Open the dashboard

Open your web browser (Chrome, Edge, Firefox) and go to:

```
http://localhost:5000
```

The dashboard will load and start showing live BTC data. You should see:
- Live price updating in real time via WebSocket
- Order flow panel (CVD, LOB imbalance, tape reading)
- WebSocket stream status indicators (green = connected)
- Confluence score with 5-category breakdown
- Signal cards with entry/exit levels when conditions are met

---

## Stopping the dashboard

Go back to the Command Prompt window where the server is running and press **Ctrl + C**.

---

## Starting it again later

Open Command Prompt, then:

```
cd %USERPROFILE%\Desktop\btc-scalp-dashboard
python server.py
```

Then open http://localhost:5000 in your browser.

---

## Running the backtester

The backtester fetches 3 days of historical 1m candles and simulates the v2 strategy to give you a feel for how it performs. In the project folder, run:

```
python backtest.py
```

It takes about 30 seconds to download data and run. When done, it prints a summary and saves:
- `backtest_results.csv` — trade-by-trade log
- `backtest_results.md` — formatted summary report

**Backtest options:**

```
python backtest.py --days 5                        # Fetch 5 days instead of 3
python backtest.py --exchange binance.com           # Use binance.com data
python backtest.py --fee 0.0006 --slippage 0.0003   # Lower fees (VIP tier)
```

> **Important:** The backtester simulates order flow from candle data since historical tick-level data is not freely available. Real-time performance with actual WebSocket order flow will differ.

---

## Using the Settings panel

Click the **gear icon** (⚙) in the top-right corner of the dashboard to open Settings. You can adjust:

- **Confluence weights** — how much each category contributes to the score (must sum to 100%)
  - Order Flow: 45% (CVD, LOB imbalance, tape, absorption)
  - Technical: 20% (RSI, EMA, MACD, Bollinger)
  - Derivatives: 15% (funding rate, OI, liquidations)
  - Sentiment: 5% (Fear & Greed)
  - Macro: 15% (DXY, S&P 500, 10Y yield)
- **RSI parameters** — period, overbought/oversold levels
- **Signal thresholds** — minimum confluence for High/Medium signals
- **Risk:Reward** — primary (2R) and secondary (3R) targets
- **Transaction costs** — fee and slippage rates

Changes take effect immediately (sent to the backend via WebSocket).

---

## Troubleshooting

**"pip is not recognized"** — Try `python -m pip install -r requirements.txt` instead.

**"Port 5000 already in use"** — Another program is using that port. Either close it, or edit `.env` and change `PORT=5000` to `PORT=8080`, then open `http://localhost:8080` instead.

**WebSocket indicators show red** — The streams may take a few seconds to connect after startup. If they stay red, check your internet connection. The Command Prompt window will show error messages.

**Dashboard shows but no data** — Check that your internet connection works and that Binance is not blocked by your network.

**Windows Firewall popup** — Click "Allow access" when Windows asks about Python accessing the network.

**"ModuleNotFoundError: No module named 'xyz'"** — Run `pip install -r requirements.txt` again to make sure all dependencies are installed.

**Backtest shows 0 trades** — This can happen in very low-volatility periods where ATR is below the transaction cost threshold. Try fetching more days: `python backtest.py --days 7`.
