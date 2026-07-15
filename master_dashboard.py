# master_dashboard.py
"""
===========================================================
 MASTER TRADING DASHBOARD (PRO FORMATTING)
===========================================================
BUGS JO FIX KIYE:
1. yfinance ka multi-index column order yfinance version ke hisab se
   badal sakta hai (kabhi ticker level 0 par, kabhi field level 0 par)
   - purana code sirf EK order assume karta tha, dusre order mein
   silently khaali data deta (sab stocks ka CMP = Entry dikhta, 0%
   P&L, galat). Ab dono order defensively check karte hain (jaisा
   downloader.py mein already hota hai).
2. Apna alag, bina-headers wala yfinance call kar raha tha (rate-limit
   risk + baaki codebase se inconsistent). Ab tracker.py wala SAME
   session (browser-like headers ke saath) use karta hai.
===========================================================
"""

import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo
from database import get_db_connection
from utils import clean_symbol, escape_html
from market_data_fetcher import fetch_latest_close_batch
from logger import logger
from telegram_alerts import CARD_DIVIDER

# V8.2.0 FIX: IST-aware date header - UTC servers par bhi sahi date dikhe.
_IST = ZoneInfo("Asia/Kolkata")

try:
    from tracker import session  # browser-headers wala shared session (rate-limit safe)
except ImportError:
    import requests
    session = requests.Session()


def _extract_close_price(df, symbol, multi_ticker):
    """
    yfinance ka group_by='ticker' response version ke hisab se columns
    ko alag order mein rakh sakta hai - dono order defensively try karte hain.
    """
    try:
        if not multi_ticker:
            return round(float(df["Close"].iloc[-1]), 2)

        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            if symbol in df.columns.get_level_values(0):
                sub = df[symbol]
            elif symbol in df.columns.get_level_values(1):
                sub = df.xs(symbol, axis=1, level=1)
            else:
                return None
            return round(float(sub["Close"].iloc[-1]), 2)
    except Exception:
        return None
    return None


