"""
===========================================================
 WHATSAPP ALERT MODULE (via Twilio)
===========================================================
Setup (one-time):
1. https://www.twilio.com par free account banao.
2. Console se ACCOUNT_SID aur AUTH_TOKEN copy karo -> config.py mein daalo.
3. Twilio Console -> Messaging -> Try it out -> "Send a WhatsApp message"
   mein jaake sandbox join karo (apne WhatsApp se ek join code bhejna
   hoga jo Twilio dikhayega).
4. TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886" (Twilio sandbox number)
5. WHATSAPP_TO = "whatsapp:+91XXXXXXXXXX" (tumhara apna number, jisne join kiya)
6. config.py mein WHATSAPP_ENABLED = True karo.

Note: Twilio sandbox free hai lekin 24-hour session window hoti hai -
production ke liye Twilio se apna verified WhatsApp Business number
lena hoga.
===========================================================
"""

import requests
import time
from requests.auth import HTTPBasicAuth

from config import (
    WHATSAPP_ENABLED, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_FROM, WHATSAPP_TO, TOP_N_BUY_LIST,
)
from logger import logger

TWILIO_API_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

# V8.2.0 FIX (bug #17): pehle default requests timeout 15s tha but
# Twilio API kabhi-kabhi 30s+ leta hai on slow networks. Ab 30s.
# V8.2.0 FIX (bug #29): 1 retry on transient failure (telegram_alerts
# pattern jaisa hi) - single attempt se alert permanently lost hota tha.
_TWILIO_TIMEOUT = 30
_TWILIO_MAX_ATTEMPTS = 2
_TWILIO_RETRY_SLEEP_SEC = 3


def _format_message(result):
    # V8.2.0 FIX (bug #19): Filter out stocks with None entry/sl/target.
    # Pehle "R:R 1:None" / "Entry None | SL None" jaisa unprofessional
    # output user ke WhatsApp par jaata tha agar scanner.py me koi field
    # None thi (edge case). Ab sirf complete-records hi format karte hain.
    top = [
        r for r in result
        if r["Signal"] in ("STRONG BUY", "BUY")
        and r.get("Entry") is not None
        and r.get("Stoploss") is not None
        and r.get("Target") is not None
    ][:TOP_N_BUY_LIST]

    if not top:
        return "AI Stock Scanner: Aaj koi BUY / STRONG BUY signal nahi mila."

    lines = [f"*AI Stock Scanner - Top {len(top)} Buy List*\n"]
    for i, r in enumerate(top, start=1):
        stock = r.get('Stock', 'N/A')
        signal = r.get('Signal', 'N/A')
        score = r.get('Score', 'N/A')
        entry = r.get('Entry', 0)
        sl = r.get('Stoploss', 0)
        target = r.get('Target', 0)
        rr = r.get('Risk_Reward', 'N/A')
        # V8.2.0 FIX (bug #19): None RR ko "N/A" show karo, "1:None" nahi.
        rr_str = f"1:{rr}" if rr not in (None, 'N/A') else "N/A"
        lines.append(
            f"{i}. {stock} - {signal} (Score {score})\n"
            f"   Entry {entry} | SL {sl} | Target {target} | R:R {rr_str}"
        )
    return "\n".join(lines)


def send_alert(result):
    if not WHATSAPP_ENABLED:
        logger.info("WHATSAPP_ENABLED=False hai, WhatsApp alert skip kar raha hoon")
        return

    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and WHATSAPP_TO):
        logger.warning("Twilio credentials config.py mein poore set nahi hain, WhatsApp alert skip")
        return

    url = TWILIO_API_URL.format(sid=TWILIO_ACCOUNT_SID)
    body = _format_message(result)
    data = {
        "From": TWILIO_WHATSAPP_FROM,
        "To": WHATSAPP_TO,
        "Body": body,
    }

    # V8.2.0 FIX (bug #29): 1 retry on transient network failure.
    last_exc = None
    for attempt in range(1, _TWILIO_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url,
                auth=HTTPBasicAuth(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                data=data,
                timeout=_TWILIO_TIMEOUT,  # V8.2.0 FIX (bug #17): explicit timeout
            )
            resp.raise_for_status()
            logger.info("WhatsApp par summary message bhej diya")
            return
        except requests.exceptions.RequestException as e:
            last_exc = e
            logger.warning(f"WhatsApp attempt {attempt}/{_TWILIO_MAX_ATTEMPTS} fail: {e}")
            if attempt < _TWILIO_MAX_ATTEMPTS:
                time.sleep(_TWILIO_RETRY_SLEEP_SEC)
        except Exception as e:
            last_exc = e
            logger.warning(f"WhatsApp unexpected error (attempt {attempt}): {e}")
            if attempt < _TWILIO_MAX_ATTEMPTS:
                time.sleep(_TWILIO_RETRY_SLEEP_SEC)

    logger.warning(f"WhatsApp message fail after {_TWILIO_MAX_ATTEMPTS} attempts: {last_exc}")
