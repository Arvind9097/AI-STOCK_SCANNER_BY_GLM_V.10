"""
===========================================================
 SHARED UTILITIES  (V8.2.0 — atomic writes + chunking added)
===========================================================
Chhote helper functions jo multiple files mein use hote hain -
ek hi jagah rakhte hain taaki sab jagah consistent behavior rahe.
===========================================================
"""

import html
import json
import os
import tempfile


def clean_symbol(symbol):
    """
    'RELIANCE.NS' -> 'RELIANCE', '^NSEI' -> 'NIFTY50'
    Display ke liye (chart title, Telegram message, PDF, Excel) -
    DB/internal logic mein asli symbol (.NS ke saath) hi use hota hai,
    sirf USER KO DIKHANE wali jagah par ye function chalao.
    """
    if not isinstance(symbol, str):
        return symbol
    if symbol == "^NSEI":
        return "NIFTY50"
    return symbol.replace(".NS", "").replace(".BO", "")


def escape_html(text):
    """
    Telegram HTML parse_mode ke liye text safe banata hai.
    Stock names mein '&' (M&M, L&T, J&K Bank) jaise characters
    HTML mein special hote hain - agar escape na karein to Telegram
    poora message reject kar deta hai ("can't parse entities").
    """
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def atomic_write_json(path, data):
    """
    JSON file ko ATOMICALLY likhta hai - pehle ek temporary file
    mein likhta hai, phir use destination par rename karta hai
    (os.replace atomic hota hai same filesystem par).

    Khaas faayda: crash ho jaaye (power off, SIGTERM, exception)
    to destination file KABHI half-written / corrupt nahi hogi -
    ya to purani version reh jaayegi ya nayi, beech ki kharab
    state nahi.

    Use resume_state.py, dispatch_state.py, cache.py, breaking_news.py
    mein file-based JSON state save karne ke liye.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # tempfile usi directory mein banao taaki os.rename same-filesystem ho
    fd, tmp_path = tempfile.mkstemp(dir=directory or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # cleanup temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_bytes(path, data):
    """
    Binary data (e.g. pickle cache) ko atomically likhta hai.
    Same guarantee as atomic_write_json - no half-written files.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def chunk_text(text, max_length=3800):
    """
    Long text ko Telegram message limit (4096 chars) ke andar
    chunks mein todta hai. Line boundaries par todne ki koshish
    karta hai taaki words/beech mein na toote.

    Use telegram_alerts.py, bot_listener.py, master_dashboard.py
    mein long messages bhejne se pehle.
    """
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        # max_length ke andar last newline dhoondo
        cut = remaining.rfind("\n", 0, max_length)
        if cut == -1:
            # newline nahi mila, space dhoondo
            cut = remaining.rfind(" ", 0, max_length)
        if cut == -1:
            # space bhi nahi, hard cut
            cut = max_length
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks
