"""
BTC Scalp Dashboard — FastAPI Backend (v2)
Order-flow-first architecture with WebSocket streaming, CVD, tape reading,
enhanced LOB imbalance, and research-aligned confluence scoring.
"""

import asyncio
import json
import os
import math
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx
import numpy as np
import uvicorn
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

app = FastAPI(title="BTC Scalp Dashboard v2")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EXCHANGE = os.getenv("EXCHANGE", "binance.us")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT").lower()
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
FEE_RATE = float(os.getenv("FEE_RATE", "0.001"))
SLIPPAGE_RATE = float(os.getenv("SLIPPAGE_RATE", "0.0005"))

if EXCHANGE == "binance.com":
    REST_BASE = "https://api.binance.com"
    WS_BASE = "wss://stream.binance.com:9443/ws"
else:
    REST_BASE = "https://api.binance.us"
    WS_BASE = "wss://stream.binance.us:9443/ws"

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
clients: list[WebSocket] = []

# Settings — defaults (adjustable via settings page)
settings: dict[str, Any] = {
    "confluence_weights": {
        "orderflow": 0.45,
        "technical": 0.20,
        "derivatives": 0.15,
        "sentiment": 0.05,
        "macro": 0.15,
    },
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "rsi_period": 14,
    "ema_periods": [9, 21, 55, 200],
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "bb_period": 20,
    "bb_std": 2,
    "atr_period": 14,
    "rr_primary": 2.0,
    "rr_secondary": 3.0,
    "min_confluence_high": 75,
    "min_confluence_medium": 60,
    "emergency_exit": 40,
    "fee_rate": FEE_RATE,
    "slippage_rate": SLIPPAGE_RATE,
}

# Market data store
market_data: dict[str, Any] = {
    "price": 0,
    "price_24h_ago": 0,
    "change_24h": 0,
    "change_24h_pct": 0,
    "high_24h": 0,
    "low_24h": 0,
    "volume_24h": 0,
    "candles_1m": [],
    "candles_5m": [],
    "orderbook": {"bids": [], "asks": [], "bid_volume": 0, "ask_volume": 0, "delta": 0},
    "fear_greed": {"value": 50, "classification": "Neutral", "timestamp": ""},
    "funding_rate": 0,
    "open_interest": 0,
    "oi_change_pct": 0,
    "macro": {"dxy": 0, "dxy_change": 0, "sp500": 0, "sp500_change": 0, "us10y": 0, "us10y_change": 0},
    "liquidations": {"long": 0, "short": 0},
}

# Order flow data (from WebSocket streams)
orderflow_data: dict[str, Any] = {
    # CVD
    "cvd_value": 0.0,
    "cvd_trend": "flat",
    "cvd_divergence": "none",
    "cvd_history": [],        # last 60 values (one per second)
    # LOB imbalance
    "lob_ratio": 1.0,
    "lob_imbalance": 0.0,     # -1 to +1
    "lob_imbalance_ema": 0.0,
    "best_bid": 0,
    "best_ask": 0,
    "spread": 0,
    "spread_pct": 0,
    "spread_avg": 0,
    # Tape / absorption
    "tape_aggression": "balanced",
    "absorption_signal": "none",
    "large_trade_bias": "neutral",
    "recent_buy_volume": 0,
    "recent_sell_volume": 0,
}

# Internal buffers
_trade_buffer: deque = deque(maxlen=2000)
_cvd_raw: float = 0.0
_depth_spreads: deque = deque(maxlen=200)
_lob_imbalances: deque = deque(maxlen=100)
_depth_updates_count: int = 0
_ws_connected: dict[str, bool] = {"trades": False, "depth": False, "kline_1m": False, "kline_5m": False}

# Technical analysis results
ta_data: dict[str, Any] = {
    "emas": {},
    "rsi": 50,
    "prev_rsi": 50,
    "macd": {"macd": 0, "signal": 0, "histogram": 0, "crossover": "none"},
    "vwap": 0,
    "vwap_upper": 0,
    "vwap_lower": 0,
    "bb": {"upper": 0, "middle": 0, "lower": 0, "squeeze": False, "width": 0},
    "fib_levels": {},
    "atr": 0,
    "atr_pct": 0,
    "adx": 25,
    "market_condition": "Ranging",
    "volume_profile": [],
    "poc_price": 0,
    "hvn_levels": [],
    "lvn_levels": [],
}

# Signals
signals_log: list[dict] = []
active_signal: dict | None = None

# ---------------------------------------------------------------------------
# Technical Analysis Functions (pure Python + numpy)
# ---------------------------------------------------------------------------

def calc_ema(data: list[float], period: int) -> list[float]:
    if len(data) < period:
        return [data[-1]] * len(data) if data else [0]
    ema = [sum(data[:period]) / period]
    k = 2 / (period + 1)
    for val in data[period:]:
        ema.append(val * k + ema[-1] * (1 - k))
    return ema


def calc_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(closes: list[float], fast: int = 12, slow: int = 26, sig: int = 9) -> dict:
    if len(closes) < slow + sig:
        return {"macd": 0, "signal": 0, "histogram": 0, "crossover": "none"}
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-(min_len - i)] - ema_slow[-(min_len - i)] for i in range(min_len)]
    if len(macd_line) < sig:
        return {"macd": macd_line[-1] if macd_line else 0, "signal": 0, "histogram": 0, "crossover": "none"}
    signal_line = calc_ema(macd_line, sig)
    histogram = macd_line[-1] - signal_line[-1]
    crossover = "none"
    if len(signal_line) >= 2 and len(macd_line) >= 2:
        prev_diff = macd_line[-2] - signal_line[-2]
        curr_diff = macd_line[-1] - signal_line[-1]
        if prev_diff <= 0 < curr_diff:
            crossover = "bullish"
        elif prev_diff >= 0 > curr_diff:
            crossover = "bearish"
    return {"macd": round(macd_line[-1], 2), "signal": round(signal_line[-1], 2),
            "histogram": round(histogram, 2), "crossover": crossover}


