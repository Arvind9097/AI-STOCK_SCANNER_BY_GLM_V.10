"""
===========================================================
 AI STOCK SCANNER V5.2 - MAIN
===========================================================
Commands:
    python main.py --scan        (Subah naye stocks aur charts ke liye)
    python main.py --monitor     (Din me Target/SL live check karne ke liye)
    python main.py --closebuys   (3pm - market band hone se pehle "Best Buys into Close")
    python main.py --report      (4pm - poore din ka Win-Rate)
    python main.py --evening     (8pm - Weekly (Friday)/Monthly (month-end)/Watchlist)
    python main.py --nifty       (NIFTY 50 ka live value Telegram par bhejta hai)
    python main.py --schedule    (Poora din auto: 8am Nifty, 9:20 scan, monitor loop,
                                   3pm close-buys, 4pm report, 8pm evening summary)
    Add '--force' kisi bhi command ke saath agar aaj already bheja gaya
    ho aur dobara bhejna ho.
===========================================================
"""

import sys
import time

from downloader import download_all, download_benchmark
from scanner import scan
from charts import generate_charts_for_top
from report import save_report
from whatsapp_alert import send_alert as send_whatsapp_alert
from email_alert import send_alert as send_email_alert
from config import CHARTS_FOR_TOP_N, TOP_N_BUY_LIST, BENCHMARK_SYMBOL, CLOSE_BESTBUYS_COUNT
from logger import logger
from utils import clean_symbol, escape_html
from targets import calculate_targets
from news import format_news_text

from telegram_alerts import send_telegram_text, send_telegram_chart, CARD_DIVIDER
from tracker import (
    add_recommendations_to_db, check_live_market_hits,
    generate_daily_performance_report, generate_evening_summary,
    # V9.0 NAYA: morning P&L + evening GLM summary ke liye
    generate_yesterday_pnl_report, get_day_stats,
)
from mtf import enrich_top_candidates_with_mtf
from dispatch_state import already_dispatched_today, mark_dispatched_today


def _fmt_price(value):
    """
    Telegram par '₹None' ya '₹nan' dikhane se bachne ke liye helper.
    None / NaN ko dash ('—') se replace karta hai.
    """
    if value is None:
        return "—"
    try:
        if value != value:  # NaN check (NaN != NaN is the only True case)
            return "—"
        return f"₹{value}"
    except (TypeError, ValueError):
        return "—"


def _fmt_rr(value):
    """Risk:Reward display helper - None/NaN ko 'N/A' dikhata hai."""
    if value is None:
        return "N/A"
    try:
        if value != value:  # NaN
            return "N/A"
        return f"1:{value}"
    except (TypeError, ValueError):
        return "N/A"


def _signal_emoji(signal):
    """Signal label ke liye consistent emoji (purane code mein sirf 2 the)."""
    if signal == "STRONG BUY":
        return "🔥"
    if signal == "BUY":
        return "⚡"
    if signal == "WATCH":
        return "👀"
    return "⚠️"  # SELL / AVOID ya unknown


def send_chunked_telegram_report(text_content):
    """Telegram ki 4096 character limit ko safe handle karne ke liye chunks function"""
    if len(text_content) <= 3800:
        send_telegram_text(text_content)
        return

    lines = text_content.split('\n')
    current_chunk = ""
    for line in lines:
        if len(current_chunk) + len(line) + 1 > 3800:
            send_telegram_text(current_chunk)
            current_chunk = line + '\n'
            time.sleep(1.5)
        else:
            current_chunk += line + '\n'

    if current_chunk:
        send_telegram_text(current_chunk)


