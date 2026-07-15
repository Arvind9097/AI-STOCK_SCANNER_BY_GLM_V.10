"""
===========================================================
 TELEGRAM INTERACTIVE BOT (polling) - GROUP & CHANNEL FIXED
===========================================================
* Supports both Groups ('message') and Channels ('channel_post').
* Handles local network/ISP timeouts gracefully.

V8.2.0 FIXES (Task F5):
  1. `offset` ab data/bot_offset.json mein persist hota hai (atomic write).
     Restart/Render-deploy ke baad duplicate replies nahi honge, aur
     downtime ke dauran >100 pending updates bhi lost nahi honge.
  2. `/rerun <name>` ke liye threading.Lock-guarded registry - do
     concurrent `/rerun scan` ab parallel pipelines nahi chalayenge
     (duplicate DB inserts, duplicate Telegram sends, resume_state
     corruption - sab fix).
  3. `reply_to_telegram` ab long messages ko `chunk_text` se 3800-char
     chunks mein tod kar alag-alag bhejta hai - Telegram 4096-char hard
     limit se reject hone ka issue gaya. Master Dashboard ab safely
     bheja jaata hai (pehle silently drop ho jaata tha).
  4. UNKNOWN-intent fallback ab raw user message ko stock-ticker ki
     tarah treat nahi karta - "thanks"/"ok"/"market kaisa lagega kal"
     jaise inputs ko help message reply karta hai. Sirf short
     alphanumeric text ko stock-name maan ke lookup karta hai.
  5. SIGTERM/SIGINT graceful shutdown - polling loop cleanly exit
     karta hai, in-progress Telegram sends complete ho jaate hain.
  6. IST timezone use kiya gaya hai (today's picks date etc.).
===========================================================
"""

import json
import os
import signal
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from tracker import (
    check_live_market_hits, generate_daily_performance_report,
    generate_weekly_report, generate_monthly_report,
    generate_target_hit_stocks, generate_best_rr_stocks,
)
from database import get_db_connection
from stock_lookup import get_stock_snapshot
from utils import escape_html, atomic_write_json, chunk_text
from nlu import parse_intent
from logger import logger

# V9.0: AI Brain (GLM) integration - conversational replies ke liye.
# Module import fail hone par (e.g. ZAI_API_KEY/config issue) bot
# rule-based mode mein gracefully chalta rahe (ask_ai = None).
try:
    from ai_brain import ask_ai
    _AI_BRAIN_AVAILABLE = True
except Exception as _ai_brain_err:
    logger.warning(f"AI Brain import fail - rule-based fallback active: {_ai_brain_err}")
    ask_ai = None
    _AI_BRAIN_AVAILABLE = False

# V9.0: AI Brain enable flag (config.py). Guarded import - agar config
# mein flag define na ho to False (rule-based mode).
try:
    from config import AI_BRAIN_ENABLED
except Exception:
    AI_BRAIN_ENABLED = False

# V9.0: Intraday live alert flag - bot startup par intraday_tracker
# background thread start karne ke liye.
try:
    from config import INTRADAY_LIVE_ALERT_ENABLED
except Exception:
    INTRADAY_LIVE_ALERT_ENABLED = False

# V8.2.0: IST for date-sensitive queries (today's picks etc.)
IST = ZoneInfo("Asia/Kolkata")

# V8.1.2 NAYA: "/rerun <name>" command - agar koi scheduled slot
# (deploy/restart ki wajah se) beech mein cut ho jaaye ya miss ho
# jaaye, Telegram se hi usse turant dobara trigger kiya ja sakta hai
# (Render Shell ki zaroorat nahi). dispatch_state.py ka lock hamesha
# disabled hai, isliye ye safely "--force" jaisa hi kaam karta hai.
#
# main.py ke functions ko yahan LAZY-IMPORT karte hain (function ke
# andar, top-level par nahi) - taaki circular-import na ho (main.py
# khud bot_listener.py ko import karta hai --schedule mode mein).
_RERUN_COMMANDS = {
    "morning": "Morning Briefing",
    "scan": "Swing Scan",
    "intraday": "Intraday Scan",
    "swing": "Swing Chart Digest",
    "closebuys": "Close-Buys (3 PM)",
    "btst": "BTST Scan",
    "report": "Daily Performance Report",
    "evening": "Evening Summary",
}

# V8.2.0: concurrent /rerun race-condition fix - ek hi name ke
# concurrent reruns ko block karta hai. Thread-safe registry.
_rerun_lock = threading.Lock()
_active_reruns = {}  # name -> threading.Thread

# V8.2.0: Health-server deep check ke liye - har polling iteration par
# update hota hai. health_server.py ka /ping ise read karke 503 return
# karta hai agar 10 min se stale ho (bot-listener thread deadlock/crash).
_last_bot_tick = None
_bot_tick_lock = threading.Lock()


def get_last_bot_tick():
    """health_server.py se read hota hai - None ya >10 min purana = unhealthy."""
    with _bot_tick_lock:
        return _last_bot_tick


def _heartbeat():
    global _last_bot_tick
    with _bot_tick_lock:
        _last_bot_tick = datetime.now(IST)


# V8.2.0: graceful shutdown flag - SIGTERM/SIGINT polling loop exit karwate hain.
_shutdown_requested = False

