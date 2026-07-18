# tracker.py
import sqlite3
import yfinance as yf
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import get_db_connection, db_transaction
from telegram_alerts import send_telegram_text, CARD_DIVIDER
from targets import calculate_targets
from utils import clean_symbol, escape_html
from market_data_fetcher import fetch_latest_close_batch, fetch_latest_ohlc_batch
from logger import logger

# V8.2.0 FIX: timezone-aware datetime - UTC servers par bhi IST date
# use hogi (NSE market IST par chalti hai, warna after 18:30 UTC
# today_str agle din ki ho jaati thi - weekly/monthly reports +
# closed_date sab galat ho jaate the).
IST = ZoneInfo("Asia/Kolkata")

# --- Aapke existing translator.py se directly import ---
try:
    from translator import to_hindi
except ImportError:
    logger.warning("translator.py nahi mila! Fallback English par set hai.")
    def to_hindi(text, max_len=450):
        return text

# Yahoo Rate limit se bachne ke liye Fake Browser Header Session Setup
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})


def get_priority_watchlist_symbols(recent_days=5):
    """
    Return: list of stock symbols jo OPEN hain ya recent picks rahe hain.
    """
    start_date = (datetime.now(IST) - timedelta(days=recent_days)).strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT stock FROM recommendations WHERE status = 'OPEN' OR date_added >= ?",
        (start_date,),
    )
    rows = cursor.fetchall()
    conn.close()

    return [r["stock"] for r in rows]


def generate_target_hit_stocks():
    """Jin stocks ka koi bhi target hit hua hai - attractive format (V8.3.0 G4)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM recommendations WHERE t1_hit = 1 OR t2_hit = 1 OR t3_hit = 1 OR status = 'FULL_TARGET' "
        "ORDER BY date_added DESC LIMIT 15"
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return to_hindi("Target Hit Stocks: Abhi tak koi target hit nahi hua hai.")

    # V8.3.0 (G4): header card + per-stock card with 🎉 emojis + bold
    # English stock name (clean_symbol + escape_html, never translated).
    lines = [
        "🎉 <b>SUCCESS REPORT: TARGET HIT STOCKS</b> 🎉",
        f"{CARD_DIVIDER}\n",
    ]
    for r in rows:
        # V8.2.0 FIX (bug #1): sqlite3.Row does NOT support .get() - use
        # dict(r) so we can safely use .get() with default None.
        row = dict(r)
        # V8.3.0 (G4): clean_symbol first (.NS hatata hai, ^NSEI -> NIFTY50),
        # phir escape_html (M&M -> M&amp;M safe), phir <b> wrap. NEVER
        # translate stock name (per design standard - always English bold).
        stk = f"<b>{escape_html(clean_symbol(row['stock']))}</b>"
        hits = []
        if row["t1_hit"]:
            hits.append("🎯 T1")
        if row["t2_hit"]:
            hits.append("🚀 T2")
        if row["t3_hit"] or row["status"] == "FULL_TARGET":
            hits.append("🔥 T3/Final")

        pnl_str = ""
        # V8.2.0 FIX (bug #27): use `is not None` instead of truthy check.
        if row.get("closed_price") is not None and row.get("entry_price") is not None:
            pct = ((row["closed_price"] - row["entry_price"]) / row["entry_price"]) * 100
            pnl_str = f" | 📈 Return: 🟢 <b>{pct:+.1f}%</b>"

        lines.append(
            f"🎉 {stk} ➔ {' | '.join(hits)}\n"
            f"💵 Entry: ₹{row['entry_price']}{pnl_str}\n"
            f"{CARD_DIVIDER}"
        )
    return "\n".join(lines)


def generate_best_rr_stocks(top_n=5):
    """OPEN positions mein sabse achha Risk:Reward wale stocks (V8.3.0 G4)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recommendations WHERE status = 'OPEN'")
    rows = cursor.fetchall()
    conn.close()

    scored = []
    for r in rows:
        entry, sl, t1 = r["entry_price"], r["sl_price"], r["target_1"]
        if entry is None or sl is None or t1 is None or (entry - sl) <= 0:
            continue
        rr = (t1 - entry) / (entry - sl)
        scored.append((r["stock"], rr, entry, sl, t1))

    if not scored:
        return to_hindi("Best Risk:Reward Stocks: Abhi koi OPEN position nahi hai jiske liye R:R calculate ho sake.")

    scored.sort(key=lambda x: x[1], reverse=True)

    # V8.3.0 (G4): header card + per-stock card with 📊 R:R badge + bold
    # English stock name (clean_symbol + escape_html, never translated).
    lines = [
        "📊 <b>TOP RISK:REWARD SETUPS (OPEN TRADES)</b>",
        f"{CARD_DIVIDER}\n",
    ]
    for stock, rr, entry, sl, t1 in scored[:top_n]:
        # V8.3.0 (G4): clean_symbol + escape_html + <b> wrap (M&M safe).
        # Never translate stock name (per design standard - always English bold).
        stk = f"<b>{escape_html(clean_symbol(stock))}</b>"
        lines.append(
            f"📊 {stk} | R:R = <b>1:{rr:.2f}</b>\n"
            f"💵 Entry: ₹{entry} | 🛑 SL: ₹{sl} | 🎯 T1: ₹{t1}\n"
            f"{CARD_DIVIDER}"
        )
    return "\n".join(lines)