def run_scan_pipeline(force=False):
    """
    SUBAH KA ENGINE: Naye recommendations dhoondhna aur DB me register karna

    force=True: aaj already scan+charts Telegram par bheji ja chuki ho
    to bhi dobara bhejo (default False - duplicate-send se bachne ke liye)
    """
    start = time.time()
    logger.info("===== AI STOCK SCANNER V4.2 START =====")

    if not force and already_dispatched_today("scan_charts"):
        logger.warning(
            "Aaj already scan + charts Telegram par bhej diye gaye hain "
            "(duplicate-send se bachne ke liye skip kar raha hoon). "
            "Zaroorat ho to 'python main.py --scan --force' chalao."
        )
        return

    all_data = download_all()
    if not all_data:
        logger.error("Koi stock data download nahi ho paya. Internet / yfinance check karo. Ruk raha hoon.")
        return

    benchmark_df = download_benchmark()

    result = scan(all_data, benchmark_df=benchmark_df)
    if not result:
        logger.error("Scan se koi result nahi mila.")
        return

    # v5: 1H confirmation SIRF top-scoring candidates ke liye (extra
    # download lagta hai, isliye poore universe ke liye nahi chalata -
    # rate-limit safe rehne ke liye). Score/Signal yahan update ho
    # sakta hai (1H confirm na ho to BUY->WATCH downgrade).
    from config import MTF_1H_TOP_N
    result = enrich_top_candidates_with_mtf(result, top_n=MTF_1H_TOP_N)

    top_buys = [r for r in result if r["Signal"] in ("STRONG BUY", "BUY")][:TOP_N_BUY_LIST]

    # ============================================================
    # V8.3.0: MARKET BREADTH (overall sentiment context)
    # ============================================================
    # Scan results se advances/declines/sentiment calculate karta hai.
    # Pure computation - no network. Pipeline crash hone se bachata hai
    # try/except (breadth kabhi crash nahi karega).
    breadth = None
    try:
        from market_breadth import compute_market_breadth
        breadth = compute_market_breadth(result)
        if breadth:
            logger.info(
                f"Breadth: {breadth['sentiment']} "
                f"(A/D: {breadth['advances']}/{breadth['declines']}, "
                f"breadth%: {breadth['breadth_pct']})"
            )
    except Exception as e:
        logger.warning(f"Breadth compute fail (pipeline phir bhi chalega): {e}")
        breadth = None

    # ============================================================
    # V8.3.0: GLM AI SCREENER (top candidates ko GLM se rank karwana)
    # ============================================================
    # Scanner poore universe ko technical score deta hai (fast, free).
    # Top ~30 candidates (by Score, result already sorted desc) GLM
    # API ko bheje jaate hain. GLM inme se best 8 picks select karta
    # hai Hinglish rationale + confidence score ke saath.
    # Fallback: GLM fail hone par technical-score ranking (V8.2.0 jaisa).
    glm_picks = None
    try:
        from config import GLM_SCREENER_ENABLED
        if GLM_SCREENER_ENABLED:
            from glm_screener import rank_with_glm
            from universe_fetcher import get_symbol_name_map as _universe_name_map

            # Universe_fetcher se symbol → name map (cached - dobara
            # network call nahi hoga, downloader.download_all() mein
            # already set ho chuka hoga process-lifetime cache).
            try:
                _name_map = _universe_name_map()
            except Exception as _ne:
                logger.warning(f"Universe name map fail (GLM ke liye names empty): {_ne}")
                _name_map = {}

            # Build candidate dicts for GLM (top 30 by Score - result
            # already sorted descending by Score at scanner.py end).
            glm_candidates = []
            for r in result[:30]:
                entry = r.get("Entry")
                sl = r.get("Stoploss")
                t1, t2, t3 = calculate_targets(entry, sl) if (entry and sl) else (None, None, None)
                glm_candidates.append({
                    "symbol": r["Stock"],
                    "name": _name_map.get(r["Stock"], ""),
                    "score": r.get("Score", 0),
                    "signal": r.get("Signal", ""),
                    "close": r.get("Close"),
                    "rsi": r.get("RSI"),
                    "adx": r.get("ADX"),
                    "ema20": r.get("EMA20"),
                    "ema50": r.get("EMA50"),
                    "ema200": r.get("EMA200"),
                    "macd": r.get("MACD"),
                    "macd_signal": r.get("MACD_SIGNAL"),
                    "supertrend": r.get("Supertrend"),
                    "volume_spike": r.get("Volume_Spike"),
                    "breakout": r.get("Breakout"),
                    "patterns": ",".join(r.get("Patterns", []) or []),
                    "weekly_trend": r.get("Weekly_Trend"),
                    "mtf_status": r.get("MTF_1H_Status"),
                    "entry_low": r.get("Entry_Low"),
                    "entry_high": r.get("Entry_High"),
                    "sl": sl,
                    "t1": t1,
                    "t2": t2,
                    "t3": t3,
                    "rr": r.get("Risk_Reward"),
                    "rel_strength": r.get("Relative_Strength"),
                })

            if glm_candidates:
                glm_picks = rank_with_glm(glm_candidates)
                logger.info(
                    f"GLM screener: {len(glm_picks) if glm_picks else 0} picks returned "
                    f"(from {len(glm_candidates)} candidates)"
                )
            else:
                logger.info("GLM screener: koi candidates nahi (result empty) - skip")
    except Exception as e:
        logger.warning(f"GLM screener fail (pipeline phir bhi chalega): {e}")
        glm_picks = None

    # Database mein naye recommendations register karo (Target1/2/3 yahin calculate hote hain)
    add_recommendations_to_db(top_buys)

    # Chart images (scanner ke row ke saath - taaki chart/telegram/DB SAME numbers dikhayein)
    chart_data = generate_charts_for_top(all_data, result, top_n=CHARTS_FOR_TOP_N)
    chart_paths_dict = chart_data[0] if isinstance(chart_data, tuple) else chart_data

    # V8.3.0: glm_picks + breadth ko PDF report tak pass karo (PDF agent
    # inhe use karega - hum sirf pass-through kar rahe hain).
    excel_path, pdf_path = save_report(result, chart_data, glm_picks=glm_picks, breadth=breadth)

    # ---- TELEGRAM DISPATCH ----
    try:
        logger.info("Telegram channel par Charts aur Report bheji ja rahi hai...")

        announcement = (
            "🚀 <b>AI INSTITUTIONAL SCANNER — EXECUTION COMPLETE!</b>\n"
            f"{CARD_DIVIDER}\n"
            "📊 Aaj ke top swing/breakout stocks ki quant list taiyaar hai.\n"
            "📈 Charts niche live post ho rahe hain. 👉\n"
            f"{CARD_DIVIDER}"
        )
        send_telegram_text(announcement)
        time.sleep(2)

        if chart_paths_dict:
            logger.info(f"Telegram par total {len(chart_paths_dict)} charts post ho rahe hain...")
            # V8.3.0 (G4): caption ab clean card-style — emoji + bold stock
            # name + signal badge. Stock name clean_symbol+escape_html+<b>
            # wrap (M&M, L&T safe). Koi boilerplate text nahi.
            signal_lookup = {r["Stock"]: r.get("Signal", "BUY") for r in result}
            for stock_name, chart_file_path in chart_paths_dict.items():
                display = escape_html(clean_symbol(stock_name))
                signal = signal_lookup.get(stock_name, "BUY")
                emoji = _signal_emoji(signal)
                # Score (optional) - taaki caption mein bhi quick score dikhe.
                score_lookup = {r["Stock"]: r.get("Score", 0) for r in result}
                score = score_lookup.get(stock_name, 0)
                caption = f"{emoji} <b>{display}</b> | 📊 Score: {score}/100 | {escape_html(signal.title())}"
                send_telegram_chart(chart_file_path, caption_text=caption)
                time.sleep(2.5)

        # ============================================================
        # V8.3.0 (G2): MARKET BREADTH + GLM AI PICKS (BEFORE detailed analysis)
        # ============================================================
        # Order (per spec):
        #   1. format_breadth_text(breadth)  - overall market sentiment
        #   2. format_glm_picks_text(picks)  - AI-ranked top picks
        #   3. existing detailed analysis section (master dashboard etc.)
        # Breadth kabhi crash nahi karega (try/except wrap).
        # GLM picks sirf if GLM_SCREENER_ENABLED + glm_picks non-None.

        # --- (1) Market Breadth ---
        try:
            if breadth:
                from market_breadth import format_breadth_text
                breadth_text = format_breadth_text(breadth)
                if breadth_text:
                    send_telegram_text(breadth_text)
                    time.sleep(1.5)
                    logger.info("Market breadth Telegram par bhej diya")
        except Exception as e:
            logger.warning(f"Breadth telegram dispatch fail (continuing): {e}")

        # --- (2) GLM AI Top Picks ---
        try:
            if glm_picks:
                from glm_screener import format_glm_picks_text
                glm_text = format_glm_picks_text(glm_picks)
                if glm_text:
                    send_telegram_text(glm_text)
                    time.sleep(1.5)
                    logger.info("GLM AI top picks Telegram par bhej diye")
        except Exception as e:
            logger.warning(f"GLM picks telegram dispatch fail (continuing): {e}")

        # Master Trading Dashboard - naya card-style format (emoji + Hindi
        # labels + Target Hit/SL Hit highlighting + green/red profit coding)
        from master_dashboard import generate_master_trading_dashboard
        report_msg = generate_master_trading_dashboard()
        report_msg += f"\n🔍 <b>DETAILED ANALYSIS + NEWS</b>\n{CARD_DIVIDER}\n"
        for r in top_buys:
            # V8.3.0 (G4): clean_symbol + escape_html + <b> wrap (M&M safe).
            stk = escape_html(clean_symbol(r['Stock']))
            score = r.get('Score', 0)
            signal = escape_html(r.get('Signal', 'STRONG BUY'))
            signal_emoji = _signal_emoji(r.get('Signal', 'STRONG BUY'))
            entry = r.get('Entry')
            e_lo, e_hi = r.get('Entry_Low'), r.get('Entry_High')
            sl = r.get('Stoploss')
            t1, t2, t3 = calculate_targets(entry, sl) if (entry and sl) else (None, None, None)
            rr = r.get('Risk_Reward', 'N/A')
            rsi = r.get('RSI')
            adx = r.get('ADX')
            vol_spike = r.get('Volume_Spike', False)
            patterns = escape_html(
                ", ".join(r.get('Patterns', [])) if r.get('Patterns') else "None"
            )
            analysis = escape_html(r.get('AI_Analysis', ''))
            news_text = escape_html(format_news_text(r['Stock'], limit=2))
            weekly = escape_html(r.get('Weekly_Trend', 'UNKNOWN') or 'UNKNOWN')

            # V8.3.0 (G4): compact card - header / levels / R:R+RSI+ADX /
            # Pattern+Weekly / analysis / news / divider. Scannable in 3s.
            vol_emoji = " 📈VolSpike" if vol_spike else ""
            rsi_str = f"{rsi:.1f}" if isinstance(rsi, (int, float)) else "N/A"
            adx_str = f"{adx:.1f}" if isinstance(adx, (int, float)) else "N/A"

            report_msg += (
                f"{signal_emoji} <b>{stk}</b> | 📊 Score: {score}/100 | {signal}{vol_emoji}\n"
                f"───────────────────────────\n"
                f"💵 Entry: {_fmt_price(e_lo)}-{_fmt_price(e_hi)} | 🛑 SL: {_fmt_price(sl)}\n"
                f"🎯 T1: {_fmt_price(t1)} | T2: {_fmt_price(t2)} | Final: {_fmt_price(t3)}\n"
                f"📊 R:R: {_fmt_rr(rr)} | 📈 RSI: {rsi_str} | 💪 ADX: {adx_str}\n"
                f"🔮 Pattern: {patterns} | 📅 Weekly: {weekly}\n"
                f"───────────────────────────\n"
            )
            if analysis:
                report_msg += f"💡 {analysis}\n"
            if news_text:
                report_msg += f"📰 {news_text}\n"
            report_msg += f"{CARD_DIVIDER}\n"

        send_chunked_telegram_report(report_msg)
        logger.info("Telegram Report fully sent successfully!")

        # Aaj ke liye scan+charts dispatch ho gaya - ab dobara (jab tak
        # --force na ho) is din ke liye dobara nahi bhejega
        mark_dispatched_today("scan_charts")

    except Exception as telegram_error:
        logger.error(f"Telegram pipeline failure: {telegram_error}")

    # ---- BACKUP ALERTS ----
    try:
        send_whatsapp_alert(result)
        send_email_alert(result, excel_path, pdf_path)
    except Exception as e:
        logger.warning(f"Backup alerts error: {e}")

    elapsed = time.time() - start
    logger.info(f"\n===== FINISHED in {elapsed:.1f}s =====")


