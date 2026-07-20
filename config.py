"""
===========================================================
 AI STOCK SCANNER V8.1 - CONFIG
===========================================================
Yahan par saari settings ek jagah hain.
Jo bhi cheez change karni ho (stocks list, thresholds,
telegram token, etc.) - sab yahin karo.

V8.1 UPDATE - OPTIONAL .env SUPPORT:
Secrets (Telegram token, Anthropic key, Twilio, SMTP) ab EK .env
file se bhi load ho sakte hain - lekin ye purely OPTIONAL hai.
  - .env file NAHI hai ya python-dotenv install NAHI hai -> koi
    error nahi aayega, neeche diye hardcoded defaults chalte
    rahenge (bilkul V6 jaisa behavior, zero setup).
  - .env file BANATE ho (isi folder mein, .env.example se copy
    karke) -> usme diye values hardcoded defaults ko OVERRIDE
    kar denge, bina kisi aur file mein change kiye.
Isliye ye upgrade PURANA kaam karne wala setup kabhi nahi todega,
sirf ek zyada secure option ADD karta hai.
===========================================================
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # .env file mile to load, nahi to chup-chaap aage badho
except ImportError:
    # python-dotenv install nahi hai - koi baat nahi, hardcoded
    # defaults (neeche) use ho jaayenge. Crash BILKUL nahi hoga.
    pass


def _env_or(key, default):
    """os.environ mein value mile to wo use karo, warna hardcoded default."""
    val = os.getenv(key)
    return val if val else default


# -----------------------------------------------------------
# STOCK UNIVERSE
# -----------------------------------------------------------
SYMBOL_SOURCE = "NIFTY500"

CUSTOM_STOCKS = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS",
    "ICICIBANK.NS", "SBIN.NS", "LT.NS", "ITC.NS",
    "BHARTIARTL.NS", "HINDUNILVR.NS",
]

NIFTY500_LOCAL_CSV = "data/nifty500.csv"

# V9.1.3: NSE ALL STOCKS CSV (user-uploaded)
# User ek CSV file upload karta hai jisme sabhi NSE listed stocks hain
# (~7000 symbols). Ye file data/ folder mein rakhi jaati hai.
# Code is CSV ko PRIMARY source maanta hai — NSE archives (jo cloud
# par block hoti hain) ki zaroorat nahi padti.
#
# CSV FORMAT:
#   - Column "Symbol" hona chahiye (case-insensitive)
#   - Symbols bina .NS suffix ke (e.g. "RELIANCE", "TCS", "INFY")
#   - Code automatically .NS suffix add kar deta hai
#   - Example CSV row: RELIANCE, TCS, INFY, ...
#
# CSV kaise upload karein:
#   1. NSE website se EQUITY_L.csv download karo
#   2. Usse data/nse_all_stocks.csv naam se save karo
#   3. Git push karo — Render par permanently available
#   4. Ya Render Shell se directly upload karo
#
# Agar CSV nahi hai, to code fallback chain use karega:
#   NSE archives → static_universe → CUSTOM_STOCKS
NSE_ALL_STOCKS_CSV = "data/nse_all_stocks.csv"

# -----------------------------------------------------------
# DATA DOWNLOAD SETTINGS
# -----------------------------------------------------------
PERIOD = "1y"
INTERVAL = "1d"
# V8.1.2 UPDATE: CHUNK_SIZE 20 se 10 kiya gaya - primary sources
# (NSE Chart/Stooq/jugaad-data) individually har symbol try karte hain,
# yfinance sirf UNKI FAILURE ke baad hi call hota hai. Lekin agar kabhi
# teeno primary EK SAATH down ho jaayein (jaisa hosting-IP-block ki
# wajah se ho sakta hai), to poore bache hue symbols yfinance-batch
# mein chale jaate hain - chhota chunk-size us worst-case mein bhi
# rate-limit lagne se pehle zyada der tak kaam karne mein madad karta hai.
CHUNK_SIZE = 10
SLEEP_BETWEEN_CHUNKS_SEC = 5
DOWNLOAD_RETRIES = 4
RETRY_SLEEP_SEC = 5

RATE_LIMIT_BACKOFF_BASE_SEC = 30
RATE_LIMIT_MAX_BACKOFF_SEC = 300

CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN_SEC = 300

# -----------------------------------------------------------
# INDICATOR SETTINGS
# -----------------------------------------------------------
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200

RSI_PERIOD = 14
MIN_RSI = 55

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ADX_PERIOD = 14
MIN_ADX = 20

ATR_PERIOD = 14

SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 3

VOLUME_AVG_PERIOD = 20
VOLUME_SPIKE_MULTIPLIER = 1.5

BREAKOUT_LOOKBACK = 20
CONSOLIDATION_LOOKBACK = 15
CONSOLIDATION_RANGE_PCT = 6.0

SUPPORT_RESISTANCE_LOOKBACK = 60

# -----------------------------------------------------------
# RISK / REWARD SETTINGS
# -----------------------------------------------------------
ATR_STOPLOSS_MULTIPLIER = 1.5
MIN_RISK_REWARD = 1.5

# -----------------------------------------------------------
# SCORING SYSTEM (total = 100)
# -----------------------------------------------------------
SCORE_WEIGHTS = {
    "trend_ema": 20,
    "rsi": 15,
    "macd": 15,
    "adx": 15,
    "volume_spike": 15,
    "breakout": 10,
    "supertrend": 10,
}

SIGNAL_THRESHOLDS = {
    "STRONG BUY": 80,
    "BUY": 60,
    "WATCH": 40,
}

TOP_N_BUY_LIST = 10

# -----------------------------------------------------------
# OUTPUT PATHS
# -----------------------------------------------------------
REPORTS_DIR = "reports"
CHARTS_DIR = "charts"
LOGS_DIR = "logs"
DATA_DIR = "data"

EXCEL_REPORT_PATH = f"{REPORTS_DIR}/AI_Report.xlsx"
PDF_REPORT_PATH = f"{REPORTS_DIR}/AI_Report.pdf"

CHARTS_FOR_TOP_N = 10

# -----------------------------------------------------------
# TELEGRAM ALERT SETTINGS
# -----------------------------------------------------------
TELEGRAM_ENABLED = True
# V8.1.2 SECURITY FIX: Purana hardcoded bot token (jo is conversation
# mein plaintext share ho chuka tha) yahan se, aur telegram_alerts.py +
# bot_listener.py se bhi, PERMANENTLY hata diya gaya hai. Ab TOKEN sirf
# .env file (ya Render/hosting environment variable) se hi aata hai -
# koi hardcoded fallback nahi hai. Agar TELEGRAM_BOT_TOKEN set nahi hai,
# to bot startup par turant clear error dega (silently purana leaked
# token use nahi karega).
#
# SETUP: @BotFather ko Telegram par "/revoke" bhejo (agar purana token
# abhi tak revoke nahi kiya), naya token generate karo, phir:
#   - Local run: .env file mein TELEGRAM_BOT_TOKEN=<naya_token> daalo
#   - Render: Dashboard -> Environment -> TELEGRAM_BOT_TOKEN set karo
TELEGRAM_BOT_TOKEN = _env_or("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env_or("TELEGRAM_CHAT_ID", "")
TELEGRAM_SEND_CHARTS = True

# V9.1.2 FIX: Pehle ye config import par sys.exit(1) hota tha agar token
# missing tha. Problem: Render cold start par health server bhi start nahi
# hota tha → /ping respond nahi karta → UptimeRobot fail → Render restart
# loop mein aa jaata tha.
#
# Ab sirf warning print karte hain (no sys.exit). Token missing hone par:
#   - Health server start hoga (Render /ping respond karega)
#   - Bot listener warning log karega + skip ho jayega
#   - Scheduler + breaking news + scans bhi chaleinge (Telegram bhejna skip)
# Isse Render restart loop se bach jaate hain.
if not TELEGRAM_BOT_TOKEN:
    import sys
    print(
        "\n"
        "⚠️  WARNING: TELEGRAM_BOT_TOKEN environment variable set nahi hai.\n"
        "   Telegram alerts/bot DISABLED rahega jab tak token set nahi hota.\n"
        "   .env file mein (ya Render Dashboard -> Environment mein) ye daalo:\n"
        "       TELEGRAM_BOT_TOKEN=<aapka_naya_bot_token>\n"
        "       TELEGRAM_CHAT_ID=<aapka_group/channel_id>\n"
        "   (Health server + scheduler phir bhi chalenge — /ping respond karega.)\n",
        file=sys.stderr,
    )
    # V9.1.2: NO sys.exit(1) — health server start hone do, Render restart loop se bachao
    # Bot listener + Telegram alerts disable rahenge jab tak token set nahi hota

# -----------------------------------------------------------
# AI ANALYSIS SETTINGS
# -----------------------------------------------------------
# V8.2.0 UPDATE: Claude/Anthropic API ki jagah ab Z.AI ka GLM API
# use hota hai (OpenAI-compatible, cheaper, aur Hindi/Hinglish mein
# behtar). Claude mode COMPLETELY hata diya gaya hai.
#
# AI_MODE = "RULE_BASED"  -> free, instant, templated (default)
# AI_MODE = "GLM_API"     -> Z.AI GLM API se real natural-language
#                            analysis. ZAI_API_KEY zaroori hai.
#                            (https://z.ai par account banao, API key
#                             lo, yahan .env mein daalo)
AI_MODE = "RULE_BASED"

# Z.AI GLM API settings (https://z.ai)
# API key: Z.AI management console se lo, .env mein daalo.
# Model: "glm-4.5" recommended (current flagship). Agar future mein
#        "glm-5.2" ya koi aur model available ho, bas yahan ya .env
#        mein ZAI_MODEL set kar do.
ZAI_API_KEY = _env_or("ZAI_API_KEY", "")
ZAI_API_BASE = _env_or("ZAI_API_BASE", "https://api.z.ai/api/paas/v4")
ZAI_MODEL = _env_or("ZAI_MODEL", "glm-4.5")

# Backward-compat: agar koi purana setup ANTHROPIC_API_KEY use kar raha
# tha to usse gracefully ignore kiya jaata hai (warn ke saath), crash nahi.
ANTHROPIC_API_KEY = _env_or("ANTHROPIC_API_KEY", "")  # deprecated, ignore

# -----------------------------------------------------------
# LOCAL CACHE & RESUME SETTINGS (v2.2)
# -----------------------------------------------------------
USE_CACHE = True
CACHE_DIR = "data/cache"
CACHE_MAX_AGE_HOURS = 20

RESUME_ENABLED = True
RESUME_STATE_FILE = "data/resume_state.json"

RANDOM_DELAY_MIN_SEC = 4.0
RANDOM_DELAY_MAX_SEC = 9.0

# -----------------------------------------------------------
# CHART PATTERN & BONUS SETTINGS
# -----------------------------------------------------------
SWING_WINDOW = 5
PATTERN_TOLERANCE_PCT = 2.5
FLAG_LOOKBACK = 10
FLAG_POLE_MIN_MOVE_PCT = 8.0

BENCHMARK_SYMBOL = "^NSEI"
RELATIVE_STRENGTH_LOOKBACK = 60

PATTERN_BONUS_POINTS = 5
RELATIVE_STRENGTH_BONUS_POINTS = 5

# -----------------------------------------------------------
# ALERTS & SCHEDULER SETTINGS
# -----------------------------------------------------------
WHATSAPP_ENABLED = False
TWILIO_ACCOUNT_SID = _env_or("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = _env_or("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = _env_or("TWILIO_WHATSAPP_FROM", "")
WHATSAPP_TO = _env_or("WHATSAPP_TO", "")

EMAIL_ENABLED = False
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = _env_or("SMTP_USER", "")
SMTP_PASSWORD = _env_or("SMTP_PASSWORD", "")
EMAIL_FROM = _env_or("EMAIL_FROM", "")
EMAIL_TO = _env_or("EMAIL_TO", "")
EMAIL_ATTACH_REPORTS = True

SCHEDULE_ENABLED_DEFAULT = False
SCHEDULE_TIME = "09:20"

NIFTY_MORNING_TIME = "08:00"
MORNING_SHOW_GIFT_NIFTY = True
MORNING_SHOW_BULK_DEALS = True
MORNING_SHOW_PREOPEN_MOVERS = True
MORNING_SHOW_GENERAL_NEWS = True
MORNING_SHOW_WATCHLIST_NEWS = True
MORNING_GENERAL_NEWS_STOCKS = ["^NSEI", "RELIANCE.NS", "TCS.NS"]
MORNING_WATCHLIST_NEWS_DAYS = 5
MORNING_NEWS_PER_STOCK = 1

TRANSLATE_NEWS_TO_HINDI = True
MONITOR_START_TIME = "09:15"
MONITOR_END_TIME = "15:30"
MONITOR_INTERVAL_MIN = 15
DAILY_SUMMARY_TIME = "16:00"
CLOSE_BESTBUYS_TIME = "15:00"
CLOSE_BESTBUYS_COUNT = 4
EVENING_SUMMARY_TIME = "20:00"

PDF_HINDI_FONT_PATH = ""

WEEKLY_EMA_FAST = 10
WEEKLY_EMA_SLOW = 30

MTF_1H_ENABLED = True
MTF_1H_TOP_N = 25
MTF_1H_PERIOD = "3mo"
MTF_1H_INTERVAL = "60m"
MTF_1H_EMA = 20

WEEKLY_TREND_BONUS_POINTS = 5
MTF_1H_CONFIRM_BONUS_POINTS = 5
ENTRY_ZONE_ATR_FRACTION = 0.3

# -----------------------------------------------------------
# INTRADAY SCANNER (naya, 9:30 AM) - ORB/VWAP/Rel-Volume based
# -----------------------------------------------------------
# NOTE: Ye SWING scanner (scanner.py) se BILKUL ALAG hai - swing
# scanner ko bilkul touch nahi kiya gaya. Intraday scanner ORB
# (Opening Range Breakout), asli intraday VWAP crossover, aur
# Relative Volume >2x check karta hai - inke liye INTRADAY candles
# chahiye (5-min bars), jo sirf yfinance se milte hain (koi free
# NSE/Stooq source intraday history nahi deta - jaisa mtf.py ke
# 1H confirmation mein bhi hai). Isliye ye scanner seedha yfinance
# use karta hai, primary/secondary NSE/Stooq chain nahi.
INTRADAY_SCAN_TIME = "09:30"
INTRADAY_SCAN_ENABLED = True
INTRADAY_UNIVERSE_TOP_N = 100          # poore Nifty500 par intraday scan practical nahi (rate-limit+time) - liquid top-N
INTRADAY_INTERVAL = "5m"               # 5-minute candles (yfinance)
INTRADAY_ORB_MINUTES = 15              # Opening Range = pehle 15 min (9:15-9:30)
INTRADAY_RVOL_THRESHOLD = 2.0          # document: "Volume > 2x of 20-day average"
INTRADAY_TOP_N_RESULTS = 5             # Telegram par top kitne dikhaane hain

# -----------------------------------------------------------
# BTST SCANNER (naya, 3:05 PM) - last-hour price action based
# -----------------------------------------------------------
# NOTE: Ye run_close_bestbuys_pipeline() (3 PM, purana swing-score
# wala) se ALAG hai - wo unchanged hai. Ye naya scanner specifically
# "pichle 1 ghante ka price action + volume accumulation + Day's
# High ke paas closing" check karta hai (document ki exact requirement).
BTST_SCAN_TIME = "15:10"  # V9.3: 3:05 -> 3:10 (user request)
BTST_SCAN_ENABLED = True
BTST_LOOKBACK_MINUTES = 60             # document: "last 1 hour (2:00 PM - 3:00 PM)"
BTST_DAY_HIGH_PROXIMITY_PCT = 1.5      # Close, Day's High ke kitne % andar hona chahiye
BTST_TOP_N_RESULTS = 2                 # document: "1-2 high-conviction BTST stock"

# -----------------------------------------------------------
# SECTORAL INDICES REPORT (naya, 4 PM report mein add hota hai)
# -----------------------------------------------------------
SECTORAL_INDICES_ENABLED = True
SECTORAL_INDICES = {
    "NIFTY BANK": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "NIFTY AUTO": "^CNXAUTO",
    "NIFTY PHARMA": "^CNXPHARMA",
    "NIFTY FMCG": "^CNXFMCG",
}

# -----------------------------------------------------------
# WEEKEND SWING REPORT (Saturday aur Sunday dono - document requirement)
# -----------------------------------------------------------
# NOTE: generate_weekly_report() (tracker.py) pehle se hai, sirf
# Friday evening summary ke andar chalta tha. Document mein "Saturday
# & Sunday" dono din maanga gaya hai - isliye ab dono din chalega.
WEEKEND_REPORT_DAYS = [5, 6]  # Python weekday(): 5=Saturday, 6=Sunday

# -----------------------------------------------------------
# SWING TRADING CHART DIGEST (naya, 10 AM) - document requirement:
# "Swing trading ke liye stocks ka time 10 AM"
# -----------------------------------------------------------
# NOTE: Ye run_scan_pipeline() (9:20 AM, jo asli Swing-scan karta hai
# aur database mein recommendations save karta hai) ko DOBARA scan
# karne ke liye call NAHI karta - sirf 9:20 AM se already-database
# mein aayi fresh recommendations ke charts 10 AM par (dedicated
# "Swing Trading" branding ke saath) bhejta hai. Isse duplicate
# scanning/API-load nahi hota.
# V9.0 UPDATE: Swing breakout digest 10:00 AM se 11:00 AM kar diya
# (user request: "11 AM me aaj ka top swing stocks jo breakout stocks ho")
SWING_CHART_DIGEST_TIME = "11:00"
SWING_CHART_DIGEST_ENABLED = True
SWING_CHART_DIGEST_TOP_N = 5

# -----------------------------------------------------------
# V9.0: INTRADAY LIVE ALERTS (har 5 min, 9:30-15:00)
# -----------------------------------------------------------
# Intraday picks (9:30 AM wale) ka live price check - entry zone hit,
# target hit, ya SL hit hone par sharp Telegram alert bhejta hai.
# Pehle --monitor sirf swing picks track karta tha - ab intraday bhi.
INTRADAY_LIVE_ALERT_ENABLED = True
INTRADAY_LIVE_ALERT_INTERVAL = 5   # minutes (9:30-15:00 ke beech har 5 min)
INTRADAY_LIVE_ALERT_START = "09:30"
INTRADAY_LIVE_ALERT_END = "15:00"

# -----------------------------------------------------------
# V9.0: MORNING BRIEFING - kal ke closed trades ka P&L
# -----------------------------------------------------------
# 8 AM morning briefing mein GIFT Nifty + news ke saath, kal ke
# closed trades (target hit / SL hit) ka P&L list bhi dikhana.
MORNING_SHOW_YESTERDAY_PNL = True

# -----------------------------------------------------------
# V9.0: EVENING SUMMARY - GLM AI ka din ka analysis
# -----------------------------------------------------------
# 8 PM evening report mein GLM se poore din ka Hinglish summary
# (kya achha hua, kya bura, kal kya expect) likhwana.
EVENING_GLM_SUMMARY_ENABLED = True

# -----------------------------------------------------------
# V9.0: AI BRAIN (conversational chatbot)
# -----------------------------------------------------------
# Bot ko AI conversational mode - GLM se natural Hinglish replies.
# Rule-based NLU pehle try hota hai; UNKNOWN hone par GLM use hota hai.
AI_BRAIN_ENABLED = True
AI_BRAIN_MAX_TOKENS = 600  # reply length control (cost)

# -----------------------------------------------------------
# V8.3.0: EXPANDED UNIVERSE (NSE + BSE full list)
# -----------------------------------------------------------
# Pehle sirf NIFTY 500 use hota tha. Ab NSE + BSE dono exchanges
# ke sabhi listed equities scan hote hain (liquidity-filtered).
#
# UNIVERSE_MODE:
#   "FAST" (default) — NIFTY 500 + NSE top-liquid + BSE Group A/B
#                      ~1300 symbols. Scan time ~10-15 min.
#   "FULL"            — poore NSE + BSE (7000+ symbols).
#                      Scan time ~40-60 min. Comprehensive lekin slow.
UNIVERSE_MODE = "FULL"
UNIVERSE_MAX_SYMBOLS = 7000         # V9.1.3: FULL mode — saare NSE stocks (CSV se)

# Stage-1 quick filter (scanner.py mein): in criteria se neeche wale
# stocks full indicator scan se pehle hi skip ho jaate hain. Isse
# 7000+ universe mein bhi sirf ~500-800 stocks par full scan hota hai.
UNIVERSE_MIN_PRICE = 20.0          # ₹20 se neeche penny stocks skip
UNIVERSE_MIN_AVG_VOLUME_LAKH = 5   # daily avg volume < 5 lakh skip (illiquid)

# -----------------------------------------------------------
# V8.3.0: GLM AI SCREENER (top candidates ko GLM se rank karwana)
# -----------------------------------------------------------
# Scanner poore universe ko technical score deta hai (fast, free).
# Top ~30 candidates GLM API ko bheje jaate hain, jo inme se best
# 8 picks select karta hai Hinglish rationale + confidence score
# ke saath. See glm_screener.py.
#
# GLM_SCREENER_ENABLED = False hone par traditional rule-based
# top-N (Score se sorted) use hota hai (V8.2.0 jaisa behavior).
GLM_SCREENER_ENABLED = True
GLM_SCREEN_TOP_N = 8              # Telegram/PDF par kitne GLM picks dikhane

# -----------------------------------------------------------
# V9.2: UPSTOX API SETTINGS (OAuth Telegram Login Flow)
# -----------------------------------------------------------
# Upstox se 7000+ stocks ka data 2-3 min mein (vs Yahoo 40 min).
# Login flow: Telegram alert → click link → Upstox login → auto token.
#
# Environment variables (Render → Environment tab):
#   UPSTOX_API_KEY       = your-api-key
#   UPSTOX_API_SECRET    = your-api-secret
#   UPSTOX_REDIRECT_URI  = https://your-app.onrender.com/upstox/callback
#
# Token auto-expires har 24 hours. Scheduler 9:05 AM check karta hai,
# agar token missing hai to Telegram pe login link bhejta hai.
UPSTOX_ENABLED = True  # Set False to disable Upstox (use Yahoo fallback)

# -----------------------------------------------------------
# V9.3: GEMINI AI FALLBACK (GLM fail hone par auto-switch)
# -----------------------------------------------------------
# Jab GLM API 429/error de, to automatically Gemini AI pe
# switch ho jaata hai. Gemini 1.5 Flash (fast + cheap).
#
# Environment variable (Render → Environment):
#   GEMINI_API_KEY = your-gemini-api-key
#
# Flow: GLM → fail → Gemini → fail → rule-based
GEMINI_API_KEY = _env_or("GEMINI_API_KEY", "")
GEMINI_MODEL = _env_or("GEMINI_MODEL", "gemini-2.5-flash")

# V9.3: TRAILING STOPLOSS + 9 EMA EXIT settings
TRAILING_SL_ATR_MULTIPLIER = 2.0    # SL = close - (ATR × 2.0)
EXIT_ON_9EMA_WEEKLY = True          # Exit if weekly close < 9 EMA
EXIT_ON_9EMA_MONTHLY = True         # Exit if monthly close < 9 EMA