def add_recommendations_to_db(top_buys):
    """Naye recommendations ko database mein secure entry karne ke liye"""
    # V8.2.0 FIX (bug #11): timezone-aware IST datetime.
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    # V8.2.0 FIX (bug #3 pattern): use db_transaction context manager so
    # exception pe auto-rollback + auto-close hota hai (no leak).
    with db_transaction() as cursor:
        for r in top_buys:
            stock = r["Stock"]
            cursor.execute("SELECT id FROM recommendations WHERE stock = ? AND status = 'OPEN'", (stock,))
            if cursor.fetchone():
                continue

            try:
                patterns_str = ", ".join(r['Patterns']) if r['Patterns'] else "None"
                entry = float(r["Entry"])
                sl = float(r["Stoploss"])
                entry_low = r.get("Entry_Low")
                entry_high = r.get("Entry_High")

                t1, t2, t3 = calculate_targets(entry, sl)
                if t1 is None:
                    continue

                cursor.execute('''
                    INSERT INTO recommendations (stock, date_added, entry_price, entry_low, entry_high, sl_price, target_1, target_2, target_3, status, signal, score, patterns)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)
                ''', (stock, today_str, entry, entry_low, entry_high, sl, t1, t2, t3, r["Signal"], int(r["Score"]), patterns_str))
            except Exception as e:
                logger.error(f"Error inserting {stock} to DB: {e}")