def run_close_bestbuys_pipeline(force=False):
    """
    3 PM - "BEST BUYS INTO CLOSE": Market band hone se pehle, aaj ke
    already-cached data se dobara score karke, sirf top 3-4 sabse
    achhe setups suggest karta hai (jaise ki subah ke wide list ki
    jagah ek chhoti, high-conviction "aaj ke close ke paas entry"
    list). Naya download NAHI hota (agar subah ka cache abhi bhi
    fresh hai - CACHE_MAX_AGE_HOURS ke andar) - isliye ye fast hai.
    """
    logger.info("===== BEST BUYS INTO CLOSE (3 PM) START =====")

    if not force and already_dispatched_today("close_bestbuys"):
        logger.warning(
            "Aaj already 'Best Buys into Close' bheja ja chuka hai, skip kar raha hoon. "
            "Zaroorat ho to 'python main.py --closebuys --force' chalao."
        )
        return

    all_data = download_all()  # agar subah ka cache fresh hai to yahan koi naya download nahi hoga
    if not all_data:
        logger.error("Data available nahi hai, close-bestbuys skip.")
        return

    benchmark_df = download_benchmark()
    result = scan(all_data, benchmark_df=benchmark_df)
    if not result:
        return

    # Sirf top candidates ke liye 1H confirmation (chhoti list, isliye fast)
    result = enrich_top_candidates_with_mtf(result, top_n=10)

    best = [r for r in result if r["Signal"] in ("STRONG BUY", "BUY")][:CLOSE_BESTBUYS_COUNT]

    if not best:
        send_telegram_text(
            "🕒 <b>BEST BUYS INTO CLOSE (3 PM)</b>\n"
            f"{CARD_DIVIDER}\n"
            "Aaj close ke paas koi high-conviction setup nahi mila. 🙏"
        )
        mark_dispatched_today("close_bestbuys")
        return

    add_recommendations_to_db(best)
    chart_data = generate_charts_for_top(all_data, best, top_n=len(best))
    chart_paths_dict = chart_data[0] if isinstance(chart_data, tuple) else chart_data

    # V8.3.0 (G4): header card + compact per-stock card (bold English name,
    # emoji signal, clean levels) + divider between stocks.
    msg = (
        "🕒 <b>BEST BUYS INTO CLOSE (3 PM)</b>\n"
        f"{CARD_DIVIDER}\n"
        "📈 Market band hone se pehle, agle din ke swing ke liye ye setups sabse strong hain:\n"
        f"{CARD_DIVIDER}\n"
    )

    for i, r in enumerate(best, start=1):
        stk = escape_html(clean_symbol(r['Stock']))
        signal = r.get('Signal', '')
        signal_emoji = _signal_emoji(signal)
        e_lo, e_hi = r.get('Entry_Low'), r.get('Entry_High')
        sl = r.get('Stoploss')
        # V8.2.0 BUGFIX: `if sl else` ko `if (entry and sl) else` se
        # replace kiya - main.py:148 ke saath consistent. calculate_targets
        # ab internally NaN bhi handle karta hai (targets.py), but
        # defensive guard rakha gaya hai.
        entry = r.get('Entry')
        t1, t2, t3 = calculate_targets(entry, sl) if (entry and sl) else (None, None, None)
        msg += (
            f"{i}. {signal_emoji} <b>{stk}</b> | 📊 Score: {r.get('Score',0)}/100 | {escape_html(signal)}\n"
            f"💵 Entry: {_fmt_price(e_lo)}-{_fmt_price(e_hi)} | 🛑 SL: {_fmt_price(sl)}\n"
            f"🎯 T1: {_fmt_price(t1)} | T2: {_fmt_price(t2)} | Final: {_fmt_price(t3)}\n"
            f"{CARD_DIVIDER}\n"
        )

    send_telegram_text(msg)
    time.sleep(1.5)
    # V8.3.0 (G4): caption - emoji + bold stock name + score (consistent
    # with run_scan_pipeline chart captions).
    signal_lookup = {r["Stock"]: r.get("Signal", "BUY") for r in best}
    score_lookup = {r["Stock"]: r.get("Score", 0) for r in best}
    for stock_name, chart_file_path in chart_paths_dict.items():
        display = escape_html(clean_symbol(stock_name))
        signal = signal_lookup.get(stock_name, "BUY")
        # V8.2.0 BUGFIX: _signal_emoji helper se proper emojis (sirf 2 ki
        # jagah 4 signals cover).
        emoji = _signal_emoji(signal)
        score = score_lookup.get(stock_name, 0)
        # V8.3.0 (G4): bold stock name + score badge (consistent across
        # run_scan_pipeline and run_close_bestbuys_pipeline).
        caption = f"{emoji} <b>{display}</b> | 📊 Score: {score}/100 | {escape_html(signal.title())}"
        send_telegram_chart(chart_file_path, caption_text=caption)
        time.sleep(2)

    mark_dispatched_today("close_bestbuys")
    logger.info("Best Buys into Close successfully sent.")