def generate_master_trading_dashboard():
    """
    MASTER TRADING DASHBOARD (V8.3.0 G4 redesign — Pro Formatting)
    - Header: 👑 + bold title + italic IST date + divider.
    - Each stock: compact 4-line card, bold English name, emoji status,
      entry/CMP/target/SL, R:R badge, P&L badge.
    - Color coding: 🟢 profit, 🔴 loss, 🎉 target hit, 🛑 SL hit.
    - Footer: Hinglish disclaimer (italic).
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # Active aur haal hi me close hue trades nikalen
    cursor.execute("SELECT * FROM recommendations ORDER BY date_added DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "📭 <b>Master Dashboard khaali hai.</b>\n<i>Abhi koi trade available nahi hai.</i>"

    # V8.1.2: NSE Chart/Stooq (primary) -> yfinance (secondary, shared session) - live CMP
    tickers = list(set([r["stock"] for r in rows if r["status"] == 'OPEN']))
    live_prices = {}

    def _yfinance_close_fallback(pending_symbols):
        out = {}
        try:
            df = yf.download(" ".join(pending_symbols), period="1d", group_by="ticker", session=session, progress=False, auto_adjust=True)
            multi_ticker = len(pending_symbols) > 1
            for stk in pending_symbols:
                price = _extract_close_price(df, stk, multi_ticker)
                if price is not None:
                    out[stk] = price
        except Exception as e:
            logger.warning(f"Batch price (yfinance secondary) error: {e}")
        return out

    if tickers:
        try:
            live_prices = fetch_latest_close_batch(tickers, yfinance_batch_fallback=_yfinance_close_fallback)
        except Exception as e:
            logger.warning(f"Batch price error: {e}")

    # Header (IST-aware date).
    today_str = datetime.now(_IST).strftime("%a, %d %b %Y")
    lines = [
        "👑 <b>MASTER AI TRADING DASHBOARD</b> 👑",
        f"📅 <i>{escape_html(today_str)}</i>",
        f"{CARD_DIVIDER}\n",
    ]

    for r in rows:
        # V8.3.0 (G4): clean_symbol first (.NS hatata hai, ^NSEI -> NIFTY50),
        # phir escape_html (M&M -> M&amp;M safe), phir <b> wrap. Stock name
        # ALWAYS English bold - never translated (per design standard).
        stock = f"<b>{escape_html(clean_symbol(r['stock']))}</b>"
        entry = float(r["entry_price"]) if r["entry_price"] else 0.0
        target = float(r["target_1"]) if r["target_1"] else 0.0
        sl = float(r["sl_price"]) if r["sl_price"] else 0.0
        status = r["status"]

        # CMP Determination
        # V8.2.0: For OPEN trades, agar live_prices mein price nahi mila
        # to "entry" use hota hai (display fallback). Ise clearly annotate
        # karte hain taaki user "0% P&L" ko stale-data na samjhe.
        cmp_unavailable = False
        if status in ['FULL_TARGET', 'SL_HIT'] and r["closed_price"]:
            cmp = float(r["closed_price"])
        else:
            cmp = live_prices.get(r["stock"])
            if cmp is None:
                cmp = entry  # fallback display
                cmp_unavailable = True

        pnl_pct = ((cmp - entry) / entry) * 100 if entry > 0 else 0.0

        # Risk:Reward badge (entry vs SL vs target_1) - defensive for None.
        rr_str = "N/A"
        if entry and sl and target and (entry - sl) > 0:
            rr_val = (target - entry) / (entry - sl)
            rr_str = f"1:{rr_val:.2f}"

        # Highlighting & Color Coding Logic
        # V8.3.0 (G4): cleaner compact card - 4 lines per stock, emoji-led
        # status, English bold stock name (per design standard).
        if status == 'FULL_TARGET' or (target > 0 and cmp >= target):
            header = f"🎉 {stock} | 🎯 <b>TARGET HIT</b>"
            pnl_badge = f"📈 Profit: 🟢 <b>+{pnl_pct:.2f}%</b>"
        elif status == 'SL_HIT' or (sl > 0 and cmp <= sl and cmp > 0):
            header = f"🛑 {stock} | 📉 <b>STOPLOSS HIT</b>"
            pnl_badge = f"📉 Loss: 🔴 <b>{pnl_pct:.2f}%</b>"
        elif cmp_unavailable:
            # V8.2.0: live price unavailable - don't show fake 0% P&L
            header = f"⏳ {stock} | 📊 <b>OPEN</b>"
            pnl_badge = f"⚡ Live CMP abhi fetch nahi ho payi"
        elif pnl_pct >= 0:
            header = f"🟢 {stock} | 🚀 <b>IN PROFIT</b>"
            pnl_badge = f"📈 Return: 🟢 <b>+{pnl_pct:.2f}%</b>"
        else:
            header = f"🔴 {stock} | 📉 <b>IN LOSS</b>"
            pnl_badge = f"📉 Return: 🔴 <b>{pnl_pct:.2f}%</b>"

        cmp_display = "⚡ N/A" if cmp_unavailable else f"<b>₹{cmp}</b>"
        # Compact card - 4 lines per stock (header / levels / CMP+RR / P&L).
        card = (
            f"{header}\n"
            f"💵 Entry: ₹{entry} | 🎯 T1: ₹{target} | 🛑 SL: ₹{sl}\n"
            f"⚡ CMP: {cmp_display} | 📊 R:R: {rr_str}\n"
            f"{pnl_badge}\n"
            f"{CARD_DIVIDER}"
        )
        lines.append(card)

    lines.append(
        "💡 <i>Yeh data AI technical parameters (EMA 20/44/200, RSI, ADX) "
        "par aadharit hai. Apna research karke hi trade karein.</i>"
    )
    return "\n".join(lines)