def check_live_market_hits(chat_id=None):
    """Saare open stocks ka live rate check karke colorful report dena"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recommendations WHERE status = 'OPEN'")
    open_positions = cursor.fetchall()
    
    if not open_positions:
        conn.close()
        return (
            "📭 <b>Watchlist Status</b>\n"
            f"{CARD_DIVIDER}\n"
            "Abhi database mein koi OPEN position nahi hai jise monitor kiya jaye. 🙏"
        )
        
    # V8.2.0 FIX (bug #11): timezone-aware IST datetime.
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    tickers = [pos["stock"] for pos in open_positions]
    tickers_str = " ".join(tickers)
    
    summary_lines = []
    hits_triggered = 0
    
    def _yfinance_ohlc_fallback(pending_symbols):
        """V8.1.2: original yfinance batch logic, ab sirf PENDING (primary se na mile) symbols ke liye chalta hai."""
        out = {}
        pend_str = " ".join(pending_symbols)
        raw = yf.download(tickers=pend_str, period="1d", group_by="ticker", session=session, progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return out
        for stock in pending_symbols:
            sdf = raw if len(pending_symbols) == 1 else (raw[stock] if hasattr(raw.columns, 'nlevels') and raw.columns.nlevels > 1 and stock in raw.columns.get_level_values(0) else None)
            if sdf is None or sdf.empty:
                continue
            try:
                out[stock] = {
                    "High": round(float(sdf['High'].iloc[-1]), 2),
                    "Low": round(float(sdf['Low'].iloc[-1]), 2),
                    "Close": round(float(sdf['Close'].iloc[-1]), 2),
                }
            except Exception:
                continue
        return out

    try:
        # V8.1.2: Stooq (genuine OHLC) primary -> yfinance batch secondary (sirf pending symbols ke liye)
        ohlc_map = fetch_latest_ohlc_batch(tickers, yfinance_batch_fallback=_yfinance_ohlc_fallback)
        if not ohlc_map:
            conn.close()
            return "❌ <b>Data Error:</b> Live data fetch nahi ho paya (primary + secondary dono fail)."

        for pos in open_positions:
            pos = dict(pos)  # V8.2.0 FIX (bug #1): sqlite3.Row -> dict for safe .get()
            stock = pos["stock"]
            pos_id = pos["id"]
            entry = pos["entry_price"]
            sl = pos["sl_price"]
            t1, t2, t3 = pos["target_1"], pos["target_2"], pos["target_3"]
            t1_hit, t2_hit, t3_hit = pos["t1_hit"], pos["t2_hit"], pos["t3_hit"]

            stock_ohlc = ohlc_map.get(stock)
            if stock_ohlc is None:
                continue

            try:
                today_high = stock_ohlc["High"]
                today_low = stock_ohlc["Low"]
                current_price = stock_ohlc["Close"]
            except Exception:
                continue

            # V8.3.0 (G4): clean_symbol + escape_html + bold wrap - stock name
            # ALWAYS English (never translated, per design standard).
            # Reverse ordering (escape-first) broke M&M, L&T, J&K Bank jaise
            # stocks earlier - ab clean_symbol+escape_html safe hai.
            stk_name = escape_html(clean_symbol(stock)) if isinstance(stock, str) else stock
            pnl_pct = ((current_price - entry) / entry) * 100
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"

            status_text = f"• <b>{stk_name}</b> | 💥 CMP: ₹{current_price} ({pnl_emoji} {pnl_pct:+.1f}%) | 📈 High: ₹{today_high}"

            # V8.2.0 FIX (bug #16): TARGET checks PEHLE, SL check BAAD
            # mein (so that SL overrides status if both hit same day -
            # conservative risk-management approach). Pehle elif chain
            # tha jisme sirf EK event detect hota tha - agar T1 hit +
            # SL hit ek hi din hua to sirf SL record hota tha, T1 ka
            # partial-profit booking LOST ho jaata tha.
            #
            # New order:
            #   1. T3 (elif T2 elif T1) - sets t*_hit flags + status=FULL_TARGET (only T3)
            #   2. SL - if hit, override status to SL_HIT + closed_price=sl
            #           (t*_hit flags preserve any partial-profit booking)
            if today_high >= t3 and not t3_hit:
                cursor.execute(
                    "UPDATE recommendations SET t3_hit = 1, status = 'FULL_TARGET', closed_date = ?, closed_price = ? WHERE id = ?",
                    (today_str, t3, pos_id),
                )
                # V8.2.0 FIX (bug #3): COMMIT AFTER EACH UPDATE+send pair,
                # so that DB state matches alerts already sent. Agar next
                # iteration throw kare to is iteration ka UPDATE persist
                # raha, duplicate alerts next run se bache.
                conn.commit()
                # V8.3.0 (G4): redesigned alert - bold stock name, clean
                # levels, emoji-led header (Hinglish).
                alert_msg = (
                    f"🎉 <b>FULL TARGET HIT!</b> 🔥\n"
                    f"{CARD_DIVIDER}\n"
                    f"📊 <b>{escape_html(clean_symbol(stock))}</b> ne Final Target 3 hit kar diya!\n"
                    f"💵 Entry: ₹{entry} | 🎯 T3: ₹{t3:.2f} | 📈 Profit: 🟢 <b>+{((t3-entry)/entry)*100:.1f}%</b>\n"
                    f"{CARD_DIVIDER}"
                )
                send_telegram_text(alert_msg)
                status_text += " ➔ 🎉 <b>T3 FULL HIT 🔥</b>"
                hits_triggered += 1
            elif today_high >= t2 and not t2_hit:
                cursor.execute('UPDATE recommendations SET t2_hit = 1 WHERE id = ?', (pos_id,))
                conn.commit()  # V8.2.0 FIX (bug #3)
                alert_msg = (
                    f"🎉 <b>TARGET 2 HIT!</b> 🚀\n"
                    f"{CARD_DIVIDER}\n"
                    f"📊 <b>{escape_html(clean_symbol(stock))}</b> ne Target 2 hit kar diya!\n"
                    f"💵 Entry: ₹{entry} | 🎯 T2: ₹{t2:.2f} | 📈 Profit: 🟢 <b>+{((t2-entry)/entry)*100:.1f}%</b>\n"
                    f"{CARD_DIVIDER}"
                )
                send_telegram_text(alert_msg)
                status_text += " ➔ 🎉 <b>T2 HIT 🚀</b>"
                hits_triggered += 1
            elif today_high >= t1 and not t1_hit:
                cursor.execute('UPDATE recommendations SET t1_hit = 1 WHERE id = ?', (pos_id,))
                conn.commit()  # V8.2.0 FIX (bug #3)
                alert_msg = (
                    f"🎉 <b>TARGET 1 HIT!</b> 🎯\n"
                    f"{CARD_DIVIDER}\n"
                    f"📊 <b>{escape_html(clean_symbol(stock))}</b> ne Target 1 hit kar diya!\n"
                    f"💵 Entry: ₹{entry} | 🎯 T1: ₹{t1:.2f} | 📈 Profit: 🟢 <b>+{((t1-entry)/entry)*100:.1f}%</b>\n"
                    f"{CARD_DIVIDER}"
                )
                send_telegram_text(alert_msg)
                status_text += " ➔ 🎉 <b>T1 HIT ✅</b>"
                hits_triggered += 1

            # SEPARATE if (not elif) - agar SL bhi aaj hit hua to status
            # override karke SL_HIT set karo (conservative risk outcome).
            # t1/t2/t3_hit flags (agar just-above set hue) preserve rahenge
            # - matlab partial-profit-then-SL scenario properly recorded.
            if today_low <= sl:
                # V8.2.0 FIX (bug #6): closed_price = SL price (not
                # today_low). today_low could be far below SL on gap-down,
                # overstating loss. SL trigger price = `sl` itself.
                cursor.execute(
                    "UPDATE recommendations SET status = 'SL_HIT', closed_date = ?, closed_price = ? WHERE id = ?",
                    (today_str, sl, pos_id),
                )
                conn.commit()  # V8.2.0 FIX (bug #3)
                # V8.3.0 (G4): redesigned SL alert - bold stock name, clean
                # levels, emoji-led header (Hinglish).
                alert_msg = (
                    f"🛑 <b>STOPLOSS HIT!</b> 📉\n"
                    f"{CARD_DIVIDER}\n"
                    f"📊 <b>{escape_html(clean_symbol(stock))}</b> ka stoploss trigger ho gaya!\n"
                    f"💵 Entry: ₹{entry} | 🛑 SL: ₹{sl:.2f} | 📉 Loss: 🔴 <b>{((sl-entry)/entry)*100:.1f}%</b>\n"
                    f"{CARD_DIVIDER}"
                )
                send_telegram_text(alert_msg)
                status_text += " ➔ 🛑 <b>SL HIT</b>"
                hits_triggered += 1
                
            summary_lines.append(status_text)
    except Exception as e:
        logger.error(f"Live tracking failed: {e}")
        conn.close()
        return f"❌ <b>Error:</b> Live tracking engine me problem aayi: {e}"
        
    conn.close()
    
    response_msg = (
        "⚡ <b>LIVE MARKET MONITORING REPORT</b> ⚡\n"
        f"{CARD_DIVIDER}\n"
    )
    response_msg += "\n".join(summary_lines) if summary_lines else "Watchlist data render nahi ho paya."
    if hits_triggered == 0:
        response_msg += (
            f"\n{CARD_DIVIDER}\n"
            "🛡️ <i>Abhi koi naya Target ya Stoploss break nahi hua hai. Saari positions safe hain!</i>"
        )
    return response_msg


def _sectoral_indices_section():
    """
    NAYA (document requirement, 4 PM report): Sectoral indices ka
    aaj ka % change dikhata hai (Nifty Bank, Nifty IT, waghera).
    """
    from config import SECTORAL_INDICES_ENABLED
    if not SECTORAL_INDICES_ENABLED:
        return ""

    from nse_market_data import get_sectoral_indices_performance
    sectors = get_sectoral_indices_performance()
    if not sectors:
        return f"📊 <b>SECTORAL PERFORMANCE</b>\n{CARD_DIVIDER}\nAbhi data available nahi hai.\n\n"

    # V8.3.0 (G4): header card + per-sector line (clean format).
    lines = ["📊 <b>SECTORAL INDICES PERFORMANCE</b>", CARD_DIVIDER]
    for s in sectors:
        arrow = "🟢" if s["pct_change"] >= 0 else "🔴"
        lines.append(f"{arrow} <b>{escape_html(s['name'])}</b>: {s['pct_change']:+.2f}% (₹{s['last_close']})")
    return "\n".join(lines) + f"\n{CARD_DIVIDER}\n\n"


def generate_daily_performance_report():
    """Shaam ko pure database ka accuracy analytics scorecard (V8.3.0 G4)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recommendations")
    all_data = cursor.fetchall()
    conn.close()
    
    if not all_data:
        return to_hindi("Performance Report: Database mein koi records available nahi hain.")
        
    total_trades = len(all_data)
    full_target_hits = sum(1 for x in all_data if x["status"] == "FULL_TARGET")
    sl_hits = sum(1 for x in all_data if x["status"] == "SL_HIT")
    open_count = sum(1 for x in all_data if x["status"] == "OPEN")
    
    # V8.2.0 FIX (bug #7): Win-rate ko misleading banane wala bug.
    # Trades jo T1/T2 hit karke baad mein SL hit karte hain (status=
    # SL_HIT, lekin t1_hit=1 ya t2_hit=1) wo pure-loss count hote the.
    # Ab inhe separately "PARTIAL (T1/T2 hit + SL hit)" category mein
    # count karte hain aur win-rate denominator se nikalte hain (kyunki
    # inka net outcome purely loss nahi hota - partial profit booked).
    partial_hits = sum(
        1 for x in all_data
        if x["status"] == "SL_HIT" and (x["t1_hit"] or x["t2_hit"])
    )
    pure_sl_hits = sl_hits - partial_hits
    
    closed_trades = full_target_hits + pure_sl_hits
    win_rate = (full_target_hits / closed_trades * 100) if closed_trades > 0 else 0.0

    # V8.3.0 (G4): best/worst performer calculate karo (closed trades mein se).
    full_target_rows = [
        x for x in all_data
        if x["status"] == "FULL_TARGET"
        and x["closed_price"] is not None and x["entry_price"] is not None
    ]
    pure_sl_rows = [
        x for x in all_data
        if x["status"] == "SL_HIT" and not (x["t1_hit"] or x["t2_hit"])
        and x["closed_price"] is not None and x["entry_price"] is not None
    ]
    best_row = max(
        full_target_rows,
        key=lambda r: (r["closed_price"] - r["entry_price"]) / r["entry_price"],
        default=None,
    )
    worst_row = min(
        pure_sl_rows,
        key=lambda r: (r["closed_price"] - r["entry_price"]) / r["entry_price"],
        default=None,
    )

    # V8.1.2: sectoral indices section sabse upar (document requirement)
    try:
        report = _sectoral_indices_section()
    except Exception as e:
        logger.warning(f"Sectoral indices section fail: {e}")
        report = ""

    # V8.3.0 (G4): summary card with win-rate front-and-center, best/worst
    # performer badges (English bold stock names per design standard).
    report += "📊 <b>DAILY ACCURACY & PERFORMANCE SCORECARD</b>\n"
    report += f"{CARD_DIVIDER}\n"
    report += f"📌 Total Picks: <b>{total_trades}</b> | ⏳ Active: <b>{open_count}</b>\n"
    report += f"🟢 Target Hit: <b>{full_target_hits} ✅</b> | 🔴 SL Hit: <b>{pure_sl_hits} 🚨</b>\n"
    if partial_hits > 0:
        report += f"🟡 Partial (T1/T2 + SL): <b>{partial_hits} ⚠️</b>\n"
    report += f"{CARD_DIVIDER}\n"
    report += f"🏆 <b>SYSTEM WIN-RATE: {win_rate:.1f}%</b>\n"
    if partial_hits > 0:
        report += f"<i>📋 Note: {partial_hits} partial-profit-then-SL trades alag se dikhaye gaye hain (pure-loss count nahi hue).</i>\n"
    # Best/worst performer card.
    if best_row is not None:
        best_pct = (best_row["closed_price"] - best_row["entry_price"]) / best_row["entry_price"] * 100
        best_stk = escape_html(clean_symbol(best_row["stock"]))
        report += f"🥇 <b>Best Performer:</b> <b>{best_stk}</b> 🟢 <b>+{best_pct:.2f}%</b>\n"
    if worst_row is not None:
        worst_pct = (worst_row["closed_price"] - worst_row["entry_price"]) / worst_row["entry_price"] * 100
        worst_stk = escape_html(clean_symbol(worst_row["stock"]))
        report += f"⚠️ <b>Worst Performer:</b> <b>{worst_stk}</b> 🔴 <b>{worst_pct:.2f}%</b>\n"
    report += f"{CARD_DIVIDER}\n\n"
    
    report += generate_watchlist_summary()
    return report