def _fii_dii_section():
    from nse_market_data import get_fii_dii_report

    report = get_fii_dii_report()
    if report is None:
        return "🏦 <b>FII/DII ACTIVITY:</b> Aaj data available nahi hai.\n"

    lines = ["🏦 <b>FII/DII ACTIVITY (Cash Market, provisional)</b>"]
    for key in ("FII", "DII"):
        if key in report:
            r = report[key]
            arrow = "🟢 Net Buy" if r["net"] >= 0 else "🔴 Net Sell"
            lines.append(f"{key}: {arrow} ₹{abs(r['net']):.2f} Cr (Buy ₹{r['buy']:.2f} Cr | Sell ₹{r['sell']:.2f} Cr)")
    return "\n".join(lines) + "\n"


def send_evening_summary(force=False):
    """8 PM - Watchlist + FII/DII + (Friday ko) Weekly + (month-end ko) Monthly report."""
    if not force and already_dispatched_today("evening_summary"):
        logger.warning("Aaj already evening summary bheja ja chuka hai, skip.")
        return

    msg = _safe_section(_fii_dii_section) + "\n" + generate_evening_summary()
    send_chunked_telegram_report(msg)
    mark_dispatched_today("evening_summary")

    # V9.0 NAYA: GLM AI ka din ka Hinglish summary - alag Telegram
    # message mein (taaki existing structured report se alag, AI ka
    # conversational analysis clearly dikhe). Guarded by config flag.
    # Fail hone par sirf warning log - baaki evening pipeline unaffected.
    try:
        from config import EVENING_GLM_SUMMARY_ENABLED
        if EVENING_GLM_SUMMARY_ENABLED:
            # `import ai_brain` (module-qualified) - taaki tracker ki
            # same-name `generate_evening_summary()` (no-args, upar
            # already imported + used) ke saath conflict na ho. ai_brain
            # wala signature day_stats leta hai.
            import ai_brain
            day_stats = get_day_stats()
            glm_summary = ai_brain.generate_evening_summary(day_stats)
            if glm_summary:
                send_telegram_text(glm_summary)
                time.sleep(1.5)
                logger.info("GLM evening AI summary sent as separate message.")
            else:
                logger.warning("GLM evening summary empty return hua (skip send).")
    except ImportError as e:
        logger.warning(f"ai_brain module import nahi ho paya (GLM evening summary skip): {e}")
    except Exception as e:
        logger.warning(f"GLM evening summary fail (baaki evening pipeline unaffected): {e}")


