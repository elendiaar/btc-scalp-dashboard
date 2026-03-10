"""
BTC Scalp Dashboard — FastAPI Backend
Real-time market data aggregation, confluence scoring, and signal generation.
"""

import asyncio
import json
import time
import math
import traceback
from datetime import datetime, timezone
from typing import Any

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="BTC Scalp Dashboard")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
clients: list[WebSocket] = []

# Settings — defaults (adjustable via settings page)
settings: dict[str, Any] = {
    "confluence_weights": {
        "technical": 0.40,
        "orderflow": 0.20,
        "onchain": 0.15,
        "sentiment": 0.15,
        "macro": 0.10,
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
    "rr_primary": 2.0,
    "rr_secondary": 3.0,
    "min_confluence_high": 75,
    "min_confluence_medium": 60,
    "emergency_exit": 40,
}

# Data store
market_data: dict[str, Any] = {
    "price": 0,
    "price_24h_ago": 0,
    "change_24h": 0,
    "change_24h_pct": 0,
    "candles_1m": [],
    "candles_5m": [],
    "orderbook": {"bids": [], "asks": [], "bid_volume": 0, "ask_volume": 0, "delta": 0},
    "fear_greed": {"value": 50, "classification": "Neutral", "timestamp": ""},
    "funding_rate": 0,
    "open_interest": 0,
    "oi_change_pct": 0,
    "macro": {"dxy": 0, "dxy_change": 0, "sp500": 0, "sp500_change": 0, "us10y": 0, "us10y_change": 0, "events": []},
    "sentiment_social": {"reddit_sentiment": 0, "twitter_volume": 0, "trending_keywords": []},
    "whale_txns": [],
    "exchange_netflow": 0,
    "liquidations": {"long": 0, "short": 0},
}

# Technical analysis results
ta_data: dict[str, Any] = {
    "emas": {},
    "rsi": 50,
    "macd": {"macd": 0, "signal": 0, "histogram": 0, "crossover": "none"},
    "vwap": 0,
    "vwap_upper": 0,
    "vwap_lower": 0,
    "bb": {"upper": 0, "middle": 0, "lower": 0, "squeeze": False, "width": 0},
    "fib_levels": {},
    "adx": 25,
    "market_condition": "Ranging",
    "volume_profile": [],
}

# Signals
signals_log: list[dict] = []
active_signal: dict | None = None

# ---------------------------------------------------------------------------
# Technical Analysis Functions
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
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
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
        if prev_diff <= 0 and curr_diff > 0:
            crossover = "bullish"
        elif prev_diff >= 0 and curr_diff < 0:
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
    squeeze = width < 3.0  # tight squeeze threshold
    return {"upper": round(upper, 2), "middle": round(middle, 2),
            "lower": round(lower, 2), "squeeze": squeeze, "width": round(width, 4)}

def calc_vwap(candles: list[dict]) -> tuple[float, float, float]:
    """Session VWAP with bands from candle data."""
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
    # standard deviation band
    variance = sum((tp - vwap) ** 2 for tp in tp_list) / len(tp_list)
    std = math.sqrt(variance)
    return round(vwap, 2), round(vwap + std, 2), round(vwap - std, 2)

def calc_adx(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period * 2:
        return 25.0
    tr_list = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(candles)):
        h, l, c_prev = candles[i]["h"], candles[i]["l"], candles[i-1]["c"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)
        up = h - candles[i-1]["h"]
        down = candles[i-1]["l"] - l
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
    dx = 100 * abs(plus_di - minus_di) / di_sum
    return round(dx, 1)

def find_swing_levels(candles: list[dict]) -> dict:
    """Find last significant swing high/low for Fibonacci retracement."""
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

def calc_volume_profile(candles: list[dict], bins: int = 20) -> list[dict]:
    """Simple volume profile from candle data."""
    if not candles:
        return []
    prices = [c["c"] for c in candles]
    volumes = [c["v"] for c in candles]
    min_p, max_p = min(prices), max(prices)
    if max_p == min_p:
        return [{"price": min_p, "volume": sum(volumes)}]
    step = (max_p - min_p) / bins
    profile = []
    for i in range(bins):
        low = min_p + i * step
        high = low + step
        vol = sum(v for p, v in zip(prices, volumes) if low <= p < high)
        profile.append({"price": round(low + step / 2, 2), "volume": round(vol, 2)})
    return profile

def determine_market_condition(adx: float, bb_width: float) -> str:
    if adx > 25 and bb_width > 4:
        return "Trending"
    elif adx < 20 and bb_width < 2.5:
        return "Choppy"
    else:
        return "Ranging"

# ---------------------------------------------------------------------------
# Confluence Scoring Engine
# ---------------------------------------------------------------------------

def compute_confluence() -> dict:
    """
    Computes weighted confluence score 0-100.
    Returns score breakdown and direction.
    """
    w = settings["confluence_weights"]
    price = market_data["price"]
    if price == 0:
        return {"score": 0, "direction": "neutral", "breakdown": {}, "reasons": []}

    reasons = []

    # --- Technical Score (0-100) ---
    tech_score = 50  # neutral baseline
    tech_reasons = []

    # RSI
    rsi = ta_data["rsi"]
    if rsi < settings["rsi_oversold"]:
        tech_score += 15
        tech_reasons.append(f"RSI oversold ({rsi:.0f})")
    elif rsi > settings["rsi_overbought"]:
        tech_score -= 15
        tech_reasons.append(f"RSI overbought ({rsi:.0f})")
    elif rsi < 45:
        tech_score += 5
    elif rsi > 55:
        tech_score -= 5

    # EMA alignment
    emas = ta_data["emas"]
    if emas:
        sorted_periods = sorted(emas.keys())
        ema_values = [emas[p] for p in sorted_periods]
        if all(ema_values[i] >= ema_values[i+1] for i in range(len(ema_values)-1)):
            tech_score += 12
            tech_reasons.append("EMA ribbon bullish alignment")
        elif all(ema_values[i] <= ema_values[i+1] for i in range(len(ema_values)-1)):
            tech_score -= 12
            tech_reasons.append("EMA ribbon bearish alignment")
        # Price vs 200 EMA
        if 200 in emas and price > emas[200]:
            tech_score += 5
        elif 200 in emas:
            tech_score -= 5

    # MACD
    macd = ta_data["macd"]
    if macd["crossover"] == "bullish":
        tech_score += 10
        tech_reasons.append("Bullish MACD crossover")
    elif macd["crossover"] == "bearish":
        tech_score -= 10
        tech_reasons.append("Bearish MACD crossover")
    if macd["histogram"] > 0:
        tech_score += 3
    else:
        tech_score -= 3

    # Bollinger
    bb = ta_data["bb"]
    if bb["squeeze"]:
        tech_reasons.append("BB squeeze detected — breakout imminent")
    if price <= bb["lower"]:
        tech_score += 8
        tech_reasons.append("Price at lower BB")
    elif price >= bb["upper"]:
        tech_score -= 8
        tech_reasons.append("Price at upper BB")

    # VWAP
    vwap = ta_data["vwap"]
    if vwap > 0:
        if price > vwap:
            tech_score += 5
        else:
            tech_score -= 5

    # Fibonacci proximity
    fib = ta_data.get("fib_levels", {})
    if fib:
        for level_name, level_price in fib.items():
            if abs(price - level_price) / price < 0.003:  # within 0.3%
                tech_reasons.append(f"Price at Fib {level_name} ({level_price})")
                tech_score += 5  # support confluence
                break

    tech_score = max(0, min(100, tech_score))

    # --- Order Flow Score (0-100) ---
    flow_score = 50
    flow_reasons = []
    ob = market_data["orderbook"]
    delta = ob.get("delta", 0)
    if delta > 0.1:
        flow_score += 20
        flow_reasons.append(f"Positive order flow delta ({delta:.2f})")
    elif delta < -0.1:
        flow_score -= 20
        flow_reasons.append(f"Negative order flow delta ({delta:.2f})")

    # Volume confirmation
    candles = market_data.get("candles_5m", [])
    if len(candles) >= 3:
        recent_vol = candles[-1].get("v", 0)
        avg_vol = sum(c.get("v", 0) for c in candles[-10:]) / min(10, len(candles))
        if avg_vol > 0 and recent_vol > avg_vol * 1.5:
            flow_score += 15
            flow_reasons.append("Volume spike detected")

    flow_score = max(0, min(100, flow_score))

    # --- On-Chain Score (0-100) ---
    onchain_score = 50
    onchain_reasons = []

    # Funding rate
    fr = market_data.get("funding_rate", 0)
    if fr > 0.01:
        onchain_score -= 10
        onchain_reasons.append(f"High funding rate ({fr:.4f}) — overleveraged longs")
    elif fr < -0.01:
        onchain_score += 10
        onchain_reasons.append(f"Negative funding rate ({fr:.4f}) — shorts paying")

    # Exchange netflow
    netflow = market_data.get("exchange_netflow", 0)
    if netflow > 0:
        onchain_score -= 10
        onchain_reasons.append("Net exchange inflow — sell pressure")
    elif netflow < 0:
        onchain_score += 10
        onchain_reasons.append("Net exchange outflow — accumulation")

    # Liquidations
    liqs = market_data.get("liquidations", {"long": 0, "short": 0})
    if liqs["short"] > liqs["long"] * 1.5:
        onchain_score += 10
        onchain_reasons.append("Short squeeze pressure (more short liquidations)")
    elif liqs["long"] > liqs["short"] * 1.5:
        onchain_score -= 10
        onchain_reasons.append("Long liquidation cascade")

    # OI change
    oi_chg = market_data.get("oi_change_pct", 0)
    if oi_chg > 5:
        onchain_reasons.append(f"OI rising +{oi_chg:.1f}% — new positions entering")
    elif oi_chg < -5:
        onchain_reasons.append(f"OI falling {oi_chg:.1f}% — positions closing")

    onchain_score = max(0, min(100, onchain_score))

    # --- Sentiment Score (0-100) ---
    sent_score = 50
    sent_reasons = []

    fg = market_data.get("fear_greed", {})
    fg_val = fg.get("value", 50)
    fg_class = fg.get("classification", "Neutral")
    if fg_val < 25:
        sent_score += 15
        sent_reasons.append(f"Extreme Fear ({fg_val}) — contrarian bullish")
    elif fg_val > 75:
        sent_score -= 15
        sent_reasons.append(f"Extreme Greed ({fg_val}) — contrarian bearish")
    elif fg_val < 40:
        sent_score += 5
        sent_reasons.append(f"Fear zone ({fg_val})")
    elif fg_val > 60:
        sent_score -= 5
        sent_reasons.append(f"Greed zone ({fg_val})")

    social = market_data.get("sentiment_social", {})
    reddit_sent = social.get("reddit_sentiment", 0)
    if reddit_sent > 0.3:
        sent_score += 5
    elif reddit_sent < -0.3:
        sent_score -= 5

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

    us10y_chg = macro.get("us10y_change", 0)
    if us10y_chg > 0.05:
        macro_score -= 5
        macro_reasons.append("10Y yield rising — headwind")
    elif us10y_chg < -0.05:
        macro_score += 5
        macro_reasons.append("10Y yield falling — tailwind")

    macro_score = max(0, min(100, macro_score))

    # --- Weighted Total ---
    total = (
        tech_score * w["technical"]
        + flow_score * w["orderflow"]
        + onchain_score * w["onchain"]
        + sent_score * w["sentiment"]
        + macro_score * w["macro"]
    )
    total = round(total, 1)

    # Direction
    direction = "neutral"
    if total >= 55:
        direction = "long"
    elif total <= 45:
        direction = "short"

    all_reasons = tech_reasons + flow_reasons + onchain_reasons + sent_reasons + macro_reasons

    return {
        "score": total,
        "direction": direction,
        "breakdown": {
            "technical": {"score": round(tech_score, 1), "weight": w["technical"], "reasons": tech_reasons},
            "orderflow": {"score": round(flow_score, 1), "weight": w["orderflow"], "reasons": flow_reasons},
            "onchain": {"score": round(onchain_score, 1), "weight": w["onchain"], "reasons": onchain_reasons},
            "sentiment": {"score": round(sent_score, 1), "weight": w["sentiment"], "reasons": sent_reasons},
            "macro": {"score": round(macro_score, 1), "weight": w["macro"], "reasons": macro_reasons},
        },
        "reasons": all_reasons,
    }

# ---------------------------------------------------------------------------
# Signal Generation
# ---------------------------------------------------------------------------

def generate_signal(confluence: dict) -> dict | None:
    """Generate a trade signal if confluence threshold is met."""
    global active_signal
    score = confluence["score"]
    direction = confluence["direction"]
    condition = ta_data.get("market_condition", "Ranging")

    # Suppress in choppy markets
    if condition == "Choppy":
        return None

    if score < settings["min_confluence_medium"]:
        return None

    price = market_data["price"]
    if price == 0:
        return None

    confidence = "High" if score >= settings["min_confluence_high"] else "Medium"

    # Calculate entry, SL, TP
    atr_proxy = abs(ta_data["bb"]["upper"] - ta_data["bb"]["lower"]) / 4 if ta_data["bb"]["upper"] else price * 0.005

    if direction == "long":
        entry = price
        stop_loss = round(price - atr_proxy, 2)
        risk = entry - stop_loss
        tp1 = round(entry + risk * settings["rr_primary"], 2)
        tp2 = round(entry + risk * settings["rr_secondary"], 2)
    elif direction == "short":
        entry = price
        stop_loss = round(price + atr_proxy, 2)
        risk = stop_loss - entry
        tp1 = round(entry - risk * settings["rr_primary"], 2)
        tp2 = round(entry - risk * settings["rr_secondary"], 2)
    else:
        return None

    # Validate LONG conditions
    if direction == "long":
        rsi = ta_data["rsi"]
        macd = ta_data["macd"]
        fg_val = market_data.get("fear_greed", {}).get("value", 50)
        delta = market_data["orderbook"].get("delta", 0)

        # RSI rising from oversold OR bullish MACD
        rsi_ok = rsi < 45 or (rsi < 55 and rsi > ta_data.get("prev_rsi", rsi))
        macd_ok = macd["crossover"] == "bullish" or macd["histogram"] > 0
        if not (rsi_ok or macd_ok):
            return None
        # Order flow not strongly negative
        if delta < -0.3:
            return None
        # Fear & Greed not extreme greed
        if fg_val > 75:
            return None

    # Validate SHORT conditions (inverse)
    if direction == "short":
        rsi = ta_data["rsi"]
        macd = ta_data["macd"]
        fg_val = market_data.get("fear_greed", {}).get("value", 50)
        delta = market_data["orderbook"].get("delta", 0)

        rsi_ok = rsi > 55 or (rsi > 45 and rsi < ta_data.get("prev_rsi", rsi))
        macd_ok = macd["crossover"] == "bearish" or macd["histogram"] < 0
        if not (rsi_ok or macd_ok):
            return None
        if delta > 0.3:
            return None
        if fg_val < 25:
            return None

    signal = {
        "id": f"SIG-{int(time.time())}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "direction": direction.upper(),
        "confidence": confidence,
        "score": score,
        "breakdown": confluence["breakdown"],
        "reasons": confluence["reasons"],
        "entry": entry,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "risk_reward": settings["rr_primary"],
        "market_condition": condition,
    }

    # Deduplicate — don't repeat same direction within 3 minutes
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

# ---------------------------------------------------------------------------
# Data Fetchers
# ---------------------------------------------------------------------------

async def fetch_binance_klines(client: httpx.AsyncClient, interval: str = "5m", limit: int = 100) -> list[dict]:
    """Fetch BTC/USDT klines from Binance.us (spot)."""
    try:
        r = await client.get(
            "https://api.binance.us/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=10,
        )
        data = r.json()
        if isinstance(data, dict) and data.get("code"):
            print(f"[Binance.us klines] API error: {data}")
            return []
        return [
            {
                "t": int(k[0]),
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
                "v": float(k[5]),
            }
            for k in data
        ]
    except Exception as e:
        print(f"[Binance.us klines] {e}")
        return []

async def fetch_binance_ticker(client: httpx.AsyncClient) -> dict:
    """Fetch BTC/USDT ticker from Binance.us."""
    try:
        r = await client.get(
            "https://api.binance.us/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"},
            timeout=10,
        )
        d = r.json()
        if isinstance(d, dict) and d.get("code"):
            print(f"[Binance.us ticker] API error: {d}")
            return {}
        return {
            "price": float(d.get("lastPrice", 0)),
            "change": float(d.get("priceChange", 0)),
            "change_pct": float(d.get("priceChangePercent", 0)),
            "volume": float(d.get("volume", 0)),
            "high": float(d.get("highPrice", 0)),
            "low": float(d.get("lowPrice", 0)),
        }
    except Exception as e:
        print(f"[Binance.us ticker] {e}")
        return {}