def _period_stats(rows):
    """Weekly/monthly period ke stats. V8.2.0 FIX (bug #7): partial-then-SL
    category add ki - trades jo T1/T2 hit karke baad mein SL hit kiye wo
    ab pure-loss count nahi hote, alag se 'partial' category mein jaate hain."""
    total = len(rows)
    full_target = [r for r in rows if r["status"] == "FULL_TARGET"]
    # V8.2.0 FIX (bug #7): SL_HIT mein se jo T1/T2 hit kar chuke the
    # unhe alag se nikal lo - ye 'partial profit + SL' trades hain,
    # pure-loss nahi.
    sl_hit_pure = [
        r for r in rows
        if r["status"] == "SL_HIT" and not (r["t1_hit"] or r["t2_hit"])
    ]
    sl_hit_partial = [
        r for r in rows
        if r["status"] == "SL_HIT" and (r["t1_hit"] or r["t2_hit"])
    ]
    open_count = sum(1 for r in rows if r["status"] == "OPEN")

    closed = len(full_target) + len(sl_hit_pure)
    win_rate = (len(full_target) / closed * 100) if closed > 0 else 0.0

    # V8.2.0 FIX (bug #27): use `is not None` instead of truthy check.
    gains = [
        (r["stock"], (r["closed_price"] - r["entry_price"]) / r["entry_price"] * 100)
        for r in full_target
        if r["closed_price"] is not None and r["entry_price"] is not None
    ]
    losses = [
        (r["stock"], (r["closed_price"] - r["entry_price"]) / r["entry_price"] * 100)
        for r in sl_hit_pure
        if r["closed_price"] is not None and r["entry_price"] is not None
    ]

    return {
        "total": total, "full_target": len(full_target), "sl_hit": len(sl_hit_pure),
        "partial": len(sl_hit_partial),
        "open": open_count, "win_rate": win_rate, 
        "best": max(gains, key=lambda x: x[1]) if gains else None, 
        "worst": min(losses, key=lambda x: x[1]) if losses else None,
    }