def calc_bollinger(closes: list[float], period: int = 20, std_dev: float = 2.0) -> dict:
    if len(closes) < period:
        p = closes[-1] if closes else 0
        return {"upper": p, "middle": p, "lower": p, "squeeze": False, "width": 0}
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    width = (upper - lower) / middle * 100 if middle else 0
    squeeze = width < 3.0
    return {"upper": round(upper, 2), "middle": round(middle, 2),
            "lower": round(lower, 2), "squeeze": squeeze, "width": round(width, 4)}


def calc_vwap(candles: list[dict]) -> tuple[float, float, float]:
    if not candles:
        return 0, 0, 0
    cum_tp_vol = 0
    cum_vol = 0
    tp_list = []
    for c in candles:
        tp = (c["h"] + c["l"] + c["c"]) / 3
        vol = c["v"]
        cum_tp_vol += tp * vol
        cum_vol += vol
        tp_list.append(tp)
    if cum_vol == 0:
        return 0, 0, 0
    vwap = cum_tp_vol / cum_vol
    variance = sum((tp - vwap) ** 2 for tp in tp_list) / len(tp_list)
    std = math.sqrt(variance)
    return round(vwap, 2), round(vwap + std, 2), round(vwap - std, 2)


def calc_atr(candles: list[dict], period: int = 14) -> float:
    """True ATR calculation."""
    if len(candles) < period + 1:
        if len(candles) >= 2:
            return candles[-1]["h"] - candles[-1]["l"]
        return 0
    tr_list = []
    for i in range(1, len(candles)):
        h, l, c_prev = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)
    if len(tr_list) < period:
        return sum(tr_list) / len(tr_list) if tr_list else 0
    atr = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
    return round(atr, 2)


