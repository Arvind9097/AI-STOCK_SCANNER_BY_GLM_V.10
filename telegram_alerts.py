"""
===================================================================
 TELEGRAM DISPATCH PIPELINE
===================================================================
BUGS JO FIX KIYE:
1. Token/Chat ID do jagah (yahan + bot_listener.py) hardcoded the -
   agar ek jagah change karte to dusri jagah purana reh jaata.
   Ab dono config.py se ek hi jagah se aate hain.
2. send_telegram_chart() aur send_telegram_pdf() non-200 response
   ko silently ignore kar rahe the - error kabhi dikhta hi nahi tha,
   isliye "chatbot kaam nahi kar raha" jaisa lagta tha jabki asal
   mein Telegram error de raha tha aur log hi nahi ho raha tha.
3. parse_mode="Markdown" (legacy) special characters (_, *, [, ], `)
   se poora message reject kar deta hai agar stock name ya AI text
   mein ye characters aa jaayein (e.g. "M&M") - EK bhi bigda character
   poora message fail kar deta tha. Ab HTML parse_mode + proper
   escaping use kiya hai, jo zyada robust hai.
4. Koi timeout nahi tha - agar Telegram slow ho to poora script hang
   ho sakta tha. Ab sabme timeout hai.
5. Koi retry nahi tha - ek transient network error par message
   permanently lost ho jaata tha. Ab 1 retry hai.
===================================================================
"""

import time
import requests
import os
from logger import logger
from utils import chunk_text

# V8.1.2 SECURITY FIX: Hardcoded leaked-token fallback yahan se
# PERMANENTLY hata diya gaya hai. config.py khud hi startup par
# TELEGRAM_BOT_TOKEN missing hone par clear error deke exit ho jaata
# hai - isliye yahan tak pahunchne ka matlab hai TOKEN already valid
# environment variable se aaya hai.
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
TOKEN = TELEGRAM_BOT_TOKEN
CHAT_ID = TELEGRAM_CHAT_ID

_REQUEST_TIMEOUT = 20
# V8.1.2 FIX: Chart-image upload kabhi-kabhi 20-sec text-message-timeout
# se fail ho jaata tha (production logs mein dekha gaya: "Connection
# aborted... write operation timed out") - file-upload (photo/document)
# ko text-message se zyada time chahiye (file-read + network-upload
# dono). PDF-upload mein pehle se hi 60-sec timeout tha (proven value,
# PDFs charts se bade ho sakte hain) - dono (chart + PDF) ab isi shared,
# lambe timeout ko use karte hain, consistent behavior ke liye.
_FILE_UPLOAD_TIMEOUT = 60


def _post_file_with_retry(url, path, field_name, data):
    """
    File-upload (photo/document) ke liye retry-wrapper - send_telegram_text
    jaisa hi retry-pattern, lekin file ko HAR ATTEMPT PAR DOBARA khola
    jaata hai (ek baar-khula-file-handle dobara POST mein reuse nahi ho
    sakta agar pehla attempt fail ho jaaye).
    """
    for attempt in (1, 2):
        try:
            with open(path, 'rb') as f:
                files = {field_name: f}
                resp = requests.post(url, data=data, files=files, timeout=_FILE_UPLOAD_TIMEOUT)
            return resp
        except requests.exceptions.RequestException as e:
            if attempt == 1:
                logger.warning(f"Telegram file-upload fail (attempt 1), retry kar raha hoon: {e}")
                time.sleep(2)
            else:
                logger.error(f"Telegram file-upload permanently fail: {e}")
                return None


def _post_with_retry(url, **kwargs):
    """Ek retry ke saath POST karta hai, response return karta hai (ya None)."""
    for attempt in (1, 2):
        try:
            resp = requests.post(url, timeout=_REQUEST_TIMEOUT, **kwargs)
            return resp
        except requests.exceptions.RequestException as e:
            if attempt == 1:
                logger.warning(f"Telegram request fail (attempt 1), retry kar raha hoon: {e}")
                time.sleep(2)
            else:
                logger.error(f"Telegram request permanently fail: {e}")
                return None


def _check_response(resp, context):
    """Response check karke error clearly log karta hai (silent failure nahi)."""
    if resp is None:
        return False
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code != 200 or not data.get("ok", False):
        logger.error(
            f"Telegram {context} FAILED (status {resp.status_code}): "
            f"{data.get('description', resp.text[:300])}"
        )
        return False
    return True


