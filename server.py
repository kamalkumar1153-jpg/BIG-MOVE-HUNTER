import os
import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
PAPER_FILE = DATA_DIR / "paper_trades.json"
PAPER_STATE_FILE = DATA_DIR / "paper_state.json"

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

UPSTOX_BASE = "https://api.upstox.com"
# Verify these instrument keys against the latest Upstox instruments list for your account.
INSTRUMENTS = {
    "NIFTY": {"name": "NIFTY 50", "key": os.getenv("UPSTOX_NIFTY_KEY", "NSE_INDEX|Nifty 50"), "option_key": os.getenv("UPSTOX_NIFTY_KEY", "NSE_INDEX|Nifty 50")},
    "BANKNIFTY": {"name": "BANK NIFTY", "key": os.getenv("UPSTOX_BANKNIFTY_KEY", "NSE_INDEX|Nifty Bank"), "option_key": os.getenv("UPSTOX_BANKNIFTY_KEY", "NSE_INDEX|Nifty Bank")},
    "SENSEX": {"name": "SENSEX", "key": os.getenv("UPSTOX_SENSEX_KEY", ""), "option_key": os.getenv("UPSTOX_SENSEX_KEY", "")},
}
REQUEST_TIMEOUT = 12
cache = {}
cache_lock = threading.Lock()

def token():
    return os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

def api_get(path, params=None):
    access = token()
    if not access:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN is not configured on the server.")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {access}"}
    r = requests.get(UPSTOX_BASE + path, headers=headers, params=params or {}, timeout=REQUEST_TIMEOUT)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:300]
        raise RuntimeError(f"Upstox API HTTP {r.status_code}: {detail}")
    payload = r.json()
    if payload.get("status") == "error":
        raise RuntimeError(str(payload))
    return payload

def cache_get(key, ttl, fn):
    now = time.time()
    with cache_lock:
        entry = cache.get(key)
        if entry and now - entry["at"] < ttl:
            return entry["value"]
    value = fn()
    with cache_lock:
        cache[key] = {"at": now, "value": value}
    return value

def safe_num(v, default=0):
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default

def load_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback

def save_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def get_quotes():
    keys = [v["key"] for v in INSTRUMENTS.values() if v["key"]]
    if not keys:
        raise RuntimeError("No index instrument keys configured. Set the UPSTOX_*_KEY environment variables.")
    payload = cache_get("quotes:" + ",".join(keys), 2, lambda: api_get("/v2/market-quote/ltp", {"instrument_key": ",".join(keys)}))
    data = payload.get("data", {})
    result = {}
    for symbol, info in INSTRUMENTS.items():
        key = info["key"]
        if not key:
            result[symbol] = {"name": info["name"], "available": False, "message": "Set the correct Upstox instrument key."}
            continue
        row = data.get(key) or data.get(quote(key, safe=""))
        if not row:
            # API response keys may be normalized; find the matching instrument_token field.
            row = next((v for v in data.values() if v.get("instrument_token") == key), None)
        if row:
            ltp = safe_num(row.get("last_price"))
            close = safe_num(row.get("cp") or row.get("close_price"))
            result[symbol] = {
                "name": info["name"], "available": True, "ltp": ltp,
                "change": round(ltp - close, 2) if close else None,
                "change_pct": round((ltp - close) / close * 100, 2) if close else None,
                "instrument_key": key
            }
        else:
            result[symbol] = {"name": info["name"], "available": False, "message": "No quote returned for this instrument key."}
    return {"data": result, "timestamp": datetime.now(timezone.utc).isoformat(), "source": "Upstox REST API"}

@app.get("/")
def index():
    return render_template("dashboard.html")

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "token_configured": bool(token()), "time": datetime.now(timezone.utc).isoformat()})

@app.get("/api/market")
def market():
    try:
        return jsonify(get_quotes())
    except Exception as e:
        return jsonify({"error": str(e), "data": {}, "source": "Upstox API"}), 503

@app.get("/api/option-chain")
def option_chain():
    symbol = request.args.get("symbol", "NIFTY").upper()
    expiry = request.args.get("expiry", "").strip()
    info = INSTRUMENTS.get(symbol)
    if not info:
        return jsonify({"error": "Unsupported symbol"}), 400
    if not info["option_key"]:
        return jsonify({"error": f"Configure a valid Upstox instrument key for {symbol}. Check the latest instrument list."}), 400
    if not expiry:
        return jsonify({"error": "Choose an expiry date (YYYY-MM-DD) from the available expiries."}), 400
    try:
        payload = cache_get(f"chain:{symbol}:{expiry}", 5, lambda: api_get("/v2/option/chain", {"instrument_key": info["option_key"], "expiry_date": expiry}))
        rows = payload.get("data", [])
        return jsonify({"data": rows, "timestamp": datetime.now(timezone.utc).isoformat(), "source": "Upstox option-chain REST API"})
    except Exception as e:
        return jsonify({"error": str(e)}), 503