def _gift_nifty_section():
    from nse_market_data import get_gift_nifty
    gn = get_gift_nifty()
    if gn is None:
        return "🌅 <b>GIFT NIFTY</b>\nAbhi data available nahi hai (NSE se fetch nahi ho paya).\n"

    arrow = "🟢" if gn["change"] >= 0 else "🔴"
    return (
        f"🌅 <b>GIFT NIFTY</b> (Indian market band hone ke baad ka overnight indicator)\n"
        f"{arrow} {gn['last']:.2f}  ({gn['change']:+.2f}, {gn['pct_change']:+.2f}%)\n"
        f"Expiry: {gn['expiry']}\n"
    )


def _bulk_deals_section():
    from nse_market_data import get_bulk_block_deals
    deals = get_bulk_block_deals(top_n=5)
    if deals is None or not (deals["bulk"] or deals["block"]):
        return "📦 <b>Bulk/Block Deal Stocks:</b> Aaj koi bada deal report nahi hui.\n"

    lines = ["📦 <b>BULK/BLOCK DEAL STOCKS</b>"]
    for d in deals["bulk"][:5]:
        stk = escape_html(clean_symbol(d["symbol"]))
        lines.append(f"• {stk} - {d['deal_type']} {d['quantity']} @ ₹{d['price']} ({escape_html(d['client'])})")
    for d in deals["block"][:5]:
        stk = escape_html(clean_symbol(d["symbol"]))
        lines.append(f"• [Block] {stk} - {d['deal_type']} {d['quantity']} @ ₹{d['price']}")
    return "\n".join(lines) + "\n"


def _preopen_movers_section():
    from nse_market_data import get_pre_open_movers
    movers = get_pre_open_movers(top_n=5)
    if movers is None:
        return "📊 <b>Pre-Market Movers:</b> Abhi data available nahi hai.\n"

    lines = ["📊 <b>PRE-MARKET MOVERS</b>"]
    if movers["gainers"]:
        lines.append("Gainers: " + ", ".join(
            f"{escape_html(clean_symbol(g['symbol']))} ({g['pct_change']:+.1f}%)" for g in movers["gainers"][:5]
        ))
    if movers["losers"]:
        lines.append("Losers: " + ", ".join(
            f"{escape_html(clean_symbol(l['symbol']))} ({l['pct_change']:+.1f}%)" for l in movers["losers"][:5]
        ))
    return "\n".join(lines) + "\n"


def _general_news_section():
    from config import MORNING_GENERAL_NEWS_STOCKS, MORNING_NEWS_PER_STOCK, TRANSLATE_NEWS_TO_HINDI
    # V8.3.0: format_news_text() ab internally to_hinglish() use karta hai
    # (stock names English preserve karte hue). Isliye yahan dobara
    # translate NAHI karna — warna double-translate ho jaata (Hinglish →
    # Devanagari), aur stock names bhi Devanagari mein convert ho jaate.
    lines = ["📰 <b>AAJ KI MARKET NEWS</b>"]
    found_any = False
    for sym in MORNING_GENERAL_NEWS_STOCKS:
        text = format_news_text(sym, limit=MORNING_NEWS_PER_STOCK)
        if "nahi mili" in text:
            continue
        found_any = True
        # format_news_text already Hinglish-translated (if TRANSLATE_NEWS_TO_HINDI)
        lines.append(escape_html(text))

    if not found_any:
        lines.append("Abhi koi badi news nahi mili.")
    return "\n".join(lines) + "\n"


def _watchlist_news_section():
    from config import MORNING_WATCHLIST_NEWS_DAYS, MORNING_NEWS_PER_STOCK, TRANSLATE_NEWS_TO_HINDI
    from tracker import get_priority_watchlist_symbols
    # V8.3.0: format_news_text() ab internally to_hinglish() use karta hai.
    # Double-translate remove kiya gaya (pehle yahan to_hindi() dobara call
    # hota tha jo Hinglish ko Devanagari mein convert kar deta tha + stock
    # names bhi translate ho jaate the).

    symbols = get_priority_watchlist_symbols(recent_days=MORNING_WATCHLIST_NEWS_DAYS)
    if not symbols:
        return "📌 <b>Watchlist News:</b> Abhi watchlist mein koi stock nahi hai.\n"

    lines = ["📌 <b>WATCHLIST/TOP-PICK STOCKS KI NEWS</b> (priority)"]
    for sym in symbols[:10]:  # zyada lamba message na ho jaaye
        stk = escape_html(clean_symbol(sym))
        text = format_news_text(sym, limit=MORNING_NEWS_PER_STOCK)
        if "nahi mili" in text:
            continue
        # format_news_text already Hinglish-translated (if TRANSLATE_NEWS_TO_HINDI)
        text = text.lstrip("• ").strip()
        lines.append(f"• <b>{stk}</b>: {escape_html(text)}")

    if len(lines) == 1:
        lines.append("Abhi watchlist stocks ki koi nayi news nahi mili.")
    return "\n".join(lines) + "\n"


def _yesterday_pnl_section():
    """
    V9.0 NAYA: Kal close huye trades ka P&L section - morning briefing
    mein dikhane ke liye. tracker.generate_yesterday_pnl_report() ko
    call karke uska output + trailing newline return karta hai.

    Empty case ("📊 Kal koi trade close nahi hua.") bhi as-is return
    hota hai - user ko pata chalta hai ki kal koi close nahi hua.
    """
    return generate_yesterday_pnl_report() + "\n"


