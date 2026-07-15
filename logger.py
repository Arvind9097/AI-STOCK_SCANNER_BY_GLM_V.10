"""
===========================================================
 LOGGER (V8.2.0 — RotatingFileHandler + date in format)
===========================================================
Simple central logger. Console + logs/scanner.log dono jagah likhta hai.

V8.2.0 FIX (Task F5):
  1. `RotatingFileHandler` use karta hai (10 MB × 5 files). Pehle
     `FileHandler` append-only tha - Render free tier (512MB disk) par
     kuch hafton mein disk full ho jaata tha aur har write silently fail.
  2. Date format mein `%Y-%m-%d` add kiya - pehle sirf `%H:%M:%S` tha,
     multi-day logs mein line kis din ki hai ye pata nahi chalta tha.
===========================================================
"""

import logging
import logging.handlers
import os

from config import LOGS_DIR

os.makedirs(LOGS_DIR, exist_ok=True)

logger = logging.getLogger("ai_scanner")
logger.setLevel(logging.INFO)

if not logger.handlers:
    # V8.2.0: date+time format - multi-day logs mein line identification easy.
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # V8.2.0: RotatingFileHandler - 10MB × 5 files = max 50MB disk usage.
    # Pehle FileHandler use hota tha - unbounded growth, Render free tier
    # disk full issue.
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOGS_DIR, "scanner.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
