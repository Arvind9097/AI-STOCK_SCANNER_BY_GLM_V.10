"""
===========================================================
 DISPATCH STATE (V8.1 - duplicate-send guard PERMANENTLY DISABLED)
===========================================================
V6 mein ye file ek "har command din mein sirf ek baar chalega"
guard implement karti thi. --force flag ke bina agar koi command
(--scan, --morning, --closebuys, --report, --evening) dobara chalao
to Telegram par kuch nahi jaata tha (already_dispatched_today True
mil jaata).

V8.1 REQUIREMENT: Ye lock ab HAMESHA KE LIYE HATA diya gaya hai. Ab
har command - CLI se ho ya --schedule scheduler se - HAR BAAR fresh
data fetch karke turant Telegram par bhejta hai, chahe wahi command
usi din pehle bhi chal chuka ho. Koi state file, koi date-check, koi
skip nahi hota.

--force flag ab bhi CLI mein accept hota hai (backward-compatible -
purane scripts/scheduler entries "--scan --force" jaisa likhte hain
to wo abhi bhi chalega, bas ab iska koi alag effect nahi hai kyunki
lock khud hi hamesha "nahi laga" wala result deta hai).

V8.2.0 (Task F5):
  - Atomic-write helpers (`atomic_write_json`) aur IST timezone
    available (state re-enabled karne ke liye ready), lekin V8.1
    behavior (always-False) preserved - backward-compatible.
  - `load_dispatch_state()` graceful missing-file handling - default
    return karta hai, crash nahi karta.
===========================================================
"""

import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

# V8.2.0: IST for date comparison (host TZ se independent).
IST = ZoneInfo("Asia/Kolkata")

# V8.2.0: state file location - re-enabled karne par use hoga.
try:
    from config import DATA_DIR
    _STATE_FILE = os.path.join(DATA_DIR, "dispatch_state.json")
except ImportError:
    _STATE_FILE = "data/dispatch_state.json"

# V8.2.0: feature flag - V8.1 mein intentionally False. True karne par
# atomic-write based per-day dispatch guard activate ho jaayega (utility
# available, just disabled by design).
_DISPATCH_LOCK_ENABLED = False


def _today_str():
    """V8.2.0: IST date (host TZ se independent)."""
    return datetime.now(IST).strftime("%Y-%m-%d")


def load_dispatch_state():
    """
    V8.2.0: state file atomically read karta hai. Missing/corrupt file
    par default empty dict return karta hai - crash nahi karta.
    Re-enabled (V8.2.0 flag True hone par) atomic_write_json se
    compatible structure use karta hai.
    """
    if not _DISPATCH_LOCK_ENABLED:
        return {}
    if not os.path.exists(_STATE_FILE):
        return {}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        # corrupt / half-written file - default return
        return {}


def already_dispatched_today(key):
    """
    V8.1: HAMESHA False return karta hai - koi bhi command kabhi
    'already sent today' nahi maana jaata. Matlab: har run par
    turant fresh dispatch hota hai, chahe --force diya ho ya nahi.

    V8.2.0: Agar `_DISPATCH_LOCK_ENABLED` True ho jaaye (future), to
    per-day atomic state check karega. Abhi False = no-op.
    """
    if not _DISPATCH_LOCK_ENABLED:
        return False
    state = load_dispatch_state()
    entry = state.get(key)
    if not entry:
        return False
    return entry.get("date") == _today_str()


def mark_dispatched_today(key):
    """
    V8.1: No-op - state ab track hi nahi hoti (jaanbujh kar).

    V8.2.0: Agar flag True ho to atomic_write_json use karega
    (crash-safe). Abhi disabled.
    """
    if not _DISPATCH_LOCK_ENABLED:
        return
    try:
        from utils import atomic_write_json
        state = load_dispatch_state()
        state[key] = {"date": _today_str(), "ts": datetime.now(IST).isoformat()}
        os.makedirs(os.path.dirname(_STATE_FILE) or ".", exist_ok=True)
        atomic_write_json(_STATE_FILE, state)
    except Exception:
        # Silent fail - dispatch guard best-effort hai, crash nahi hona chahiye.
        pass