def send_morning_briefing(force=False):
    """
    8 AM - Poora morning briefing (v6):
    1. GIFT Nifty (NSE se free, real data)
    2. Bulk/Block Deal stocks
    3. Pre-market movers (gap-up/gap-down)
    4. Aaj ki general market news (Hindi)
    5. Watchlist/recent top-pick stocks ki priority news (Hindi)

    Har section independent hai - koi ek fail ho jaaye (NSE block/
    network issue) to baaki sections phir bhi bhej diye jaate hain.
    """
    from config import (
        MORNING_SHOW_GIFT_NIFTY, MORNING_SHOW_BULK_DEALS,
        MORNING_SHOW_PREOPEN_MOVERS, MORNING_SHOW_GENERAL_NEWS,
        MORNING_SHOW_WATCHLIST_NEWS, MORNING_SHOW_YESTERDAY_PNL,
    )

    logger.info("===== MORNING BRIEFING (8 AM) START =====")

    if not force and already_dispatched_today("morning_briefing"):
        logger.warning("Aaj already morning briefing bheja ja chuka hai, skip (--force se override karo).")
        return

    sections = ["🌞 <b>SUBAH KI MARKET BRIEFING</b>\n"]

    if MORNING_SHOW_GIFT_NIFTY:
        sections.append(_safe_section(_gift_nifty_section))
    if MORNING_SHOW_BULK_DEALS:
        sections.append(_safe_section(_bulk_deals_section))
    if MORNING_SHOW_PREOPEN_MOVERS:
        sections.append(_safe_section(_preopen_movers_section))
    if MORNING_SHOW_GENERAL_NEWS:
        sections.append(_safe_section(_general_news_section))
    if MORNING_SHOW_WATCHLIST_NEWS:
        sections.append(_safe_section(_watchlist_news_section))
    # V9.0 NAYA: Kal ke closed trades ka P&L section - last section
    # (taaki morning briefing khatam hone se pehle user ko kal ka
    # performance scorecard dikhe).
    if MORNING_SHOW_YESTERDAY_PNL:
        sections.append(_safe_section(_yesterday_pnl_section))

    send_chunked_telegram_report("\n".join(sections))
    mark_dispatched_today("morning_briefing")
    logger.info("Morning briefing sent successfully.")


def _safe_section(func):
    """Ek section fail ho jaaye to poora briefing nahi रुकna chahiye."""
    try:
        return func()
    except Exception as e:
        logger.warning(f"Morning briefing section '{func.__name__}' fail: {e}")
        return f"⚠️ ({func.__name__} abhi available nahi hai)\n"


def send_nifty_update():
    """Backward-compatible alias - ab poora morning briefing bhejta hai."""
    send_morning_briefing()


def run_intraday_scan_pipeline(force=False):
    """
    9:30 AM - NAYA (document requirement): Intraday-specific scan
    (ORB, VWAP crossover, RVOL>2x) - SWING scanner (run_scan_pipeline)
    se BILKUL ALAG, intraday_scanner.py module use karta hai.

    Document ka exact format:
      [Emoji] Stock Name / News Heading
      Entry Price, Target (T1, T2), Stop-Loss (SL), brief logic note
    """
    from config import INTRADAY_SCAN_ENABLED
    if not INTRADAY_SCAN_ENABLED:
        return

    logger.info("===== INTRADAY SCAN (9:30 AM) START =====")

    if not force and already_dispatched_today("intraday_scan"):
        logger.warning(
            "Aaj already intraday scan bheja ja chuka hai, skip kar raha hoon. "
            "Zaroorat ho to 'python main.py --intraday --force' chalao."
        )
        return

    try:
        from intraday_scanner import run_intraday_scan
        results = run_intraday_scan()
    except Exception as e:
        logger.error(f"Intraday scan mein error: {e}")
        return

    if not results:
        send_telegram_text(
            "⚡ <b>INTRADAY TRADING RECOMMENDATIONS</b>\n"
            f"{CARD_DIVIDER}\n"
            "Abhi koi high-conviction ORB/VWAP/RVOL setup nahi mila. 🙏"
        )
        mark_dispatched_today("intraday_scan")
        return

    # V8.3.0 (G4): header card + compact per-stock card (bold English name,
    # emoji, clean levels) + divider between stocks.
    msg = (
        "⚡ <b>INTRADAY TRADING RECOMMENDATIONS (9:30 AM)</b>\n"
        f"{CARD_DIVIDER}\n"
        "🚀 Opening momentum ke top setups:\n"
        f"{CARD_DIVIDER}\n"
    )

    for i, r in enumerate(results, start=1):
        stk = escape_html(clean_symbol(r["stock"]))
        entry = r["current_price"]

        # Entry/SL/Target - ATR data nahi hai (intraday-only scan hai),
        # isliye simple % based zone use karte hain (professional-desk
        # jaisa conservative intraday risk: 0.5% SL, 1%/2% targets)
        sl = round(entry * 0.995, 2) if entry else None
        t1 = round(entry * 1.01, 2) if entry else None
        t2 = round(entry * 1.02, 2) if entry else None

        logic_note = ", ".join(r["reasons"])

        msg += (
            f"{i}. ⚡ <b>{stk}</b>\n"
            f"💵 Entry: ₹{entry} | 🎯 T1: ₹{t1} | T2: ₹{t2} | 🛑 SL: ₹{sl}\n"
            f"💡 Logic: {escape_html(logic_note)}\n"
            f"{CARD_DIVIDER}\n"
        )

    send_chunked_telegram_report(msg)

    # V8.1.2 NAYA: har candidate ka TradingView-theme chart bhi bhejo
    # (same theme jo Swing scanner use karta hai - generate_simple_chart
    # charts.py mein hai, koi alag styling nahi banayi)
    from charts import generate_simple_chart
    for r in results:
        try:
            df = r.get("df")
            if df is None:
                continue
            entry = r["current_price"]
            sl = round(entry * 0.995, 2) if entry else None
            t1 = round(entry * 1.01, 2) if entry else None
            chart_path = generate_simple_chart(r["stock"], df, entry=entry, sl=sl, target=t1)
            if chart_path:
                # V8.3.0 (G4): bold stock name + setup label (consistent).
                display = escape_html(clean_symbol(r['stock']))
                send_telegram_chart(chart_path, caption_text=f"⚡ <b>{display}</b> | Intraday Setup")
        except Exception as e:
            logger.warning(f"{r['stock']}: Intraday chart bhejne mein error (message phir bhi bhej diya gaya): {e}")

    mark_dispatched_today("intraday_scan")
    logger.info("Intraday scan recommendations sent successfully.")


