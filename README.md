# BTC Scalp Dashboard v2

Real-time Bitcoin scalping decision support tool with order-flow-first architecture, WebSocket streaming, and research-aligned confluence scoring for day trading on the 1-minute and 5-minute timeframes.

**This is a decision support tool, not an auto-trader.** It shows signals — you execute manually.

## What's new in v2

- **Order flow first** — CVD (Cumulative Volume Delta), LOB imbalance, tape reading, and absorption detection via live WebSocket streams
- **Real-time WebSocket data** — trades, order book depth (100ms), and kline streams instead of polling
- **Rebalanced confluence weights** — Order Flow 45%, Technical 20%, Derivatives 15%, Macro 15%, Sentiment 5%
- **Order flow gate** — entry requires at least 1 order flow confirmation (no more purely technical signals)
- **ATR-based dynamic exits** — stop-loss at 1.5x ATR, take-profit at 2R/3R, with multi-reason exit logic
- **Volume Profile** — POC, HVN, and LVN levels calculated and displayed on chart
- **Transaction cost awareness** — signals are filtered against fee + slippage to avoid unprofitable trades
- **Backtester** — historical simulation with trade-by-trade CSV output and summary report
- **Environment config** — `.env` file for exchange selection, fees, and server settings

## Features

### Data Sources (Real-Time via WebSocket)
- **Trade stream** — tick-by-tick trades for CVD calculation and tape reading
- **Order book depth** — 20-level LOB snapshots every 100ms for bid/ask imbalance
- **Kline streams** — 1m and 5m candle updates for technical indicators
- **Fear & Greed Index** — Alternative.me (current + 24h trend)
- **Funding Rate** — estimated from price momentum (or direct API if futures available)
- **Macro Context** — DXY, S&P 500 futures, US 10Y yield (Yahoo Finance)

### Order Flow Analysis
- **CVD (Cumulative Volume Delta)** — tracks buying vs selling pressure with trend detection and divergence alerts
- **LOB Imbalance** — bid/ask volume ratio from 20-level order book depth
- **Tape Reading** — classifies trade flow aggression (buyers dominating, sellers dominating, or balanced)
- **Absorption Detection** — identifies large resting orders absorbing aggressive flow

### Confluence Scoring Engine
Weighted score from 0-100 combining:
- **Order Flow: 45%** — CVD trend, LOB imbalance, tape aggression, absorption, CVD divergence
- **Technical: 20%** — RSI, EMA ribbon, MACD, Bollinger Bands, ADX
- **Derivatives: 15%** — funding rate, open interest, liquidations
- **Sentiment: 5%** — Fear & Greed index
- **Macro: 15%** — DXY, S&P 500, 10Y yield alignment

Signal thresholds:
- **≥ 75** → High Confidence (audio alert)
- **60-74** → Medium Confidence (visual only)
- **< 60** → No signal

### Entry & Exit Logic
- **Order flow gate** — at least 1 order flow signal must confirm the direction before entry
- **ATR-based stops** — stop-loss at 1.5x ATR from entry
- **Dynamic targets** — primary TP at 2R, secondary TP at 3R
- **Exit signals** — position is closed when ≥2 exit reasons trigger (CVD flip, LOB reversal, absorption against, BB touch, RSI extreme, max hold time)
- **Market filter** — signals suppressed in "Choppy" conditions (low ADX + tight BB)
- **Transaction cost filter** — trades skipped when expected move (ATR) is below round-trip cost

### Dashboard
- Dark-themed UI optimized for fast scanning
- Live price chart with switchable 1m/5m timeframes
- Order flow panel with CVD, LOB ratio, tape aggression, and absorption status
- WebSocket stream status indicators (green/red per stream)
- Active position banner with real-time P&L tracking
- Exit signal cards showing triggered exit reasons
- POC/HVN/LVN chart annotations from volume profile
- Confluence score with 5-category visual breakdown
- Audio alerts for High Confidence signals
- Full signal log with timestamps
- Settings modal with live weight validation and transaction cost fields

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy config (edit .env to set exchange, fees, etc.)
cp .env.example .env

# Run the server
python server.py

# Open in browser
# http://localhost:5000
```

See [INSTALL_GUIDE.md](INSTALL_GUIDE.md) for detailed Windows setup instructions.

## Backtester

```bash
# Run with defaults (3 days, binance.us)
python backtest.py

# Customize
python backtest.py --exchange binance.com --days 5 --fee 0.0006
```

Outputs `backtest_results.csv` (trade log) and `backtest_results.md` (summary).

> Order flow is simulated from candle data in backtesting. Real-time performance with actual WebSocket order flow will differ.

## Configuration

### Environment variables (`.env` file)

| Variable | Default | Description |
|---|---|---|
| `EXCHANGE` | `binance.us` | `binance.com` or `binance.us` |
| `SYMBOL` | `btcusdt` | Trading pair |
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `5000` | Server port |
| `FEE_RATE` | `0.001` | Taker fee (0.1%) |
| `SLIPPAGE_RATE` | `0.0005` | Estimated slippage (0.05%) |

### Settings panel (gear icon in UI)

Adjustable at runtime via WebSocket:
- Confluence weights (must sum to 100%)
- RSI period and overbought/oversold levels
- EMA periods
- Signal confidence thresholds
- Risk-to-reward targets (primary 2R, secondary 3R)
- Transaction cost parameters

## Architecture

```
btc-scalp-dashboard/
├── server.py              # FastAPI backend — WS streams, order flow, TA, scoring
├── backtest.py            # Historical backtester with trade-by-trade output
├── requirements.txt       # Python dependencies
├── .env.example           # Environment config template
├── .gitignore
├── INSTALL_GUIDE.md       # Step-by-step Windows setup
├── README.md
└── static/
    ├── index.html         # Dashboard HTML
    ├── css/style.css      # Dark theme styles
    └── js/app.js          # Frontend logic, chart rendering, WebSocket client
```

### Backend (`server.py`)
- **FastAPI + Uvicorn** with WebSocket broadcasting to connected dashboards
- **Binance WebSocket streams** — trade, depth20@100ms, kline_1m, kline_5m
- **Order flow engine** — CVD accumulation, LOB imbalance, tape classification, absorption detection
- **Technical analysis** — EMA, RSI, MACD, Bollinger Bands, VWAP, ADX, ATR, Fibonacci, Volume Profile (POC/HVN/LVN)
- **Confluence scoring** — 5-category weighted system with order flow gate
- **Dynamic exits** — multi-reason exit logic with ATR-based stop/TP levels
- **REST data loop** (15s) for macro data, **analysis loop** (2s) for signal generation

### Frontend (`static/`)
- Vanilla HTML/CSS/JS (no build step)
- Chart.js with annotation plugin for levels and volume profile
- WebSocket client with auto-reconnect
- Audio alerts via Web Audio API
- Real-time order flow visualization

## Data Source Notes

- Uses **public Binance endpoints only** — no API keys required
- Default is `binance.us` (works worldwide). Set `EXCHANGE=binance.com` in `.env` for binance.com data
- **Fear & Greed Index** from Alternative.me free API — updates every ~12 hours
- **Macro data** from Yahoo Finance public endpoints
- **Funding rate** is estimated from price momentum when direct futures API is unavailable

## Disclaimer

This tool is for educational and informational purposes only. It is not financial advice. Trading cryptocurrencies involves substantial risk. Always do your own research and never trade with money you cannot afford to lose.