def _format_period_report(title, stats):
    # V8.3.0 (G4): header card + summary + best/worst performer with
    # clean_symbol+escape_html bold English stock names.
    lines = [f"📅 <b>{escape_html(title)}</b>", CARD_DIVIDER]
    lines.append(f"📌 Total Picks: <b>{stats['total']}</b> | ⏳ Active: <b>{stats['open']}</b>")
    lines.append(f"🟢 Target Hit: <b>{stats['full_target']} ✅</b> | 🔴 SL Hit: <b>{stats['sl_hit']} 🚨</b>")
    # V8.2.0 FIX (bug #7): partial category display (only if non-zero).
    if stats.get("partial", 0) > 0:
        lines.append(f"🟡 Partial (T1/T2 + SL): <b>{stats['partial']} ⚠️</b>")
    lines.append(CARD_DIVIDER)
    lines.append(f"🏆 <b>WIN-RATE: {stats['win_rate']:.1f}%</b>")
    lines.append("")

    if stats["best"]:
        # V8.3.0 (G4): clean_symbol + escape_html + bold (never translated).
        best_stk = escape_html(clean_symbol(stats['best'][0]))
        lines.append(f"🥇 <b>Best Performer:</b> <b>{best_stk}</b> 🟢 <b>+{stats['best'][1]:.2f}%</b>")
    if stats["worst"]:
        worst_stk = escape_html(clean_symbol(stats['worst'][0]))
        lines.append(f"⚠️ <b>Worst Performer:</b> <b>{worst_stk}</b> 🔴 <b>{stats['worst'][1]:.2f}%</b>")

    return "\n".join(lines)


def generate_weekly_report():
    # V8.2.0 FIX (bug #11): timezone-aware IST datetime.
    start_date = (datetime.now(IST) - timedelta(days=7)).strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recommendations WHERE date_added >= ?", (start_date,))
    rows = cursor.fetchall()
    conn.close()

    if not rows: return to_hindi("Weekly Report: Pichhle 7 din mein koi recommendation generate nahi hua.")
    return _format_period_report("WEEKLY PERFORMANCE REPORT", _period_stats(rows))


def generate_monthly_report():
    # V8.2.0 FIX (bug #11): timezone-aware IST datetime.
    start_date = datetime.now(IST).strftime("%Y-%m-01")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recommendations WHERE date_added >= ?", (start_date,))
    rows = cursor.fetchall()
    conn.close()

    if not rows: return to_hindi("Monthly Report: Is mahine koi recommendation generate nahi hua.")
    return _format_period_report("MONTHLY PERFORMANCE REPORT", _period_stats(rows))