async def fetch_binance_depth(client: httpx.AsyncClient) -> dict:
    """Fetch BTC/USDT order book from Binance.us."""
    try:
        r = await client.get(
            "https://api.binance.us/api/v3/depth",
            params={"symbol": "BTCUSDT", "limit": 20},
            timeout=10,
        )
        d = r.json()
        bids = [[float(b[0]), float(b[1])] for b in d.get("bids", [])]
        asks = [[float(a[0]), float(a[1])] for a in d.get("asks", [])]
        bid_vol = sum(b[1] for b in bids)
        ask_vol = sum(a[1] for a in asks)
        total = bid_vol + ask_vol
        delta = (bid_vol - ask_vol) / total if total > 0 else 0
        return {"bids": bids[:10], "asks": asks[:10], "bid_volume": round(bid_vol, 3),
                "ask_volume": round(ask_vol, 3), "delta": round(delta, 4)}
    except Exception as e:
        print(f"[Binance.us depth] {e}")
        return {"bids": [], "asks": [], "bid_volume": 0, "ask_volume": 0, "delta": 0}

async def fetch_funding_and_oi(client: httpx.AsyncClient) -> dict:
    """Fetch funding rate and OI from CoinGlass public API or estimate from spot data."""
    result = {"funding_rate": 0, "oi": 0}
    try:
        # Try CoinGlass public summary
        r = await client.get(
            "https://open-api-v3.coinglass.com/api/futures/funding-rate?symbol=BTC",
            headers={"accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("data"):
                for item in data["data"]:
                    if "binance" in item.get("exchangeName", "").lower():
                        result["funding_rate"] = float(item.get("rate", 0))
                        break
    except Exception:
        pass
    # Funding rate estimation: if spot price diverges from 24h avg, estimate
    if result["funding_rate"] == 0:
        try:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
                timeout=10,
            )
            d = r.json()
            change = d.get("bitcoin", {}).get("usd_24h_change", 0)
            # Rough funding estimate from momentum
            result["funding_rate"] = round(change / 10000, 6)
        except Exception:
            pass
    return result

async def fetch_coingecko_supplement(client: httpx.AsyncClient) -> dict:
    """Supplementary data from CoinGecko (24h vol, market cap for context)."""
    try:
        r = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd",
                    "include_24hr_vol": "true", "include_24hr_change": "true",
                    "include_market_cap": "true"},
            timeout=10,
        )
        d = r.json().get("bitcoin", {})
        return {
            "price_cg": d.get("usd", 0),
            "vol_24h": d.get("usd_24h_vol", 0),
            "change_24h_cg": d.get("usd_24h_change", 0),
            "market_cap": d.get("usd_market_cap", 0),
        }
    except Exception as e:
        print(f"[CoinGecko] {e}")
        return {}

