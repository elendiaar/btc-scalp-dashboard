"""
BTC Scalp Dashboard v2 — Backtester
Fetches 3 days of historical 1m klines from Binance,
simulates order flow from candle data, runs the v2 strategy,
and reports PnL, win rate, and max drawdown.

Usage:
    python backtest.py
    python backtest.py --exchange binance.com --days 5

Output:
    backtest_results.csv  — trade-by-trade log
    backtest_results.md   — summary report
"""

import argparse
import csv
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy not installed. Run: pip install numpy")
    sys.exit(1)


# ── CLI Args ──
parser = argparse.ArgumentParser(description="BTC Scalp Dashboard v2 Backtester")
parser.add_argument("--exchange", default="binance.us", help="binance.us or binance.com")
parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
parser.add_argument("--days", type=int, default=3, help="Days of history to fetch")
parser.add_argument("--fee", type=float, default=0.001, help="Fee rate (decimal)")
parser.add_argument("--slippage", type=float, default=0.0005, help="Slippage rate (decimal)")
args = parser.parse_args()

EXCHANGE = args.exchange
SYMBOL = args.symbol.upper()
DAYS = args.days
FEE_RATE = args.fee
SLIPPAGE_RATE = args.slippage

if EXCHANGE == "binance.com":
    REST_BASE = "https://api.binance.com"
else:
    REST_BASE = "https://api.binance.us"


# ── TA Functions (same as server.py) ──

def calc_ema(data, period):
    if len(data) < period:
        return [data[-1]] * len(data) if data else [0]
    ema = [sum(data[:period]) / period]
    k = 2 / (period + 1)
    for val in data[period:]:
        ema.append(val * k + ema[-1] * (1 - k))
    return ema


def calc_rsi(closes, period=14):
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


def calc_macd(closes, fast=12, slow=26, sig=9):
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


def calc_bollinger(closes, period=20, std_dev=2.0):
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


def calc_vwap(candles):
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


def calc_atr(candles, period=14):
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


def calc_adx(candles, period=14):
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
    atr_val = sum(tr_list[:period]) / period
    plus_di_val = sum(plus_dm[:period]) / period
    minus_di_val = sum(minus_dm[:period]) / period
    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period
        plus_di_val = (plus_di_val * (period - 1) + plus_dm[i]) / period
        minus_di_val = (minus_di_val * (period - 1) + minus_dm[i]) / period
    if atr_val == 0:
        return 0
    plus_di = 100 * plus_di_val / atr_val
    minus_di = 100 * minus_di_val / atr_val
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return 0
    return round(100 * abs(plus_di - minus_di) / di_sum, 1)


def determine_market_condition(adx, bb_width, atr_pct):
    if adx > 25 and bb_width > 4:
        return "Trending"
    elif adx < 20 and bb_width < 2.5:
        return "Choppy"
    else:
        return "Ranging"


# ── Mock Order Flow from Candle Data ──

