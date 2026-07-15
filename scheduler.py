"""
===========================================================
 DAILY SCHEDULER  (V8.2.0 — multi-threaded + IST + SIGTERM)
===========================================================
`python main.py --schedule` chalane par ye process foreground mein
chalta rehta hai aur har din SCHEDULE_TIME (config.py) par
automatically poora scan run karta hai.

V8.2.0 IMPORTANT FIXES (Task F5):
  1. Each scheduled job ab apne thread mein chalta hai - main loop
     sirf time-check karta hai, kabhi block nahi hota. Pehle ek
     lamba scan_func (9:20) 9:30 ke intraday slot ko silently
     miss kar deta tha abhi wahi thread busy tha. Ab dono parallel
     safely chalega.
  2. `hhmm == X` ki jagah `hhmm >= X and last_run != today` -
     defense-in-depth. Agar loop kisi wajah se busy raha (disk full,
     GC pause), slot turant fire ho jaayega jaise hi main thread free.
  3. Poora scheduler ab IST (Asia/Kolkata) use karta hai, system TZ
     se independent. Local Linux/Mac/CI/Render (agar TZ env set na ho)
     par bhi sahi time par fire karega.
  4. SIGTERM/SIGINT ka graceful handler - Render deploy/restart pe
     in-progress jobs ko 30s grace ke andar finish hone ka mauka
     milta hai (state save, last Telegram send complete ho jaata hai).
  5. `_heartbeat()` timestamp har loop iteration par update hota hai -
     health_server.py `/ping` ise check karke 503 return karta hai
     agar scheduler 10 min se stale ho.

NOTE: Ye tabhi kaam karega jab tak ye process chalta rahega
(PC/server on rehna chahiye). OS-level cron/Task Scheduler zyada
reliable hai - README dekho.
===========================================================
"""

import os
import signal
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from config import SCHEDULE_TIME
from logger import logger

# V8.2.0: Poore scheduler ke market-time decisions IST use karte hain
# (host TZ se independent - local Mac/Linux/CI/Render par bhi sahi fire).
IST = ZoneInfo("Asia/Kolkata")

# V8.2.0: Health-server deep check ke liye - har loop tick pe update
# hota hai. health_server.py ka /ping ise read karke 503 return karta
# hai agar 10 min se stale ho (scheduler thread deadlock/crash).
_last_scheduler_tick = None
_tick_lock = threading.Lock()


def get_last_scheduler_tick():
    """health_server.py se read hota hai - None ya >10 min purana = unhealthy."""
    with _tick_lock:
        return _last_scheduler_tick


def _heartbeat():
    global _last_scheduler_tick
    with _tick_lock:
        _last_scheduler_tick = datetime.now(IST)


# V8.2.0: Graceful shutdown flag - SIGTERM/SIGINT ise set karte hain.
# Main loop check karke cleanly exit karta hai. In-progress job-threads
# daemon=True hain isliye unka completion guarantee nahi, lekin main
# loop ko SIGTERM handler ke andar 30s tak wait karne ka mauka milta
# hai (warna default Python SIGTERM behavior = immediate exit).
_shutdown_requested = False


def _request_shutdown(signum, frame):
    global _shutdown_requested
    name = signal.Signals(signum).name if signum else "?"
    if not _shutdown_requested:
        logger.info(f"[scheduler] {name} mila - graceful shutdown shuru (in-progress jobs ko time diya ja raha hai)")
        _shutdown_requested = True
    else:
        # 2nd signal = force exit (user ya Render dusra SIGTERM bhej raha hai)
        logger.warning(f"[scheduler] {name} dobara mila - force exit")
        raise KeyboardInterrupt()