def get_todays_swing_recommendations():
    """
    V8.1.2 NAYA: 10 AM Swing Trading Chart Digest ke liye - AAJ (date_added
    = today) ke fresh recommendations laata hai (jo 9:20 AM ke scan se
    already database mein aa chuke hain). Ye run_scan_pipeline() ko
    DOBARA scan karne ke liye call NAHI karta - sirf already-existing
    fresh records ko query karta hai, taaki 10 AM slot lightweight rahe
    aur koi duplicate API/rate-limit load na ho.

    Return: list of sqlite3.Row objects (poore recommendation columns
    ke saath, entry/sl/target/stock/patterns waghera) ya [] (koi na mile)
    """
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # V8.2.0 FIX (bug #11): timezone-aware IST datetime.
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    cursor.execute(
        "SELECT * FROM recommendations WHERE date_added = ? ORDER BY score DESC",
        (today_str,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def generate_watchlist_summary():
    """🔥 ATTRACTIVE WATCHLIST & PROFIT TRACKER WITH % P&L 🔥"""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recommendations WHERE status = 'OPEN' ORDER BY date_added DESC")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return (
            "📭 <b>ACTIVE WATCHLIST KHALI HAI</b>\n"
            f"{CARD_DIVIDER}\n"
            "<i>Abhi koi bhi trade active running mein nahi hai. Naye breakouts ke liye scan ka wait karein! 🚀</i>"
        )

    tickers = [r["stock"] for r in rows]
    live_prices = {}

    def _yfinance_close_fallback(pending_symbols):
        """V8.1.2: original yfinance batch logic - sirf PENDING symbols ke liye."""
        out = {}
        pend_str = " ".join(pending_symbols)
        raw = yf.download(tickers=pend_str, period="1d", group_by="ticker", session=session, progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return out
        for stock in pending_symbols:
            sdf = raw if len(pending_symbols) == 1 else (raw[stock] if hasattr(raw.columns, 'nlevels') and raw.columns.nlevels > 1 and stock in raw.columns.get_level_values(0) else None)
            if sdf is not None and not sdf.empty:
                try:
                    out[stock] = round(float(sdf['Close'].iloc[-1]), 2)
                except Exception:
                    continue
        return out

    try:
        if tickers:
            # V8.1.2: NSE Chart/Stooq (primary, close-only) -> yfinance (secondary)
            live_prices = fetch_latest_close_batch(tickers, yfinance_batch_fallback=_yfinance_close_fallback)
    except Exception as e:
        logger.warning(f"Watchlist batch price download failed: {e}")

    # V8.3.0 (G4): header card + per-stock card (bold English name, emoji
    # status, clean levels) + overall summary footer.
    lines = ["🔥 <b>LIVE WATCHLIST & PROFIT TRACKER</b> 🔥", f"{CARD_DIVIDER}\n"]
    total_profit_pct = 0.0
    active_count = len(rows)

    for r in rows:
        # V8.3.0 (G4): clean_symbol + escape_html + bold (never translated).
        stock = f"<b>{escape_html(clean_symbol(r['stock']))}</b>"
        # V8.2.0 FIX (bug #27): use `is not None` instead of truthy check.
        entry = float(r["entry_price"]) if r["entry_price"] is not None else 0.0
        sl = float(r["sl_price"]) if r["sl_price"] is not None else 0.0
        t1 = float(r["target_1"]) if r["target_1"] is not None else 0.0

        try:
            # V8.2.0 FIX (bug #11): timezone-aware IST datetime.
            days_running = max((datetime.now(IST) - datetime.strptime(r["date_added"], "%Y-%m-%d")).days, 1)
        except Exception:
            days_running = 1

        cmp = live_prices.get(r["stock"], entry)
        pnl_pct = ((cmp - entry) / entry) * 100 if entry > 0 else 0.0
        total_profit_pct += pnl_pct

        if cmp <= sl and cmp > 0:
            status_badge = f"🔴 <b>SL HIT ({((sl - entry)/entry)*100:.1f}%)</b>"
            pnl_str = f"📉 Down: <code>₹{entry - cmp:.2f} ({pnl_pct:.2f}%)</code>"
        elif cmp >= t1 and cmp > 0:
            status_badge = f"🎉 <b>TARGET HIT (+{pnl_pct:.1f}%)</b>"
            pnl_str = f"📈 Gain: <code>+₹{cmp - entry:.2f} (+{pnl_pct:.2f}%)</code>"
        elif pnl_pct >= 0:
            status_badge = "🟢 <b>IN PROFIT</b>"
            pnl_str = f"🚀 Gain: <code>+₹{cmp - entry:.2f} (+{pnl_pct:.2f}%)</code>"
        else:
            status_badge = "🔴 <b>IN LOSS</b>"
            pnl_str = f"⚠️ Down: <code>₹{entry - cmp:.2f} ({pnl_pct:.2f}%)</code>"

        card = (
            f"📌 {stock} | {status_badge}\n"
            f"💵 Entry: <b>₹{entry}</b> ➔ CMP: <b>₹{cmp}</b> | {pnl_str}\n"
            f"🎯 T1: ₹{t1} | 🛑 SL: ₹{sl} | ⏳ Running: <b>{days_running} Days</b>\n"
            f"{CARD_DIVIDER}"
        )
        lines.append(card)

    avg_pnl = total_profit_pct / active_count if active_count > 0 else 0
    avg_emoji = "🟢" if avg_pnl >= 0 else "🔴"
    lines.append(
        f"\n📊 <b>OVERALL WATCHLIST RETURN</b>\n"
        f"👥 Active Trades: <b>{active_count}</b> | {avg_emoji} Avg Return: <b>{avg_pnl:+.2f}%</b>"
    )
    return "\n".join(lines)


def generate_evening_summary():
    from calendar import monthrange
    from config import WEEKEND_REPORT_DAYS
    parts = [generate_watchlist_summary()]
    # V8.2.0 FIX (bug #11): timezone-aware IST datetime.
    today = datetime.now(IST)
    # V8.1.2 UPDATE: document ki requirement "Saturday & Sunday" dono din
    # weekly report chahiye - pehle sirf Friday (weekday()==4) par chalta
    # tha. WEEKEND_REPORT_DAYS (config.py) = [5, 6] matlab Saturday+Sunday.
    if today.weekday() in WEEKEND_REPORT_DAYS:
        parts.append(generate_weekly_report())
    if today.day == monthrange(today.year, today.month)[1]: parts.append(generate_monthly_report())
    return "\n\n" + ("\n\n" + "➖" * 15 + "\n\n").join(parts)


# ============================================================
# V9.0 NAYA: Morning P&L + Day Stats (GLM evening summary ke liye)
# ============================================================

def generate_yesterday_pnl_report():
    """
    V9.0 NAYA: Kal (yesterday IST) close huye trades ka P&L report
    banata hai - morning briefing (8 AM) mein dikhane ke liye.

    Sirf FULL_TARGET aur SL_HIT status wale trades, jinka closed_date
    = yesterday (IST), consider karte hain. Hinglish Telegram message
    (HTML format) return karta hai.

    Agar kal koi trade close nahi hua to short message return karta hai.
    """
    # V8.2.0 FIX (bug #11): timezone-aware IST datetime - UTC servers
    # par bhi sahi "yesterday" milega.
    yesterday_str = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM recommendations "
        "WHERE status IN ('FULL_TARGET','SL_HIT') AND closed_date = ? "
        "ORDER BY status DESC, closed_date DESC",
        (yesterday_str,),
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "📊 Kal koi trade close nahi hua."

    lines = [
        "📊 <b>KAL KE CLOSED TRADES (P&amp;L)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    wins = 0
    losses = 0
    for r in rows:
        # V8.2.0 FIX (bug #1): sqlite3.Row -> dict for safe .get()
        row = dict(r)
        # V8.3.0 (G4): clean_symbol + escape_html + <b> wrap (M&M safe).
        # Stock name ALWAYS English (never translated, per design standard).
        stk = f"<b>{escape_html(clean_symbol(row.get('stock', '')))}</b>"
        entry = row.get("entry_price")
        closed = row.get("closed_price")
        status = row.get("status", "")

        # P&L % calculate karo - agar entry/closed prices available.
        # V8.2.0 FIX (bug #27): use `is not None` instead of truthy check.
        pnl_str = ""
        if entry is not None and closed is not None and entry > 0:
            pnl_pct = ((closed - entry) / entry) * 100
            pnl_str = f", {pnl_pct:+.1f}%"

        # Status emoji + label
        if status == "FULL_TARGET":
            emoji = "🟢"
            label = "Target hit"
            wins += 1
        elif status == "SL_HIT":
            emoji = "🛑"
            label = "SL hit"
            losses += 1
        else:
            emoji = "⚪"
            label = status

        lines.append(f"{emoji} {stk}: {label}{pnl_str}")

    total_closed = wins + losses
    win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"📈 Win: <b>{wins}</b> | 📉 Loss: <b>{losses}</b> | Win rate: <b>{win_rate:.1f}%</b>"
    )
    return "\n".join(lines)


def get_day_stats():
    """
    V9.0 NAYA: Aaj (today IST) ke trading stats return karta hai -
    evening GLM AI summary (ai_brain.generate_evening_summary) ke liye.

    Return dict structure:
      {
        "total_trades": 15,            # aaj ke total recommendations
        "win_rate": 60.0,              # closed trades ka win rate %
        "target_hits": 6,              # FULL_TARGET count
        "sl_hits": 4,                  # SL_HIT count (pure + partial dono)
        "intraday_picks": [],          # intraday scanner DB mein nahi likhta
        "swing_picks":  [{"stock": "RELIANCE.NS", "result": "target hit +3%"}],
        "btst_picks":   [],            # BTST scanner DB mein nahi likhta
      }

    Notes:
      - Sirf 9:20 AM Swing scan DB mein recommendations likhta hai.
        Intraday (9:30) aur BTST (3 PM) scanners sirf Telegram par
        bhejte hain, DB mein nahi. Isliye intraday_picks/btst_picks
        empty rahenge (future mein 'scanner_type' column add hone par
        proper categorize ho jaayega).
      - Closed trades (FULL_TARGET/SL_HIT) ke liye result string mein
        P&L % aata hai. Open trades ke liye live price se P&L % (agar
        fetch ho paya), warna "pending".
    """
    # V8.2.0 FIX (bug #11): timezone-aware IST datetime.
    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM recommendations WHERE date_added = ? ORDER BY score DESC",
        (today_str,),
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "target_hits": 0,
            "sl_hits": 0,
            "intraday_picks": [],
            "swing_picks": [],
            "btst_picks": [],
        }

    target_hits = 0
    sl_hits = 0
    closed_count = 0
    picks_list = []

    # Live prices fetch karo (OPEN trades ke P&L ke liye). Fail hone
    # par picks_list mein "pending" result string jaata hai - crash nahi.
    tickers = [r["stock"] for r in rows]
    live_prices = {}
    try:
        live_prices = fetch_latest_close_batch(tickers) or {}
    except Exception as e:
        logger.debug(f"get_day_stats: live price fetch fail (OPEN picks 'pending' dikhenge): {e}")
        live_prices = {}

    for r in rows:
        # V8.2.0 FIX (bug #1): sqlite3.Row -> dict for safe .get()
        row = dict(r)
        stock = row.get("stock", "")
        entry = row.get("entry_price")
        closed = row.get("closed_price")
        status = row.get("status", "OPEN")

        result_str = ""
        if status == "FULL_TARGET":
            target_hits += 1
            closed_count += 1
            # V8.2.0 FIX (bug #27): use `is not None` instead of truthy.
            if entry is not None and closed is not None and entry > 0:
                pct = ((closed - entry) / entry) * 100
                result_str = f"target hit {pct:+.1f}%"
            else:
                result_str = "target hit"
        elif status == "SL_HIT":
            # Pure SL_HIT + partial-then-SL dono count (V8.2.0 bug #7
            # wala distinction yahan zaroori nahi - GLM ko overall
            # picture chahiye, micro-categorize nahi).
            sl_hits += 1
            closed_count += 1
            if entry is not None and closed is not None and entry > 0:
                pct = ((closed - entry) / entry) * 100
                result_str = f"SL hit {pct:+.1f}%"
            else:
                result_str = "SL hit"
        else:
            # OPEN trade - live P&L if available, else "pending"
            cmp = live_prices.get(stock)
            if cmp is not None and entry is not None and entry > 0:
                pct = ((cmp - entry) / entry) * 100
                result_str = f"open, P&L {pct:+.1f}%"
            else:
                result_str = "pending"

        picks_list.append({"stock": stock, "result": result_str})

    win_rate = (target_hits / closed_count * 100) if closed_count > 0 else 0.0

    return {
        "total_trades": len(rows),
        "win_rate": win_rate,
        "target_hits": target_hits,
        "sl_hits": sl_hits,
        # V9.0: 9:20 AM Swing scan hi DB mein likhta hai, isliye saari
        # aaj ki DB picks swing_picks mein jaate hain. Intraday (9:30)
        # aur BTST (3 PM) scanners sirf Telegram par bhejte hain - DB
        # mein nahi - isliye ye do lists abhi empty hain. Future mein
        # 'scanner_type' column add hone par proper categorize ho jaayega.
        "intraday_picks": [],
        "swing_picks": picks_list,
        "btst_picks": [],
    }

# ═══════════════════════════════════════════════════════════════════
# V9.3: TRAILING STOPLOSS + 9 EMA EXIT LOGIC
# ═══════════════════════════════════════════════════════════════════

def calculate_trailing_stoploss(entry_price, current_close, atr, multiplier=2.0):
    """
    V9.3: Trailing stoploss calculate karta hai.

    Trailing SL = max(previous SL, current_close - ATR × multiplier)
    Matlab SL hamesha price ke saath upar move karta hai (never goes down).

    Args:
        entry_price: Original entry price
        current_close: Current closing price
        atr: Current ATR value
        multiplier: ATR multiplier (default 2.0 = 2× ATR below price)

    Returns:
        dict: {
            "trailing_sl": float,      # new trailing SL level
            "sl_pct_from_close": float, # SL % below current close
            "profit_pct": float,        # current profit %
            "should_exit": bool,        # price below trailing SL?
        }
    """
    try:
        if not all([entry_price, current_close, atr]) or atr <= 0:
            return None

        # Trailing SL = close - (ATR × multiplier)
        new_sl = current_close - (atr * multiplier)

        # SL never goes below original entry - (ATR × multiplier)
        # (initial SL stays, trailing only moves UP)
        initial_sl = entry_price - (atr * multiplier)
        trailing_sl = max(new_sl, initial_sl)

        # Exit signal: price below trailing SL
        should_exit = current_close < trailing_sl

        profit_pct = ((current_close - entry_price) / entry_price) * 100
        sl_pct = ((current_close - trailing_sl) / current_close) * 100

        return {
            "trailing_sl": round(trailing_sl, 2),
            "sl_pct_from_close": round(sl_pct, 2),
            "profit_pct": round(profit_pct, 2),
            "should_exit": should_exit,
        }
    except Exception as e:
        logger.warning(f"Trailing SL calc error: {e}")
        return None


def check_9ema_exit(df_weekly=None, df_monthly=None):
    """
    V9.3: 9 EMA exit check on weekly/monthly candle.

    EXIT CONDITION: Weekly ya Monthly close 9 EMA se neeche break kare.
    This is a longer-term exit signal (swing/positional trades ke liye).

    Args:
        df_weekly: Weekly OHLCV DataFrame (optional)
        df_monthly: Monthly OHLCV DataFrame (optional)

    Returns:
        dict: {
            "exit_signal": bool,       # True = exit karo
            "exit_reason": str,        # "Weekly 9 EMA broken" etc
            "weekly_9ema": float,      # weekly 9 EMA value
            "weekly_close": float,     # last weekly close
            "monthly_9ema": float,     # monthly 9 EMA value
            "monthly_close": float,    # last monthly close
        }
    """
    result = {
        "exit_signal": False,
        "exit_reason": "",
        "weekly_9ema": None,
        "weekly_close": None,
        "monthly_9ema": None,
        "monthly_close": None,
    }

    try:
        # Check Weekly 9 EMA
        if df_weekly is not None and len(df_weekly) >= 9:
            weekly_ema9 = df_weekly["Close"].ewm(span=9, adjust=False).mean()
            last_weekly_ema = float(weekly_ema9.iloc[-1])
            last_weekly_close = float(df_weekly["Close"].iloc[-1])

            result["weekly_9ema"] = round(last_weekly_ema, 2)
            result["weekly_close"] = round(last_weekly_close, 2)

            if last_weekly_close < last_weekly_ema:
                result["exit_signal"] = True
                result["exit_reason"] = "Weekly close below 9 EMA (exit signal)"
                return result

        # Check Monthly 9 EMA
        if df_monthly is not None and len(df_monthly) >= 9:
            monthly_ema9 = df_monthly["Close"].ewm(span=9, adjust=False).mean()
            last_monthly_ema = float(monthly_ema9.iloc[-1])
            last_monthly_close = float(df_monthly["Close"].iloc[-1])

            result["monthly_9ema"] = round(last_monthly_ema, 2)
            result["monthly_close"] = round(last_monthly_close, 2)

            if last_monthly_close < last_monthly_ema:
                result["exit_signal"] = True
                result["exit_reason"] = "Monthly close below 9 EMA (exit signal)"
                return result

        if not result["exit_signal"]:
            result["exit_reason"] = "No exit signal — above 9 EMA on weekly/monthly"

    except Exception as e:
        logger.warning(f"9 EMA exit check error: {e}")
        result["exit_reason"] = f"Check failed: {e}"

    return result


def format_exit_alert(stock, trailing_info, ema_exit_info):
    """
    V9.3: Exit alert message Telegram ke liye format karta hai.

    Args:
        stock: Stock symbol
        trailing_info: dict from calculate_trailing_stoploss()
        ema_exit_info: dict from check_9ema_exit()

    Returns:
        HTML-formatted string for Telegram.
    """
    from utils import escape_html, clean_symbol
    display = escape_html(clean_symbol(stock))

    lines = [f"🚪 <b>EXIT ALERT</b> — <b>{display}</b>"]
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if trailing_info:
        lines.append(f"📊 Trailing SL: ₹{trailing_info['trailing_sl']}")
        lines.append(f"📈 Profit: {trailing_info['profit_pct']:+.2f}%")
        if trailing_info.get("should_exit"):
            lines.append("⚠️ Price below trailing SL — EXIT NOW!")

    if ema_exit_info:
        if ema_exit_info.get("exit_signal"):
            lines.append(f"📉 {ema_exit_info['exit_reason']}")
        if ema_exit_info.get("weekly_9ema"):
            lines.append(f"📅 Weekly 9 EMA: ₹{ema_exit_info['weekly_9ema']}")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)