@app.get("/api/expiries")
def expiries():
    symbol = request.args.get("symbol", "NIFTY").upper()
    info = INSTRUMENTS.get(symbol)
    if not info or not info["option_key"]:
        return jsonify({"error": "No valid instrument key configured for this symbol."}), 400
    try:
        payload = cache_get(f"contracts:{symbol}", 60, lambda: api_get("/v2/option/contract", {"instrument_key": info["option_key"]}))
        data = payload.get("data", [])
        dates = sorted({str(item.get("expiry"))[:10] for item in data if item.get("expiry")})
        return jsonify({"data": dates, "source": "Upstox option-contract API"})
    except Exception as e:
        return jsonify({"error": str(e)}), 503

@app.get("/api/candles")
def candles():
    """Historical candles from Upstox; latest availability depends on instrument and API entitlement."""
    symbol = request.args.get("symbol", "NIFTY").upper()
    interval = request.args.get("interval", "1")
    info = INSTRUMENTS.get(symbol)
    if not info or not info["key"]:
        return jsonify({"error": "No valid instrument key configured for this symbol."}), 400
    if interval not in {"1", "3", "5", "15", "30", "day"}:
        return jsonify({"error": "Interval must be 1, 3, 5, 15, 30 or day."}), 400
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        instrument = quote(info["key"], safe="")
        if interval == "day":
            path = f"/v2/historical-candle/{instrument}/day/{today}"
        else:
            path = f"/v3/historical-candle/intraday/{instrument}/minutes/{interval}"
        payload = cache_get(f"candles:{symbol}:{interval}:{today}", 10, lambda: api_get(path))
        rows = payload.get("data", {}).get("candles", [])
        candles_out = []
        for row in rows:
            # Upstox candle: timestamp, open, high, low, close, volume, open_interest (optional)
            if len(row) >= 6:
                try:
                    dt = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                    candles_out.append({"time": int(dt.timestamp()), "open": safe_num(row[1]), "high": safe_num(row[2]), "low": safe_num(row[3]), "close": safe_num(row[4]), "volume": safe_num(row[5])})
                except Exception:
                    continue
        return jsonify({"data": candles_out, "timestamp": datetime.now(timezone.utc).isoformat(), "source": "Upstox historical candles"})
    except Exception as e:
        return jsonify({"error": str(e)}), 503

@app.get("/api/signals")
def signals():
    """Transparent rule-based educational signal from real candle data; not a profit guarantee."""
    symbol = request.args.get("symbol", "NIFTY").upper()
    try:
        raw = candles_for_signal(symbol)
        if len(raw) < 22:
            return jsonify({"error": "Not enough candle data yet to calculate a signal.", "signal": "WAIT"}), 200
        closes = [safe_num(x[4]) for x in raw]
        volumes = [safe_num(x[5]) for x in raw]
        ema9 = ema(closes, 9)
        ema20 = ema(closes, 20)
        rsi = calc_rsi(closes, 14)
        last = closes[-1]
        avgvol = sum(volumes[-20:]) / max(1, len(volumes[-20:]))
        volratio = volumes[-1] / avgvol if avgvol else 0
        score = 0
        reasons = []
        if ema9 > ema20:
            score += 1; reasons.append("EMA 9 is above EMA 20")
        else:
            score -= 1; reasons.append("EMA 9 is below EMA 20")
        if last > ema20:
            score += 1; reasons.append("Price is above EMA 20")
        else:
            score -= 1; reasons.append("Price is below EMA 20")
        if rsi >= 55:
            score += 1; reasons.append(f"RSI is supportive ({rsi:.1f})")
        elif rsi <= 45:
            score -= 1; reasons.append(f"RSI is weak ({rsi:.1f})")
        else:
            reasons.append(f"RSI is neutral ({rsi:.1f})")
        if volratio >= 1.2:
            reasons.append(f"Latest volume is {volratio:.1f}x recent average")
        signal = "BUY BIAS" if score >= 2 else ("SELL BIAS" if score <= -2 else "WAIT")
        return jsonify({"symbol": symbol, "signal": signal, "score": score, "last_close": last, "ema9": round(ema9, 2), "ema20": round(ema20, 2), "rsi": round(rsi, 1), "volume_ratio": round(volratio, 2), "reasons": reasons, "timestamp": datetime.now(timezone.utc).isoformat(), "note": "Rule-based analysis, not a prediction or guarantee. Confirm live market conditions before acting."})
    except Exception as e:
        return jsonify({"error": str(e), "signal": "WAIT"}), 503

