# ... existing code ...
import signal
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# V10.1 FIX: Persistent session banayi gayi hai.
# Isse HTTP "Keep-Alive" enable hoga aur bot har 1-2 second mein naya 
# TCP connection/SSL handshake nahi banayega, jisse server CPU/network load bachega.
_telegram_session = requests.Session()

from tracker import (
    check_live_market_hits, generate_daily_performance_report,
# ... existing code ...
from config import TELEGRAM_BOT_TOKEN
BOT_TOKEN = TELEGRAM_BOT_TOKEN

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"


def clear_old_webhook():
    """Script start hote hi purane webhook ko automatic delete karne ke liye"""
    try:
        url = BASE_URL + "deleteWebhook"
        res = _telegram_session.get(url, timeout=10).json()
        if res.get("ok"):
            logger.info("Purana Telegram Webhook successfully clean kar diya gaya hai.")
        else:
            logger.warning(f"Webhook clear status: {res.get('description')}")
    except Exception as e:
        logger.error(f"Webhook clean karne me error: {e}")


# V8.3.1: last non-200 status code — 409 Conflict detection ke liye.
# ... existing code ...
def get_bot_updates(offset=None):
    """Telegram server se updates lene ke liye (handles ISP drop/timeout gracefully)"""
    global _last_getupdates_status
    try:
        url = BASE_URL + "getUpdates?timeout=20"
        if offset:
            url += f"&offset={offset}"
        response = _telegram_session.get(url, timeout=30)
        _last_getupdates_status = response.status_code

        if response.status_code != 200:
# ... existing code ...
def reply_to_telegram(chat_id, text_message):
    """
    Group/Channel ya User ko text message bhejne ke liye helper function.
# ... existing code ...
        # V8.2.0: chunk_text line-boundaries par todta hai - clean output
        chunks = chunk_text(text_str, max_length=3800)
        for chunk in chunks:
            url = BASE_URL + "sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            response = _telegram_session.post(url, json=payload, timeout=20)
            if response.status_code != 200:
                # V8.2.0: truncated response.text - chat_id/message content leak prevent
                logger.error(f"Reply fail (Chat ID: {chat_id}): {response.text[:200]}")
# ... existing code ...
                    # Row 5: Info
                    [{"text": "📰 Latest News", "callback_data": "run_news"}],
                    [{"text": "📄 Full PDF Report", "callback_data": "run_pdf"}],
                    [{"text": "📅 Performance Report", "callback_data": "run_report"}],
                    [{"text": "👑 Master Dashboard", "callback_data": "run_dashboard"}],
                ]
            },
        }
        _telegram_session.post(url, json=payload, timeout=20)
    except Exception as e:
        logger.error(f"Menu buttons bhejne me error: {e}")


def answer_callback_query(callback_query_id):
    try:
        url = BASE_URL + "answerCallbackQuery"
        _telegram_session.post(url, json={"callback_query_id": callback_query_id}, timeout=10)
    except Exception:
        pass


def send_pdf_report(chat_id):
# ... existing code ...