def mock_orderflow(candles, idx):
    """Simulate order flow signals from candle data.
    We use price/volume heuristics since we don't have tick-level data."""
    if idx < 10:
        return {
            "cvd_trend": "flat", "cvd_divergence": "none",
            "lob_ratio": 1.0, "lob_imbalance_ema": 0.0,
            "tape_aggression": "balanced", "absorption_signal": "none",
            "spread_pct": 0.01, "spread_avg": 0.01,
        }

    window = candles[max(0, idx - 20):idx + 1]
    c = candles[idx]

    # CVD proxy: cumulative close-open direction weighted by volume
    cum_delta = 0
    for w in window:
        delta = (w["c"] - w["o"]) / max(abs(w["h"] - w["l"]), 0.01)
        cum_delta += delta * w["v"]

    recent_5 = candles[max(0, idx - 5):idx + 1]
    delta_recent = sum((w["c"] - w["o"]) / max(abs(w["h"] - w["l"]), 0.01) * w["v"] for w in recent_5)
    delta_older = sum((w["c"] - w["o"]) / max(abs(w["h"] - w["l"]), 0.01) * w["v"] for w in candles[max(0, idx - 10):max(0, idx - 5)])

    # Use directional bias from recent candles (more sensitive for backtest)
    recent_3 = candles[max(0, idx - 3):idx + 1]
    green_count = sum(1 for w in recent_3 if w["c"] > w["o"])
    red_count = sum(1 for w in recent_3 if w["c"] <= w["o"])

    if green_count >= 3 or delta_recent > 0:
        cvd_trend = "rising"
    elif red_count >= 3 or delta_recent < 0:
        cvd_trend = "falling"
    else:
        cvd_trend = "flat"

    # CVD divergence proxy
    cvd_divergence = "none"
    if idx >= 20:
        prices_first = [candles[i]["c"] for i in range(idx - 20, idx - 10)]
        prices_second = [candles[i]["c"] for i in range(idx - 10, idx + 1)]
        if min(prices_second) < min(prices_first) and delta_recent > delta_older:
            cvd_divergence = "bullish"
        elif max(prices_second) > max(prices_first) and delta_recent < delta_older:
            cvd_divergence = "bearish"

    # LOB ratio proxy: volume-weighted direction with wider window
    recent_8 = candles[max(0, idx - 8):idx + 1]
    buy_vol = sum(w["v"] for w in recent_8 if w["c"] > w["o"])
    sell_vol = sum(w["v"] for w in recent_8 if w["c"] <= w["o"])
    total_vol = buy_vol + sell_vol
    lob_ratio = buy_vol / sell_vol if sell_vol > 0 else 2.0
    lob_imb_ema = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0

    # Tape aggression (lowered thresholds for candle-based proxy)
    if total_vol > 0:
        buy_pct = buy_vol / total_vol
        tape = "buyers" if buy_pct > 0.55 else "sellers" if buy_pct < 0.45 else "balanced"
    else:
        tape = "balanced"

    # Absorption proxy: high volume + flat price
    absorption = "none"
    if idx >= 5:
        vol_5 = sum(w["v"] for w in recent_5)
        avg_5_vol = vol_5 / len(recent_5) if recent_5 else 0
        price_move = abs(c["c"] - recent_5[0]["o"]) / recent_5[0]["o"] * 100 if recent_5[0]["o"] else 0
        if vol_5 > avg_5_vol * 2 and price_move < 0.1:
            if sell_vol > buy_vol * 1.3:
                absorption = "bullish_absorption"
            elif buy_vol > sell_vol * 1.3:
                absorption = "bearish_absorption"

    # Spread proxy (constant small value — no real LOB in backtest)
    spread_pct = 0.01
    spread_avg = 0.01

    return {
        "cvd_trend": cvd_trend,
        "cvd_divergence": cvd_divergence,
        "lob_ratio": round(lob_ratio, 3),
        "lob_imbalance_ema": round(lob_imb_ema, 4),
        "tape_aggression": tape,
        "absorption_signal": absorption,
        "spread_pct": spread_pct,
        "spread_avg": spread_avg,
    }


# ── Confluence Scoring (mirrors server.py) ──

WEIGHTS = {
    "orderflow": 0.45, "technical": 0.20, "derivatives": 0.15,
    "sentiment": 0.05, "macro": 0.15,
}