def candles_for_signal(symbol):
    info = INSTRUMENTS[symbol]
    instrument = quote(info["key"], safe="")
    today = datetime.now().strftime("%Y-%m-%d")
    path = f"/v3/historical-candle/intraday/{instrument}/minutes/1"
    payload = cache_get(f"signal-candles:{symbol}:{today}", 10, lambda: api_get(path))
    return payload.get("data", {}).get("candles", [])

def ema(values, period):
    k = 2 / (period + 1)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1-k)
    return out

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(-period, 0):
        d = closes[i] - closes[i-1]
        gains.append(max(0, d)); losses.append(max(0, -d))
    avg_gain = sum(gains)/period
    avg_loss = sum(losses)/period
    if avg_loss == 0:
        return 100
    rs = avg_gain/avg_loss
    return 100 - 100/(1+rs)

@app.get("/api/paper")
def paper_state():
    state = load_json(PAPER_STATE_FILE, {"balance": 100000, "initial_balance": 100000})
    trades = load_json(PAPER_FILE, [])
    return jsonify({"state": state, "trades": trades})

@app.post("/api/paper/reset")
def paper_reset():
    body = request.get_json(silent=True) or {}
    balance = safe_num(body.get("balance"), 100000)
    if balance <= 0 or balance > 100000000:
        return jsonify({"error": "Balance must be between ₹1 and ₹10 crore."}), 400
    save_json(PAPER_STATE_FILE, {"balance": balance, "initial_balance": balance})
    save_json(PAPER_FILE, [])
    return jsonify({"ok": True, "balance": balance})

@app.post("/api/paper/trade")
def paper_trade():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol", "NIFTY")).upper()
    side = str(body.get("side", "BUY")).upper()
    qty = int(safe_num(body.get("quantity"), 1))
    entry = safe_num(body.get("entry"), 0)
    if symbol not in INSTRUMENTS or side not in {"BUY", "SELL"} or qty < 1 or qty > 100000 or entry <= 0:
        return jsonify({"error": "Invalid symbol, side, quantity, or entry price."}), 400
    state = load_json(PAPER_STATE_FILE, {"balance": 100000, "initial_balance": 100000})
    trades = load_json(PAPER_FILE, [])
    required = entry * qty
    if side == "BUY" and required > safe_num(state.get("balance")):
        return jsonify({"error": "Not enough virtual balance for this paper trade."}), 400
    trade = {"id": int(time.time()*1000), "symbol": symbol, "side": side, "quantity": qty, "entry": entry, "status": "OPEN", "created_at": datetime.now(timezone.utc).isoformat(), "exit": None, "pnl": None}
    trades.insert(0, trade)
    if side == "BUY":
        state["balance"] = round(safe_num(state.get("balance")) - required, 2)
    save_json(PAPER_STATE_FILE, state); save_json(PAPER_FILE, trades)
    return jsonify({"ok": True, "trade": trade, "state": state})

@app.post("/api/paper/close")
def paper_close():
    body = request.get_json(silent=True) or {}
    try:
        trade_id = int(body.get("id"))
    except (ValueError, TypeError):
        return jsonify({"error": "Trade id required."}), 400
    exit_price = safe_num(body.get("exit"), 0)
    if exit_price <= 0:
        return jsonify({"error": "Exit price must be greater than zero."}), 400
    state = load_json(PAPER_STATE_FILE, {"balance": 100000, "initial_balance": 100000})
    trades = load_json(PAPER_FILE, [])
    trade = next((t for t in trades if t["id"] == trade_id and t["status"] == "OPEN"), None)
    if not trade:
        return jsonify({"error": "Open paper trade not found."}), 404
    pnl = (exit_price - trade["entry"]) * trade["quantity"]
    if trade["side"] == "SELL":
        pnl = -pnl
    trade.update({"status": "CLOSED", "exit": exit_price, "pnl": round(pnl, 2), "closed_at": datetime.now(timezone.utc).isoformat()})
    state["balance"] = round(safe_num(state.get("balance")) + (trade["entry"]*trade["quantity"] + pnl if trade["side"] == "BUY" else pnl), 2)
    save_json(PAPER_STATE_FILE, state); save_json(PAPER_FILE, trades)
    return jsonify({"ok": True, "trade": trade, "state": state})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
