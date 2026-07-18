"""
===========================================================
 SCANNER - core scoring + risk/reward + AI analysis engine
===========================================================
"""

import pandas as pd

from indicators import add_indicators
from ai_analysis import generate_analysis
from patterns import detect_all_patterns
from relative_strength import calc_benchmark_return, calc_stock_return, relative_strength
from mtf import get_weekly_trend
from targets import calculate_entry_zone
from config import (
    MIN_RSI, MIN_ADX, SCORE_WEIGHTS, SIGNAL_THRESHOLDS,
    ATR_STOPLOSS_MULTIPLIER, MIN_RISK_REWARD,
    PATTERN_BONUS_POINTS, RELATIVE_STRENGTH_BONUS_POINTS,
    WEEKLY_TREND_BONUS_POINTS,
    # V8.3.0: Stage-1 quick filter (penny/illiquid stocks full scan
    # se pehle hi skip karne ke liye).
    UNIVERSE_MIN_PRICE, UNIVERSE_MIN_AVG_VOLUME_LAKH,
)
from logger import logger


def pd_notna(val):
    return pd.notna(val)


def _get_signal(score):
    if score >= SIGNAL_THRESHOLDS["STRONG BUY"]:
        return "STRONG BUY"
    elif score >= SIGNAL_THRESHOLDS["BUY"]:
        return "BUY"
    elif score >= SIGNAL_THRESHOLDS["WATCH"]:
        return "WATCH"
    return "SELL / AVOID"


def _calc_risk_reward(entry, atr_val, support, resistance):
    """
    Stoploss = entry - (ATR * multiplier)   [ATR based, chart pattern se independent]
    Target   = resistance (agar entry se upar ho), warna entry + 2*(entry-stoploss)
    R:R      = (target - entry) / (entry - stoploss)
    """
    if atr_val is None or atr_val <= 0 or entry is None:
        return None, None, None

    stoploss = entry - (atr_val * ATR_STOPLOSS_MULTIPLIER)
    risk = entry - stoploss

    if resistance is not None and resistance > entry:
        target = resistance
    else:
        target = entry + (2 * risk)  # fallback: 2R target

    reward = target - entry

    if risk <= 0:
        return round(stoploss, 2), round(target, 2), None

    rr = round(reward / risk, 2)
    return round(stoploss, 2), round(target, 2), rr