async def fetch_fear_greed(client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get("https://api.alternative.me/fng/?limit=2", timeout=10)
        d = r.json()
        data = d.get("data", [])
        if data:
            current = data[0]
            prev = data[1] if len(data) > 1 else data[0]
            return {
                "value": int(current.get("value", 50)),
                "classification": current.get("value_classification", "Neutral"),
                "timestamp": current.get("timestamp", ""),
                "prev_value": int(prev.get("value", 50)),
            }
    except Exception as e:
        print(f"[Fear&Greed] {e}")
    return {"value": 50, "classification": "Neutral", "timestamp": "", "prev_value": 50}

async def fetch_liquidation_estimate(client: httpx.AsyncClient) -> dict:
    """Estimate liquidation pressure from price volatility and volume."""
    # Without direct futures API access, estimate from spot data
    try:
        candles = market_data.get("candles_5m", [])
        if len(candles) >= 5:
            recent = candles[-5:]
            avg_vol = sum(c["v"] for c in recent) / 5
            price = candles[-1]["c"]
            # Estimate: strong moves = liquidations on opposing side
            change = (candles[-1]["c"] - candles[-5]["c"]) / candles[-5]["c"] * 100
            base_liq = avg_vol * price * 0.1  # rough estimate
            if change > 0.5:
                return {"long": round(base_liq * 0.3, 0), "short": round(base_liq * 0.7, 0)}
            elif change < -0.5:
                return {"long": round(base_liq * 0.7, 0), "short": round(base_liq * 0.3, 0)}
            else:
                return {"long": round(base_liq * 0.5, 0), "short": round(base_liq * 0.5, 0)}
    except Exception:
        pass
    return {"long": 0, "short": 0}

async def fetch_coinglass_data(client: httpx.AsyncClient) -> dict:
    """Attempt to get aggregated data from CoinGlass public endpoints."""
    result = {"exchange_netflow": 0, "whale_txns": []}
    try:
        # Public CoinGlass endpoints (limited but free)
        r = await client.get(
            "https://open-api.coinglass.com/public/v2/indicator/funding",
            params={"symbol": "BTC", "time_type": "h8"},
            headers={"accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            # Process if available
    except Exception:
        pass
    return result

# ---------------------------------------------------------------------------
# Background Data Loop
# ---------------------------------------------------------------------------

async def data_loop():
    """Main data fetching and processing loop."""
    prev_oi = 0
    cycle = 0

    async with httpx.AsyncClient() as client:
        while True:
            try:
                cycle += 1

                # --- Every cycle (15s): Technical data from Binance ---
                ticker_task = fetch_binance_ticker(client)
                depth_task = fetch_binance_depth(client)
                klines_5m_task = fetch_binance_klines(client, "5m", 100)
                klines_1m_task = fetch_binance_klines(client, "1m", 100)
                funding_task = fetch_funding_and_oi(client)
                cg_task = fetch_coingecko_supplement(client)

                tasks = [ticker_task, depth_task, klines_5m_task, klines_1m_task, funding_task, cg_task]

                # Every 5th cycle (~75s): sentiment & on-chain
                if cycle % 5 == 1:
                    tasks.append(fetch_fear_greed(client))
                    tasks.append(fetch_liquidation_estimate(client))

                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Process ticker
                ticker = results[0] if not isinstance(results[0], Exception) else {}
                if ticker:
                    market_data["price"] = ticker.get("price", market_data["price"])
                    market_data["change_24h"] = ticker.get("change", 0)
                    market_data["change_24h_pct"] = ticker.get("change_pct", 0)

                # Process depth
                depth = results[1] if not isinstance(results[1], Exception) else {}
                if depth:
                    market_data["orderbook"] = depth

                # Process klines
                klines_5m = results[2] if not isinstance(results[2], Exception) else []
                klines_1m = results[3] if not isinstance(results[3], Exception) else []
                if klines_5m:
                    market_data["candles_5m"] = klines_5m
                if klines_1m:
                    market_data["candles_1m"] = klines_1m

                # Process funding + OI
                funding_data = results[4] if not isinstance(results[4], Exception) else {}
                if isinstance(funding_data, dict):
                    if funding_data.get("funding_rate"):
                        market_data["funding_rate"] = funding_data["funding_rate"]
                    new_oi = funding_data.get("oi", 0)
                    if new_oi > 0:
                        if prev_oi > 0:
                            market_data["oi_change_pct"] = round((new_oi - prev_oi) / prev_oi * 100, 2)
                        market_data["open_interest"] = new_oi
                        prev_oi = new_oi

                # Process CoinGecko supplement
                cg_data = results[5] if not isinstance(results[5], Exception) else {}
                if isinstance(cg_data, dict) and cg_data.get("price_cg"):
                    # Use CoinGecko as fallback price if Binance.us fails
                    if market_data["price"] == 0:
                        market_data["price"] = cg_data["price_cg"]
                        market_data["change_24h_pct"] = cg_data.get("change_24h_cg", 0)

                # Process sentiment (every 5th cycle)
                result_idx = 6
                if cycle % 5 == 1 and len(results) > result_idx:
                    fg = results[result_idx]
                    if not isinstance(fg, Exception) and fg:
                        market_data["fear_greed"] = fg
                    result_idx += 1
                    if len(results) > result_idx:
                        liqs = results[result_idx]
                        if not isinstance(liqs, Exception):
                            market_data["liquidations"] = liqs

                # --- Run Technical Analysis ---
                candles = market_data["candles_5m"]
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

                    # ADX
                    ta_data["adx"] = calc_adx(candles)

                    # Fibonacci
                    fib = find_swing_levels(candles)
                    ta_data["fib_levels"] = fib["levels"]
                    ta_data["fib_swing_high"] = fib["swing_high"]
                    ta_data["fib_swing_low"] = fib["swing_low"]

                    # Volume Profile
                    ta_data["volume_profile"] = calc_volume_profile(candles)

                    # Market condition
                    ta_data["market_condition"] = determine_market_condition(ta_data["adx"], ta_data["bb"]["width"])

                # --- Confluence Scoring ---
                confluence = compute_confluence()

                # --- Signal Generation ---
                signal = generate_signal(confluence)

                # --- Broadcast to WebSocket clients ---
                payload = {
                    "type": "update",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": {
                        "price": market_data["price"],
                        "change_24h": market_data["change_24h"],
                        "change_24h_pct": market_data["change_24h_pct"],
                        "high_24h": ticker.get("high", 0) if ticker else 0,
                        "low_24h": ticker.get("low", 0) if ticker else 0,
                        "volume_24h": ticker.get("volume", 0) if ticker else 0,
                    },
                    "orderbook": market_data["orderbook"],
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
                        "adx": ta_data["adx"],
                        "fib_levels": ta_data["fib_levels"],
                        "volume_profile": ta_data["volume_profile"],
                        "market_condition": ta_data["market_condition"],
                    },
                    "onchain": {
                        "funding_rate": market_data["funding_rate"],
                        "open_interest": market_data["open_interest"],
                        "oi_change_pct": market_data["oi_change_pct"],
                        "liquidations": market_data["liquidations"],
                        "exchange_netflow": market_data["exchange_netflow"],
                    },
                    "sentiment": {
                        "fear_greed": market_data["fear_greed"],
                        "social": market_data["sentiment_social"],
                    },
                    "macro": market_data["macro"],
                    "confluence": confluence,
                    "signal": signal,
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
                print(f"[Data loop error] {e}")
                traceback.print_exc()

            await asyncio.sleep(15)

# ---------------------------------------------------------------------------
# Macro data fetcher (runs less frequently)
# ---------------------------------------------------------------------------

async def macro_loop():
    """Fetch macro data every 60 seconds."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Use Yahoo Finance for DXY, S&P, 10Y
                headers = {"User-Agent": "Mozilla/5.0"}

                # DXY
                try:
                    r = await client.get(
                        "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
                        params={"interval": "1d", "range": "2d"},
                        headers=headers,
                        timeout=10,
                    )
                    d = r.json()
                    meta = d.get("chart", {}).get("result", [{}])[0].get("meta", {})
                    market_data["macro"]["dxy"] = meta.get("regularMarketPrice", 0)
                    prev_close = meta.get("chartPreviousClose", meta.get("regularMarketPrice", 0))
                    if prev_close:
                        market_data["macro"]["dxy_change"] = round(
                            (meta.get("regularMarketPrice", 0) - prev_close) / prev_close * 100, 2
                        )
                except Exception as e:
                    print(f"[Macro DXY] {e}")

                # S&P 500
                try:
                    r = await client.get(
                        "https://query1.finance.yahoo.com/v8/finance/chart/ES=F",
                        params={"interval": "1d", "range": "2d"},
                        headers=headers,
                        timeout=10,
                    )
                    d = r.json()
                    meta = d.get("chart", {}).get("result", [{}])[0].get("meta", {})
                    market_data["macro"]["sp500"] = meta.get("regularMarketPrice", 0)
                    prev_close = meta.get("chartPreviousClose", meta.get("regularMarketPrice", 0))
                    if prev_close:
                        market_data["macro"]["sp500_change"] = round(
                            (meta.get("regularMarketPrice", 0) - prev_close) / prev_close * 100, 2
                        )
                except Exception as e:
                    print(f"[Macro SP500] {e}")

                # 10Y Treasury
                try:
                    r = await client.get(
                        "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX",
                        params={"interval": "1d", "range": "2d"},
                        headers=headers,
                        timeout=10,
                    )
                    d = r.json()
                    meta = d.get("chart", {}).get("result", [{}])[0].get("meta", {})
                    market_data["macro"]["us10y"] = meta.get("regularMarketPrice", 0)
                    prev_close = meta.get("chartPreviousClose", meta.get("regularMarketPrice", 0))
                    if prev_close:
                        market_data["macro"]["us10y_change"] = round(
                            meta.get("regularMarketPrice", 0) - prev_close, 3
                        )
                except Exception as e:
                    print(f"[Macro 10Y] {e}")

            except Exception as e:
                print(f"[Macro loop error] {e}")

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
    clean = []
    for s in signals_log:
        c = {k: v for k, v in s.items() if not k.startswith("_")}
        clean.append(c)
    return clean

@app.get("/api/settings")
async def get_settings():
    return settings

@app.on_event("startup")
async def startup():
    asyncio.create_task(data_loop())
    asyncio.create_task(macro_loop())

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