def compute_confluence(candles, idx, of_data):
    price = candles[idx]["c"]
    if price == 0 or idx < 35:
        return {"score": 50, "direction": "neutral"}

    window = candles[max(0, idx - 100):idx + 1]
    closes = [c["c"] for c in window]

    # Order Flow score
    flow_score = 50
    if of_data["lob_imbalance_ema"] > 0.15:
        flow_score += 15
    elif of_data["lob_imbalance_ema"] < -0.15:
        flow_score -= 15

    if of_data["cvd_trend"] == "rising":
        flow_score += 12
    elif of_data["cvd_trend"] == "falling":
        flow_score -= 12

    if of_data["cvd_divergence"] == "bullish":
        flow_score += 10
    elif of_data["cvd_divergence"] == "bearish":
        flow_score -= 10

    if of_data["tape_aggression"] == "buyers":
        flow_score += 8
    elif of_data["tape_aggression"] == "sellers":
        flow_score -= 8

    if of_data["absorption_signal"] == "bullish_absorption":
        flow_score += 10
    elif of_data["absorption_signal"] == "bearish_absorption":
        flow_score -= 10

    flow_score = max(0, min(100, flow_score))

    # Technical score
    tech_score = 50
    rsi = calc_rsi(closes, 14)
    if rsi < 30:
        tech_score += 12
    elif rsi > 70:
        tech_score -= 12
    elif rsi < 45:
        tech_score += 4
    elif rsi > 55:
        tech_score -= 4

    emas = {}
    for period in [9, 21, 55, 200]:
        ema_vals = calc_ema(closes, period)
        if ema_vals:
            emas[period] = ema_vals[-1]

    if len(emas) >= 4:
        vals = [emas[p] for p in sorted(emas.keys())]
        if all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)):
            tech_score += 10
        elif all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)):
            tech_score -= 10

    macd = calc_macd(closes, 12, 26, 9)
    if macd["crossover"] == "bullish":
        tech_score += 8
    elif macd["crossover"] == "bearish":
        tech_score -= 8
    if macd["histogram"] > 0:
        tech_score += 2
    else:
        tech_score -= 2

    bb = calc_bollinger(closes, 20, 2.0)
    if price <= bb["lower"]:
        tech_score += 6
    elif price >= bb["upper"]:
        tech_score -= 6

    vwap, _, _ = calc_vwap(window[-50:])
    if vwap > 0:
        tech_score += 4 if price > vwap else -4

    tech_score = max(0, min(100, tech_score))

    # Derivatives, sentiment, macro = neutral (50) in backtest
    deriv_score = 50
    sent_score = 50
    macro_score = 50

    total = (
        flow_score * WEIGHTS["orderflow"]
        + tech_score * WEIGHTS["technical"]
        + deriv_score * WEIGHTS["derivatives"]
        + sent_score * WEIGHTS["sentiment"]
        + macro_score * WEIGHTS["macro"]
    )
    total = round(total, 1)

    direction = "neutral"
    if total >= 55:
        direction = "long"
    elif total <= 45:
        direction = "short"

    return {
        "score": total, "direction": direction,
        "flow_score": flow_score, "tech_score": tech_score,
    }


# ── Fetch Historical Data ──