# V8.3.0 (G4): Shared section divider - 27-char "━" line. Sab Telegram
# cards/sections ke beech visually consistent separation ke liye use hota
# hai. Single source of truth - agar koi width badalni ho to ek hi jagah.
CARD_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def send_telegram_card(title, body_lines, footer=None, emoji="📊"):
    """
    V8.3.0 (G4): Consistent card-style Telegram message dispatcher.

    Ek shared helper jo har section ko ek clean card format mein render
    karta hai - top par emoji+bold title, beech mein body lines, niche
    divider (ya footer). Sab modules (master_dashboard, main, tracker)
    isko use karke visually consistent output dete hain.

    Format:
        {emoji} <b>{TITLE}</b>
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━
        {body_lines (joined by newline)}
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━
        {footer (italic, optional)}

    Args:
        title: Card headline - helper escape_html karega (dynamic safe).
        body_lines: List of strings (already HTML-formatted/escaped),
                    har string ek line. Ya single multi-line string.
        footer: Optional italic footer (disclaimer waghera). HTML-safe
                nahi hona chahiye, helper escape_html karega.
        emoji: Title ke aage wala emoji (default "📊").

    Long messages (>4096 chars) chunk_text se split ho jaate hain -
    caller ko chunking ki tension nahi leni.
    """
    from utils import escape_html as _escape

    safe_title = _escape(title)
    parts = [f"{emoji} <b>{safe_title}</b>", CARD_DIVIDER]

    if isinstance(body_lines, (list, tuple)):
        parts.extend(body_lines)
    elif body_lines:
        parts.append(str(body_lines))

    parts.append(CARD_DIVIDER)
    if footer:
        parts.append(f"<i>{_escape(footer)}</i>")

    return send_telegram_text("\n".join(parts))


def send_telegram_text(message):
    """Channel par text message post karna (HTML parse mode - zyada robust).

    V8.2.0 FIXES:
    - bug #28: resp.json() pehle JSONDecodeError throw kar sakta tha agar
      Telegram HTML error page return kare (e.g. proxy, empty body).
      Ab try/except se safe-guarded.
    - chunk_text usage: agar message > 4096 chars (Telegram limit)
      hai to multiple messages mein todke bhejta hai. Pehle silently
      fail kar jaata tha.
    """
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    # V8.2.0: Long messages (e.g. detailed watchlist summary, breaking
    # news digest) Telegram 4096-char limit se zyada hone par silently
    # fail ho jaate the. Ab chunk_text use karke line-boundaries par
    # todke multiple messages bhejta hai (avoid word-mid cut).
    chunks = chunk_text(message, max_length=3800) if message else []
    if not chunks:
        chunks = [""]

    last_resp = None
    for chunk in chunks:
        payload = {
            "chat_id": CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = _post_with_retry(url, json=payload)
        _check_response(resp, "sendMessage")
        # V8.2.0 FIX (bug #28): wrap resp.json() in try/except. Agar
        # Telegram ne non-JSON response bheja (proxy HTML page, empty
        # body, timeout) to JSONDecodeError throw hota tha jo caller tak
        # propagate karta tha - ab None return karte hain.
        if resp is not None:
            try:
                last_resp = resp.json()
            except Exception as e:
                logger.debug(f"Telegram sendMessage JSON decode fail: {e}")
                last_resp = None
    return last_resp


def send_telegram_chart(image_path, caption_text=""):
    """Chart image ko channel par upload karna (lamba timeout + 1 retry - file-uploads text-messages se zyada time lete hain)."""
    if not os.path.exists(image_path):
        logger.warning(f"Chart image path missing for Telegram dispatch: {image_path}")
        return None
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    data = {
        'chat_id': CHAT_ID,
        'caption': caption_text,
        'parse_mode': 'HTML',
    }
    resp = _post_file_with_retry(url, image_path, 'photo', data)
    if resp is None:
        return None
    _check_response(resp, f"sendPhoto ({os.path.basename(image_path)})")
    try:
        return resp.json()
    except Exception:
        return None


def send_telegram_pdf(pdf_path, caption_text=""):
    """PDF Report ko document ki tarah channel par bhejna (lamba timeout + 1 retry)."""
    if not os.path.exists(pdf_path):
        logger.warning(f"PDF report path missing for Telegram dispatch: {pdf_path}")
        return None
    url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    data = {
        'chat_id': CHAT_ID,
        'caption': caption_text,
        'parse_mode': 'HTML',
    }
    resp = _post_file_with_retry(url, pdf_path, 'document', data)
    if resp is None:
        return None
    _check_response(resp, "sendDocument")
    try:
        return resp.json()
    except Exception:
        return None
