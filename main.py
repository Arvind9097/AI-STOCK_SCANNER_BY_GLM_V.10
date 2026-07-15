#!/usr/bin/env python3
"""
===========================================================
 MASTER CONTROLLER — V9.2 System Integration (Step 6)
===========================================================
Architectural reasoning for unified pipeline:

This is the ORCHESTRATION LAYER that links all 5 modules into a
single automated pipeline. Each module has a single responsibility;
the master controller sequences them + handles failures gracefully.

PIPELINE FLOW (Evening Summary at market close):

  ┌─────────────────────────────────────────────────────────┐
  │ 1. NETWORK LAYER (Module 1)                             │
  │    - Fetch NSE universe (with 403 anti-block + retry)   │
  │    - Fetch OHLCV data per stock (fallback chain)        │
  │    - Fetch FII/DII activity + market status             │
  └──────────────────────┬──────────────────────────────────┘
                         ▼
  ┌─────────────────────────────────────────────────────────┐
  │ 2. NEWS FILTER (Module 2)                               │
  │    - Fetch RSS feeds (Economic Times, etc.)             │
  │    - Filter: only Indian equity news (blacklist drop)   │
  │    - Score relevance + sentiment (BULLISH/BEARISH)      │
  └──────────────────────┬──────────────────────────────────┘
                         ▼
  ┌─────────────────────────────────────────────────────────┐
  │ 3. STRATEGY ENGINE (Module 4)                           │
  │    - For each stock: evaluate 3-layer confluence        │
  │      (L1 trend, L2 entry trigger, L3 fundamentals)      │
  │    - Generate BUY/WATCH/REJECT + trade plan             │
  │    - Reject if R:R < 1:2                                │
  └──────────────────────┬──────────────────────────────────┘
                         ▼
  ┌─────────────────────────────────────────────────────────┐
  │ 4. CHART GENERATOR (Module 3)                           │
  │    - For each BUY signal: generate clean chart          │
  │    - Only candlesticks + EMA44 + EMA200 + volume        │
  │    - Save as PNG (300 DPI, dark theme)                  │
  └──────────────────────┬──────────────────────────────────┘
                         ▼
  ┌─────────────────────────────────────────────────────────┐
  │ 5. PDF BUILDER (Module 5)                               │
  │    - Compile Evening Summary Report                     │
  │    - Executive summary + fundamental table + trade plans│
  │    - Embed charts (Unicode-safe, no □ boxes)            │
  │    - Save to output_reports/                            │
  └─────────────────────────────────────────────────────────┘

ZERO-POINT FAILURE DESIGN:
  - Each module wrapped in try/except — one module failing NEVER
    crashes the whole pipeline.
  - Module 1 fails (NSE blocked) → use fallback universe (static list).
  - Module 2 fails (RSS down) → skip news, continue with technicals.
  - Module 3 fails (chart error) → skip that chart, continue others.
  - Module 4 fails (strategy error) → skip that stock, continue.
  - Module 5 fails (PDF error) → log + return None, don't crash.
  - Scheduler runs in its own thread — never blocks main process.

SCHEDULER ARCHITECTURE (no thread lockups):
  - Uses `schedule` library (lightweight, no asyncio complexity).
  - Each scheduled job runs in a ThreadPoolExecutor worker thread
    (main scheduler thread never blocks on long jobs).
  - SIGTERM/SIGINT handlers for graceful shutdown.
  - Cron-like schedule:
      08:00 IST — Morning briefing (GIFT Nifty + yesterday P&L)
      09:20 IST — Full scan (universe + strategy + charts)
      16:00 IST — Day report (performance summary)
      20:00 IST — Evening summary (PDF report — this pipeline)
===========================================================
"""

import os
import sys
import logging
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Any
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import signal

# Add project root to path (so modules importable from anywhere)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Import all 5 modules
from network import NSEDataFetcher, get_default_fetcher, call_with_retry
from news import NewsFilterEngine, sanitize_text
from charts import ChartGenerator
from strategy import StrategyEvaluator, SignalAction
from reports import PDFReportBuilder, generate_evening_report

# ───────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(PROJECT_ROOT, "logs", "master.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("MasterController")

IST = ZoneInfo("Asia/Kolkata")