def run_btst_scan_pipeline(force=False):
    """
    3:05 PM - NAYA (document requirement): BTST-specific scan
    (last-1-hour price action, volume accumulation, Day's-High
    proximity) - run_close_bestbuys_pipeline() (purana swing-score
    wala 3 PM) se BILKUL ALAG, btst_scanner.py module use karta hai.

    Document ka exact format:
      Stock Name, Entry Range (CMP), Target, Stop-Loss
    """
    from config import BTST_SCAN_ENABLED
    if not BTST_SCAN_ENABLED:
        return

    logger.info("===== BTST SCAN (3:05 PM) START =====")

    if not force and already_dispatched_today("btst_scan"):
        logger.warning(
            "Aaj already BTST scan bheja ja chuka hai, skip kar raha hoon. "
            "Zaroorat ho to 'python main.py --btst --force' chalao."
        )
        return

    try:
        from btst_scanner import run_btst_scan
        results = run_btst_scan()
    except Exception as e:
        logger.error(f"BTST scan mein error: {e}")
        return

    if not results:
        send_telegram_text(
            "🌙 <b>BTST CALLS (3:05 PM)</b>\n"
            f"{CARD_DIVIDER}\n"
            "Aaj koi high-conviction BTST setup nahi mila (last-hour price "
            "action/volume/Day's-High criteria pass nahi hui). 🙏"
        )
        mark_dispatched_today("btst_scan")
        return

    # V8.3.0 (G4): header card + compact per-stock card (bold English name,
    # emoji, clean levels) + divider between stocks.
    msg = (
        "🌙 <b>BTST CALLS (Buy Today Sell Tomorrow)</b>\n"
        f"{CARD_DIVIDER}\n"
    )

    for i, r in enumerate(results, start=1):
        stk = escape_html(clean_symbol(r["stock"]))
        entry = r["current_price"]
        sl = round(entry * 0.98, 2) if entry else None      # BTST overnight risk, thoda wider SL
        target = round(entry * 1.025, 2) if entry else None

        logic_note = ", ".join(r["reasons"])

        msg += (
            f"{i}. 🌙 <b>{stk}</b>\n"
            f"💵 Entry (CMP): ₹{entry} | 🎯 Target: ₹{target} | 🛑 SL: ₹{sl}\n"
            f"💡 Logic: {escape_html(logic_note)}\n"
            f"{CARD_DIVIDER}\n"
        )

    send_chunked_telegram_report(msg)

    # V8.1.2 NAYA: har BTST candidate ka bhi TradingView-theme chart
    from charts import generate_simple_chart
    for r in results:
        try:
            df = r.get("df")
            if df is None:
                continue
            entry = r["current_price"]
            sl = round(entry * 0.98, 2) if entry else None
            target = round(entry * 1.025, 2) if entry else None
            chart_path = generate_simple_chart(r["stock"], df, entry=entry, sl=sl, target=target)
            if chart_path:
                # V8.3.0 (G4): bold stock name + setup label (consistent).
                display = escape_html(clean_symbol(r['stock']))
                send_telegram_chart(chart_path, caption_text=f"🌙 <b>{display}</b> | BTST Setup")
        except Exception as e:
            logger.warning(f"{r['stock']}: BTST chart bhejne mein error (message phir bhi bhej diya gaya): {e}")

    mark_dispatched_today("btst_scan")
    logger.info("BTST scan calls sent successfully.")


def run_swing_chart_digest_pipeline(force=False):
    """
    10:00 AM - NAYA (user requirement: "Swing trading ke liye stocks
    ka time 10 AM"). Ye run_scan_pipeline() (9:20 AM, jo asli scan
    karke database mein recommendations save karta hai) ko DOBARA
    call NAHI karta - sirf aaj ke fresh Swing-recommendations (jo
    9:20 AM se already database mein hain) ke TradingView-theme
    charts "Swing Trading" branding ke saath bhejta hai.
    """
    from config import SWING_CHART_DIGEST_ENABLED, SWING_CHART_DIGEST_TOP_N
    if not SWING_CHART_DIGEST_ENABLED:
        return

    logger.info("===== SWING TRADING CHART DIGEST (10:00 AM) START =====")

    if not force and already_dispatched_today("swing_chart_digest"):
        logger.warning(
            "Aaj already Swing chart digest bheja ja chuka hai, skip kar raha hoon. "
            "Zaroorat ho to 'python main.py --swing-digest --force' chalao."
        )
        return

    try:
        from tracker import get_todays_swing_recommendations
        rows = get_todays_swing_recommendations()
    except Exception as e:
        logger.error(f"Swing chart digest mein error: {e}")
        return

    if not rows:
        send_telegram_text(
            "📈 <b>SWING TRADING — CHART DIGEST (10:00 AM)</b>\n"
            f"{CARD_DIVIDER}\n"
            "Aaj koi fresh Swing recommendation nahi mili (9:20 AM scan mein). 🙏"
        )
        mark_dispatched_today("swing_chart_digest")
        return

    top_rows = rows[:SWING_CHART_DIGEST_TOP_N]
    send_telegram_text(
        f"📈 <b>SWING TRADING — CHART DIGEST (10:00 AM)</b>\n"
        f"{CARD_DIVIDER}\n"
        f"🎯 Aaj ke top {len(top_rows)} Swing setups, charts ke saath: 👇\n"
        f"{CARD_DIVIDER}"
    )

    from charts import generate_simple_chart
    from market_data_fetcher import fetch_daily_ohlcv

    for row in top_rows:
        stock = row["stock"]
        try:
            df = fetch_daily_ohlcv(stock, period="6mo")
            if df is None or df.empty:
                continue
            chart_path = generate_simple_chart(
                stock, df,
                entry=row["entry_price"], sl=row["sl_price"], target=row["target_1"],
            )
            if chart_path:
                # V8.3.0 (G4): bold stock name + clean levels (consistent).
                display = escape_html(clean_symbol(stock))
                caption = (
                    f"📈 <b>{display}</b> | Swing Trade | 📊 Score: {row['score']}\n"
                    f"💵 Entry: ₹{row['entry_price']} | 🛑 SL: ₹{row['sl_price']} | 🎯 T1: ₹{row['target_1']}"
                )
                send_telegram_chart(chart_path, caption_text=caption)
        except Exception as e:
            logger.warning(f"{stock}: Swing chart digest error (baaki stocks jaari rahenge): {e}")

    mark_dispatched_today("swing_chart_digest")
    logger.info("Swing Trading chart digest sent successfully.")