def scan(all_data, benchmark_df=None, max_workers=8):
    """
    Input:
      all_data: dict {symbol: raw OHLCV dataframe}
      benchmark_df: optional raw OHLCV dataframe of benchmark index
                    (downloader.download_benchmark() se aata hai) -
                    diya to Relative Strength calculate hoti hai.
      max_workers: V9.1.2 — parallel scan ke liye thread count (default 8).
                   1300-stock scan ab 10-15 min ki jagah 3-4 min mein complete.
    Output: list of dicts, ek entry per stock, sab indicators/score/
    signal/risk-reward/patterns/analysis ke saath

    V9.1.2 PARALLEL SCAN (Claude AI review fix #4):
      Pehle ye sequential tha — 1300 stocks × indicators = slow (10-15 min).
      Ab ThreadPoolExecutor use karta hai (max_workers=8 threads parallel).
      Per-stock logic _scan_single() mein extract kiya gaya hai.
      Expected speedup: 4-8x on Render (CPU-bound indicator computation
      releases GIL during pandas/numpy C calls).
    """
    result = []

    total = len(all_data)
    # V8.3.0: Stage-1 quick filter counters (penny + illiquid stocks
    # full indicator scan se pehle hi skip karne ke liye). Isse 7000+
    # universe mein bhi sirf ~500-800 stocks par full scan hota hai.
    stage1_skipped_price = 0      # ₹20 se neeche wale penny stocks
    stage1_skipped_volume = 0     # avg daily volume < 5 lakh wale illiquid
    stage1_skipped_data = 0       # raw_df mein Close/Volume column na ho

    benchmark_return = None
    if benchmark_df is not None:
        try:
            benchmark_return = calc_benchmark_return(benchmark_df)
        except Exception as e:
            logger.warning(f"Benchmark return calculate nahi ho paya: {e}")

    # ===========================================================
    # V9.1.2: STAGE-1 QUICK FILTER (sequential — fast, ~1ms/stock)
    # ===========================================================
    # Stage-1 sequential rakha gaya kyunki ye bahut fast hai (~1ms per
    # stock) aur parallel overhead worthwhile nahi hai. Sirf passing
    # stocks ko Stage-2 (parallel) mein bhejte hain.
    stage2_candidates = []  # list of (stock, raw_df) tuples

    for stock, raw_df in all_data.items():
        try:
            if raw_df is None or getattr(raw_df, "empty", True):
                stage1_skipped_data += 1
                continue
            if "Close" not in raw_df.columns or "Volume" not in raw_df.columns:
                stage1_skipped_data += 1
                continue

            _close_series = pd.to_numeric(raw_df["Close"], errors="coerce").dropna()
            if _close_series.empty:
                stage1_skipped_data += 1
                continue
            last_close = float(_close_series.iloc[-1])

            if last_close < UNIVERSE_MIN_PRICE:
                stage1_skipped_price += 1
                continue

            _vol_series = pd.to_numeric(raw_df["Volume"], errors="coerce").dropna()
            if _vol_series.empty:
                stage1_skipped_volume += 1
                continue
            _vol_window = _vol_series.tail(20)
            avg_vol = float(_vol_window.mean())
            avg_vol_lakh = avg_vol / 100000.0

            if avg_vol_lakh < UNIVERSE_MIN_AVG_VOLUME_LAKH:
                stage1_skipped_volume += 1
                continue

            # Passed Stage-1 — add to Stage-2 candidates
            stage2_candidates.append((stock, raw_df))
        except Exception as stage1_err:
            logger.debug(f"{stock}: Stage-1 check error ({stage1_err}) - Stage-2 continue")
            # Defensive: Stage-1 fail ho to stock skip na karo, Stage-2 mein bhejo
            try:
                stage2_candidates.append((stock, raw_df))
            except Exception:
                pass

    logger.info(
        f"Stage-1 quick filter: {len(all_data) - len(stage2_candidates)}/{total} stocks skip "
        f"(penny<{int(UNIVERSE_MIN_PRICE)}: {stage1_skipped_price}, "
        f"illiquid vol<{int(UNIVERSE_MIN_AVG_VOLUME_LAKH)}L: {stage1_skipped_volume}, "
        f"missing data: {stage1_skipped_data}) - "
        f"{len(stage2_candidates)} stocks par full Stage-2 scan hua (parallel, {max_workers} threads)"
    )

    # ===========================================================
    # V9.1.2: STAGE-2 FULL SCAN (parallel via ThreadPoolExecutor)
    # ===========================================================
    from concurrent.futures import ThreadPoolExecutor, as_completed

    done = 0
    total_s2 = len(stage2_candidates)

    def _scan_single(stock, raw_df):
        """
        Per-stock full scan (Stage-2). Runs in worker thread.
        Returns result dict ya None (on skip/error).
        """
        try:
            df = add_indicators(raw_df.copy())
            df = df.dropna(subset=["Close", "EMA200"])

            if df.empty:
                logger.warning(f"{stock}: indicators ke baad usable data nahi bacha, skip")
                return None

            last = df.iloc[-1]

            # SCORING (core, 0-100)
            score = 0
            reasons = []

            if last["Close"] > last["EMA20"] > last["EMA50"] > last["EMA200"]:
                score += SCORE_WEIGHTS["trend_ema"]
                reasons.append("Trend up (Close>EMA20>EMA50>EMA200)")

            if last["RSI"] > MIN_RSI:
                score += SCORE_WEIGHTS["rsi"]
                reasons.append(f"RSI strong ({last['RSI']:.1f})")

            if last["MACD"] > last["MACD_SIGNAL"]:
                score += SCORE_WEIGHTS["macd"]
                reasons.append("MACD bullish crossover")

            if last["ADX"] > MIN_ADX:
                score += SCORE_WEIGHTS["adx"]
                reasons.append(f"ADX strong trend ({last['ADX']:.1f})")

            if bool(last["VOLUME_SPIKE"]):
                score += SCORE_WEIGHTS["volume_spike"]
                reasons.append("Volume spike today")

            if bool(last["BREAKOUT"]):
                score += SCORE_WEIGHTS["breakout"]
                reasons.append("20-day breakout")

            if int(last["SUPERTREND_DIR"]) == 1:
                score += SCORE_WEIGHTS["supertrend"]
                reasons.append("Supertrend bullish")

            # BONUS SCORING (patterns + relative strength)
            pattern_info = detect_all_patterns(df)
            bullish_set = {"Double Bottom", "Bull Flag"}
            bearish_set = {"Double Top", "Head & Shoulders", "Bear Flag"}
            bull_count = sum(1 for p in pattern_info["patterns"] if p in bullish_set)
            bear_count = sum(1 for p in pattern_info["patterns"] if p in bearish_set)
            if bull_count > bear_count:
                score += PATTERN_BONUS_POINTS
                reasons.append(f"Bullish pattern bias: {', '.join(pattern_info['patterns'])}")
            elif bear_count > bull_count:
                score -= PATTERN_BONUS_POINTS
                reasons.append(f"Bearish pattern bias: {', '.join(pattern_info['patterns'])}")
            elif pattern_info["patterns"]:
                reasons.append(f"Conflicting patterns (no bonus): {', '.join(pattern_info['patterns'])}")

            stock_return = calc_stock_return(df)
            rs_diff, outperforming = relative_strength(stock_return, benchmark_return)
            if rs_diff is not None and outperforming:
                score += RELATIVE_STRENGTH_BONUS_POINTS
                reasons.append(f"Outperforming NIFTY by {rs_diff:+.1f}pts")
            elif rs_diff is not None:
                reasons.append(f"Underperforming NIFTY by {rs_diff:+.1f}pts")

            weekly_info = get_weekly_trend(raw_df)
            weekly_trend = weekly_info["trend"] if weekly_info else "UNKNOWN"
            if weekly_trend == "BULLISH":
                score += WEEKLY_TREND_BONUS_POINTS
                reasons.append("Weekly trend bullish (bada trend bhi supportive hai)")
            elif weekly_trend == "BEARISH":
                score -= WEEKLY_TREND_BONUS_POINTS
                reasons.append("Weekly trend bearish (bada trend against hai)")

            score = max(0, min(100, score))  # 0-100 clamp

            # RISK / REWARD
            entry = float(last["Close"])
            stoploss, target, rr = _calc_risk_reward(
                entry, float(last["ATR"]) if pd_notna(last["ATR"]) else None,
                float(last["SUPPORT"]) if pd_notna(last["SUPPORT"]) else None,
                float(last["RESISTANCE"]) if pd_notna(last["RESISTANCE"]) else None,
            )

            entry_low, entry_high = calculate_entry_zone(
                entry, float(last["ATR"]) if pd_notna(last["ATR"]) else None
            )

            if rr is not None and rr < MIN_RISK_REWARD and score >= SIGNAL_THRESHOLDS["BUY"]:
                score = min(score, SIGNAL_THRESHOLDS["BUY"] - 1)
                reasons.append(f"R:R weak ({rr}) -> downgraded to WATCH")

            signal = _get_signal(score)

            row = {
                "Stock": stock,
                "Close": round(entry, 2),
                "EMA20": round(float(last["EMA20"]), 2),
                "EMA50": round(float(last["EMA50"]), 2),
                "EMA200": round(float(last["EMA200"]), 2),
                "RSI": round(float(last["RSI"]), 2),
                "MACD": round(float(last["MACD"]), 2),
                "MACD_SIGNAL": round(float(last["MACD_SIGNAL"]), 2),
                "ADX": round(float(last["ADX"]), 2),
                "ATR": round(float(last["ATR"]), 2) if pd_notna(last["ATR"]) else None,
                "Supertrend": "BULLISH" if int(last["SUPERTREND_DIR"]) == 1 else "BEARISH",
                "Volume_Spike": bool(last["VOLUME_SPIKE"]),
                "RVOL": round(float(last["RVOL"]), 2) if pd_notna(last["RVOL"]) else None,
                "Volume_Dryup": bool(last["VOLUME_DRYUP"]),
                "Breakout": bool(last["BREAKOUT"]),
                "Consolidating": bool(last["CONSOLIDATING"]),
                "Support": round(float(last["SUPPORT"]), 2) if pd_notna(last["SUPPORT"]) else None,
                "Resistance": round(float(last["RESISTANCE"]), 2) if pd_notna(last["RESISTANCE"]) else None,
                "Pivot_PP": round(float(last["PIVOT_PP"]), 2) if pd_notna(last["PIVOT_PP"]) else None,
                "Pivot_R1": round(float(last["PIVOT_R1"]), 2) if pd_notna(last["PIVOT_R1"]) else None,
                "Pivot_S1": round(float(last["PIVOT_S1"]), 2) if pd_notna(last["PIVOT_S1"]) else None,
                "VWAP20": round(float(last["VWAP20"]), 2) if pd_notna(last["VWAP20"]) else None,
                "Patterns": pattern_info["patterns"],
                "Relative_Strength": rs_diff,
                "Weekly_Trend": weekly_trend,
                "Entry": round(entry, 2),
                "Entry_Low": entry_low,
                "Entry_High": entry_high,
                "Stoploss": stoploss,
                "Target": target,
                "Risk_Reward": rr,
                "Score": score,
                "Signal": signal,
                "Reasons": reasons,
                "MTF_1H_Status": "NOT_CHECKED",
            }

            row["AI_Analysis"] = generate_analysis(row)
            return row

        except Exception as e:
            logger.warning(f"{stock}: scan karte waqt error - {e}")
            return None

    # Parallel execution
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_scan_single, stock, raw_df): stock
                   for stock, raw_df in stage2_candidates}
        for future in as_completed(futures):
            done += 1
            try:
                row = future.result()
                if row:
                    result.append(row)
            except Exception as e:
                logger.warning(f"{futures[future]}: parallel scan error - {e}")
            if done % 50 == 0 or done == total_s2:
                logger.info(f"Stage-2 parallel scan progress: {done}/{total_s2}")

    result.sort(key=lambda r: r["Score"], reverse=True)
    return result
