"""
===========================================================
 INTRADAY LIVE TRACKER (V9.0)
===========================================================
9:30 AM wale intraday picks ka live price check — har 5 min
(9:30-15:00). Jab kisi pick ka entry zone hit ho, target hit
ho, ya SL hit ho, to sharp Telegram alert bhejta hai.

Features:
  - Entry zone hit → "🟢 ENTRY NOW: RELIANCE @ ₹2945 (zone 2940-2955)"
  - Target hit → "🎉 TARGET HIT: RELIANCE T1 ₹3010 hit!"
  - SL hit → "🛑 SL HIT: RELIANCE @ ₹2890"
  - Dedup: ek hi stock-event ke liye dobara alert nahi
  - Intraday picks ek hi din ke hote hain (next day fresh)

State: data/intraday_alerts_state.json mein per-stock per-event
alert-sent flags store hote hain (atomic write, crash-safe).
===========================================================
"""

import os
import json
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from config import (
    INTRADAY_LIVE_ALERT_ENABLED, INTRADAY_LIVE_ALERT_INTERVAL,
    INTRADAY_LIVE_ALERT_START, INTRADAY_LIVE_ALERT_END,
    DATA_DIR,
)
from utils import escape_html, atomic_write_json
from logger import logger

IST = ZoneInfo("Asia/Kolkata")

_STATE_FILE = os.path.join(DATA_DIR, "intraday_alerts_state.json")
_state_lock = threading.Lock()


def _load_state():
    """Load alert-sent state. Returns dict {stock: {event: True}}."""
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logger.debug(f"intraday state load fail: {e}")
        return {}


def _save_state(state):
    """Atomically save alert-sent state."""
    try:
        state["_date"] = datetime.now(IST).strftime("%Y-%m-%d")
        atomic_write_json(_STATE_FILE, state)
    except Exception as e:
        logger.warning(f"intraday state save fail: {e}")


def _is_new_day(state):
    """Check if state is from a previous day (reset needed)."""
    stored_date = state.get("_date")
    if not stored_date:
        return True
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return stored_date != today


def _in_market_hours():
    """Check if current time is within intraday alert window (IST)."""
    now = datetime.now(IST)
    # Weekend check
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    hhmm = now.strftime("%H:%M")
    return INTRADAY_LIVE_ALERT_START <= hhmm <= INTRADAY_LIVE_ALERT_END


def _get_intraday_picks():
    """
    Aaj ke intraday picks fetch karta hai.
    Intraday picks 9:30 AM scan se aate hain — yahan DB ya
    in-memory cache se laata hain.

    Format: list of {
        "stock": "RELIANCE.NS",
        "entry": 2940,
        "entry_low": 2940,
        "entry_high": 2955,
        "sl": 2890,
        "target": 3010,
    }
    """
    # Try in-memory cache first (intraday_scanner sets this)
    try:
        import intraday_scanner
        if hasattr(intraday_scanner, "_last_picks_cache"):
            picks = intraday_scanner._last_picks_cache
            if picks:
                return picks
    except Exception:
        pass

    # Fallback: try file-based cache
    cache_file = os.path.join(DATA_DIR, "intraday_picks_today.json")
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Check date
            if data.get("_date") == datetime.now(IST).strftime("%Y-%m-%d"):
                return data.get("picks", [])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    except Exception as e:
        logger.debug(f"intraday picks cache read fail: {e}")

    return []


def _fetch_live_price(symbol):
    """Single stock ka live price (Yahoo Direct, fast)."""
    try:
        from market_data_fetcher import _fetch_yahoo_direct
        df = _fetch_yahoo_direct(symbol, period="1d")
        if df is not None and not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception as e:
        logger.debug(f"intraday live price {symbol} fail: {e}")
    return None


def _send_alert(text):
    """Telegram par alert bhejta hai."""
    try:
        from telegram_alerts import send_telegram_text
        send_telegram_text(text)
    except Exception as e:
        logger.warning(f"intraday alert send fail: {e}")


def check_intraday_picks():
    """
    MAIN ENTRY: Aaj ke intraday picks ka live price check karke
    entry/target/SL hit hone par alert bhejta hai.

    Called by scheduler every INTRADAY_LIVE_ALERT_INTERVAL minutes.
    """
    if not INTRADAY_LIVE_ALERT_ENABLED:
        return

    if not _in_market_hours():
        return

    picks = _get_intraday_picks()
    if not picks:
        logger.debug("Intraday tracker: aaj koi picks nahi milhe")
        return

    # Load + reset state if new day
    with _state_lock:
        state = _load_state()
        if _is_new_day(state):
            state = {}
            logger.info("Intraday tracker: naya din, alert state reset")

    alerts_sent = 0
    for pick in picks:
        stock = pick.get("stock", "")
        if not stock:
            continue

        # State key: stock -> {entry_sent, target_sent, sl_sent}
        stock_state = state.get(stock, {})

        live_price = _fetch_live_price(stock)
        if live_price is None:
            continue

        entry_low = pick.get("entry_low") or pick.get("entry")
        entry_high = pick.get("entry_high") or pick.get("entry")
        sl = pick.get("sl")
        target = pick.get("target")

        display = escape_html(stock.replace(".NS", "").replace(".BO", ""))

        # 1. ENTRY ZONE HIT
        if not stock_state.get("entry_sent") and entry_low and entry_high:
            if entry_low <= live_price <= entry_high:
                _send_alert(
                    f"🟢 <b>ENTRY NOW</b> — <b>{display}</b>\n"
                    f"💵 Price: ₹{live_price:.2f} (entry zone ₹{entry_low}-{entry_high})\n"
                    f"🎯 Target: ₹{target} | 🛑 SL: ₹{sl}"
                )
                stock_state["entry_sent"] = True
                alerts_sent += 1

        # 2. TARGET HIT
        if not stock_state.get("target_sent") and target and live_price >= target:
            _send_alert(
                f"🎉 <b>TARGET HIT</b> — <b>{display}</b>\n"
                f"💵 Price: ₹{live_price:.2f} (target ₹{target})\n"
                f"✅ Profit book kar sakte ho!"
            )
            stock_state["target_sent"] = True
            alerts_sent += 1

        # 3. SL HIT
        if not stock_state.get("sl_sent") and sl and live_price <= sl:
            _send_alert(
                f"🛑 <b>SL HIT</b> — <b>{display}</b>\n"
                f"💵 Price: ₹{live_price:.2f} (SL ₹{sl})\n"
                f"❌ Exit kar do, loss book karo."
            )
            stock_state["sl_sent"] = True
            alerts_sent += 1

        state[stock] = stock_state

    # Save state
    with _state_lock:
        _save_state(state)

    if alerts_sent > 0:
        logger.info(f"📡 Intraday tracker: {alerts_sent} alert(s) bheje")


def run_intraday_alert_loop():
    """
    Background thread loop — har INTRADAY_LIVE_ALERT_INTERVAL
    minutes mein check_intraday_picks() call karta hai.

    Market hours ke bahad automatically sleep karta hai.
    """
    logger.info(
        f"📡 Intraday live alert tracker shuru "
        f"(har {INTRADAY_LIVE_ALERT_INTERVAL} min, {INTRADAY_LIVE_ALERT_START}-{INTRADAY_LIVE_ALERT_END} IST)"
    )
    interval_sec = INTRADAY_LIVE_ALERT_INTERVAL * 60

    while True:
        try:
            check_intraday_picks()
        except Exception as e:
            logger.error(f"Intraday alert loop error: {e}")
        time.sleep(interval_sec)