def fetch_klines(symbol, interval, days, rest_base):
    """Fetch klines in chunks of 1000."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    all_candles = []

    print(f"Fetching {days} days of {interval} klines from {rest_base}...")
    with httpx.Client(timeout=30) as client:
        current = start_ms
        while current < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": current,
                "limit": 1000,
            }
            try:
                r = client.get(f"{rest_base}/api/v3/klines", params=params)
                r.raise_for_status()
                data = r.json()
                if not data:
                    break
                for k in data:
                    all_candles.append({
                        "t": int(k[0]),
                        "o": float(k[1]),
                        "h": float(k[2]),
                        "l": float(k[3]),
                        "c": float(k[4]),
                        "v": float(k[5]),
                    })
                current = int(data[-1][0]) + 60000  # next minute
                print(f"  Fetched {len(all_candles)} candles...")
                time.sleep(0.2)  # rate limit courtesy
            except Exception as e:
                print(f"  Error: {e}")
                break

    print(f"Total: {len(all_candles)} candles")
    return all_candles


# ── Backtest Engine ──

def aggregate_5m(candles_1m):
    """Aggregate 1m candles into 5m candles for TA (matches server.py behavior)."""
    candles_5m = []
    for i in range(0, len(candles_1m) - 4, 5):
        chunk = candles_1m[i:i+5]
        candles_5m.append({
            "t": chunk[0]["t"],
            "o": chunk[0]["o"],
            "h": max(c["h"] for c in chunk),
            "l": min(c["l"] for c in chunk),
            "c": chunk[-1]["c"],
            "v": sum(c["v"] for c in chunk),
        })
    return candles_5m


def run_backtest(candles):
    trades = []
    position = None  # {"direction", "entry", "sl", "tp1", "entry_idx"}
    last_signal_idx = -180  # cooldown tracker (3 min = ~3 candles at 1m)

    total_pnl = 0
    wins = 0
    losses = 0
    max_drawdown = 0
    peak_pnl = 0

    # Pre-compute 5m candles for ATR/BB/ADX (matches server.py which uses 5m)
    candles_5m = aggregate_5m(candles)

    print(f"\nRunning backtest on {len(candles)} 1m candles ({len(candles_5m)} 5m candles)...")
    print(f"Fee rate: {FEE_RATE}, Slippage: {SLIPPAGE_RATE}")
    print("-" * 60)

    for idx in range(35, len(candles)):
        c = candles[idx]
        price = c["c"]

        # Map 1m index to nearest 5m candle index
        idx_5m = min(idx // 5, len(candles_5m) - 1)

        # Check exit first if in position
        if position is not None:
            exit_reasons = []
            of_data = mock_orderflow(candles, idx)

            if position["direction"] == "LONG":
                if price <= position["sl"]:
                    exit_reasons.append("Stop loss hit")
                if price >= position["tp1"]:
                    exit_reasons.append("Take profit reached")
                if of_data["cvd_trend"] == "falling":
                    exit_reasons.append("CVD flipped bearish")
                if of_data["lob_imbalance_ema"] < -0.2:
                    exit_reasons.append("LOB reversed bearish")
            else:
                if price >= position["sl"]:
                    exit_reasons.append("Stop loss hit")
                if price <= position["tp1"]:
                    exit_reasons.append("Take profit reached")
                if of_data["cvd_trend"] == "rising":
                    exit_reasons.append("CVD flipped bullish")
                if of_data["lob_imbalance_ema"] > 0.2:
                    exit_reasons.append("LOB reversed bullish")

            # Also force exit after 30 candles (30 min max hold for scalping)
            if idx - position["entry_idx"] > 30:
                exit_reasons.append("Max hold time exceeded (30m)")

            if len(exit_reasons) >= 2 or (idx - position["entry_idx"] > 30 and len(exit_reasons) >= 1):
                # Calculate PnL
                if position["direction"] == "LONG":
                    raw_pnl = price - position["entry"]
                else:
                    raw_pnl = position["entry"] - price

                cost = 2 * (FEE_RATE + SLIPPAGE_RATE) * position["entry"]
                net_pnl = raw_pnl - cost
                pnl_pct = net_pnl / position["entry"] * 100

                total_pnl += net_pnl
                if net_pnl > 0:
                    wins += 1
                else:
                    losses += 1

                if total_pnl > peak_pnl:
                    peak_pnl = total_pnl
                dd = peak_pnl - total_pnl
                if dd > max_drawdown:
                    max_drawdown = dd

                ts = datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                trades.append({
                    "timestamp": ts,
                    "direction": position["direction"],
                    "entry_price": round(position["entry"], 2),
                    "exit_price": round(price, 2),
                    "pnl_usd": round(net_pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                    "cost_usd": round(cost, 2),
                    "hold_candles": idx - position["entry_idx"],
                    "exit_reason": "; ".join(exit_reasons),
                })

                position = None
                last_signal_idx = idx
                continue

        # Skip entry check if in position or cooling down
        if position is not None or idx - last_signal_idx < 3:
            continue

        # Compute signals
        of_data = mock_orderflow(candles, idx)
        confluence = compute_confluence(candles, idx, of_data)

        if confluence["score"] < 60:
            continue
        if confluence["direction"] == "neutral":
            continue

        # Market condition filter — use 5m candles for ATR/BB/ADX (matches server.py)
        window_5m = candles_5m[max(0, idx_5m - 100):idx_5m + 1]
        if len(window_5m) < 15:
            continue
        closes_5m = [cn["c"] for cn in window_5m]
        bb = calc_bollinger(closes_5m, 20, 2.0)
        adx_val = calc_adx(window_5m)
        atr_val = calc_atr(window_5m, 14)
        atr_pct = atr_val / price * 100 if price else 0
        condition = determine_market_condition(adx_val, bb["width"], atr_pct)

        if condition == "Choppy":
            continue

        # Order flow gate
        direction = confluence["direction"].upper()
        of_confirms = 0
        if direction == "LONG":
            if of_data["lob_ratio"] > 1.3: of_confirms += 1
            if of_data["cvd_trend"] == "rising": of_confirms += 1
            if of_data["absorption_signal"] == "bullish_absorption": of_confirms += 1
            if of_data["tape_aggression"] == "buyers": of_confirms += 1
            if of_data["cvd_divergence"] == "bullish": of_confirms += 1
        else:
            if of_data["lob_ratio"] < 0.77: of_confirms += 1
            if of_data["cvd_trend"] == "falling": of_confirms += 1
            if of_data["absorption_signal"] == "bearish_absorption": of_confirms += 1
            if of_data["tape_aggression"] == "sellers": of_confirms += 1
            if of_data["cvd_divergence"] == "bearish": of_confirms += 1

        if of_confirms == 0:
            continue

        # Transaction cost check — use 5m ATR
        min_move = 2 * (FEE_RATE + SLIPPAGE_RATE) * price
        if atr_val > 0 and atr_val < min_move:
            continue

        # Calculate levels using 5m ATR
        if atr_val == 0:
            atr_val = abs(bb["upper"] - bb["lower"]) / 4 if bb["upper"] else price * 0.005

        if direction == "LONG":
            entry = price
            sl = round(price - 1.5 * atr_val, 2)
            risk = entry - sl
            tp1 = round(entry + risk * 2.0, 2)
        else:
            entry = price
            sl = round(price + 1.5 * atr_val, 2)
            risk = sl - entry
            tp1 = round(entry - risk * 2.0, 2)

        position = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "entry_idx": idx,
        }
        last_signal_idx = idx

    # Close any open position at the end
    if position is not None:
        price = candles[-1]["c"]
        if position["direction"] == "LONG":
            raw_pnl = price - position["entry"]
        else:
            raw_pnl = position["entry"] - price
        cost = 2 * (FEE_RATE + SLIPPAGE_RATE) * position["entry"]
        net_pnl = raw_pnl - cost
        total_pnl += net_pnl
        if net_pnl > 0:
            wins += 1
        else:
            losses += 1
        ts = datetime.fromtimestamp(candles[-1]["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        trades.append({
            "timestamp": ts,
            "direction": position["direction"],
            "entry_price": round(position["entry"], 2),
            "exit_price": round(price, 2),
            "pnl_usd": round(net_pnl, 2),
            "pnl_pct": round(net_pnl / position["entry"] * 100, 4),
            "cost_usd": round(cost, 2),
            "hold_candles": len(candles) - 1 - position["entry_idx"],
            "exit_reason": "End of backtest",
        })

    return trades, total_pnl, wins, losses, max_drawdown


# ── Output ──

def save_results(trades, total_pnl, wins, losses, max_drawdown, candles):
    total_trades = wins + losses
    win_rate = wins / total_trades * 100 if total_trades else 0
    avg_pnl = total_pnl / total_trades if total_trades else 0
    start_price = candles[0]["c"]
    end_price = candles[-1]["c"]
    bnh_return = (end_price - start_price) / start_price * 100

    avg_win = 0
    avg_loss = 0
    if trades:
        winning = [t["pnl_usd"] for t in trades if t["pnl_usd"] > 0]
        losing = [t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0]
        avg_win = sum(winning) / len(winning) if winning else 0
        avg_loss = sum(losing) / len(losing) if losing else 0

    # CSV
    csv_path = os.path.join(os.path.dirname(__file__), "backtest_results.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "direction", "entry_price", "exit_price",
            "pnl_usd", "pnl_pct", "cost_usd", "hold_candles", "exit_reason",
        ])
        writer.writeheader()
        for t in trades:
            writer.writerow(t)
    print(f"\nTrade log saved to: {csv_path}")

    # Markdown report
    md_path = os.path.join(os.path.dirname(__file__), "backtest_results.md")
    start_ts = datetime.fromtimestamp(candles[0]["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    end_ts = datetime.fromtimestamp(candles[-1]["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    md = f"""# BTC Scalp Dashboard v2 — Backtest Results

