"""
===========================================================
 EMAIL ALERT MODULE (SMTP)
===========================================================
Setup (Gmail example):
1. Google Account -> Security -> 2-Step Verification (enable karo)
2. Google Account -> Security -> App Passwords -> naya app password banao
3. config.py mein SMTP_USER = tumhara gmail, SMTP_PASSWORD = wo app password
4. EMAIL_FROM aur EMAIL_TO set karo, EMAIL_ENABLED = True
===========================================================
"""

import smtplib
import ssl
import os
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

from config import (
    EMAIL_ENABLED, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
    EMAIL_FROM, EMAIL_TO, EMAIL_ATTACH_REPORTS, TOP_N_BUY_LIST,
)
from utils import escape_html
from logger import logger

# V8.2.0 FIX (bug #29): SMTP transient failures ke liye 1 retry.
# telegram_alerts.py ke pattern jaisa hi - single attempt se message
# permanently lost ho jaata tha agar SMTP thodi der down ho.
_SMTP_MAX_ATTEMPTS = 2
_SMTP_RETRY_SLEEP_SEC = 3
_SMTP_TIMEOUT_SEC = 30  # V8.2.0: connection timeout (pehle default indefinitely)


def _format_body(result):
    top = [r for r in result if r["Signal"] in ("STRONG BUY", "BUY")][:TOP_N_BUY_LIST]

    if not top:
        return "<p>Aaj koi BUY / STRONG BUY signal nahi mila.</p>"

    # V8.2.0 FIX (bug #21): HTML body mein stock names escape nahi hote
    # the - agar stock "M&M" hai to <b>M&M</b> malformed HTML hai.
    # escape_html laga ke sab dynamic text safe banate hain.
    rows_html = "".join(
        f"<tr><td>{i}</td><td><b>{escape_html(r['Stock'])}</b></td><td>{escape_html(r['Signal'])}</td>"
        f"<td>{r['Score']}</td><td>{r['Entry']}</td><td>{r['Stoploss']}</td>"
        f"<td>{r['Target']}</td><td>1:{r['Risk_Reward']}</td></tr>"
        for i, r in enumerate(top, start=1)
    )

    return f"""
    <h2>AI Stock Scanner - Top {len(top)} Buy List</h2>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">
        <tr style="background:#263238;color:white;">
            <th>#</th><th>Stock</th><th>Signal</th><th>Score</th>
            <th>Entry</th><th>Stoploss</th><th>Target</th><th>R:R</th>
        </tr>
        {rows_html}
    </table>
    <p>Poori detail attached Excel/PDF report mein hai.</p>
    """


def send_alert(result, excel_path=None, pdf_path=None):
    if not EMAIL_ENABLED:
        logger.info("EMAIL_ENABLED=False hai, Email alert skip kar raha hoon")
        return

    if not (SMTP_USER and SMTP_PASSWORD and EMAIL_TO):
        logger.warning("Email SMTP credentials config.py mein poore set nahi hain, skip")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_FROM or SMTP_USER
        msg["To"] = EMAIL_TO
        msg["Subject"] = "AI Stock Scanner - Daily Report"

        msg.attach(MIMEText(_format_body(result), "html"))

        if EMAIL_ATTACH_REPORTS:
            for path in (excel_path, pdf_path):
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        part = MIMEApplication(f.read(), Name=os.path.basename(path))
                    part["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
                    msg.attach(part)

        # V8.2.0 FIX (bug #10): SMTP port 465 (SMTPS, implicit SSL) ke
        # liye smtplib.SMTP_SSL use karte hain (port 465 par STARTTLS
        # upgrade fail karta hai - wo 587 wala protocol hai). Ab dono
        # ports supported hain. server.ehlo() bhi starttls se pehle
        # chahiye (kuch SMTP servers require karte hain).
        #
        # V8.2.0 FIX (bug #29): 1 retry ke saath SMTP send - transient
        # network failure pe message permanently lost nahi hota.
        last_exc = None
        for attempt in range(1, _SMTP_MAX_ATTEMPTS + 1):
            server = None
            try:
                if SMTP_PORT == 465:
                    # Implicit SSL (Gmail SMTPS, port 465).
                    ctx = ssl.create_default_context()
                    server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=_SMTP_TIMEOUT_SEC, context=ctx)
                else:
                    # STARTTLS (Gmail port 587, ya koi bhi non-465 port).
                    server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=_SMTP_TIMEOUT_SEC)
                    server.ehlo()  # V8.2.0 FIX: starttls se pehle ehlo
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()  # TLS ke baad bhi ehlo
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
                logger.info(f"Email bhej diya: {EMAIL_TO}")
                return
            except Exception as e:
                last_exc = e
                logger.warning(f"Email attempt {attempt}/{_SMTP_MAX_ATTEMPTS} fail: {e}")
                if attempt < _SMTP_MAX_ATTEMPTS:
                    time.sleep(_SMTP_RETRY_SLEEP_SEC)
            finally:
                # V8.2.0 FIX (bug #19): ensure SMTP connection close
                # in finally block - exception pe bhi no leak.
                if server is not None:
                    try:
                        server.quit()
                    except Exception:
                        pass

        logger.warning(f"Email bhejne mein error after {_SMTP_MAX_ATTEMPTS} attempts: {last_exc}")

    except Exception as e:
        logger.warning(f"Email bhejne mein error: {e}")
