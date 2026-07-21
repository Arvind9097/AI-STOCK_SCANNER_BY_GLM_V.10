"""
===========================================================
 GLOBAL CONFIGURATION FILE (V10.2 - Gemini AI Edition)
===========================================================
"""
import os
from dotenv import load_dotenv

# Load environment variables from .env file (if it exists)
load_dotenv()

# ==========================================
# 🤖 AI SETTINGS (Primary: Gemini)
# ==========================================
AI_MODE = "GEMINI_API"  # Set to "RULE_BASED" if you want 100% free offline analysis
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Legacy Z.AI (GLM) keys for backward compatibility
ZAI_API_KEY = os.environ.get("ZAI_API_KEY", "")
ZAI_API_BASE = os.environ.get("ZAI_API_BASE", "https://api.z.ai/api/paas/v4")
ZAI_MODEL = os.environ.get("ZAI_MODEL", "glm-4.5")

# AI Brain (Telegram Conversational AI)
AI_BRAIN_ENABLED = True
AI_BRAIN_MAX_TOKENS = 300

# ==========================================
# 📱 TELEGRAM SETTINGS
# ==========================================
# NEVER hardcode token here. Use Render Environment or .env file
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ==========================================
# 📈 DATA DOWNLOADING & FETCHING
# ==========================================
# Missing settings fixed:
SYMBOL_SOURCE = "NSE"  # Options: 'NSE', 'CSV', 'FALLBACK'
CSV_PATH = "data/nse_all_stocks.csv"
MAX_WORKERS = 10
CHUNK_SIZE = 10
SLEEP_BETWEEN_CHUNKS_SEC = 5

# ==========================================
# 💾 DATA & CACHE SETTINGS
# ==========================================
USE_CACHE = True
DATA_DIR = "data"
CACHE_DIR = "data/cache"
CACHE_MAX_AGE_HOURS = 20
PDF_REPORT_PATH = "reports/AI_Report.pdf"

# ==========================================
# 🚀 SCANNER & SCHEDULER SETTINGS
# ==========================================
INTRADAY_UNIVERSE_TOP_N = 100
INTRADAY_INTERVAL = "5m"

BTST_LOOKBACK_MINUTES = 60
BTST_DAY_HIGH_PROXIMITY_PCT = 1.5
BTST_TOP_N_RESULTS = 5

INTRADAY_LIVE_ALERT_ENABLED = True
INTRADAY_LIVE_ALERT_INTERVAL = 5

# Alert Timings
SWING_CHART_DIGEST_TIME = "10:00"
INTRADAY_SCAN_TIME = "09:30"
BTST_SCAN_TIME = "15:05"

# Run Weekend Report on Saturday (5) & Sunday (6)
WEEKEND_REPORT_DAYS = [5, 6] 

# ==========================================
# 📊 CUSTOM UNIVERSE & SECTORS
# ==========================================
# Add your favorite stocks here (always end with .NS)
CUSTOM_STOCKS = [
    "RELIANCE.NS", 
    "TCS.NS", 
    "HDFCBANK.NS", 
    "INFY.NS", 
    "ICICIBANK.NS",
    "TATAMOTORS.NS"
]

SECTORAL_INDICES = {
    "Nifty Bank": "^NSEBANK",
    "Nifty IT": "^CNXIT",
    "Nifty Auto": "^CNXAUTO",
    "Nifty Pharma": "^CNXPHARMA",
    "Nifty FMCG": "^CNXFMCG"
}