# V8.2.0: Telegram update offset persistence file (atomic write).
# Restart/Render-deploy ke baad duplicate replies prevent karta hai.
_OFFSET_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "site-packages" in __file__ else os.getcwd(),
    "data", "bot_offset.json",
)
# Simpler & robust - use DATA_DIR from config if available
try:
    from config import DATA_DIR
    _OFFSET_FILE = os.path.join(DATA_DIR, "bot_offset.json")
except Exception:
    pass


def _load_offset():
    """V8.2.0: data/bot_offset.json se last offset+1 load karta hai (atomic read)."""
    try:
        if not os.path.exists(_OFFSET_FILE):
            return None
        with open(_OFFSET_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("offset")
    except Exception as e:
        logger.warning(f"bot_offset.json read fail, None se shuru: {e}")
        return None


def _save_offset(offset):
    """V8.2.0: offset ko atomically file mein save karta hai (crash-safe)."""
    if offset is None:
        return
    try:
        atomic_write_json(_OFFSET_FILE, {"offset": offset, "saved_at": datetime.now(IST).isoformat()})
    except Exception as e:
        logger.warning(f"bot_offset.json save fail: {e}")


def _request_shutdown(signum, frame):
    """V8.2.0: SIGTERM/SIGINT par polling loop ko cleanly exit karne bolta hai."""
    global _shutdown_requested
    name = signal.Signals(signum).name if signum else "?"
    if not _shutdown_requested:
        logger.info(f"[bot_listener] {name} mila - polling loop ko gracefully rokkar exit karunga")
        _shutdown_requested = True
    else:
        logger.warning(f"[bot_listener] {name} dobara mila - force exit")
        raise KeyboardInterrupt()


def _install_signal_handlers():
    """V8.2.0: SIGTERM/SIGINT dono ko graceful handler se route karo (main thread only)."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_shutdown)
        except (ValueError, OSError):
            pass  # non-main thread - skip


def _run_rerun_in_background(name, chat_id):
    """
    Background thread mein actual pipeline-function chalata hai (jo
    10-30+ minute tak le sakta hai bulk scan ke liye) - Telegram
    listener-loop ko block nahi karta. Complete hone par khud
    Telegram par result bhej deta hai (jaisa scheduled-run karta).

    V8.2.0: finally-block mein _active_reruns[name] ko hata deta hai
    taaki same name ka next /rerun block na ho.
    """
    try:
        import main as main_module  # lazy import - circular-import se bachne ke liye

        pipeline_map = {
            "morning": main_module.send_morning_briefing,
            "scan": main_module.run_scan_pipeline,
            "intraday": main_module.run_intraday_scan_pipeline,
            "swing": main_module.run_swing_chart_digest_pipeline,
            "closebuys": main_module.run_close_bestbuys_pipeline,
            "btst": main_module.run_btst_scan_pipeline,
            "evening": main_module.send_evening_summary,
        }

        # V8.1.2 IMPORTANT: "report" (generate_daily_performance_report,
        # tracker.py) sirf TEXT RETURN karta hai, khud Telegram nahi
        # bhejta - isliye ise explicitly reply_to_telegram() se bhejna
        # padta hai. Baaki saare (morning/scan/intraday/swing/closebuys/
        # btst/evening) khud APNE ANDAR HI Telegram par bhej dete hain
        # (send_chunked_telegram_report waghera call karte hain) aur
        # None return karte hain - unhe seedha call karna hi kaafi hai,
        # dobara reply_to_telegram() karna DUPLICATE message bhej dega.
        if name == "report":
            reply_to_telegram(chat_id, generate_daily_performance_report())
            logger.info(f"/rerun {name}: complete ho gaya")
            return

        func = pipeline_map.get(name)
        if func is None:
            return

        logger.info(f"/rerun {name}: background thread mein shuru ho raha hai (chat_id={chat_id})")
        func(force=True)
        logger.info(f"/rerun {name}: complete ho gaya")

    except Exception as e:
        logger.error(f"/rerun {name}: fail ho gaya ({e})")
        try:
            reply_to_telegram(chat_id, f"'{name}' rerun karte waqt error aaya: {e}")
        except Exception:
            pass
    finally:
        # V8.2.0: registry se hata do taaki same name ka next /rerun block na ho
        with _rerun_lock:
            _active_reruns.pop(name, None)


def handle_rerun_command(chat_id, user_message):
    """
    "/rerun <name>" ya "rerun <name>" parse karta hai. Valid name na
    ho to available options dikhata hai. Valid ho to background
    thread mein turant start kar deta hai aur "shuru ho gaya" confirm
    karta hai (poora result thoda der baad khud aayega).

    V8.2.0: Concurrent /rerun race-condition fix - agar same name ka
    rerun pehle se chal raha hai, "already running, please wait" reply
    karta hai. Do parallel pipelines (duplicate DB inserts, duplicate
    Telegram sends, resume_state corruption) ab prevent hote hain.
    """
    parts = user_message.strip().split(maxsplit=1)
    if len(parts) < 2:
        options = ", ".join(_RERUN_COMMANDS.keys())
        reply_to_telegram(
            chat_id,
            f"Kaunsa slot dobara chalana hai bataiye. Example: '/rerun swing'\n"
            f"Available: {options}"
        )
        return

    name = parts[1].strip().lower()
    if name not in _RERUN_COMMANDS:
        options = ", ".join(_RERUN_COMMANDS.keys())
        reply_to_telegram(chat_id, f"'{name}' pehchana nahi gaya. Available: {options}")
        return

    # V8.2.0: concurrent /rerun guard - same name ka rerun already
    # chal raha hai to block kar do, alag thread spawn mat karo.
    with _rerun_lock:
        existing = _active_reruns.get(name)
        if existing is not None and existing.is_alive():
            reply_to_telegram(
                chat_id,
                f"⏳ {_RERUN_COMMANDS[name]} pehle se chal raha hai background mein. "
                f"Thodi der wait karo, complete hone par result aayega."
            )
            return

        # Naya thread spawn karo aur registry mein daal do
        t = threading.Thread(
            target=_run_rerun_in_background,
            args=(name, chat_id),
            daemon=True,
            name=f"Rerun-{name}",
        )
        _active_reruns[name] = t

    reply_to_telegram(
        chat_id,
        f"🔄 {_RERUN_COMMANDS[name]} dobara shuru ho raha hai background mein... "
        f"Result thodi der mein yahi aayega."
    )
    t.start()

# V8.1.2 SECURITY FIX: Hardcoded leaked-token fallback yahan se
# PERMANENTLY hata diya gaya hai. config.py khud TELEGRAM_BOT_TOKEN
# missing hone par startup par hi error deke exit ho jaata hai.
from config import TELEGRAM_BOT_TOKEN
BOT_TOKEN = TELEGRAM_BOT_TOKEN

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"


def clear_old_webhook():
    """Script start hote hi purane webhook ko automatic delete karne ke liye"""
    try:
        url = BASE_URL + "deleteWebhook"
        res = requests.get(url, timeout=10).json()
        if res.get("ok"):
            logger.info("Purana Telegram Webhook successfully clean kar diya gaya hai.")
        else:
            logger.warning(f"Webhook clear status: {res.get('description')}")
    except Exception as e:
        logger.error(f"Webhook clean karne me error: {e}")


# V8.3.1: last non-200 status code — 409 Conflict detection ke liye.
# get_bot_updates isse set karta hai, run_listener_loop ise read karke
# longer backoff leta hai (taaki redeploy ke dauran old+new instance
# conflict mein 1/sec spam na karein).
_last_getupdates_status = 200


def get_bot_updates(offset=None):
    """Telegram server se updates lene ke liye (handles ISP drop/timeout gracefully)"""
    global _last_getupdates_status
    try:
        url = BASE_URL + "getUpdates?timeout=20"
        if offset:
            url += f"&offset={offset}"
        response = requests.get(url, timeout=30)
        _last_getupdates_status = response.status_code

        if response.status_code != 200:
            # V8.2.0: log status + truncated body - 401/409/429 errors visible
            # V8.3.1: 409 Conflict (two bot instances during redeploy) ko
            # specially mark karte hain — run_listener_loop longer backoff
            # lega taaki old instance ko marne ka time mile.
            if response.status_code == 409:
                logger.warning(
                    "getUpdates 409 Conflict — do bot instances ek saath chal rahe hain "
                    "(redeploy?). 10s wait kar raha hoon taaki purana instance exit ho jaaye."
                )
            else:
                logger.warning(f"getUpdates non-200 (status {response.status_code}): {response.text[:200]}")
            return None

        data = response.json()
        if not data.get("ok"):
            return None

        return data
    except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout):
        # Normal timeout due to long polling or ISP drop, silently ignore to prevent log spam
        return None
    except Exception as e:
        logger.warning(f"Telegram connection issue (retrying): {e}")
        return None


def reply_to_telegram(chat_id, text_message):
    """
    Group/Channel ya User ko text message bhejne ke liye helper function.

    V8.2.0: Long messages (>3800 chars) ko `chunk_text` se tod kar
    multiple sendMessage calls mein bhejta hai. Telegram ka hard 4096-
    char limit pehle silently reject kar deta tha (Master Dashboard,
    Daily Report jaise long messages). Ab safely multi-part bhejta hai.
    """
    if text_message is None:
        return
    text_str = str(text_message)
    try:
        # V8.2.0: chunk_text line-boundaries par todta hai - clean output
        chunks = chunk_text(text_str, max_length=3800)
        for chunk in chunks:
            url = BASE_URL + "sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            response = requests.post(url, json=payload, timeout=20)
            if response.status_code != 200:
                # V8.2.0: truncated response.text - chat_id/message content leak prevent
                logger.error(f"Reply fail (Chat ID: {chat_id}): {response.text[:200]}")
                # ek chunk fail hua to baaki chunks bhejne ka matlab nahi
                break
    except Exception as e:
        logger.error(f"Reply bhejne me network error: {e}")


def send_inline_menu(chat_id):
    """Button menu bhejne ke liye function (V9.0: 12 buttons, 4 rows × 3).

    V9.0 NAYA: Pehle sirf 5 buttons the (Monitor/Report/Today/PDF/Dashboard).
    Ab 12 buttons - har trading scenario cover karta hai:
      Row 1: Top Picks | Live Status | Intraday Picks
      Row 2: Swing Breakout | BTST Picks | Active Trades
      Row 3: Target Hit | Best R:R | Latest News
      Row 4: PDF Report | Performance Report | Master Dashboard
    """
    try:
        url = BASE_URL + "sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": (
                "<b>AI Stock Scanner - Menu</b>\n\n"
                "Aapko kya check karna hai? Niche 12 options mein se choose karo, "
                "ya seedha natural language mein type karo - jaise \"Reliance ka analysis bhejo\" "
                "ya \"Aaj ke top stocks kya hain?\" ('/help' se poori list milega)."
            ),
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "📊 Aaj ke Top Picks", "callback_data": "run_today"},
                        {"text": "📈 Live Market Status", "callback_data": "run_livestatus"},
                        {"text": "🔥 Intraday Picks", "callback_data": "run_intraday"},
                    ],
                    [
                        {"text": "🚀 Swing Breakout", "callback_data": "run_swing"},
                        {"text": "🌙 BTST Picks", "callback_data": "run_btst"},
                        {"text": "📋 Active Trades", "callback_data": "run_activetrades"},
                    ],
                    [
                        {"text": "🎯 Target Hit", "callback_data": "run_targethit"},
                        {"text": "⚡ Best Risk:Reward", "callback_data": "run_bestrr"},
                        {"text": "📰 Latest News", "callback_data": "run_news"},
                    ],
                    [
                        {"text": "📄 Full PDF Report", "callback_data": "run_pdf"},
                        {"text": "📅 Performance Report", "callback_data": "run_report"},
                        {"text": "👑 Master Dashboard", "callback_data": "run_dashboard"},
                    ],
                ]
            },
        }
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        logger.error(f"Menu buttons bhejne me error: {e}")


def answer_callback_query(callback_query_id):
    try:
        url = BASE_URL + "answerCallbackQuery"
        requests.post(url, json={"callback_query_id": callback_query_id}, timeout=10)
    except Exception:
        pass


def send_pdf_report(chat_id):
    """Native PDF sender function"""
    from telegram_alerts import send_telegram_pdf
    # V8.2.0: relative path ki jagah config.PDF_REPORT_PATH use karte hain
    # (CWD-dependent bug fix - systemd/Render start-from-/ cases).
    try:
        from config import PDF_REPORT_PATH
        pdf_path = PDF_REPORT_PATH
    except ImportError:
        pdf_path = "reports/AI_Report.pdf"

    if not os.path.exists(pdf_path):
        reply_to_telegram(chat_id, "Abhi tak koi PDF report generate nahi hui hai. Pehle '--scan' chalao.")
        return

    reply_to_telegram(chat_id, "Full PDF report bheji ja rahi hai...")
    send_telegram_pdf(pdf_path, caption_text="AI Stock Scanner - Full Report")


def get_today_picks_text():
    # V8.2.0: IST date use karte hain (host TZ se independent)
    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    try:
        conn = get_db_connection()
        conn.row_factory = lambda cursor, row: dict((cursor.description[idx][0], value) for idx, value in enumerate(row))
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM recommendations WHERE date_added = ?", (today_str,))
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"Database read failure: {e}")
        return "Database fetch error! Check logs."

    if not rows:
        return "Aaj abhi tak koi naya recommendation generate nahi hua (ya --scan chalaya nahi gaya)."

    lines = [f"<b>Aaj ke Top Picks ({today_str})</b>\n"]
    for r in rows:
        stk = escape_html(r["stock"].replace(".NS", ""))
        lines.append(
            f"• <b>{stk}</b> - {r['signal']} (Score {r['score']})\n"
            f"  Entry: {r['entry_price']} | SL: {r['sl_price']} | "
            f"T1: {r['target_1']} | T2: {r['target_2']} | Final: {r['target_3']}\n"
        )
    return "\n".join(lines)


def get_nifty_trend_text():
    try:
        from downloader import download_benchmark
        from mtf import get_weekly_trend

        df = download_benchmark()
        if df is None or df.empty:
            return "NIFTY 50 ka data abhi available nahi hai."

        last = df.iloc[-1]
        close, open_ = float(last["Close"]), float(last["Open"])
        change_pct = (close - open_) / open_ * 100 if open_ else 0
        arrow = "🟢" if change_pct >= 0 else "🔴"

        weekly = get_weekly_trend(df)
        weekly_text = weekly["trend"] if weekly else "UNKNOWN"

        return (
            f"📊 <b>NIFTY 50 Trend</b>\n"
            f"{arrow} Last: {close:.2f} ({change_pct:+.2f}%)\n"
            f"Weekly Trend: {weekly_text}"
        )
    except Exception as e:
        logger.warning(f"Nifty trend fetch fail: {e}")
        return "NIFTY trend abhi calculate nahi ho paya."


def get_latest_news_text(limit=5):
    """
    V9.0 NAYA: Latest 3-5 breaking news headlines - breaking_news.py
    ka get_latest_breaking_news() use karta hai (RSS feeds se).

    V9.1 FIX: News titles ab HINGLISH mein translate hote hain
    (to_hinglish se — GLM API use hota hai agar ZAI_API_KEY set hai,
    warna Devanagari fallback). Stock names English preserve hote
    hain. Global news filter (sirf Indian equity market related) bhi
    apply hota hai breaking_news.get_latest_breaking_news() ke andar.
    """
    try:
        from breaking_news import get_latest_breaking_news
        items = get_latest_breaking_news()
    except Exception as e:
        logger.warning(f"Latest news fetch fail: {e}")
        return "📰 Latest news abhi fetch nahi ho payi. Thodi der baad try karo."

    if not items:
        return "📰 Abhi koi nayi breaking news nahi mili. Thodi der baad check karo."

    # V9.1 FIX: to_hinglish import (best) ya to_hindi (fallback)
    try:
        from translator import to_hinglish
        _translate = to_hinglish
    except ImportError:
        try:
            from translator import to_hindi as _translate
        except ImportError:
            _translate = None

    lines = [f"📰 <b>LATEST MARKET NEWS</b> (top {min(limit, len(items))})\n"]
    for i, item in enumerate(items[:limit], 1):
        title = item.get("title", "No Title")
        source = item.get("source", "Unknown")
        summary = item.get("summary", "")

        # V9.1: Hinglish translation pehle (stock names English preserve)
        if _translate:
            try:
                title = _translate(title)
                if summary:
                    summary = _translate(summary)
            except Exception as e:
                logger.debug(f"news translate fail: {e}")

        # HTML-escape after translation (to_hinglish unescapes entities)
        title = escape_html(title)
        source = escape_html(source)

        line = f"{i}. {title}\n   — <i>{source}</i>"
        if summary:
            summary_clean = escape_html(summary[:150])
            line += f"\n   📝 {summary_clean}"
        lines.append(line + "\n")
    return "\n".join(lines)


def get_help_text():
    rerun_options = ", ".join(_RERUN_COMMANDS.keys())
    return (
        "🤖 <b>AI Stock Scanner - Kya kar sakta hoon</b>\n\n"
        "Seedha type karo (natural language, koi fix command nahi chahiye):\n"
        "• \"Reliance ka analysis bhejo\" - kisi bhi stock ka snapshot\n"
        "• \"Aaj ke top stocks kya hain?\" - aaj ki top pick list\n"
        "• \"Nifty ka trend kya hai?\" - market trend\n"
        "• \"Mere active trades dikhao\" - watchlist\n"
        "• \"Target hit stocks dikhao\" - kaunse targets hit hue\n"
        "• \"Best risk reward stocks batao\" - sabse achha R:R\n"
        "• \"Weekly report\" / \"Monthly report\" - performance summary\n"
        "• \"PDF report bhejo\" - poori PDF report\n\n"
        "Ya '/start' bhej kar button menu bhi use kar sakte ho.\n\n"
        f"🔄 <b>/rerun &lt;name&gt;</b> - agar koi scheduled slot deploy/restart "
        f"ki wajah se miss ho jaaye, ise dobara turant chala sakte ho.\n"
        f"Available: {rerun_options}\n"
        f"Example: /rerun swing"
    )


# V8.2.0: UNKNOWN-intent fallback ke liye - raw user message ko stock
# symbol maan ke lookup karne se pehle ek heuristic check. "thanks",
# "ok", "bye", "market kaisa lagega kal" jaise inputs ko stock maan
# ke yfinance lookup karna bogus tha.
_COMMON_WORDS = {
    "thanks", "thank", "thank you", "thx", "ok", "okay", "k", "kk",
    "bye", "goodbye", "hi", "hello", "hey", "yo", "yes", "no", "yep",
    "nope", "cool", "great", "nice", "awesome", "got it", "okk",
    "done", "ok", "fine", "alright", "wow", "lol", "haha", "nice",
    "good", "bad", "ok", "okey",
}


def _looks_like_stock_name(text):
    """
    V8.2.0: Heuristic - text ko stock-name/symbol maan ke lookup karne
    se pehle check karta hai. Common greeting/acknowledgment words aur
    long sentences (jisme multiple words hain) ko reject karta hai.
    Sirf short, alphanumeric, stock-like text allow karta hai.
    """
    if not text or not isinstance(text, str):
        return False
    t = text.strip().lower()
    if not t:
        return False
    # common conversation words reject
    if t in _COMMON_WORDS:
        return False
    # >3 words = sentence, not a stock name
    if len(t.split()) > 3:
        return False
    # >30 chars = too long for a stock name
    if len(t) > 30:
        return False
    # alphanumeric + space + & + . allowed (L&T, M&M, TCS, RELIANCE.NS)
    if not all(c.isalnum() or c in " &.-_" for c in t):
        return False
    return True


def handle_natural_language(chat_id, user_message):
    intent, symbol = parse_intent(user_message)

    # V9.0: AI Brain (GLM) integration - STOCK_ANALYSIS, conversational
    # intents (MARKET_VIEW/BEST_STOCK/ENTRY_EXIT), aur UNKNOWN ke liye
    # GLM se Hinglish reply. AI_BRAIN_ENABLED guard hai - disabled ya
    # module-unavailable hone par purana rule-based behavior chalta rahe.

    if intent == "STOCK_ANALYSIS" and symbol:
        clean_sym = symbol.replace(".NS", "")
        # Pehle text snapshot bhejo (price/RSI/levels - raw data display)
        reply_to_telegram(chat_id, get_stock_snapshot(clean_sym))

        # V9.0: AI Brain ON hone par chart photo + GLM analysis text bhi bhejo
        if AI_BRAIN_ENABLED and _AI_BRAIN_AVAILABLE:
            # Chart photo (best-effort, non-fatal agar generate/send fail ho)
            try:
                from stock_lookup import _fetch_with_cache
                from charts import generate_simple_chart
                from telegram_alerts import send_telegram_chart
                from utils import clean_symbol as _cs
                df = _fetch_with_cache(symbol)
                if df is not None and len(df) >= 30:
                    chart_path = generate_simple_chart(symbol, df)
                    if chart_path:
                        send_telegram_chart(
                            chart_path,
                            caption_text=f"📊 <b>{escape_html(_cs(symbol))}</b> Chart"
                        )
            except Exception as e:
                logger.debug(f"Stock chart send fail ({symbol}): {e}")
            # GLM AI analysis text (Hinglish commentary on the stock)
            try:
                ai_text = ask_ai(user_message, intent="STOCK_ANALYSIS", symbol=symbol)
                if ai_text:
                    reply_to_telegram(chat_id, ai_text)
            except Exception as e:
                logger.warning(f"AI Brain STOCK_ANALYSIS fail: {e}")
    elif intent == "TOP_PICKS":
        reply_to_telegram(chat_id, get_today_picks_text())
    elif intent == "PDF_REPORT":
        send_pdf_report(chat_id)
    elif intent == "WEEKLY_REPORT":
        reply_to_telegram(chat_id, generate_weekly_report())
    elif intent == "MONTHLY_REPORT":
        reply_to_telegram(chat_id, generate_monthly_report())
    elif intent == "TARGET_HIT":
        reply_to_telegram(chat_id, generate_target_hit_stocks())
    elif intent == "BEST_RR":
        reply_to_telegram(chat_id, generate_best_rr_stocks())
    elif intent == "ACTIVE_TRADES":
        from tracker import generate_watchlist_summary
        reply_to_telegram(chat_id, generate_watchlist_summary())
    elif intent == "NIFTY_TREND":
        reply_to_telegram(chat_id, get_nifty_trend_text())
    elif intent == "DAILY_REPORT":
        reply_to_telegram(chat_id, generate_daily_performance_report())
    elif intent == "MASTER_DASHBOARD":
        from master_dashboard import generate_master_trading_dashboard
        # V8.2.0: chunking ab reply_to_telegram ke andar hai - safely long dashboards
        reply_to_telegram(chat_id, generate_master_trading_dashboard())
    elif intent in ("MARKET_VIEW", "BEST_STOCK", "ENTRY_EXIT"):
        # V9.0: Conversational intents - GLM se Hinglish reply.
        # AI_BRAIN_ENABLED False ya module unavailable hone par
        # rule-based fallback (help text) bhejta hai.
        if AI_BRAIN_ENABLED and _AI_BRAIN_AVAILABLE:
            try:
                ai_text = ask_ai(user_message, intent=intent, symbol=symbol)
                if ai_text:
                    reply_to_telegram(chat_id, ai_text)
                else:
                    reply_to_telegram(chat_id, get_help_text())
            except Exception as e:
                logger.warning(f"AI Brain {intent} fail: {e}")
                reply_to_telegram(chat_id, get_help_text())
        else:
            reply_to_telegram(
                chat_id,
                "🤔 AI Brain abhi disabled hai. /help bhejo available "
                "commands dekhne ke liye, ya koi stock naam likho (jaise 'TCS')."
            )
    elif intent == "HELP":
        reply_to_telegram(chat_id, get_help_text())
    else:
        # V9.0: UNKNOWN intent - AI Brain ON hone par GLM se
        # conversational Hinglish reply. OFF/unavailable hone par
        # purana rule-based heuristic (stock-name lookup ya help message).
        if AI_BRAIN_ENABLED and _AI_BRAIN_AVAILABLE:
            try:
                ai_text = ask_ai(user_message, intent="UNKNOWN")
                if ai_text:
                    reply_to_telegram(chat_id, ai_text)
                    return
            except Exception as e:
                logger.warning(f"AI Brain UNKNOWN fail: {e}")
                # fall through to rule-based behavior below

        # V8.2.0: UNKNOWN-intent fallback - pehle raw user message ko
        # stock-ticker maan ke yfinance lookup karta tha, jo "thanks"/
        # "ok"/"market kaisa lagega kal" jaise inputs par bogus ticker
        # dhoondhne ki koshish karta tha. Ab heuristic check karke:
        # - agar short alphanumeric stock-like text hai → snapshot lookup
        # - warna friendly "samajh nahi aaya, /help try karo" reply
        if _looks_like_stock_name(user_message):
            reply_to_telegram(chat_id, get_stock_snapshot(user_message.replace(".NS", "")))
        else:
            reply_to_telegram(
                chat_id,
                "🤔 Samajh nahi aaya. Main kya kar sakta hoon:\n"
                "• <b>Stock snapshot</b> - naam likho (jaise 'TCS' ya 'Reliance')\n"
                "• <b>'Aaj ke top stocks kya hain?'</b> - aaj ki picks\n"
                "• <b>'Nifty ka trend kya hai?'</b> - market overview\n"
                "• <b>'Master dashboard'</b> - active trades overview\n"
                "• <b>'/help'</b> - poori commands list\n\n"
                "Example: 'Reliance ka analysis bhejo' ya seedha 'TCS'"
            )


def run_listener_loop():
    """
    V8.1.2 NAYA: Sirf Telegram message-listening loop (offset-polling,
    intent-handling) - koi health-server ya breaking-news poller setup
    NAHI karta. Ye function isliye alag nikala gaya hai taaki
    main.py --schedule (jo apna khud ka health-server aur breaking-news
    poller pehle se chala chuka hai) is loop ko ek background THREAD
    mein safely chala sake, bina duplicate Flask-port-bind crash ke.

    Standalone run karna ho (`python bot_listener.py` seedha) to
    start_bot_engine() use karo - wo pehle setup bhi karta hai.

    V8.2.0: offset ab data/bot_offset.json se persist hota hai (atomic
    write). SIGTERM/SIGINT cleanly exit karwate hain.
    """
    _install_signal_handlers()
    clear_old_webhook()
    logger.info("AI Stock Bot Listener Active Ho Gaya Hai (message-listening loop)...")

    # V8.2.0: persisted offset load karo - restart/redeploy ke baad
    # duplicate replies prevent ho jaate hain, aur downtime ke dauran
    # >100 pending updates bhi lost nahi hote.
    offset = _load_offset()
    if offset is not None:
        logger.info(f"bot_offset.json se persisted offset load hua: {offset}")
    else:
        logger.info("Koi persisted offset nahi mila, fresh polling shuru")

    # V8.3.1: random startup delay (2-8s) — redeploy ke dauran old + new
    # instance ek saath getUpdates poll karte the (409 Conflict). Random
    # delay se chances badhte hain ki dono simultaneously start na hon,
    # aur agar ho bhi jaayein to shorter-wait wala pehle backoff kar le.
    import random as _random
    _startup_delay = _random.uniform(2.0, 8.0)
    logger.info(f"[bot_listener] startup delay {_startup_delay:.1f}s (409 conflict avoid karne ke liye)")
    time.sleep(_startup_delay)

    # V8.2.0: exponential backoff on persistent errors (1s → 2s → 4s → ... → 60s cap)
    backoff = 1

    while not _shutdown_requested:
        try:
            _heartbeat()  # V8.2.0: deep-health heartbeat update
            updates = get_bot_updates(offset)

            # V8.3.1: 409 Conflict (redeploy — do instances) hone par
            # 10s wait karo taaki purana instance SIGTERM se exit ho jaaye.
            # Normal backoff (1s) se har second 409 spam hota tha.
            if updates is None and _last_getupdates_status == 409:
                time.sleep(10)
                continue

            if updates and "result" in updates:
                # V8.2.0: successful fetch - backoff reset
                backoff = 1

                for update in updates["result"]:
                    offset = update["update_id"] + 1

                    # 1. BUTTON CLICKS
                    if "callback_query" in update:
                        callback_id = update["callback_query"]["id"]
                        chat_id = update["callback_query"]["message"]["chat"]["id"]
                        callback_data = update["callback_query"]["data"]

                        answer_callback_query(callback_id)

                        if callback_data == "run_monitor":
                            reply_to_telegram(chat_id, "Live Market Tracking Shuru... Data fetch kiya ja raha hai...")
                            reply_to_telegram(chat_id, check_live_market_hits(chat_id))
                        elif callback_data == "run_report":
                            reply_to_telegram(chat_id, "Performance Report taiyar ki ja rahi hai...")
                            reply_to_telegram(chat_id, generate_daily_performance_report())
                        elif callback_data == "run_today":
                            reply_to_telegram(chat_id, get_today_picks_text())
                        elif callback_data == "run_pdf":
                            send_pdf_report(chat_id)
                        elif callback_data == "run_dashboard":
                            from master_dashboard import generate_master_trading_dashboard
                            # V8.2.0: chunking ab reply_to_telegram ke andar hai
                            reply_to_telegram(chat_id, generate_master_trading_dashboard())
                        # ---- V9.0: Naye 12-button menu callbacks ----
                        elif callback_data == "run_intraday":
                            # 9:30 AM intraday scan pipeline - background thread
                            # (handle_rerun_command khud "shuru ho raha hai" reply
                            # bhejta hai aur thread spawn karta hai - button loop
                            # block nahi hota)
                            handle_rerun_command(chat_id, "/rerun intraday")
                        elif callback_data == "run_swing":
                            # Aaj ke swing recommendations DB se (scanner.py 9:20 AM output)
                            reply_to_telegram(chat_id, get_today_picks_text())
                        elif callback_data == "run_btst":
                            # 3:05 PM BTST scan pipeline - background thread
                            handle_rerun_command(chat_id, "/rerun btst")
                        elif callback_data == "run_activetrades":
                            # OPEN positions DB se - watchlist summary
                            try:
                                from tracker import generate_watchlist_summary
                                reply_to_telegram(chat_id, generate_watchlist_summary())
                            except Exception as e:
                                logger.warning(f"Active trades fetch fail: {e}")
                                reply_to_telegram(chat_id, "Active trades fetch nahi ho paaye. Thodi der baad try karo.")
                        elif callback_data == "run_targethit":
                            reply_to_telegram(chat_id, generate_target_hit_stocks())
                        elif callback_data == "run_bestrr":
                            reply_to_telegram(chat_id, generate_best_rr_stocks())
                        elif callback_data == "run_news":
                            reply_to_telegram(chat_id, get_latest_news_text())
                        elif callback_data == "run_livestatus":
                            reply_to_telegram(chat_id, get_nifty_trend_text())

                    # 2. TEXT MESSAGES (Supports both Group 'message' & Channel 'channel_post')
                    else:
                        msg_obj = None
                        if "message" in update and "text" in update["message"]:
                            msg_obj = update["message"]
                        elif "channel_post" in update and "text" in update["channel_post"]:
                            msg_obj = update["channel_post"]

                        if msg_obj:
                            chat_id = msg_obj["chat"]["id"]
                            user_message = msg_obj["text"].strip()
                            user_message_lower = user_message.lower()
                            # V8.1.2 BUG FIX: Pehle sirf "/start" list mein tha,
                            # "start" (bina slash ke) nahi - isliye "Start" jaisa
                            # message match nahi hota tha aur galti se stock-symbol
                            # lookup tak pahunch jaata tha (START.NS dhoondhne ki
                            # koshish, jo kabhi exist hi nahi karta). Ab leading
                            # "/" normalize kar dete hain taaki "/start" aur "start"
                            # dono ek hi jagah route hon.
                            cmd = user_message_lower.lstrip("/")

                            logger.info(f"Message Processed from Chat ID {chat_id}: '{user_message}'")

                            if cmd in ["hello ai", "hi", "hello", "start", "menu"]:
                                send_inline_menu(chat_id)
                            elif cmd in ["monitor"]:
                                reply_to_telegram(chat_id, check_live_market_hits(chat_id))
                            elif cmd in ["report"]:
                                reply_to_telegram(chat_id, generate_daily_performance_report())
                            elif cmd in ["today"]:
                                reply_to_telegram(chat_id, get_today_picks_text())
                            elif cmd in ["pdf"]:
                                send_pdf_report(chat_id)
                            elif cmd in ["dashboard"]:
                                from master_dashboard import generate_master_trading_dashboard
                                reply_to_telegram(chat_id, generate_master_trading_dashboard())
                            elif cmd in ["help"]:
                                reply_to_telegram(chat_id, get_help_text())
                            elif cmd.startswith("rerun"):
                                handle_rerun_command(chat_id, user_message)
                            elif user_message.startswith("--") or user_message.startswith("/"):
                                # V8.1.2 BUG FIX: "--scan" jaisa CLI-syntax (jo
                                # main.py command-line ke liye hai, Telegram-command
                                # nahi) ko bhi galat symbol-lookup mein jaane se
                                # rokte hain - explicit "samajh nahi aaya" reply.
                                reply_to_telegram(
                                    chat_id,
                                    f"'{user_message}' ek command jaisa lag raha hai lekin "
                                    f"pehchana nahi gaya. /help bhejo available commands "
                                    f"dekhne ke liye, ya seedha stock ka naam likho "
                                    f"(jaise 'TCS' ya 'Reliance')."
                                )
                            else:
                                handle_natural_language(chat_id, user_message)

            # V8.2.0: offset atomically persist karo - har loop iteration ke baad
            # (agar crash ho jaaye to bhi next restart last processed offset se
            # shuru hoga, duplicate replies prevent)
            _save_offset(offset)

        except Exception as main_err:
            logger.error(f"Critical Main Loop Failure: {main_err}")
            # V8.2.0: exponential backoff - persistent error par 1 req/sec
            # hammering Telegram ki jagah stepped backoff. 60s cap.
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        # V8.2.0: short sleep - long polling already 20s block karta hai
        # isliye 1s sleep sufficient hai, no need to spam.
        time.sleep(1)

    logger.info("[bot_listener] polling loop cleanly exit ho gaya (SIGTERM/SIGINT received)")


def start_bot_engine():
    """
    Standalone entry-point (`python bot_listener.py` seedha chalane ke
    liye) - health-server aur breaking-news poller khud setup karta
    hai, phir run_listener_loop() call karta hai.

    main.py --schedule se background-thread ke roop mein use karna ho
    to seedha run_listener_loop() call karo (health-server/breaking-
    news duplicate setup se bachne ke liye) - dekho main.py.

    V9.0: Intraday live alert tracker bhi yahin se start hota hai
    (background daemon thread - har 5 min intraday picks check karta
    hai 9:30-15:00 IST ke beech).
    """
    # V8.1.2: Render 24x7 health server - /ping endpoint for UptimeRobot.
    # Agar bot_listener.py standalone chal raha hai (main.py --schedule
    # ke bina), yahi is process ke liye health check provide karta hai.
    try:
        from health_server import setup_render_deployment
        setup_render_deployment("V9.0")
    except Exception as e:
        logger.warning(f"Health server start nahi ho paya (bot chalta rahega): {e}")

    # V8.1.2: Breaking news poller - background thread, har 3 min Indian
    # market RSS feeds check karke nayi headline turant Hindi mein
    # Telegram par bhejta hai. Agar main.py --schedule se bhi start ho
    # chuka ho to ye no-op hoga (duplicate thread nahi banega).
    try:
        from breaking_news import start_breaking_news_poller
        start_breaking_news_poller()
    except Exception as e:
        logger.warning(f"Breaking news poller start nahi ho paya (bot chalta rahega): {e}")

    # V9.0: Intraday live alert tracker (background daemon thread) -
    # har INTRADAY_LIVE_ALERT_INTERVAL min (default 5) aaj ke intraday
    # picks ka live price check karke entry/target/SL hit hone par
    # sharp Telegram alert bhejta hai (9:30-15:00 IST window).
    # INTRADAY_LIVE_ALERT_ENABLED False hone par skip.
    try:
        if INTRADAY_LIVE_ALERT_ENABLED:
            from intraday_tracker import run_intraday_alert_loop
            threading.Thread(
                target=run_intraday_alert_loop,
                daemon=True,
                name="intraday-tracker",
            ).start()
            logger.info("V9.0: Intraday live alert tracker thread shuru ho gaya (daemon)")
        else:
            logger.info("V9.0: INTRADAY_LIVE_ALERT_ENABLED=False, tracker thread skip")
    except Exception as e:
        logger.warning(f"Intraday tracker thread start nahi ho paya (bot chalta rahega): {e}")

    run_listener_loop()


if __name__ == "__main__":
    start_bot_engine()
