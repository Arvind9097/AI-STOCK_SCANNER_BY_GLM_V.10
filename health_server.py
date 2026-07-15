# health_server.py
"""
===========================================================
 HEALTH SERVER (V8.2.0) - Render 24x7 Deployment Support + Deep Health
===========================================================
Render.com free tier ka problem: agar koi bhi HTTP request 15 min tak
nahi aati, service "sleep" ho jaati hai - agla request 50+ second lagta
hai (cold start), jisse scheduled scans/alerts miss ho sakte hain.

SOLUTION:
  1. Flask ka halka health server BACKGROUND THREAD mein chalao
     (main.py ka scheduler.py wala blocking `while True` loop bilkul
     waisa hi chalta rehta hai - koi behavior change nahi)
  2. /ping aur /health endpoints expose karo
  3. UptimeRobot (free, external) har 5 min /ping ko hit karta hai ->
     Render service kabhi sleep nahi hoti
  4. Backup ke taur par internal self-ping thread bhi chalta hai

V8.2.0 FIXES (Task F5):
  1. `/ping` ab DEEP health check karta hai - scheduler aur bot-listener
     ki heartbeat timestamps check karta hai. Agar koi thread 10 min se
     stale hai to 503 return karta hai (pehle hamesha 200 deta tha,
     deadlock/crash ke baad bhi UptimeRobot ko "OK" dikhta tha).
  2. `app.run(threaded=True)` - werkzeug dev server multi-threaded
     (concurrent UptimeRobot + self-ping + Render healthCheck handle kar paaye).
  3. self-ping thread ab pehle ping 60s startup delay ke baad karta hai
     (pehle interval*60 wait karta tha - Render 15-min inactivity sleep
     kick in ho jaata tha pehle ping se).
  4. IST timezone use kiya gaya hai (host TZ se independent).
===========================================================
"""

import os
import threading
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

HEALTH_PORT = int(os.environ.get("PORT", 8080))
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
SELF_PING_MINUTES = int(os.environ.get("SELF_PING_MINUTES", "10"))
IS_RENDER = os.environ.get("RENDER", "") == "true"

_start_time = datetime.now(IST)
_ping_count = 0
_last_ping_at = None

# V8.2.0: Deep-health heartbeat tracking. scheduler.py aur bot_listener.py
# apne-2 _heartbeat() functions se inhe update karte hain. /ping inhe
# check karke 503 return karta hai agar 10 min se stale ho.
_health_lock = threading.Lock()
_last_scheduler_tick = None  # datetime (IST-aware) ya None
_last_bot_tick = None
_HEALTH_STALE_THRESHOLD_MIN = 10  # >10 min stale = unhealthy


def update_scheduler_tick():
    """scheduler.py se har loop iteration par call hota hai (deep-health)."""
    global _last_scheduler_tick
    with _health_lock:
        _last_scheduler_tick = datetime.now(IST)


def update_bot_tick():
    """bot_listener.py se har polling iteration par call hota hai (deep-health)."""
    global _last_bot_tick
    with _health_lock:
        _last_bot_tick = datetime.now(IST)


def _get_scheduler_tick():
    """V8.2.0: scheduler.py ke internal _last_scheduler_tick ko lazy-import se read karta hai."""
    try:
        from scheduler import get_last_scheduler_tick
        return get_last_scheduler_tick()
    except Exception:
        return None


def _get_bot_tick():
    """V8.2.0: bot_listener.py ke internal _last_bot_tick ko read karta hai (agar exposed)."""
    try:
        from bot_listener import get_last_bot_tick
        return get_last_bot_tick()
    except Exception:
        return None


def _deep_health_status():
    """
    V8.2.0: scheduler aur bot-listener ki heartbeat timestamps check karta hai.
    scheduler.py se get_last_scheduler_tick() lazy-import karta hai (no
    circular-import issue). Agar koi 10 min se stale hai to unhealthy.
    Return: (healthy: bool, details: dict)
    """
    now = datetime.now(IST)
    details = {}
    healthy = True

    # V8.2.0: scheduler.py ke internal heartbeat se read karo (lazy import)
    sched_tick = _get_scheduler_tick()
    bot_tick = _get_bot_tick()
    if sched_tick is None:
        with _health_lock:
            sched_tick = _last_scheduler_tick
    if bot_tick is None:
        with _health_lock:
            bot_tick = _last_bot_tick

    if sched_tick is not None:
        age_min = (now - sched_tick).total_seconds() / 60
        details["scheduler_age_min"] = round(age_min, 1)
        if age_min > _HEALTH_STALE_THRESHOLD_MIN:
            healthy = False
            details["scheduler_status"] = f"STALE (> {_HEALTH_STALE_THRESHOLD_MIN} min, likely deadlock/crash)"
        else:
            details["scheduler_status"] = "alive"
    else:
        # scheduler abhi tick nahi kiya - shayad start hote hi check hua
        # ya scheduler branch mein nahi hai (e.g. standalone bot_listener).
        # Not necessarily unhealthy, just unknown.
        details["scheduler_status"] = "no_tick_yet"

    if bot_tick is not None:
        age_min = (now - bot_tick).total_seconds() / 60
        details["bot_age_min"] = round(age_min, 1)
        if age_min > _HEALTH_STALE_THRESHOLD_MIN:
            healthy = False
            details["bot_status"] = f"STALE (> {_HEALTH_STALE_THRESHOLD_MIN} min, likely deadlock/crash)"
        else:
            details["bot_status"] = "alive"
    else:
        details["bot_status"] = "no_tick_yet"

    return healthy, details