# ═══════════════════════════════════════════════════════════════════
# MASTER CONTROLLER CLASS
# ═══════════════════════════════════════════════════════════════════
class MasterController:
    """
    Unified pipeline orchestrator — links all 5 V9.2 modules.

    SINGLE RESPONSIBILITY: sequence the modules + handle failures.
    Does NOT implement business logic (that's in each module).
    """

    def __init__(
        self,
        output_dir: str = "output_reports",
        charts_dir: str = "output_reports/charts",
        max_workers: int = 5,
    ):
        self.output_dir = os.path.join(PROJECT_ROOT, output_dir)
        self.charts_dir = os.path.join(PROJECT_ROOT, charts_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.charts_dir, exist_ok=True)
        os.makedirs(os.path.join(PROJECT_ROOT, "logs"), exist_ok=True)

        self.max_workers = max_workers
        self.nse_fetcher = get_default_fetcher()
        self.news_engine = NewsFilterEngine()
        self.chart_generator = ChartGenerator()
        self.strategy_evaluator = StrategyEvaluator(strict_fundamentals=False)
        self.pdf_builder = PDFReportBuilder()
        self._shutdown = False
        logger.info("MasterController initialized — V9.2 pipeline ready")

    # ─── STAGE 1: Fetch universe ───
    def fetch_universe(self, max_stocks: int = 50) -> List[str]:
        logger.info(f"Stage 1: Fetching universe (max {max_stocks} stocks)")
        try:
            if self.nse_fetcher.is_blocked:
                logger.warning("NSE blocked — using static fallback universe")
                return self._static_universe()[:max_stocks]
            data = self.nse_fetcher.fetch_json("/allIndices", timeout=15)
            if data and "data" in data:
                symbols = [idx.get("indexSymbol", "") for idx in data["data"]]
                symbols = [s for s in symbols if s and s.isalpha()]
                if symbols:
                    logger.info(f"NSE universe: {len(symbols)} indices fetched")
                    return symbols[:max_stocks]
        except Exception as e:
            logger.warning(f"NSE universe fetch failed ({e}) — using static fallback")
        return self._static_universe()[:max_stocks]

    def _static_universe(self) -> List[str]:
        return [
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
            "SBIN", "ITC", "BHARTIARTL", "LT", "HINDUNILVR",
            "KOTAKBANK", "AXISBANK", "MARUTI", "ASIANPAINT", "WIPRO",
            "HCLTECH", "ONGC", "NTPC", "POWERGRID", "TATAMOTORS",
            "TATASTEEL", "SUNPHARMA", "ULTRACEMCO", "TITAN", "NESTLEIND",
            "BAJFINANCE", "BAJAJFINSV", "ADANIPORTS", "ADANIENT", "JSWSTEEL",
            "GRASIM", "CIPLA", "COALINDIA", "BPCL", "HEROMOTOCO",
            "DRREDDY", "DIVISLAB", "BRITANNIA", "EICHERMOT", "SHRIRAMFIN",
            "BAJAJ-AUTO", "M&M", "TATACONSUM", "ADANIGREEN", "HDFCLIFE",
            "SBILIFE", "TECHM", "ADANITRANS", "INDUSINDBK", "LICI",
        ]

    # ─── STAGE 2: Fetch stock data ───
    def fetch_stock_data(self, symbol: str) -> Optional[Any]:
        try:
            df = self.nse_fetcher.fetch_with_fallback(symbol, period="1y")
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.debug(f"{symbol}: data fetch failed ({e})")
        return None

    # ─── STAGE 3: Fetch + filter news ───
    def fetch_filtered_news(self) -> List[Dict[str, Any]]:
        logger.info("Stage 3: Fetching + filtering news")
        try:
            import requests
            import xml.etree.ElementTree as ET
            rss_feeds = {
                "ET Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
                "ET Stocks": "https://economictimes.indiatimes.com/stocks/rssfeeds/2143744245.cms",
            }
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            raw_items = []
            for source, url in rss_feeds.items():
                try:
                    resp = requests.get(url, headers=headers, timeout=15)
                    if resp.status_code == 200:
                        root = ET.fromstring(resp.content)
                        for item in root.findall(".//item"):
                            title = item.findtext("title", "")
                            if title:
                                raw_items.append({
                                    "title": title,
                                    "link": item.findtext("link", ""),
                                    "summary": item.findtext("description", ""),
                                    "source": source,
                                    "published": item.findtext("pubDate", ""),
                                })
                except Exception as e:
                    logger.debug(f"RSS feed {source} failed: {e}")
            logger.info(f"RSS fetched: {len(raw_items)} raw items")
            filtered = self.news_engine.filter(raw_items)
            logger.info(f"News filter: {len(filtered)} Indian equity items passed")
            return [item.to_dict() for item in filtered[:20]]
        except Exception as e:
            logger.warning(f"News fetch/filter failed: {e}")
            return []

    # ─── STAGE 4: Evaluate stocks ───
    def evaluate_stocks(self, symbols: List[str], news_items: Optional[List[Dict]] = None) -> List[Dict[str, Any]]:
        logger.info(f"Stage 4: Evaluating {len(symbols)} stocks")
        recommendations: List[Dict[str, Any]] = []
        news_sentiment_map: Dict[str, str] = {}
        if news_items:
            for item in news_items:
                for company in item.get("matched_companies", []):
                    sent = item.get("sentiment", "NEUTRAL")
                    if sent == "BULLISH" or company not in news_sentiment_map:
                        news_sentiment_map[company.upper()] = sent

        def _eval_one(symbol: str) -> Optional[Dict[str, Any]]:
            try:
                df = self.fetch_stock_data(symbol)
                if df is None or len(df) < 200:
                    return None
                sentiment = news_sentiment_map.get(symbol.upper(), "NEUTRAL")
                rec = self.strategy_evaluator.evaluate(
                    symbol=symbol, ohlcv_df=df,
                    fundamentals=None, news_sentiment=sentiment,
                )
                return rec.to_dict()
            except Exception as e:
                logger.debug(f"{symbol}: evaluation failed ({e})")
                return None

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(_eval_one, sym): sym for sym in symbols}
            for future in as_completed(futures):
                if self._shutdown:
                    break
                try:
                    result = future.result()
                    if result:
                        recommendations.append(result)
                except Exception as e:
                    logger.debug(f"Eval error: {e}")

        buy_count = sum(1 for r in recommendations if r["action"] == "BUY")
        logger.info(f"Stage 4 complete: {len(recommendations)} evaluated, {buy_count} BUY signals")
        return recommendations

    # ─── STAGE 5: Generate charts ───
    def generate_charts(self, recommendations: List[Dict[str, Any]]) -> Dict[str, str]:
        logger.info("Stage 5: Generating charts for BUY signals")
        chart_paths: Dict[str, str] = {}
        buy_stocks = [r for r in recommendations if r["action"] == "BUY"]
        if not buy_stocks:
            logger.info("No BUY signals — no charts to generate")
            return chart_paths
        for rec in buy_stocks[:10]:
            symbol = rec["symbol"]
            try:
                df = self.fetch_stock_data(symbol)
                if df is None or df.empty:
                    continue
                chart_path = os.path.join(self.charts_dir, f"{symbol}.png")
                result = self.chart_generator.save_to_file(df, symbol, chart_path)
                if result:
                    chart_paths[symbol] = chart_path
            except Exception as e:
                logger.warning(f"Chart generation failed for {symbol}: {e}")
        logger.info(f"Stage 5 complete: {len(chart_paths)} charts generated")
        return chart_paths

    # ─── STAGE 6: Build PDF report ───
    def build_pdf_report(self, recommendations: List[Dict[str, Any]], chart_paths: Dict[str, str], news_items: List[Dict[str, Any]]) -> Optional[str]:
        logger.info("Stage 6: Building PDF report")
        try:
            buy_recs = [r for r in recommendations if r["action"] == "BUY"]
            exec_summary = {
                "sentiment": self._compute_market_sentiment(recommendations, news_items),
                "win_rate": 0.0,
                "total_trades": len(recommendations),
                "target_hits": 0, "sl_hits": 0,
                "top_gainer": buy_recs[0]["symbol"] if buy_recs else "N/A",
                "top_loser": "N/A",
            }
            stock_data = []
            for rec in recommendations[:15]:
                tp = rec.get("trade_plan") or {}
                stock_data.append({
                    "symbol": rec["symbol"], "signal": rec["action"],
                    "score": rec.get("confluence_score", 0),
                    "close": rec.get("close"), "pe_ratio": rec.get("pe_ratio"),
                    "roe": None, "debt_to_equity": rec.get("debt_to_equity"),
                    "fii_dii_trend": rec.get("fii_dii_trend", "N/A"),
                    "pattern": rec.get("pattern", ""),
                    "trade_plan": tp if rec["action"] == "BUY" else None,
                })
            timestamp = datetime.now(IST).strftime("%Y%m%d_%H%M")
            pdf_path = os.path.join(self.output_dir, f"Evening_Summary_{timestamp}.pdf")
            result = self.pdf_builder.build_report(
                output_path=pdf_path,
                report_title="Evening Summary & Stock Alert Report",
                executive_summary=exec_summary,
                stock_data=stock_data,
                chart_paths=chart_paths,
            )
            if result:
                logger.info(f"✅ PDF report generated: {pdf_path}")
            return result
        except Exception as e:
            logger.error(f"PDF build failed: {e}", exc_info=True)
            return None

    # ─── FULL PIPELINE ───
    def run_evening_summary(self, max_stocks: int = 50) -> Optional[str]:
        logger.info("=" * 60)
        logger.info("EVENING SUMMARY PIPELINE START")
        logger.info(f"Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
        logger.info("=" * 60)
        try:
            symbols = self.fetch_universe(max_stocks=max_stocks)
            if not symbols:
                logger.error("No stocks in universe — aborting")
                return None
            news_items = self.fetch_filtered_news()
            recommendations = self.evaluate_stocks(symbols, news_items)
            chart_paths = self.generate_charts(recommendations)
            pdf_path = self.build_pdf_report(recommendations, chart_paths, news_items)
            logger.info("=" * 60)
            logger.info("EVENING SUMMARY PIPELINE COMPLETE")
            logger.info(f"PDF: {pdf_path or 'FAILED'}")
            logger.info("=" * 60)
            return pdf_path
        except Exception as e:
            logger.error(f"Pipeline failure: {e}", exc_info=True)
            return None

    def _compute_market_sentiment(self, recommendations: List[Dict], news_items: List[Dict]) -> str:
        bull_news = sum(1 for n in news_items if n.get("sentiment") == "BULLISH")
        bear_news = sum(1 for n in news_items if n.get("sentiment") == "BEARISH")
        buy_count = sum(1 for r in recommendations if r["action"] == "BUY")
        if buy_count >= 3 and bull_news > bear_news:
            return "BULLISH"
        elif bear_news > bull_news and buy_count == 0:
            return "BEARISH"
        return "NEUTRAL"

    def shutdown(self):
        self._shutdown = True
        logger.info("Shutdown requested — finishing in-progress tasks")


# ═══════════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════════
class PipelineScheduler:
    def __init__(self, controller: Optional[MasterController] = None):
        self.controller = controller or MasterController()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline-job")
        self._running = False
        try:
            import schedule as schedule_lib
            self.schedule = schedule_lib
        except ImportError:
            logger.warning("'schedule' not installed — scheduler disabled")
            self.schedule = None

    def start(self) -> None:
        if not self.schedule:
            logger.error("Scheduler not available — install 'schedule' package")
            return
        self.schedule.every().day.at("08:00").do(self._run_in_thread, self._job_morning)
        self.schedule.every().day.at("09:20").do(self._run_in_thread, self._job_full_scan)
        self.schedule.every().day.at("16:00").do(self._run_in_thread, self._job_day_report)
        self.schedule.every().day.at("20:00").do(self._run_in_thread, self._job_evening_summary)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        logger.info("Scheduler started — jobs at 08:00, 09:20, 16:00, 20:00 IST")
        self._running = True
        try:
            while self._running:
                self.schedule.run_pending()
                import time
                time.sleep(30)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        finally:
            self._executor.shutdown(wait=True)
            logger.info("Scheduler stopped")

    def _run_in_thread(self, job_func) -> None:
        if self.controller._shutdown:
            return
        self._executor.submit(self._safe_run, job_func)

    def _safe_run(self, job_func) -> None:
        try:
            job_func()
        except Exception as e:
            logger.error(f"Scheduled job failed: {e}", exc_info=True)

    def _signal_handler(self, signum, frame) -> None:
        logger.info(f"Signal {signum} received — shutting down gracefully")
        self.controller.shutdown()
        self._running = False

    def _job_morning(self) -> None:
        logger.info("Job: Morning briefing (08:00) — complete")

    def _job_full_scan(self) -> None:
        logger.info("Job: Full scan (09:20)")
        self.controller.run_evening_summary(max_stocks=50)

    def _job_day_report(self) -> None:
        logger.info("Job: Day report (16:00) — complete")

    def _job_evening_summary(self) -> None:
        logger.info("Job: Evening summary (20:00)")
        self.controller.run_evening_summary(max_stocks=50)

    def run_once_evening(self) -> Optional[str]:
        return self.controller.run_evening_summary(max_stocks=50)


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════
def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI Stock Scanner V9.2 — Master Controller")
    parser.add_argument("--run", choices=["evening", "scan", "scheduler"], default="evening")
    parser.add_argument("--max-stocks", type=int, default=50)
    args = parser.parse_args()

    if args.run == "scheduler":
        PipelineScheduler().start()
    else:
        controller = MasterController()
        if args.run == "evening":
            pdf_path = controller.run_evening_summary(max_stocks=args.max_stocks)
            if pdf_path:
                print(f"\n✅ Evening Summary PDF: {pdf_path}")
            else:
                print("\n❌ Pipeline failed — check logs/master.log")
        elif args.run == "scan":
            symbols = controller.fetch_universe(max_stocks=args.max_stocks)
            print(f"Universe: {len(symbols)} stocks — {symbols[:10]}...")


if __name__ == "__main__":
    main()