def calc_adx(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period * 2:
        return 25.0
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        h, l, c_prev = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        tr_list.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
        up = h - candles[i - 1]["h"]
        down = candles[i - 1]["l"] - l
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    if len(tr_list) < period:
        return 25.0
    atr = sum(tr_list[:period]) / period
    plus_di_val = sum(plus_dm[:period]) / period
    minus_di_val = sum(minus_dm[:period]) / period
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        plus_di_val = (plus_di_val * (period - 1) + plus_dm[i]) / period
        minus_di_val = (minus_di_val * (period - 1) + minus_dm[i]) / period
    if atr == 0:
        return 0
    plus_di = 100 * plus_di_val / atr
    minus_di = 100 * minus_di_val / atr
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return 0
    return round(100 * abs(plus_di - minus_di) / di_sum, 1)


def find_swing_levels(candles: list[dict]) -> dict:
    if len(candles) < 20:
        p = candles[-1]["c"] if candles else 0
        return {"swing_high": p, "swing_low": p, "levels": {}}
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    swing_high = max(highs[-50:]) if len(highs) >= 50 else max(highs)
    swing_low = min(lows[-50:]) if len(lows) >= 50 else min(lows)
    diff = swing_high - swing_low
    levels = {
        "0.0": round(swing_high, 2),
        "0.236": round(swing_high - 0.236 * diff, 2),
        "0.382": round(swing_high - 0.382 * diff, 2),
        "0.5": round(swing_high - 0.5 * diff, 2),
        "0.618": round(swing_high - 0.618 * diff, 2),
        "0.786": round(swing_high - 0.786 * diff, 2),
        "1.0": round(swing_low, 2),
    }
    return {"swing_high": swing_high, "swing_low": swing_low, "levels": levels}


def calc_volume_profile(candles: list[dict], bins: int = 20) -> dict:
    """Enhanced volume profile with POC, HVN, LVN detection."""
    if not candles:
        return {"profile": [], "poc_price": 0, "hvn_levels": [], "lvn_levels": []}
    prices = [c["c"] for c in candles]
    volumes = [c["v"] for c in candles]
    min_p, max_p = min(prices), max(prices)
    if max_p == min_p:
        return {"profile": [{"price": min_p, "volume": sum(volumes)}],
                "poc_price": min_p, "hvn_levels": [], "lvn_levels": []}
    step = (max_p - min_p) / bins
    profile = []
    for i in range(bins):
        low = min_p + i * step
        high = low + step
        vol = sum(v for p, v in zip(prices, volumes) if low <= p < high)
        profile.append({"price": round(low + step / 2, 2), "volume": round(vol, 2)})

    # POC = highest volume level
    vols = [p["volume"] for p in profile]
    avg_vol = sum(vols) / len(vols) if vols else 0
    poc_idx = vols.index(max(vols)) if vols else 0
    poc_price = profile[poc_idx]["price"] if profile else 0

    # HVN = volume > 1.5x average, LVN = volume < 0.5x average
    hvn = [p["price"] for p in profile if p["volume"] > avg_vol * 1.5]
    lvn = [p["price"] for p in profile if 0 < p["volume"] < avg_vol * 0.5]

    return {"profile": profile, "poc_price": poc_price, "hvn_levels": hvn, "lvn_levels": lvn}


def determine_market_condition(adx: float, bb_width: float, atr_pct: float) -> str:
    if adx > 25 and bb_width > 4:
        return "Trending"
    elif adx < 20 and bb_width < 2.5:
        return "Choppy"
    else:
        return "Ranging"


# ---------------------------------------------------------------------------
# Order Flow Processing
# ---------------------------------------------------------------------------

def process_trade(trade: dict):
    """Process a single trade from the WebSocket stream."""
    global _cvd_raw
    price = float(trade.get("p", 0))
    qty = float(trade.get("q", 0))
    is_buyer_maker = trade.get("m", False)  # True = seller aggressor

    if is_buyer_maker:
        side = "sell"
        _cvd_raw -= qty
    else:
        side = "buy"
        _cvd_raw += qty

    _trade_buffer.append({
        "price": price,
        "qty": qty,
        "side": side,
        "time": trade.get("T", int(time.time() * 1000)),
    })

    if price > 0:
        market_data["price"] = price


def compute_orderflow_signals():
    """Compute all order flow signals from buffered trades and depth data."""
    global _cvd_raw

    trades = list(_trade_buffer)
    if not trades:
        return

    # --- CVD ---
    orderflow_data["cvd_value"] = round(_cvd_raw, 4)

    # CVD trend from recent history
    cvd_hist = orderflow_data["cvd_history"]
    cvd_hist.append(_cvd_raw)
    if len(cvd_hist) > 120:
        cvd_hist[:] = cvd_hist[-120:]
    orderflow_data["cvd_history"] = cvd_hist

    if len(cvd_hist) >= 10:
        recent = cvd_hist[-10:]
        if recent[-1] > recent[0] * 1.001:
            orderflow_data["cvd_trend"] = "rising"
        elif recent[-1] < recent[0] * 0.999:
            orderflow_data["cvd_trend"] = "falling"
        else:
            orderflow_data["cvd_trend"] = "flat"

    # CVD divergence detection (compare with price)
    orderflow_data["cvd_divergence"] = "none"
    candles = market_data.get("candles_1m", [])
    if len(candles) >= 20 and len(cvd_hist) >= 20:
        # Look for divergence over last 20 data points
        price_recent = [c["c"] for c in candles[-20:]]
        cvd_recent = cvd_hist[-20:]
        price_min_idx = price_recent.index(min(price_recent))
        price_max_idx = price_recent.index(max(price_recent))

        # Bullish: price lower low but CVD higher low
        if price_min_idx > len(price_recent) // 2:  # recent low
            first_half_min_p = min(price_recent[:len(price_recent) // 2])
            second_half_min_p = min(price_recent[len(price_recent) // 2:])
            first_half_min_cvd = min(cvd_recent[:len(cvd_recent) // 2])
            second_half_min_cvd = min(cvd_recent[len(cvd_recent) // 2:])
            if second_half_min_p < first_half_min_p and second_half_min_cvd > first_half_min_cvd:
                orderflow_data["cvd_divergence"] = "bullish"

        # Bearish: price higher high but CVD lower high
        if price_max_idx > len(price_recent) // 2:
            first_half_max_p = max(price_recent[:len(price_recent) // 2])
            second_half_max_p = max(price_recent[len(price_recent) // 2:])
            first_half_max_cvd = max(cvd_recent[:len(cvd_recent) // 2])
            second_half_max_cvd = max(cvd_recent[len(cvd_recent) // 2:])
            if second_half_max_p > first_half_max_p and second_half_max_cvd < first_half_max_cvd:
                orderflow_data["cvd_divergence"] = "bearish"

    # --- Tape Aggression & Absorption ---
    recent_trades = trades[-500:] if len(trades) >= 500 else trades
    if recent_trades:
        avg_size = sum(t["qty"] for t in recent_trades) / len(recent_trades)
        recent_window = recent_trades[-100:] if len(recent_trades) >= 100 else recent_trades

        buy_vol = sum(t["qty"] for t in recent_window if t["side"] == "buy")
        sell_vol = sum(t["qty"] for t in recent_window if t["side"] == "sell")
        total_vol = buy_vol + sell_vol

        orderflow_data["recent_buy_volume"] = round(buy_vol, 4)
        orderflow_data["recent_sell_volume"] = round(sell_vol, 4)

        if total_vol > 0:
            buy_pct = buy_vol / total_vol
            if buy_pct > 0.6:
                orderflow_data["tape_aggression"] = "buyers"
            elif buy_pct < 0.4:
                orderflow_data["tape_aggression"] = "sellers"
            else:
                orderflow_data["tape_aggression"] = "balanced"

        # Large trade bias
        large_trades = [t for t in recent_window if t["qty"] > avg_size * 2]
        if large_trades:
            large_buy = sum(t["qty"] for t in large_trades if t["side"] == "buy")
            large_sell = sum(t["qty"] for t in large_trades if t["side"] == "sell")
            if large_buy > large_sell * 1.5:
                orderflow_data["large_trade_bias"] = "buy"
            elif large_sell > large_buy * 1.5:
                orderflow_data["large_trade_bias"] = "sell"
            else:
                orderflow_data["large_trade_bias"] = "neutral"

        # Absorption detection
        orderflow_data["absorption_signal"] = "none"
        if len(recent_window) >= 50:
            last_50 = recent_window[-50:]
            first_price = last_50[0]["price"]
            last_price = last_50[-1]["price"]
            price_change_pct = abs(last_price - first_price) / first_price * 100 if first_price else 0

            vol_50 = sum(t["qty"] for t in last_50)
            sell_vol_50 = sum(t["qty"] for t in last_50 if t["side"] == "sell")
            buy_vol_50 = sum(t["qty"] for t in last_50 if t["side"] == "buy")

            # Bullish absorption: heavy selling but price barely drops
            if sell_vol_50 > buy_vol_50 * 1.3 and price_change_pct < 0.05:
                orderflow_data["absorption_signal"] = "bullish_absorption"
            # Bearish absorption: heavy buying but price barely rises
            elif buy_vol_50 > sell_vol_50 * 1.3 and price_change_pct < 0.05:
                orderflow_data["absorption_signal"] = "bearish_absorption"


def process_depth(data: dict):
    """Process depth stream update."""
    global _depth_updates_count
    _depth_updates_count += 1

    bids = [[float(b[0]), float(b[1])] for b in data.get("bids", [])[:20]]
    asks = [[float(a[0]), float(a[1])] for a in data.get("asks", [])[:20]]

    if not bids or not asks:
        return

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    spread = best_ask - best_bid
    mid = (best_bid + best_ask) / 2
    spread_pct = spread / mid * 100 if mid else 0

    # Top-5 imbalance (research recommendation)
    bid_vol_5 = sum(b[1] for b in bids[:5])
    ask_vol_5 = sum(a[1] for a in asks[:5])
    total_5 = bid_vol_5 + ask_vol_5

    lob_ratio = bid_vol_5 / ask_vol_5 if ask_vol_5 > 0 else 1.0
    lob_imbalance = (bid_vol_5 - ask_vol_5) / total_5 if total_5 > 0 else 0

    _lob_imbalances.append(lob_imbalance)
    _depth_spreads.append(spread_pct)

    # EMA of imbalance for smoothing
    lob_ema = orderflow_data["lob_imbalance_ema"]
    alpha = 2 / (51)  # 50-period EMA
    lob_ema = alpha * lob_imbalance + (1 - alpha) * lob_ema

    # Spread average
    spread_avg = sum(_depth_spreads) / len(_depth_spreads) if _depth_spreads else 0

    # Full book volumes for delta
    bid_vol = sum(b[1] for b in bids)
    ask_vol = sum(a[1] for a in asks)
    total = bid_vol + ask_vol
    delta = (bid_vol - ask_vol) / total if total > 0 else 0

    orderflow_data.update({
        "lob_ratio": round(lob_ratio, 3),
        "lob_imbalance": round(lob_imbalance, 4),
        "lob_imbalance_ema": round(lob_ema, 4),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(spread, 2),
        "spread_pct": round(spread_pct, 4),
        "spread_avg": round(spread_avg, 4),
    })

    market_data["orderbook"] = {
        "bids": bids[:10], "asks": asks[:10],
        "bid_volume": round(bid_vol, 3), "ask_volume": round(ask_vol, 3),
        "delta": round(delta, 4),
    }


def process_kline(data: dict, interval: str):
    """Process kline stream update."""
    k = data.get("k", {})
    if not k:
        return
    candle = {
        "t": int(k.get("t", 0)),
        "o": float(k.get("o", 0)),
        "h": float(k.get("h", 0)),
        "l": float(k.get("l", 0)),
        "c": float(k.get("c", 0)),
        "v": float(k.get("v", 0)),
    }
    is_closed = k.get("x", False)
    key = f"candles_{interval}"

    candles = market_data.get(key, [])
    if candles and candles[-1]["t"] == candle["t"]:
        candles[-1] = candle
    else:
        candles.append(candle)
        if len(candles) > 200:
            candles[:] = candles[-200:]
    market_data[key] = candles

    if candle["c"] > 0:
        market_data["price"] = candle["c"]


# ---------------------------------------------------------------------------
# WebSocket Stream Consumers
# ---------------------------------------------------------------------------

async def _ws_stream(stream_name: str, handler, ws_key: str):
    """Generic WebSocket stream consumer with auto-reconnect."""
    url = f"{WS_BASE}/{stream_name}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10,
                                          close_timeout=5) as ws:
                _ws_connected[ws_key] = True
                print(f"[WS] Connected: {stream_name}")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        handler(data)
                    except Exception as e:
                        print(f"[WS {stream_name}] parse error: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            _ws_connected[ws_key] = False
            print(f"[WS {stream_name}] disconnected: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


async def start_trade_stream():
    await _ws_stream(f"{SYMBOL}@trade", process_trade, "trades")


async def start_depth_stream():
    await _ws_stream(f"{SYMBOL}@depth20@100ms", process_depth, "depth")


async def start_kline_stream(interval: str):
    def handler(data):
        process_kline(data, interval)
    await _ws_stream(f"{SYMBOL}@kline_{interval}", handler, f"kline_{interval}")


# ---------------------------------------------------------------------------
# Confluence Scoring Engine (order-flow-first)
# ---------------------------------------------------------------------------

def compute_confluence() -> dict:
    w = settings["confluence_weights"]
    price = market_data["price"]
    if price == 0:
        return {"score": 0, "direction": "neutral", "breakdown": {}, "reasons": []}

    # --- Order Flow Score (0-100) — PRIMARY ---
    flow_score = 50
    flow_reasons = []

    # LOB imbalance (research Rank 1-2)
    lob_ema = orderflow_data["lob_imbalance_ema"]
    lob_ratio = orderflow_data["lob_ratio"]
    if lob_ema > 0.15:
        flow_score += 15
        flow_reasons.append(f"LOB imbalance bullish ({lob_ratio:.2f}:1)")
    elif lob_ema < -0.15:
        flow_score -= 15
        flow_reasons.append(f"LOB imbalance bearish (1:{1/lob_ratio:.2f})")

    # CVD (research Rank 2)
    cvd_trend = orderflow_data["cvd_trend"]
    if cvd_trend == "rising":
        flow_score += 12
        flow_reasons.append("CVD trending up — buying pressure")
    elif cvd_trend == "falling":
        flow_score -= 12
        flow_reasons.append("CVD trending down — selling pressure")

    # CVD divergence
    cvd_div = orderflow_data["cvd_divergence"]
    if cvd_div == "bullish":
        flow_score += 10
        flow_reasons.append("Bullish CVD divergence detected")
    elif cvd_div == "bearish":
        flow_score -= 10
        flow_reasons.append("Bearish CVD divergence detected")

    # Tape aggression
    tape = orderflow_data["tape_aggression"]
    if tape == "buyers":
        flow_score += 8
        flow_reasons.append("Tape aggression: buyers dominant")
    elif tape == "sellers":
        flow_score -= 8
        flow_reasons.append("Tape aggression: sellers dominant")

    # Absorption
    absorp = orderflow_data["absorption_signal"]
    if absorp == "bullish_absorption":
        flow_score += 10
        flow_reasons.append("Bullish absorption — passive buyers absorbing sells")
    elif absorp == "bearish_absorption":
        flow_score -= 10
        flow_reasons.append("Bearish absorption — passive sellers absorbing buys")

    # Large trade bias
    ltb = orderflow_data["large_trade_bias"]
    if ltb == "buy":
        flow_score += 5
        flow_reasons.append("Large trades favor buyers")
    elif ltb == "sell":
        flow_score -= 5
        flow_reasons.append("Large trades favor sellers")

    # Volume spike from candles
    candles = market_data.get("candles_5m", [])
    if len(candles) >= 3:
        recent_vol = candles[-1].get("v", 0)
        avg_vol = sum(c.get("v", 0) for c in candles[-10:]) / min(10, len(candles))
        if avg_vol > 0 and recent_vol > avg_vol * 1.5:
            flow_score += 5
            flow_reasons.append("Volume spike detected")

    # Spread penalty
    spread_pct = orderflow_data["spread_pct"]
    spread_avg = orderflow_data["spread_avg"]
    if spread_avg > 0 and spread_pct > spread_avg * 2:
        flow_score -= 5
        flow_reasons.append(f"Wide spread ({spread_pct:.3f}%) — low confidence")

    # Volume Profile confluence
    poc = ta_data.get("poc_price", 0)
    if poc > 0 and abs(price - poc) / price < 0.002:
        flow_score += 5
        flow_reasons.append(f"Price at POC ({poc})")

    flow_score = max(0, min(100, flow_score))

    # --- Technical Score (0-100) — CONFIRMATION ---
    tech_score = 50
    tech_reasons = []

    rsi = ta_data["rsi"]
    if rsi < settings["rsi_oversold"]:
        tech_score += 12
        tech_reasons.append(f"RSI oversold ({rsi:.0f})")
    elif rsi > settings["rsi_overbought"]:
        tech_score -= 12
        tech_reasons.append(f"RSI overbought ({rsi:.0f})")
    elif rsi < 45:
        tech_score += 4
    elif rsi > 55:
        tech_score -= 4

    emas = ta_data["emas"]
    if emas:
        sorted_periods = sorted(emas.keys())
        ema_values = [emas[p] for p in sorted_periods]
        if all(ema_values[i] >= ema_values[i + 1] for i in range(len(ema_values) - 1)):
            tech_score += 10
            tech_reasons.append("EMA ribbon bullish alignment")
        elif all(ema_values[i] <= ema_values[i + 1] for i in range(len(ema_values) - 1)):
            tech_score -= 10
            tech_reasons.append("EMA ribbon bearish alignment")
        if 200 in emas and price > emas[200]:
            tech_score += 4
        elif 200 in emas:
            tech_score -= 4

    macd = ta_data["macd"]
    if macd["crossover"] == "bullish":
        tech_score += 8
        tech_reasons.append("Bullish MACD crossover")
    elif macd["crossover"] == "bearish":
        tech_score -= 8
        tech_reasons.append("Bearish MACD crossover")
    if macd["histogram"] > 0:
        tech_score += 2
    else:
        tech_score -= 2

    bb = ta_data["bb"]
    if bb["squeeze"]:
        tech_reasons.append("BB squeeze — breakout imminent")
    if price <= bb["lower"]:
        tech_score += 6
        tech_reasons.append("Price at lower BB")
    elif price >= bb["upper"]:
        tech_score -= 6
        tech_reasons.append("Price at upper BB")

    vwap = ta_data["vwap"]
    if vwap > 0:
        tech_score += 4 if price > vwap else -4

    fib = ta_data.get("fib_levels", {})
    if fib:
        for level_name, level_price in fib.items():
            if abs(price - level_price) / price < 0.003:
                tech_reasons.append(f"Price at Fib {level_name} ({level_price})")
                tech_score += 4
                break

    tech_score = max(0, min(100, tech_score))

    # --- Derivatives Score (0-100) ---
    deriv_score = 50
    deriv_reasons = []

    fr = market_data.get("funding_rate", 0)
    if fr > 0.01:
        deriv_score -= 10
        deriv_reasons.append(f"High funding rate ({fr:.4f}) — overleveraged longs")
    elif fr < -0.01:
        deriv_score += 10
        deriv_reasons.append(f"Negative funding ({fr:.4f}) — shorts paying")

    liqs = market_data.get("liquidations", {"long": 0, "short": 0})
    if liqs["short"] > liqs["long"] * 1.5:
        deriv_score += 8
        deriv_reasons.append("Short squeeze pressure")
    elif liqs["long"] > liqs["short"] * 1.5:
        deriv_score -= 8
        deriv_reasons.append("Long liquidation cascade")

    oi_chg = market_data.get("oi_change_pct", 0)
    if oi_chg > 5:
        deriv_reasons.append(f"OI rising +{oi_chg:.1f}%")
    elif oi_chg < -5:
        deriv_reasons.append(f"OI falling {oi_chg:.1f}%")

    deriv_score = max(0, min(100, deriv_score))

    # --- Sentiment Score (0-100) — MINIMAL WEIGHT ---
    sent_score = 50
    sent_reasons = []
    fg = market_data.get("fear_greed", {})
    fg_val = fg.get("value", 50)
    if fg_val < 25:
        sent_score += 10
        sent_reasons.append(f"Extreme Fear ({fg_val}) — contrarian bullish")
    elif fg_val > 75:
        sent_score -= 10
        sent_reasons.append(f"Extreme Greed ({fg_val}) — contrarian bearish")
    sent_score = max(0, min(100, sent_score))

    # --- Macro Score (0-100) ---
    macro_score = 50
    macro_reasons = []
    macro = market_data.get("macro", {})
    dxy_chg = macro.get("dxy_change", 0)
    if dxy_chg < -0.3:
        macro_score += 15
        macro_reasons.append("DXY falling — bullish for BTC")
    elif dxy_chg > 0.3:
        macro_score -= 15
        macro_reasons.append("DXY rising — bearish for BTC")
    sp_chg = macro.get("sp500_change", 0)
    if sp_chg > 0.3:
        macro_score += 10
        macro_reasons.append("S&P 500 positive — risk-on")
    elif sp_chg < -0.3:
        macro_score -= 10
        macro_reasons.append("S&P 500 negative — risk-off")
    macro_score = max(0, min(100, macro_score))

    # --- Weighted Total ---
    total = (
        flow_score * w["orderflow"]
        + tech_score * w["technical"]
        + deriv_score * w["derivatives"]
        + sent_score * w["sentiment"]
        + macro_score * w["macro"]
    )
    total = round(total, 1)

    direction = "neutral"
    if total >= 55:
        direction = "long"
    elif total <= 45:
        direction = "short"

    all_reasons = flow_reasons + tech_reasons + deriv_reasons + sent_reasons + macro_reasons

    return {
        "score": total,
        "direction": direction,
        "breakdown": {
            "orderflow": {"score": round(flow_score, 1), "weight": w["orderflow"], "reasons": flow_reasons},
            "technical": {"score": round(tech_score, 1), "weight": w["technical"], "reasons": tech_reasons},
            "derivatives": {"score": round(deriv_score, 1), "weight": w["derivatives"], "reasons": deriv_reasons},
            "sentiment": {"score": round(sent_score, 1), "weight": w["sentiment"], "reasons": sent_reasons},
            "macro": {"score": round(macro_score, 1), "weight": w["macro"], "reasons": macro_reasons},
        },
        "reasons": all_reasons,
    }


# ---------------------------------------------------------------------------
# Signal Generation (order-flow-first gates)
# ---------------------------------------------------------------------------

def generate_signal(confluence: dict) -> dict | None:
    global active_signal
    score = confluence["score"]
    direction = confluence["direction"]
    condition = ta_data.get("market_condition", "Ranging")

    if condition == "Choppy":
        return None
    if score < settings["min_confluence_medium"]:
        return None

    price = market_data["price"]
    if price == 0:
        return None

    # --- ORDER FLOW GATE (must pass at least one) ---
    of_confirms = 0
    lob_ratio = orderflow_data["lob_ratio"]
    cvd_trend = orderflow_data["cvd_trend"]
    absorption = orderflow_data["absorption_signal"]
    tape = orderflow_data["tape_aggression"]

    if direction == "long":
        if lob_ratio > 1.3:
            of_confirms += 1
        if cvd_trend == "rising":
            of_confirms += 1
        if absorption == "bullish_absorption":
            of_confirms += 1
        if tape == "buyers":
            of_confirms += 1
        # Also accept CVD bullish divergence
        if orderflow_data["cvd_divergence"] == "bullish":
            of_confirms += 1

    elif direction == "short":
        if lob_ratio < 0.77:
            of_confirms += 1
        if cvd_trend == "falling":
            of_confirms += 1
        if absorption == "bearish_absorption":
            of_confirms += 1
        if tape == "sellers":
            of_confirms += 1
        if orderflow_data["cvd_divergence"] == "bearish":
            of_confirms += 1
    else:
        return None

    # Must have at least 1 order flow confirmation
    if of_confirms == 0:
        return None

    # --- Transaction cost check ---
    atr = ta_data.get("atr", 0)
    fee = settings["fee_rate"]
    slip = settings["slippage_rate"]
    min_move = 2 * (fee + slip) * price
    if atr > 0 and atr < min_move:
        return None  # Expected move too small vs costs

    # --- Spread check ---
    spread_pct = orderflow_data.get("spread_pct", 0)
    spread_avg = orderflow_data.get("spread_avg", 0)
    if spread_avg > 0 and spread_pct > spread_avg * 3:
        return None  # Spread too wide

    confidence = "High" if score >= settings["min_confluence_high"] else "Medium"

    # ATR-based levels
    atr_val = ta_data.get("atr", 0)
    if atr_val == 0:
        atr_val = abs(ta_data["bb"]["upper"] - ta_data["bb"]["lower"]) / 4 if ta_data["bb"]["upper"] else price * 0.005

    if direction == "long":
        entry = price
        stop_loss = round(price - 1.5 * atr_val, 2)
        risk = entry - stop_loss
        tp1 = round(entry + risk * settings["rr_primary"], 2)
        tp2 = round(entry + risk * settings["rr_secondary"], 2)
    else:
        entry = price
        stop_loss = round(price + 1.5 * atr_val, 2)
        risk = stop_loss - entry
        tp1 = round(entry - risk * settings["rr_primary"], 2)
        tp2 = round(entry - risk * settings["rr_secondary"], 2)

    est_cost = round(2 * (fee + slip) * price, 2)

    signal = {
        "id": f"SIG-{int(time.time())}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "direction": direction.upper(),
        "type": f"ENTER_{direction.upper()}",
        "confidence": confidence,
        "score": score,
        "of_confirms": of_confirms,
        "breakdown": confluence["breakdown"],
        "reasons": confluence["reasons"],
        "entry": entry,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "risk_reward": settings["rr_primary"],
        "estimated_cost": est_cost,
        "atr": atr_val,
        "market_condition": condition,
    }

    # Deduplicate — 3-minute cooldown for same direction
    if signals_log:
        last = signals_log[-1]
        elapsed = time.time() - last.get("_ts", 0)
        if elapsed < 180 and last["direction"] == signal["direction"]:
            return None

    signal["_ts"] = time.time()
    signals_log.append(signal)
    if len(signals_log) > 200:
        signals_log.pop(0)

    active_signal = signal
    return signal


def check_exit_signal() -> dict | None:
    """Check if we should exit an active position."""
    if not active_signal:
        return None

    price = market_data["price"]
    if price == 0:
        return None

    direction = active_signal["direction"]
    entry = active_signal["entry"]
    sl = active_signal["stop_loss"]
    tp1 = active_signal["tp1"]

    exit_reasons = []

    # Stop loss hit
    if direction == "LONG" and price <= sl:
        exit_reasons.append("Stop loss hit")
    elif direction == "SHORT" and price >= sl:
        exit_reasons.append("Stop loss hit")

    # TP1 hit
    if direction == "LONG" and price >= tp1:
        exit_reasons.append("Take profit 1 reached")
    elif direction == "SHORT" and price <= tp1:
        exit_reasons.append("Take profit 1 reached")

    # CVD flip
    cvd_trend = orderflow_data["cvd_trend"]
    if direction == "LONG" and cvd_trend == "falling":
        exit_reasons.append("CVD flipped bearish")
    elif direction == "SHORT" and cvd_trend == "rising":
        exit_reasons.append("CVD flipped bullish")

    # LOB reversal
    lob_imb = orderflow_data["lob_imbalance_ema"]
    if direction == "LONG" and lob_imb < -0.2:
        exit_reasons.append("LOB imbalance reversed to bearish")
    elif direction == "SHORT" and lob_imb > 0.2:
        exit_reasons.append("LOB imbalance reversed to bullish")

    # Need at least 2 exit reasons to trigger (avoid premature exits)
    if len(exit_reasons) >= 2:
        pnl = price - entry if direction == "LONG" else entry - price
        pnl_pct = pnl / entry * 100

        exit_signal = {
            "id": f"EXIT-{int(time.time())}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "EXIT",
            "direction": direction,
            "entry_price": entry,
            "exit_price": price,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "reasons": exit_reasons,
        }
        return exit_signal
    return None


# ---------------------------------------------------------------------------
# REST Data Fetchers (for slow-context data + initial load)
# ---------------------------------------------------------------------------

async def fetch_initial_klines(client: httpx.AsyncClient, interval: str, limit: int = 100):
    try:
        r = await client.get(
            f"{REST_BASE}/api/v3/klines",
            params={"symbol": SYMBOL.upper(), "interval": interval, "limit": limit},
            timeout=10,
        )
        data = r.json()
        if isinstance(data, list):
            return [
                {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                 "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
                for k in data
            ]
    except Exception as e:
        print(f"[REST klines {interval}] {e}")
    return []


async def fetch_initial_ticker(client: httpx.AsyncClient):
    try:
        r = await client.get(
            f"{REST_BASE}/api/v3/ticker/24hr",
            params={"symbol": SYMBOL.upper()},
            timeout=10,
        )
        d = r.json()
        if isinstance(d, dict) and not d.get("code"):
            return {
                "price": float(d.get("lastPrice", 0)),
                "change": float(d.get("priceChange", 0)),
                "change_pct": float(d.get("priceChangePercent", 0)),
                "volume": float(d.get("volume", 0)),
                "high": float(d.get("highPrice", 0)),
                "low": float(d.get("lowPrice", 0)),
            }
    except Exception as e:
        print(f"[REST ticker] {e}")
    return {}


async def fetch_fear_greed(client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get("https://api.alternative.me/fng/?limit=2", timeout=10)
        d = r.json()
        data = d.get("data", [])
        if data:
            current = data[0]
            return {
                "value": int(current.get("value", 50)),
                "classification": current.get("value_classification", "Neutral"),
                "timestamp": current.get("timestamp", ""),
            }
    except Exception as e:
        print(f"[Fear&Greed] {e}")
    return {"value": 50, "classification": "Neutral", "timestamp": ""}


async def fetch_funding_rate(client: httpx.AsyncClient) -> float:
    try:
        r = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=10,
        )
        d = r.json()
        change = d.get("bitcoin", {}).get("usd_24h_change", 0)
        return round(change / 10000, 6)
    except Exception:
        return 0


async def fetch_macro(client: httpx.AsyncClient):
    headers = {"User-Agent": "Mozilla/5.0"}
    symbols = [
        ("dxy", "DX-Y.NYB"),
        ("sp500", "ES=F"),
        ("us10y", "%5ETNX"),
    ]
    for key, ticker in symbols:
        try:
            r = await client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": "2d"},
                headers=headers, timeout=10,
            )
            d = r.json()
            meta = d.get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("chartPreviousClose", price)
            if key == "us10y":
                change = round(price - prev, 3) if prev else 0
            else:
                change = round((price - prev) / prev * 100, 2) if prev else 0
            market_data["macro"][key] = price
            market_data["macro"][f"{key}_change"] = change
        except Exception as e:
            print(f"[Macro {key}] {e}")


# ---------------------------------------------------------------------------
# Main Processing Loop
# ---------------------------------------------------------------------------

async def analysis_loop():
    """Run TA + confluence + signal generation every 2 seconds."""
    while True:
        try:
            # Process order flow signals
            compute_orderflow_signals()

            # Run TA on 5m candles
            candles = market_data.get("candles_5m", [])
            if candles:
                closes = [c["c"] for c in candles]

                # EMAs
                emas = {}
                for period in settings["ema_periods"]:
                    ema_vals = calc_ema(closes, period)
                    if ema_vals:
                        emas[period] = round(ema_vals[-1], 2)
                ta_data["emas"] = emas

                # RSI
                ta_data["prev_rsi"] = ta_data["rsi"]
                ta_data["rsi"] = round(calc_rsi(closes, settings["rsi_period"]), 1)

                # MACD
                ta_data["macd"] = calc_macd(closes, settings["macd_fast"], settings["macd_slow"], settings["macd_signal"])

                # Bollinger
                ta_data["bb"] = calc_bollinger(closes, settings["bb_period"], settings["bb_std"])

                # VWAP
                vwap, vwap_u, vwap_l = calc_vwap(candles[-50:])
                ta_data["vwap"] = vwap
                ta_data["vwap_upper"] = vwap_u
                ta_data["vwap_lower"] = vwap_l

                # ATR
                ta_data["atr"] = calc_atr(candles, settings["atr_period"])
                ta_data["atr_pct"] = round(ta_data["atr"] / market_data["price"] * 100, 4) if market_data["price"] else 0

                # ADX
                ta_data["adx"] = calc_adx(candles)

                # Fibonacci
                fib = find_swing_levels(candles)
                ta_data["fib_levels"] = fib["levels"]

                # Volume Profile with POC/HVN/LVN
                vp = calc_volume_profile(candles)
                ta_data["volume_profile"] = vp["profile"]
                ta_data["poc_price"] = vp["poc_price"]
                ta_data["hvn_levels"] = vp["hvn_levels"]
                ta_data["lvn_levels"] = vp["lvn_levels"]

                # Market condition
                ta_data["market_condition"] = determine_market_condition(
                    ta_data["adx"], ta_data["bb"]["width"], ta_data["atr_pct"])

            # Confluence
            confluence = compute_confluence()

            # Signal generation
            signal = generate_signal(confluence)

            # Check exit
            exit_sig = check_exit_signal()
            if exit_sig:
                signals_log.append(exit_sig)
                active_signal_ref = None  # Clear after exit

            # Broadcast
            payload = {
                "type": "update",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": {
                    "price": market_data["price"],
                    "change_24h": market_data["change_24h"],
                    "change_24h_pct": market_data["change_24h_pct"],
                    "high_24h": market_data.get("high_24h", 0),
                    "low_24h": market_data.get("low_24h", 0),
                    "volume_24h": market_data.get("volume_24h", 0),
                },
                "orderbook": market_data["orderbook"],
                "orderflow": {
                    "cvd_value": orderflow_data["cvd_value"],
                    "cvd_trend": orderflow_data["cvd_trend"],
                    "cvd_divergence": orderflow_data["cvd_divergence"],
                    "cvd_history": orderflow_data["cvd_history"][-60:],
                    "lob_ratio": orderflow_data["lob_ratio"],
                    "lob_imbalance": orderflow_data["lob_imbalance"],
                    "lob_imbalance_ema": orderflow_data["lob_imbalance_ema"],
                    "spread": orderflow_data["spread"],
                    "spread_pct": orderflow_data["spread_pct"],
                    "spread_avg": orderflow_data["spread_avg"],
                    "tape_aggression": orderflow_data["tape_aggression"],
                    "absorption_signal": orderflow_data["absorption_signal"],
                    "large_trade_bias": orderflow_data["large_trade_bias"],
                    "recent_buy_volume": orderflow_data["recent_buy_volume"],
                    "recent_sell_volume": orderflow_data["recent_sell_volume"],
                },
                "candles_5m": market_data["candles_5m"][-60:],
                "candles_1m": market_data["candles_1m"][-60:],
                "ta": {
                    "emas": ta_data["emas"],
                    "rsi": ta_data["rsi"],
                    "macd": ta_data["macd"],
                    "bb": ta_data["bb"],
                    "vwap": ta_data["vwap"],
                    "vwap_upper": ta_data["vwap_upper"],
                    "vwap_lower": ta_data["vwap_lower"],
                    "atr": ta_data["atr"],
                    "atr_pct": ta_data["atr_pct"],
                    "adx": ta_data["adx"],
                    "fib_levels": ta_data["fib_levels"],
                    "volume_profile": ta_data["volume_profile"],
                    "poc_price": ta_data["poc_price"],
                    "hvn_levels": ta_data["hvn_levels"],
                    "lvn_levels": ta_data["lvn_levels"],
                    "market_condition": ta_data["market_condition"],
                },
                "onchain": {
                    "funding_rate": market_data["funding_rate"],
                    "open_interest": market_data["open_interest"],
                    "oi_change_pct": market_data["oi_change_pct"],
                    "liquidations": market_data["liquidations"],
                },
                "sentiment": {"fear_greed": market_data["fear_greed"]},
                "macro": market_data["macro"],
                "confluence": confluence,
                "signal": signal,
                "exit_signal": exit_sig,
                "ws_status": _ws_connected,
                "settings": settings,
            }

            disconnected = []
            for ws in clients:
                try:
                    await ws.send_json(payload)
                except Exception:
                    disconnected.append(ws)
            for ws in disconnected:
                clients.remove(ws)

        except Exception as e:
            print(f"[Analysis loop error] {e}")
            traceback.print_exc()

        await asyncio.sleep(2)


async def slow_data_loop():
    """Fetch slow-context data (Fear & Greed, funding, macro) periodically."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                fg = await fetch_fear_greed(client)
                market_data["fear_greed"] = fg

                fr = await fetch_funding_rate(client)
                if fr != 0:
                    market_data["funding_rate"] = fr

                await fetch_macro(client)
            except Exception as e:
                print(f"[Slow data error] {e}")

            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.append(ws)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "update_settings":
                new_settings = msg.get("settings", {})
                for k, v in new_settings.items():
                    if k in settings:
                        settings[k] = v
                await ws.send_json({"type": "settings_updated", "settings": settings})
    except WebSocketDisconnect:
        if ws in clients:
            clients.remove(ws)


@app.get("/api/signals")
async def get_signals():
    return [{k: v for k, v in s.items() if not k.startswith("_")} for s in signals_log]


@app.get("/api/settings")
async def get_settings():
    return settings


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "exchange": EXCHANGE,
        "ws_connected": _ws_connected,
        "price": market_data["price"],
        "candles_1m": len(market_data.get("candles_1m", [])),
        "candles_5m": len(market_data.get("candles_5m", [])),
        "trades_buffered": len(_trade_buffer),
        "depth_updates": _depth_updates_count,
    }


@app.on_event("startup")
async def startup():
    # Load initial data via REST
    async with httpx.AsyncClient() as client:
        print("[Startup] Fetching initial data via REST...")
        k5, k1, ticker = await asyncio.gather(
            fetch_initial_klines(client, "5m", 100),
            fetch_initial_klines(client, "1m", 100),
            fetch_initial_ticker(client),
        )
        if k5:
            market_data["candles_5m"] = k5
            print(f"  Loaded {len(k5)} 5m candles")
        if k1:
            market_data["candles_1m"] = k1
            print(f"  Loaded {len(k1)} 1m candles")
        if ticker:
            market_data["price"] = ticker.get("price", 0)
            market_data["change_24h"] = ticker.get("change", 0)
            market_data["change_24h_pct"] = ticker.get("change_pct", 0)
            market_data["high_24h"] = ticker.get("high", 0)
            market_data["low_24h"] = ticker.get("low", 0)
            market_data["volume_24h"] = ticker.get("volume", 0)
            print(f"  BTC price: ${market_data['price']:,.2f}")

    # Start WebSocket streams
    print("[Startup] Starting WebSocket streams...")
    asyncio.create_task(start_trade_stream())
    asyncio.create_task(start_depth_stream())
    asyncio.create_task(start_kline_stream("1m"))
    asyncio.create_task(start_kline_stream("5m"))

    # Start processing loops
    asyncio.create_task(analysis_loop())
    asyncio.create_task(slow_data_loop())
    print("[Startup] All systems running.")


# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