## Configuration
| Parameter | Value |
|---|---|
| Exchange | {EXCHANGE} |
| Symbol | {SYMBOL} |
| Period | {start_ts} to {end_ts} |
| Candles | {len(candles)} x 1m |
| Fee Rate | {FEE_RATE} ({FEE_RATE*100:.2f}%) |
| Slippage Rate | {SLIPPAGE_RATE} ({SLIPPAGE_RATE*100:.2f}%) |
| Confluence Weights | OF: 45%, Tech: 20%, Deriv: 15%, Sent: 5%, Macro: 15% |

## Summary
| Metric | Value |
|---|---|
| Total Trades | {total_trades} |
| Wins | {wins} |
| Losses | {losses} |
| Win Rate | {win_rate:.1f}% |
| Total PnL | ${total_pnl:,.2f} |
| Avg PnL/Trade | ${avg_pnl:,.2f} |
| Avg Win | ${avg_win:,.2f} |
| Avg Loss | ${avg_loss:,.2f} |
| Max Drawdown | ${max_drawdown:,.2f} |
| Buy & Hold Return | {bnh_return:.2f}% (${start_price:,.2f} -> ${end_price:,.2f}) |

## Notes
- Order flow signals are **simulated** from candle data (no real tick-level LOB/tape data in backtest)
- Derivatives, sentiment, and macro scores are held at neutral (50) since no historical data
- Real-time performance may differ due to actual order flow quality and market microstructure
- Transaction costs (fee + slippage) are deducted from every trade
- Max hold time capped at 30 candles (30 minutes)

