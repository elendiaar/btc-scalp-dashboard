# BTC Scalp Dashboard

Real-time Bitcoin scalping decision support tool that synthesizes multiple market signals into high-confidence entry and exit recommendations for day trading on the 1-minute and 5-minute timeframes.

**This is a decision support tool, not an auto-trader.** It shows signals — you execute manually.

## Features

### Data Sources (Real-Time)
- **Price & Candles** — Binance.us spot API (BTC/USDT), 15-second refresh
- **Order Book Depth** — Bid/ask delta, top-of-book levels
- **Technical Indicators** — VWAP, RSI(14), EMA ribbon (9/21/55/200), MACD, Bollinger Bands, Fibonacci retracement, ADX
- **Fear & Greed Index** — Alternative.me (current + 24h trend)
- **Funding Rate** — Estimated from CoinGlass/CoinGecko
- **Liquidation Estimates** — Derived from price volatility
- **Macro Context** — DXY, S&P 500 futures, US 10Y yield (Yahoo Finance)

### Confluence Scoring Engine
Weighted score from 0-100 combining:
- Technical analysis: **40%**
- Order flow & volume: **20%**
- On-chain signals: **15%**
- Sentiment & social: **15%**
- Macro alignment: **10%**

Signal thresholds:
- **≥ 75** → High Confidence (audio alert)
- **60-74** → Medium Confidence (visual only)
- **< 60** → No signal

### Entry & Exit Logic
- **Long entries:** Price at support + RSI/MACD confirmation + positive flow + no extreme greed
- **Short entries:** Inverse conditions
- **Targets:** 2:1 and 3:1 R:R ratios
- **Market filter:** Signals suppressed in "Choppy" conditions (low ADX + tight BB)

### Dashboard
- Dark-themed UI optimized for fast scanning
- Live price chart with switchable 1m/5m timeframes
- EMA ribbon, Bollinger Bands, VWAP, Fibonacci levels overlaid
- Real-time signal feed with breakdown by category
- Fear & Greed gauge, order flow delta, macro context
- Audio alerts for High Confidence signals
- Full signal log with timestamps
- Settings page for adjustable parameters

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
python server.py

# Open in browser
# http://localhost:5000
```

## Configuration

Open the Settings panel (gear icon) to adjust:
- Confluence weights (must sum to 1.0)
- RSI period and overbought/oversold thresholds
- EMA periods
- Signal confidence thresholds
- Risk-to-reward targets

Settings are sent to the backend via WebSocket and take effect immediately.

## Architecture

```
btc-scalp-dashboard/
├── server.py              # FastAPI backend — data fetching, TA, scoring, WebSocket
├── requirements.txt       # Python dependencies
├── static/
│   ├── index.html         # Dashboard HTML
│   ├── css/style.css      # Dark theme styles
│   └── js/app.js          # Frontend logic, chart rendering, WebSocket client
└── README.md
```

### Backend (`server.py`)
- **FastAPI + Uvicorn** with WebSocket broadcasting
- Two async loops: `data_loop` (15s, market data + TA), `macro_loop` (60s, DXY/S&P/10Y)
- All technical indicators computed in pure Python (EMA, RSI, MACD, Bollinger, VWAP, ADX, Fibonacci)
- Confluence scoring engine with configurable weights
- Signal generation with deduplication and market condition filtering

### Frontend (`static/`)
- Vanilla HTML/CSS/JS (no build step)
- Chart.js with annotation plugin for Fibonacci levels
- WebSocket client with auto-reconnect
- Audio alerts via Web Audio API

## Data Source Notes

- **Binance.com futures API** is geo-restricted in some regions. The app uses **Binance.us** (spot) as primary data source. When deploying to your own server in a non-restricted region, you can update `server.py` to use `fapi.binance.com` for futures-specific data (funding rate, open interest, liquidations).
- **Fear & Greed Index** from Alternative.me free API — updates every ~12 hours.
- **Macro data** from Yahoo Finance public endpoints.
- **Funding rate** is estimated from price momentum when direct futures API is unavailable.

## Disclaimer

This tool is for educational and informational purposes only. It is not financial advice. Trading cryptocurrencies involves substantial risk. Always do your own research and never trade with money you cannot afford to lose.