def main():
    args = sys.argv[1:]
    force = "--force" in args

    if "--scan" in args:
        run_scan_pipeline(force=force)

    elif "--intraday" in args:
        run_intraday_scan_pipeline(force=force)

    elif "--btst" in args:
        run_btst_scan_pipeline(force=force)

    elif "--swing-digest" in args:
        run_swing_chart_digest_pipeline(force=force)

    elif "--monitor" in args:
        logger.info("Starting Watchlist Target/SL Tracker...")
        result_text = check_live_market_hits()
        logger.info(result_text)

    elif "--closebuys" in args:
        run_close_bestbuys_pipeline(force=force)

    elif "--report" in args:
        logger.info("Generating Daily Accuracy Summary...")
        if not force and already_dispatched_today("daily_report"):
            logger.warning("Aaj daily report already bheja ja chuka hai, skip (--force se override karo).")
        else:
            report_msg = generate_daily_performance_report()
            send_telegram_text(report_msg)
            mark_dispatched_today("daily_report")

    elif "--evening" in args:
        send_evening_summary(force=force)

    elif "--morning" in args:
        send_morning_briefing(force=force)

    elif "--nifty" in args:
        send_morning_briefing(force=force)  # backward-compatible alias

    elif "--schedule" in args:
        # V8.1.2: Render 24x7 health server (background thread, non-
        # blocking) - /ping endpoint UptimeRobot ke liye, taaki Render
        # free-tier service 15 min inactivity ke baad sleep na ho jaaye.
        try:
            from health_server import setup_render_deployment
            from market_data_fetcher import get_source_health
            setup_render_deployment("V8.1.2", extra_status_fn=lambda: {"data_sources": get_source_health()})
        except Exception as e:
            logger.warning(f"Health server start nahi ho paya (bot phir bhi normal chalega, bas Render 24x7 ping kaam nahi karega): {e}")

        # V8.1.2: Breaking news poller (background thread) - har 3 min
        # Indian market RSS feeds check karta hai, nayi headline milte
        # hi turant Hindi mein Telegram par bhej deta hai. Non-blocking -
        # scheduler ke normal kaam mein koi rukavat nahi aati.
        try:
            from breaking_news import start_breaking_news_poller
            start_breaking_news_poller()
        except Exception as e:
            logger.warning(f"Breaking news poller start nahi ho paya (baaki sab normal chalega): {e}")

        # V8.1.2 NAYA: Bot Listener (user ke messages ka reply dena -
        # "Reliance ka analysis bhejo" jaisi queries) background thread
        # mein start karo. run_listener_loop() use karte hain (poore
        # start_bot_engine() ki jagah), taaki health-server aur
        # breaking-news poller DOBARA setup na ho (jo upar already ho
        # chuka hai) - warna Flask health-server same port par dobara
        # bind karne ki koshish karta aur crash ho jaata.
        try:
            import threading
            from bot_listener import run_listener_loop
            listener_thread = threading.Thread(target=run_listener_loop, daemon=True, name="BotListener")
            listener_thread.start()
            logger.info("Bot Listener background thread mein start ho gaya - ab messages ka reply aayega.")
        except Exception as e:
            logger.warning(f"Bot Listener start nahi ho paya (scheduled alerts phir bhi normal chalenge, bas message-reply kaam nahi karega): {e}")

        # V9.0 NAYA: Intraday live alert tracker background thread -
        # har INTRADAY_LIVE_ALERT_INTERVAL (default 5) minute mein
        # aaj ke intraday picks ka live price check karke entry/target/SL
        # hit hone par sharp Telegram alert bhejta hai (9:30-15:00 IST).
        # Ye scheduler ke time-slots se ALAG hai - apni background thread
        # mein chalta hai, scheduler koi slot iske liye nahi deta.
        try:
            from config import INTRADAY_LIVE_ALERT_ENABLED
            if INTRADAY_LIVE_ALERT_ENABLED:
                from intraday_tracker import run_intraday_alert_loop
                import threading
                threading.Thread(
                    target=run_intraday_alert_loop,
                    daemon=True,
                    name="intraday-tracker",
                ).start()
                logger.info("📡 Intraday live alert tracker background thread shuru")
        except Exception as e:
            logger.warning(f"Intraday tracker start fail: {e}")

        from scheduler import run_full_day_scheduler
        run_full_day_scheduler(
            scan_func=run_scan_pipeline,
            monitor_func=check_live_market_hits,
            closebuys_func=run_close_bestbuys_pipeline,
            report_func=lambda: send_telegram_text(generate_daily_performance_report()),
            evening_func=send_evening_summary,
            nifty_func=send_morning_briefing,
            intraday_func=run_intraday_scan_pipeline,
            btst_func=run_btst_scan_pipeline,
            swing_digest_func=run_swing_chart_digest_pipeline,
        )

    else:
        run_scan_pipeline(force=force)
        print("\nTip: '--morning' 8am briefing (GIFT Nifty/bulk-deals/news), '--monitor' live hits, "
              "'--closebuys' 3pm best-buys, '--report' 4pm win-rate, '--evening' 8pm weekly/monthly/FII-DII, "
              "'--schedule' full-day automation. Add '--force' to resend even if already sent today.")


if __name__ == "__main__":
    main()
