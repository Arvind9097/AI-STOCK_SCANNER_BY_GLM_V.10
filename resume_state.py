"""
===========================================================
 RESUME STATE  (V8.2.0 — atomic write + IST)
===========================================================
Agar scan beech mein रुक jaaye (crash / Ctrl+C / internet gaya),
to agli baar jo symbols already successfully download ho chuke
the unhe dobara download nahi karta - seedha wahin se aage badhta
hai jahan se chhoda tha.

State file: data/resume_state.json
{
  "date": "2026-07-02",
  "completed": ["RELIANCE.NS", "TCS.NS", ...],
  "pending": ["INFY.NS", ...]
}

Naya trading din shuru hone par (date badalne par) state
automatically reset ho jaata hai - purane din ka resume state
kaam ka nahi hota.

V8.2.0 FIX (Task F5):
  1. Atomic write - pehle direct json.dump use hota tha, SIGTERM/OOM
     par half-written file corrupt ho jaati thi. Ab utils.atomic_write_json
     use karta hai (tempfile + os.replace + fsync - crash-safe).
  2. IST date use karta hai (`datetime.now(ZoneInfo("Asia/Kolkata"))`),
     host TZ se independent. Pehle `date.today()` system-TZ use karta
     tha - UTC box par 05:30 IST ke baad date roll hoti thi (midnight
     market resume state reset bug).
===========================================================
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from config import RESUME_ENABLED, RESUME_STATE_FILE
from utils import atomic_write_json
from logger import logger

# V8.2.0: IST for date-sensitive resume state (host TZ se independent).
# Poore India market decisions IST par based hain - system TZ (UTC etc)
# se date-roll 5:30 IST par ho jaata tha, midnight-market bug.
IST = ZoneInfo("Asia/Kolkata")


def _today_str():
    """V8.2.0: IST date return karta hai (host TZ se independent)."""
    return datetime.now(IST).strftime("%Y-%m-%d")


def load_resume_state(all_symbols):
    """
    Return: (completed_set, pending_list)
    Agar resume disabled hai ya state file nahi/purani hai, to
    completed=empty aur pending=all_symbols return hota hai.
    """
    if not RESUME_ENABLED or not os.path.exists(RESUME_STATE_FILE):
        return set(), list(all_symbols)

    try:
        with open(RESUME_STATE_FILE, "r", encoding="utf-8") as f:
            import json
            state = json.load(f)
    except Exception:
        # V8.2.0: corrupt/missing file gracefully handle - default state.
        # atomic_write_json se ab corruption highly unlikely, par phir bhi.
        return set(), list(all_symbols)

    if state.get("date") != _today_str():
        # naya din - purana resume state kaam ka nahi
        return set(), list(all_symbols)

    completed = set(state.get("completed", []))
    # sirf wahi symbols pending rakho jo aaj ki list mein bhi hain
    pending = [s for s in all_symbols if s not in completed]

    if completed:
        logger.info(f"Resume state mila: {len(completed)} stocks pehle se download ho chuke hain, unhe skip kar raha hoon")

    return completed, pending


def save_resume_state(completed_set):
    """
    V8.2.0: utils.atomic_write_json use karta hai - tempfile + os.replace
    + fsync. SIGTERM/crash par bhi file kabhi half-written nahi hogi.
    """
    if not RESUME_ENABLED:
        return
    try:
        os.makedirs(os.path.dirname(RESUME_STATE_FILE) or ".", exist_ok=True)
        atomic_write_json(
            RESUME_STATE_FILE,
            {"date": _today_str(), "completed": sorted(completed_set)},
        )
    except Exception as e:
        logger.warning(f"Resume state save nahi ho paya: {e}")


def clear_resume_state():
    """Poora scan successfully complete hone par state file hata do."""
    if os.path.exists(RESUME_STATE_FILE):
        try:
            os.remove(RESUME_STATE_FILE)
        except Exception:
            pass