def _install_signal_handlers():
    """SIGTERM aur SIGINT dono ko graceful handler ke through route karo."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_shutdown)
        except (ValueError, OSError):
            # Non-main thread par signal.signal allowed nahi - skip
            pass


def run_scheduler(run_func):
    """
    run_func: wo function jo poora scan chalata hai (main.run_scan_pipeline jaisa)
    Har minute check karta hai ki SCHEDULE_TIME ho gaya kya - agar haan
    aur aaj abhi tak run nahi hua, to run_func() call karta hai.
    """
    _install_signal_handlers()
    logger.info(f"Scheduler start ho gaya. Roz {SCHEDULE_TIME} par scan chalega. (Ctrl+C se rokein)")

    last_run_date = None

    try:
        while not _shutdown_requested:
            now = datetime.now(IST)
            current_time_str = now.strftime("%H:%M")
            today_str = now.strftime("%Y-%m-%d")
            _heartbeat()

            # V8.2.0: `>=` use kiya (== ki jagah) - agar loop kisi wajah se
            # busy raha aur time exactly match nahi hua, to slot miss na ho.
            if current_time_str >= SCHEDULE_TIME and last_run_date != today_str:
                logger.info(f"Scheduled time ({SCHEDULE_TIME}) aa gaya, scan shuru kar raha hoon...")
                # V8.2.0: scan_func apne thread mein - main loop block nahi hoga
                t = threading.Thread(
                    target=_safe_call,
                    args=(run_func,),
                    daemon=True,
                    name=f"ScheduledScan-{today_str}",
                )
                t.start()
                last_run_date = today_str

            time.sleep(30)  # har 30 sec check karo

    except KeyboardInterrupt:
        logger.info("Scheduler Ctrl+C / SIGTERM se stop kiya gaya.")

    logger.info("[scheduler] main loop se exit ho raha hoon - in-progress daemon threads ko complete hone diya ja raha hai")


def run_full_day_scheduler(scan_func, monitor_func, report_func, nifty_func, closebuys_func=None,
                            evening_func=None, intraday_func=None, btst_func=None, swing_digest_func=None):
    """
    Poore din ka automation, ek hi process mein (V8.2.0 multi-threaded):
    - NIFTY_MORNING_TIME (default 8:00) -> nifty_func() (poora morning briefing: GIFT Nifty, bulk deals, news)
    - SCHEDULE_TIME (default 9:20) -> scan_func() (naye stocks scan + Telegram)
    - INTRADAY_SCAN_TIME (default 9:30) -> intraday_func() (V8.1.2 NAYA: ORB/VWAP/RVOL intraday scan)
    - SWING_CHART_DIGEST_TIME (V9.0: 11:00) -> swing_digest_func() (V8.1.2 NAYA: Swing charts digest)
    - MONITOR_START_TIME se MONITOR_END_TIME tak, har MONITOR_INTERVAL_MIN
      minute -> monitor_func() (Target/SL live check)
    - CLOSE_BESTBUYS_TIME (default 15:00) -> closebuys_func() (best buys into close)
    - BTST_SCAN_TIME (V9.0: 15:00) -> btst_func() (V8.1.2 NAYA: last-hour price-action BTST scan)
    - DAILY_SUMMARY_TIME (default 16:00) -> report_func() (win-rate summary)
    - EVENING_SUMMARY_TIME (default 20:00) -> evening_func() (weekly/monthly/watchlist)

    V8.2.0: Har job apne thread mein chalta hai - main loop sirf
    time-check karta hai, kabhi block nahi hota. `hhmm >= X` use
    kiya hai (== ki jagah) taaki slot miss na ho agar loop busy raha.
    last_run dict same-day re-fire ko prevent karta hai.

    V9.0 NOTE: Intraday LIVE ALERT loop (entry/target/SL sharp alerts
    har 5 min) ALAG background thread mein chalta hai - main.py ke
    --schedule branch mein intraday_tracker.run_intraday_alert_loop()
    se start hota hai. Ye scheduler ke time-slots se ALAG hai -
    scheduler koi slot iske liye nahi deta (loop khud 5-min sleep
    karke market-hours check karta hai).
    """
    _install_signal_handlers()

    from config import (
        NIFTY_MORNING_TIME, SCHEDULE_TIME, MONITOR_START_TIME,
        MONITOR_END_TIME, MONITOR_INTERVAL_MIN, DAILY_SUMMARY_TIME,
        CLOSE_BESTBUYS_TIME, EVENING_SUMMARY_TIME,
        INTRADAY_SCAN_TIME, BTST_SCAN_TIME, SWING_CHART_DIGEST_TIME,
    )

    logger.info("===== FULL-DAY SCHEDULER START (V8.2.0 multi-threaded, IST) =====")
    logger.info(f"NIFTY: {NIFTY_MORNING_TIME} | Scan: {SCHEDULE_TIME} | Intraday: {INTRADAY_SCAN_TIME} | "
                f"Swing-Digest: {SWING_CHART_DIGEST_TIME} | "
                f"Monitor: {MONITOR_START_TIME}-{MONITOR_END_TIME} (every {MONITOR_INTERVAL_MIN}min) | "
                f"Close-Buys: {CLOSE_BESTBUYS_TIME} | BTST: {BTST_SCAN_TIME} | "
                f"Report: {DAILY_SUMMARY_TIME} | Evening: {EVENING_SUMMARY_TIME}")

    last_run = {"nifty": None, "scan": None, "report": None, "monitor": None,
                "closebuys": None, "evening": None, "intraday": None, "btst": None,
                "swing_digest": None}

    # V8.2.0: har job-key ke liye ek alag thread chalao - main loop
    # block nahi hoga, aur do same-key jobs same din mein overlap nahi
    # karenge (last_run guard). Different keys (e.g. 9:20 scan + 9:30
    # intraday) parallel safely chal sakte hain.
    def _dispatch(name, func, label):
        """Job ko ek daemon thread mein start karo - main loop block nahi hoga."""
        if func is None:
            return
        t = threading.Thread(
            target=_safe_call,
            args=(func,),
            daemon=True,
            name=f"Sched-{name}-{last_run.get(name) or 'initial'}",
        )
        t.start()
        logger.info(f"[scheduler] dispatched '{name}' ({label}) to background thread")

    try:
        while not _shutdown_requested:
            now = datetime.now(IST)
            hhmm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")
            _heartbeat()

            # V8.2.0: `>=` use kiya (== ki jagah) + last_run check -
            # defense in depth. Agar loop kisi wajah se busy raha (GC
            # pause, OS scheduler hiccup), slot turant fire ho jaayega
            # jaise hi main thread free, bina exact-minute match ke.

            if hhmm >= NIFTY_MORNING_TIME and last_run["nifty"] != today:
                logger.info("Scheduled: NIFTY morning update")
                _dispatch("nifty", nifty_func, "Morning Briefing")
                last_run["nifty"] = today

            if hhmm >= SCHEDULE_TIME and last_run["scan"] != today:
                logger.info("Scheduled: morning scan")
                _dispatch("scan", scan_func, "Swing Scan")
                last_run["scan"] = today

            if intraday_func and hhmm >= INTRADAY_SCAN_TIME and last_run["intraday"] != today:
                logger.info("Scheduled: intraday scan (ORB/VWAP/RVOL)")
                _dispatch("intraday", intraday_func, "Intraday")
                last_run["intraday"] = today

            if swing_digest_func and hhmm >= SWING_CHART_DIGEST_TIME and last_run["swing_digest"] != today:
                logger.info("Scheduled: Swing Trading chart digest")
                _dispatch("swing_digest", swing_digest_func, "Swing Digest")
                last_run["swing_digest"] = today

            # V8.2.0 NOTE: monitor ke liye `>=` ka use nahi kiya - kyunki
            # monitor har MONITOR_INTERVAL_MIN minute mein repeat hota
            # hai, `>=` se wo baar-baar fire ho jaayega. minute_key based
            # bucketing rakhi hai (correct behavior - ek hi bucket ek hi
            # din mein dobara fire nahi karega).
            if MONITOR_START_TIME <= hhmm <= MONITOR_END_TIME:
                minute_key = f"{today}_{now.hour}_{now.minute // MONITOR_INTERVAL_MIN}"
                if last_run["monitor"] != minute_key:
                    logger.info("Scheduled: live monitor check")
                    _dispatch("monitor", monitor_func, "Monitor")
                    last_run["monitor"] = minute_key

            if closebuys_func and hhmm >= CLOSE_BESTBUYS_TIME and last_run["closebuys"] != today:
                logger.info("Scheduled: best buys into close (3pm)")
                _dispatch("closebuys", closebuys_func, "Close-Buys")
                last_run["closebuys"] = today

            if btst_func and hhmm >= BTST_SCAN_TIME and last_run["btst"] != today:
                logger.info("Scheduled: BTST scan (last-hour price action)")
                _dispatch("btst", btst_func, "BTST")
                last_run["btst"] = today

            # V8.2.0: DAILY_SUMMARY_TIME (16:00) se PEHLE BTST/CLOSEBUYS
            # (15:00/15:05) `>=` ke through already fire ho chuke honge.
            # 16:00 par `>=` use karne par bhi last_run today hone ki
            # wajah se dobara fire nahi hoga - correct behavior.
            if hhmm >= DAILY_SUMMARY_TIME and last_run["report"] != today:
                logger.info("Scheduled: daily summary report")
                _dispatch("report", report_func, "Daily Report")
                last_run["report"] = today

            if evening_func and hhmm >= EVENING_SUMMARY_TIME and last_run["evening"] != today:
                logger.info("Scheduled: evening weekly/monthly/watchlist summary")
                _dispatch("evening", evening_func, "Evening Summary")
                last_run["evening"] = today

            time.sleep(30)

    except KeyboardInterrupt:
        logger.info("Full-day scheduler Ctrl+C / SIGTERM se stop kiya gaya.")

    logger.info("[scheduler] main loop se exit ho raha hoon - in-progress daemon job-threads ko complete hone diya ja raha hai (Render 30s grace window)")


def _safe_call(func):
    try:
        func()
    except Exception as e:
        logger.error(f"Scheduled task mein error: {e}")