## Trade Log
| # | Time | Dir | Entry | Exit | PnL | PnL% | Cost | Hold | Reason |
|---|---|---|---|---|---|---|---|---|---|
"""
    for i, t in enumerate(trades, 1):
        md += f"| {i} | {t['timestamp']} | {t['direction']} | ${t['entry_price']:,.2f} | ${t['exit_price']:,.2f} | ${t['pnl_usd']:,.2f} | {t['pnl_pct']:.2f}% | ${t['cost_usd']:,.2f} | {t['hold_candles']}m | {t['exit_reason']} |\n"

    with open(md_path, "w") as f:
        f.write(md)
    print(f"Report saved to: {md_path}")

    # Print summary
    print("\n" + "=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)
    print(f"Period:          {start_ts} to {end_ts}")
    print(f"Total Trades:    {total_trades}")
    print(f"Win Rate:        {win_rate:.1f}% ({wins}W / {losses}L)")
    print(f"Total PnL:       ${total_pnl:,.2f}")
    print(f"Avg PnL/Trade:   ${avg_pnl:,.2f}")
    print(f"Max Drawdown:    ${max_drawdown:,.2f}")
    print(f"Buy & Hold:      {bnh_return:.2f}%")
    print("=" * 60)


# ── Main ──

def main():
    candles = fetch_klines(SYMBOL, "1m", DAYS, REST_BASE)
    if len(candles) < 100:
        print(f"ERROR: Only {len(candles)} candles fetched. Need at least 100.")
        sys.exit(1)

    trades, total_pnl, wins, losses, max_dd = run_backtest(candles)
    save_results(trades, total_pnl, wins, losses, max_dd, candles)


if __name__ == "__main__":
    main()