def _get_uptime_str():
    delta = datetime.now(IST) - _start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def create_health_app(bot_version="V8.2.0", extra_status_fn=None):
    """Flask app banata hai jisme /ping, /health, /status endpoints hain."""
    try:
        from flask import Flask, jsonify
    except ImportError:
        logger.error("Flask install nahi hai. Run: pip install flask")
        return None

    app = Flask(__name__)

    @app.route("/ping")
    @app.route("/")
    def ping():
        global _ping_count, _last_ping_at
        _ping_count += 1
        _last_ping_at = datetime.now(IST).strftime("%H:%M:%S")

        # V8.2.0: DEEP health check - scheduler aur bot-listener dono
        # ka liveness verify karta hai. Agar koi 10 min se stale hai to
        # 503 return karta hai taaki UptimeRobot alert raise kare.
        healthy, details = _deep_health_status()
        if healthy:
            return "OK", 200
        else:
            logger.warning(f"/ping DEEP-HEALTH FAIL: {details}")
            return jsonify({"status": "unhealthy", "details": details}), 503

    @app.route("/health")
    def health():
        healthy, details = _deep_health_status()
        return jsonify({
            "status": "healthy" if healthy else "unhealthy",
            "version": bot_version,
            "uptime": _get_uptime_str(),
            "started_at": _start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "pings_received": _ping_count,
            "last_ping": _last_ping_at,
            "is_render": IS_RENDER,
            "deep_health": details,
            "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        }), 200 if healthy else 503

    @app.route("/status")
    def status():
        base = {"status": "running", "version": bot_version, "uptime": _get_uptime_str()}
        if extra_status_fn:
            try:
                base.update(extra_status_fn())
            except Exception as e:
                base["status_error"] = str(e)
        return jsonify(base), 200

    return app


def start_health_server(app, port=None):
    """Flask health server ko daemon thread mein start karta hai - non-blocking."""
    if app is None:
        return None

    port = port or HEALTH_PORT

    def _run():
        try:
            import logging as _log
            _log.getLogger("werkzeug").setLevel(_log.ERROR)
            # V8.2.0: threaded=True explicitly - werkzeug dev server ki
            # implicit single-threaded behavior (Flask <1.0) fix. Concurrent
            # UptimeRobot + self-ping + Render healthCheck handle kar paaye.
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
        except Exception as e:
            logger.error(f"Health server error: {e}")

    t = threading.Thread(target=_run, daemon=True, name="HealthServer")
    t.start()
    logger.info(f"Health server port {port} par start ho gaya (threaded=True)")
    return t


def start_self_ping_thread(render_url=None, interval_minutes=None):
    """
    Backup self-ping - Render URL ko andar se hi periodically hit karta
    hai (UptimeRobot na set ho ya delay ho to bhi ek safety net).

    V8.2.0: pehle interval*60 wait karta tha pehle ping ke liye - Render
    15-min inactivity sleep kick in ho jaata tha pehle ping se (cold-start
    delay). Ab 60s startup delay ke baad pehla ping, phir regular interval.
    """
    url = render_url or RENDER_URL
    interval = interval_minutes or SELF_PING_MINUTES

    if not url and not IS_RENDER:
        logger.info("Self-ping thread disable hai (Render par nahi hai, URL bhi set nahi)")
        return None

    ping_url = (url.rstrip("/") + "/ping") if url else f"http://localhost:{HEALTH_PORT}/ping"

    def _loop():
        logger.info(f"Self-ping thread shuru - har {interval} min mein khud ko ping karega")
        # V8.2.0: pehla ping 60s startup delay ke baad (pehle interval*60
        # wait karta tha - Render sleep kick in ho jaata tha).
        time.sleep(60)
        fails = 0
        while True:
            try:
                resp = requests.get(ping_url, timeout=10)
                if resp.status_code == 200:
                    fails = 0
                else:
                    fails += 1
                    logger.warning(f"Self-ping non-200 (status {resp.status_code})")
            except Exception as e:
                fails += 1
                logger.debug(f"Self-ping fail: {e}")
            if fails >= 5:
                # V8.2.0: escalate - sirf reset nahi, ERROR log (already)
                # aur additional context. Telegram alert optional - main
                # pipeline (scheduler/bot) abhi alive ho sakta hai, isliye
                # exit nahi karte, sirf counter reset.
                logger.error(f"Self-ping {fails}x consecutive fail - health-server thread may be dead. Resetting counter, scheduler/bot independent chal sakte hain.")
                fails = 0
            time.sleep(interval * 60)

    t = threading.Thread(target=_loop, daemon=True, name="SelfPing")
    t.start()
    logger.info(f"Self-ping active -> {ping_url} (pehla ping 60s baad, phir har {interval} min)")
    return t


def setup_render_deployment(bot_version="V8.2.0", extra_status_fn=None, port=None):
    """
    Ek hi call mein Render 24x7 setup complete karta hai. main.py ke
    --schedule branch mein, scheduler start hone se PEHLE call karo.
    Non-blocking - turant return hoti hai.

    Example:
        from health_server import setup_render_deployment
        setup_render_deployment("V8.2.0")
        # ... phir scheduler.run_full_day_scheduler(...) jaisa normal chalega
    """
    logger.info(f"24x7 deployment setup ho raha hai [{bot_version}]")
    logger.info(f"  Platform: {'Render.com' if IS_RENDER else 'Local/Other'}")
    logger.info(f"  Port: {port or HEALTH_PORT}")

    app = create_health_app(bot_version, extra_status_fn)
    if app:
        start_health_server(app, port)
        time.sleep(1)

    start_self_ping_thread()

    logger.info("24x7 setup complete - UptimeRobot mein /ping endpoint add karo (5 min interval)")
    return app
