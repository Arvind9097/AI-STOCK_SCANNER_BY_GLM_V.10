# AI Stock Scanner — V8.1.2 Changelog
### (Base: aapka uploaded V8.1 zip — 41 files mein se 9 modify hui, 4 nayi add hui, baaki 32 bilkul untouched)

---

## ✅ UPDATE 10 — Log Analysis: 500/500 Scan Success + Chart-Upload Timeout Fix

### Log Verified: Poora 500-Stock Scan SUCCESSFUL
Production logs se confirm hua: `Download finished: 500 success, 0 failed` -
pichhle sabhi performance-fixes (session-caching, jugaad-data-period-cap,
nse-package addition) combined-effect se poora scan pehli baar bina kisi
failure ke complete hua.

### Fix: Chart-Upload Timeout
Log mein dekha gaya: `Telegram Chart Upload Error: Connection aborted...
write operation timed out`. Root-cause: `send_telegram_chart()` text-message
jaisa hi 20-second generic-timeout use kar raha tha, jabki file-upload
(image-read + network-upload) ko zyada time chahiye. `send_telegram_pdf()`
mein pehle se hi alag 60-sec timeout tha lekin `send_telegram_chart()` mein
nahi - aur dono mein koi RETRY nahi thi (text-messages ko already thi).

**Fix** (`telegram_alerts.py`):
- Naya shared `_post_file_with_retry()` helper - dono chart aur PDF upload
  isi ko use karte hain (consistency)
- `_FILE_UPLOAD_TIMEOUT = 60` (PDF ka pehle se proven 60-sec value use
  kiya, sirf chart ke liye alag-chhota-number nahi banaya)
- Ab chart-upload ko bhi 1-retry milta hai (jaisa text-messages ko hai) -
  transient-network-hiccup se permanent-message-loss nahi hoga
- File HAR ATTEMPT PAR dobara khुलती hai (ek-baar-khula-handle dobara
  POST mein safely reuse nahi ho sakta)

### Investigated, Action Nahi Li (Honest Notes)
- **"Start" symbol-lookup mein jaate dikha log mein**: Code-review confirm
  karta hai fix (`cmd.lstrip("/")`) sahi hai. Log-window mein poori
  sequence nahi thi (koi doosra message beech mein aaya hoga jiski
  "Message Processed" line window se bahar thi) - koi code-change nahi
  ki, kyunki root-cause genuinely code mein nahi mila.
- **Market-close ke baad teeno primary-source ek-saath fail hue**: Root-
  cause samjha (watchlist-summary bahut saare OPEN-positions ko sequentially
  try karti hai, agar underlying-network-condition kharab ho to teeno NSE-
  based-source saath-saath consecutive-fail-limit hit kar sakte hain) -
  ye already-documented-limitation (market_data_fetcher.py docstring) ka
  real-world-manifestation hai, koi naya bug nahi. System ne sahi respond
  kiya (auto-disable + yfinance-secondary-fallback, dono log mein confirm).

### Verified
- Retry-logic: mocked-timeout-then-success test pass (2 calls: 1 fail,
  1 retry-success)
- Timeout-value confirm: 60s use ho raha hai (pehle 20s tha)
- File-reopen-per-attempt: content correctly-readable har call par
- Missing-file-handling: gracefully None (crash nahi)

---

## 🆕 UPDATE 9 — Naya Data Source: 'nse' Package (BennyThadikaran/NseIndiaApi)

User ne Gemini-AI se ek suggested-sources-list share ki thi. Har suggestion
ko fact-check kiya - zyadatar already-implemented ya genuinely-useful nahi
nikle, lekin EK genuinely valuable naya source mila.

### Fact-Check Summary
| Suggestion | Verdict | Reason |
|---|---|---|
| Yahoo Finance raw JSON | ❌ Naya nahi | Yahi endpoint jo yfinance internally use karta hai |
| Stooq | ✅ Already tha | market_data_fetcher.py mein pehle se |
| NSE quote-equity | ⚠️ Skip | Community-forums confirm karte hain - cloud-servers se frequently 403-block hota hai |
| `tvdatafeed` | ❌ Recommend nahi | Official docs khud kehte hain "nologin method data limited"; WebSocket-based (Render pe kaam karne ki guarantee nahi); TradingView ToS-grey-area |
| `nsepython` | ❌ Skip | Docs khud kehte hain "local version does NOT work with AWS/Google Cloud/web servers" |
| **`nse` package** | ✅ **Add kiya** | Neeche detail |
| Vercel community-API | ❌ Recommend nahi | Khud confirm karta hai "via Yahoo Finance" - yfinance ka hi slower indirect-wrapper |
| Google Sheets hack | ❌ Practical nahi | Manual-setup chahiye per-symbol, automate nahi ho sakta |

### Naya Source: 'nse' Package
- **Alag endpoint**: `www.nseindia.com/api/NextApi/apiClient/GetQuoteApi`
  (humare existing `charting.nseindia.com` se ALAG subdomain)
- **Server-deployment ke liye explicitly designed**: library docs confirm
  karte hain "works in server environments like AWS" (v1.2.0+) - `nsepython`
  se seedha ulat jo isi scenario ko explicitly unsupported bataता hai
- **Efficient chunking**: 100-din chunks (1 saal sirf 4 requests mein) -
  jugaad-data ke 30-din calendar-month-chunks (13 requests) se behtar
- **Genuine OHLC**: SL/Target-detection ke liye bhi safe (Stooq/jugaad-data
  ke saath is function mein use hota hai)
- Priority chain mein NSE Chart ke turant baad, Stooq se pehle rakha gaya

### Important Implementation Details
- **Module-level singleton instance** (`_get_nse_package_instance()`) -
  session-caching-fix se seekha gaya lesson dobara apply kiya: har call
  par naya `NSE()` object NAHI banta (jo apna khud cookie-handshake karta),
  ek hi instance poori process ke lifetime mein reuse hoti hai
- **Defensive multi-schema parsing**: Exact JSON column-names (documentation
  mein sirf `mTIMESTAMP` confirm hua tha, poori list nahi) 100% verify nahi
  ho paye is environment se. Isliye code multiple POSSIBLE key-names try
  karta hai (`mOPEN`/`CH_OPENING_PRICE`/`OPEN` waghera) - agar koi bhi na
  mile, gracefully `None` return hota hai (crash nahi, chain continue
  hoti hai Stooq/jugaad-data/yfinance tak). Verified via mocked-tests dono
  possible-schema-formats ke saath.
- **CRITICAL DEPENDENCY FIX**: `server=True` mode ke liye `httpx[http2]`
  zaroori hai library docs ke mutabik - agar sirf `pip install nse` karte
  (bina extra), server-mode silently kaam nahi karta. `requirements.txt`
  mein `nse[server]` (sahi extra-syntax) use kiya, verified actual-install
  se ki `httpx` sahi aa raha hai.

### Verified
- Defensive parsing: 2/2 possible-schema-formats correctly parsed, 1/1
  unknown-schema gracefully returns None (no crash)
- Singleton-pattern: 3 consecutive calls -> sirf 1 `NSE()` construction
  (session-per-call-bug dobara nahi hua)
- Full failover-chain: NSE Chart fail -> nse-package success verified
- `nse[server]` install verified - httpx[http2] correctly present

---

## ⚡ UPDATE 8 — Do Genuine Performance Bugs Fix (Slow Scan Issue)

Logs mein dekha gaya: 500-stock scan mein ~35 second/10-stock-batch lag raha
tha (poore scan ke liye ~28 minute estimate). Do alag root-cause mile:

### Bug 1: NSE Session Har Symbol Ke Liye Naya Ban Raha Tha
`nse_session.py` ka `get_nse_session()` HAR call par ek NAYA `requests.Session()`
banata tha aur NSE ki homepage ko dobara visit karta tha (poora cookie-handshake)
- 500-stock scan mein matlab 500 baar homepage-fetch, jabki NSE cookies
kai minute tak valid rehte hain.

**Fix**: Session ab module-level CACHE hoti hai (`SESSION_TTL_SECONDS = 240`,
4 minute). Naya session sirf tab banta hai jab: pehli baar ho, TTL expire ho
chuka ho, ya explicitly `force_new=True`/`invalidate_nse_session()` diya
jaaye (agar koi request cookie-expiry se fail ho). Verified: 500-stock batch
ke liye ab effectively SIRF EK homepage-fetch hota hai, 500 nahi.

### Bug 2: jugaad-data 1-Saal Ke Data Ke Liye 13 Alag NSE-Calls Kar Raha Tha
`jugaad-data` library ka internal `break_dates()` date-range ko CALENDAR-MONTH
boundaries par todता hai - `PERIOD="1y"` (370 din) maangne par ek symbol ke
liye 13 alag NSE-API-calls hoti thi (NSE Chart API/Stooq single-request mein
poora saal de dete hain, jugaad-data alag design hai - NSE ke month-wise
bhavcopy-reports use karta hai).

**Fix**: `JUGAAD_DATA_MAX_PERIOD_DAYS = 90` cap add kiya - jugaad-data
(jo sirf FALLBACK hai, NSE Chart+Stooq dono fail hone ke baad hi trigger
hota hai) ab max 90 din ka data maangta hai (4 chunks), poora saal nahi
(13 chunks). ~3.2x fewer network round-trips jab ye source trigger hota hai.

### Honest Expectation (Speed Ke Baare Mein)
Dono fixes overhead kaafi kam karte hain, lekin **kuch NSE-server-response-
time hamesha lagega per-request** - ye NSE ke apne servers ki processing-speed
hai, jo humare control se bahar hai. 500 stocks ka scan "turant" (instant)
kabhi nahi hoga - koi bhi free/unofficial source (chahe NSE Chart, Stooq,
jugaad-data, ya yfinance) is fundamental floor se bach nahi sakta. In fixes
ke baad scan time significantly kam hona chahiye (exact number agle deploy
ke logs se hi confirm hoga), lekin "instant" ek realistic target nahi hai
bina paid/institutional-grade real-time data-feed ke.

---

## 🐛 UPDATE 7 — Bot Listener Bug Fix + "/rerun" Command (Missed Scheduled-Slots)

### Bug (logs se pakda gaya)
Telegram par "Start" aur "--scan" bheje gaye the - dono galti se STOCK SYMBOL
samajh liye gaye (`START.NS`, `--SCAN.NS`), aur poori 4-source data-fetch chain
(NSE Chart, Stooq, jugaad-data, yfinance) trigger ho gayi in symbols ke liye
jo genuinely exist hi nahi karte.

### Fix 1: Symbol Format Validation (`stock_lookup.py`)
- Naya `_VALID_SYMBOL_PATTERN` regex - NSE cash-equity symbols sirf LETTERS,
  NUMBERS, aur "&" (jaise M&M, L&T) use karte hain (verified via web search -
  hyphen sirf futures-contract naming mein hota hai, jo is bot ka scope nahi)
- `_normalize_symbol()` ab galat-format text (jaise "--scan") ko turant reject
  kar deta hai, data-fetch-chain tak pahunchne se pehle hi

### Fix 2: Command Recognition (`bot_listener.py`)
- Pehle sirf "/start" (slash ke saath) recognize hota tha - "Start" (bina
  slash) match nahi hota tha aur natural-language-handler tak pahunch jaata
  tha. Ab leading "/" normalize kiya jaata hai, dono variants sahi route
  hote hain
- Naya explicit catch: agar message "--" ya "/" se shuru ho aur koi bhi known
  command na ho, ek clear "command samajh nahi aaya" reply milta hai (chahe
  koi bhi CLI-syntax jaisa text ho) - ab kabhi symbol-lookup tak nahi
  pahunchega

### Naya: "/rerun <name>" Command
User-requirement: agar koi scheduled slot (jaise 10 AM Swing digest) Render
deploy/restart ki wajah se beech mein cut ho jaaye, ab Telegram se hi turant
dobara chala sakte ho (Render Shell ki zaroorat nahi):

```
/rerun swing       -> 10 AM Swing Chart Digest
/rerun intraday    -> Intraday Scan
/rerun scan        -> Swing Scan
/rerun morning      -> Morning Briefing
/rerun closebuys    -> Close-Buys (3 PM)
/rerun btst         -> BTST Scan
/rerun report       -> Daily Performance Report
/rerun evening      -> Evening Summary
```

- Background thread mein chalta hai (Telegram-listener block nahi hota lambe
  scans ke dauraan)
- `dispatch_state.py` ka duplicate-send-lock hamesha disabled hai (V8.1 se),
  isliye ye safely kisi bhi waqt re-trigger ho sakta hai
- `/help` mein bhi document kiya gaya

### Verified
- 9/9 message-routing test cases pass (Start->Menu, --scan->clear-error,
  TCS->stock-lookup sab correctly route hue)
- 4/4 /rerun command tests pass (no-arg, invalid-name, valid-name-background-
  thread, help-text-integration)
- Koi circular-import issue nahi (main.py <-> bot_listener.py lazy-import
  verified)

---

## 🆕 UPDATE 6 — Breaking News Overhaul + Naya Data Source

### 1. 📰 Breaking News: Duplicate Fix + Brief News + Hidden Link

**Bug (user complaint)**: Same news 4 alag publishers (Moneycontrol/ET/BS/
LiveMint) alag wording mein likhते the - purana exact-hash dedup inhe alag
samajh kar 4 baar bhej deta tha ("irritate karta hai").

**Fix - Hybrid Cross-Source Dedup** (`breaking_news.py`):
- Naya "topic-level" dedup layer - company-naam (Proper Nouns jaise
  "Reliance", "TCS", "Infosys") ko strong-signal maankar, agar wahi
  company DEDUP_WINDOW_MINUTES (3 ghante) ke andar dubara mention ho,
  duplicate maan liya jaata hai - chahe wording bilkul alag ho
- Test kiya: 8/8 realistic same-topic-alag-wording pairs correctly
  duplicate detect hue, genuinely-different topics correctly NAHI
  flag hue
- Known trade-off (documented in code): rare case mein ek hi company
  ki 2 GENUINELY alag khabrein 3-ghante-window mein aayein to doosri
  skip ho sakti hai - user ka main complaint (same-news-repeat) solve
  karne ke liye acceptable trade-off

**Brief News (~100 words)**:
- `BRIEF_SUMMARY_MAX_CHARS`: 120 → 700 characters (~100 words target)
- Agar RSS summary khud bahut chhota ho (<25 words), title context ke
  roop mein joda jaata hai - agar already sufficient ho, title dobara
  nahi jodi jaati (no awkward repetition)
- HONEST LIMITATION: Kabhi fake/padded content generate NAHI karta -
  agar RSS source genuinely kam text de, utna hi bheja jaata hai
  (trading-alerts ke liye fabricated content khatarnaak hoga)

**Hidden Link**:
- Raw URL ab HTML `<a href>` tag ke peeche chhupi hai - Telegram mein
  sirf "🔗 Click Here for More Details" (clickable) dikhta hai, raw
  link text nahi

### 2. 📡 Naya Data Source: jugaad-data (yfinance rate-limit reduce karne ke liye)

User complaint: yfinance baar-baar rate-limit ho jaata hai.

- **Naya PRIMARY source**: `jugaad-data` (PyPI library) - NSE ka hi
  historical data, koi authentication nahi chahiye, genuine OHLC
  (SL/Target check ke liye bhi safe)
- Chain ab: NSE Chart API → Stooq → **jugaad-data (naya)** → yfinance
  (secondary/last-resort)
- **HONEST NOTE**: Angel One/Alice Blue jaise broker APIs consider
  kiye the, lekin unhe khud ka trading account + PIN + TOTP-login
  chahiye - user ke paas abhi koi broker account nahi hai, isliye
  wo option is waqt possible nahi. jugaad-data completely free/no-
  account hai.
- **IMPORTANT CAVEAT**: jugaad-data bhi NSE ke servers ko hi hit karta
  hai (jaise NSE Chart/Stooq) - agar hosting-provider ka IP NSE se
  genuinely block ho (jaisa pichhle logs mein dikha), to teeno primary
  EK SAATH fail ho sakte hain (alag library hone se network-level-
  block nahi hatta). yfinance (Yahoo - poori tarah alag infrastructure)
  isiliye hamesha secondary maujood rehta hai.
- `requirements.txt`: `jugaad-data` add kiya

### 3. ⚙️ yfinance Rate-Limit Handling Improve
- `CHUNK_SIZE`: 20 → 10 (chhota batch, worst-case mein - jab teeno
  primary ek saath down hon - yfinance par kam load, rate-limit lagne
  se pehle zyada symbols process ho paate hain)
- `SLEEP_BETWEEN_CHUNKS_SEC`: 3 → 5 (thoda zyada gap chunks ke beech)

---

## 🐛 UPDATE 4 — Bot Listener Fix (message-reply nahi aa raha tha)

### Bug
`python main.py --schedule` (jo Render par `startCommand` hai) sirf time-based
scheduled alerts chalata hai (Breaking News, Morning Briefing, Scan, Monitor,
waghera). User ke Telegram messages sunne wala process (`bot_listener.py`, jo
"Reliance ka analysis bhejo" jaise natural-language queries ka reply deta hai)
`--schedule` mode mein **kabhi start hi nahi hota tha** - isliye scheduled
alerts sahi aate the, lekin user ke messages ka koi reply nahi aata tha.

### Fix
- **`bot_listener.py`**: `start_bot_engine()` ko do parts mein split kiya:
  - `run_listener_loop()` (naya) - sirf message-polling loop, koi
    health-server/breaking-news setup nahi karta
  - `start_bot_engine()` (existing, standalone `python bot_listener.py` ke
    liye) - pehle jaisa hi setup karta hai, phir `run_listener_loop()` call
    karta hai
- **`main.py`**: `--schedule` branch mein ab `run_listener_loop()` ko ek
  background daemon-thread mein start karta hai (health-server aur
  breaking-news poller already start ho chuke hain us se pehle - `run_listener_loop()`
  use karne se duplicate-setup/Flask-port-conflict-crash nahi hota).

### Verified
Health-server aur bot-listener-thread ko ek saath actually start karke test
kiya - koi port-conflict ya crash nahi, dono cleanly coexist karte hain.

---

## 🆕 UPDATE 3 — Charts (sabhi scan-types), Swing 10 AM slot, News Filter/Format

### 1. 📊 Har Scan-Type Ka Chart (Intraday, Swing, BTST)
- **`charts.py`**: naya `generate_simple_chart()` function - Intraday/BTST scanners
  ke result-format (jo Swing scanner se alag hai - koi Entry_Low/Support/Resistance
  nahi) ke liye. **SAME TradingView dark-theme** jo Swing scanner already use karta
  hai (koi duplicate/naya styling nahi banaya) - candlesticks, EMA20, Entry/SL/Target
  lines, volume bars.
- `intraday_scanner.py` aur `btst_scanner.py` ke result-dict mein ab `df` (DataFrame)
  bhi included hai, taaki chart banane ke liye dobara data fetch na karna pade
  (rate-limit-safe).
- `main.py`: `run_intraday_scan_pipeline()` aur `run_btst_scan_pipeline()` dono ab
  message ke turant baad har candidate ka chart bhi bhejte hain.

### 2. 📈 Swing Trading - 10 AM Chart Digest (naya slot)
- User requirement: "swing trading ka liye stocks ka time 10 AM"
- **NAYA function**: `run_swing_chart_digest_pipeline()` (main.py) - 9:20 AM ke
  scan (`run_scan_pipeline`) ko DOBARA call NAHI karta (koi duplicate scanning/API
  load nahi) - sirf aaj ke fresh recommendations (jo already database mein hain,
  `tracker.get_todays_swing_recommendations()` se) ke TradingView-theme charts
  "Swing Trading" branding ke saath 10:00 AM par bhejta hai.
- `config.py`: `SWING_CHART_DIGEST_TIME="10:00"` naya setting.
- `python main.py --swing-digest` se manually bhi chala sakte ho.

### 3. 📰 Breaking News - Strict Indian-Equity Filter + Stylish Format
User requirement: "breaking stocks sirf Indian market se sambandhit hona chahiye.
Share, equity, listed company ke baare mein. Heading stylish karo. Brief news
aur shortened link hona chahiye."

- **`breaking_news.py`** mein naya `_is_indian_equity_news()` filter - do-tarafa
  keyword matching (INCLUDE: nifty/sensex/shares/results/ipo/dividend/waghera,
  EXCLUDE: election/cyclone/bollywood/cricket/waghera). Sirf equity-specific
  headlines Telegram tak pahunchti hain, baaki discard ho jaati hain
  (`get_latest_breaking_news()` mein automatically apply hota hai).
- **Stylish heading**: har news ab ek category-tag ke saath aati hai (📊 RESULTS,
  🆕 IPO, 🚀 RALLY, 📉 DECLINE, 🎯 RATING, 🤝 M&A, 💰 CORP. ACTION, 🏛️ REGULATORY,
  📈 INDEX, ya default 💹 MARKET) - content ke hisaab se auto-detect hota hai.
- **Brief news**: feedparser ka `summary` field use hota hai (agar available ho),
  HTML tags clean karke, max 120 characters tak (word-boundary par truncate,
  beech shabd mein nahi todta).
- **Shortened link**: is.gd (free, no signup/API-key) se link automatically
  shorten hoti hai. Fail hone par original link hi use hota hai (kabhi crash
  nahi, link kabhi missing nahi).
- Poll interval (`POLL_INTERVAL_SEC=180`, 3 min) already tha - "time to time
  update" requirement pehle se hi cover ho raha tha, koi change nahi kiya.

### Verified (functional tests, mocked data)
- ORB breakout, VWAP crossover, RVOL detection - bullish scenarios correctly identify
- BTST last-hour price-action, volume accumulation, Day's-High proximity - correctly identify
- Sectoral indices % change calculation aur sorting - correct
- Weekend report Saturday+Sunday dono din (Python weekday() 5,6) - correct
- Indian-equity filter: 5 equity headlines PASS, 5 non-equity headlines EXCLUDE - correct
- Category-tag detection: 10/10 test cases correct
- Brief-summary truncation + HTML-stripping - correct
- End-to-end filter+dispatch: non-equity news NEVER reaches Telegram (assertion-tested)

---

## 🆕 UPDATE 2 — Intraday/BTST/Sectoral/Weekend Additions (naya specification document ke anusaar)

Ye additions ek doosre naye specification document ke response mein aayi hain (Intraday
Selection Rules, BTST Selection Rules, strict time-bound delivery schedule).
**GIFT Nifty aur Bulk/Block Deals sections UNCHANGED rakhe gaye hain** (explicit user
decision - "GIFT Nifty zaroori nahi lekin already free hai to rehne do", "Bulk/Block
Deals wali document line ignore karo").

### Naya (2 files)
- **`intraday_scanner.py`** — ORB (Opening Range Breakout), asli intraday VWAP crossover,
  Relative Volume >2x check. **Swing scanner (scanner.py) se BILKUL ALAG**, usko touch
  nahi kiya. Intraday candles (5-min) sirf yfinance se milte hain (koi free NSE/Stooq
  source intraday history nahi deta) - isliye ye module seedha yfinance use karta hai.
  `python main.py --intraday` se manually bhi chala sakte ho.
- **`btst_scanner.py`** — Last-1-hour price action, volume accumulation, Day's-High
  proximity check. **run_close_bestbuys_pipeline() (purana 3 PM swing-score wala) se
  BILKUL ALAG**, usko touch nahi kiya. `python main.py --btst` se manually chala sakte ho.

### Modified (5 files)
- **`config.py`**: naye time-slots (`INTRADAY_SCAN_TIME=09:30`, `BTST_SCAN_TIME=15:05`),
  naye thresholds (RVOL, ORB minutes, Day's-High proximity %), `SECTORAL_INDICES` dict
  (5 indices, Yahoo Finance se verified: ^NSEBANK, ^CNXIT, ^CNXAUTO, ^CNXPHARMA, ^CNXFMCG),
  `WEEKEND_REPORT_DAYS=[5,6]` (Saturday+Sunday).
- **`main.py`**: `run_intraday_scan_pipeline()` aur `run_btst_scan_pipeline()` naye
  functions (document ka exact ⚠️/📝 emoji-format), `--intraday`/`--btst` CLI flags,
  `--schedule` mein dono naye slots wire kiye.
- **`scheduler.py`**: `run_full_day_scheduler()` mein `intraday_func`/`btst_func` optional
  parameters add kiye (backward-compatible - purana signature bhi kaam karega).
- **`tracker.py`**: `_sectoral_indices_section()` naya function, 4 PM report
  (`generate_daily_performance_report()`) ke top par add hota hai. Weekend report fix:
  pehle sirf Friday (`weekday()==4`) chalta tha, ab Saturday+Sunday dono (`WEEKEND_REPORT_DAYS`).
- **`nse_market_data.py`**: `get_sectoral_indices_performance()` naya function - existing
  `market_data_fetcher.fetch_daily_ohlcv()` reuse karta hai (index symbols automatically
  secondary/yfinance route hote hain, jaisa market_data_fetcher.py mein already tha).

### Design Constraints (bugs nahi, jaan-boojh kar)
- Intraday/BTST scanners **poore Nifty500 par nahi chalte** (`INTRADAY_UNIVERSE_TOP_N=100`)
  - rate-limit aur samay dono ki wajah se practical nahi. CUSTOM_STOCKS hamesha include
  hote hain.
- US Market cues (document mein maanga gaya) **skip kiya** - explicit user decision
  ("existing NIFTY data hi kaafi hai").
- Entry/SL/Target Intraday/BTST scans mein simple %-based hain (ATR data nahi hota
  intraday-only scan mein) - Swing scanner ke ATR-based zones se alag calculation hai.

---

## ⚠️ IMPORTANT: Is version ko deploy karne se PEHLE

1. **Naya Telegram bot token banao** (agar abhi tak nahi banaya): @BotFather ko Telegram par `/revoke` bhejo, naya token lo.
2. **`.env` file banana ab COMPULSORY hai** (V8.1 mein optional tha): `.env.example` ko copy karke `.env` banao, usme `TELEGRAM_BOT_TOKEN` aur `TELEGRAM_CHAT_ID` daalo. Bina iske bot ab startup par turant ruk jaayega (pehle jaisa silent hardcoded-fallback nahi hoga).
3. Render par deploy kar rahe ho to Dashboard → Environment mein ye same 2 values set karo.

---

## Kya Naya Hai (What's New)

### 1. 🔒 SECURITY FIX — Hardcoded Bot Token Hamesha Ke Liye Hataya Gaya
- **Bug mila:** Aapka Telegram bot token 3 alag jagah (`config.py`, `telegram_alerts.py`, `bot_listener.py`) plaintext mein hardcoded tha, aur comment khud confirm karta tha ki ye token pehle leak ho chuka hai — lekin fallback ke taur par phir bhi code mein maujood tha.
- **Fix:** Teenon jagah se token PERMANENTLY hata diya gaya. Ab `TELEGRAM_BOT_TOKEN` sirf environment variable se aata hai. Agar missing ho, bot turant clear error dekar ruk jaata hai (silently purana leaked token istemal NAHI karega).
- Files: `config.py`, `telegram_alerts.py`, `bot_listener.py`, `.env.example`

### 2. 📡 Primary/Secondary Data Source Chain (NSE/Stooq → yfinance)
- **Requirement:** Free public NSE/BSE data sources ko primary banao, yfinance ko sirf tab use karo jab primary fail ho jaaye.
- **Naya module:** `market_data_fetcher.py` — priority chain:
  1. **NSE Chart API** (`charting.nseindia.com`, existing `nse_session.py` ka cookie-session reuse karta hai — free, no-auth)
  2. **Stooq.com** (free daily OHLCV CSV, genuine High/Low/Close, no-auth)
  3. **yfinance** (SECONDARY — sirf tab call hota hai jab dono upar wale fail ho jaayein)
- Har source ka health track hota hai: 3 consecutive fails → 30 min ke liye disable → phir auto-recover.
- **5 jagah wire kiya gaya** (jahan bhi yfinance direct call ho raha tha):
  - `downloader.py` — bulk Nifty 500 scan (batch mein har symbol pehle primary se, sirf jo miss ho wo yfinance batch mein)
  - `stock_lookup.py` — Telegram par single-stock on-demand query
  - `tracker.py` — SL/Target hit-detection (2 jagah) aur watchlist live price
  - `master_dashboard.py` — live dashboard CMP
  - `mtf.py` — **NAHI badla jaan-boojh kar**: 1H intraday confirmation ke liye koi free NSE/Stooq source data nahi deta (dono sirf EOD daily dete hain), isliye ye hamesha yfinance (secondary) hi use karega — ye ek design constraint hai, bug nahi.
- **Correctness safeguard:** NSE Chart API sirf Close price deta hai (Open/High/Low ko humne Close se hi synthetically fill kiya hai, shape-consistency ke liye). SL/Target hit-detection jaisi jagah jahan GENUINE High/Low chahiye, wahan ye source jaan-boojh kar SKIP kiya jaata hai — sirf Stooq (jo asli OHLC deta hai) aur yfinance try hote hain.
- Files: `market_data_fetcher.py` (naya), `downloader.py`, `stock_lookup.py`, `tracker.py`, `master_dashboard.py`, `mtf.py`

### 3. 🌐 Render 24×7 Health Server
- **Requirement:** GitHub + Render + UptimeRobot par 24×7 chalane ke liye code adjust karo.
- **Naya module:** `health_server.py` — `/ping`, `/health`, `/status` endpoints, background thread mein (scheduler ka existing blocking loop bilkul waisa hi chalta rehta hai, koi behavior change nahi).
- Backup internal self-ping thread bhi hai (agar UptimeRobot delay ho jaaye).
- `main.py --schedule` aur `bot_listener.py` dono mein wire kiya — jo bhi entry-point use karo, health server chalega.
- **Naya `render.yaml`** — Render par ek-click deploy config (`type: web`, `startCommand: python main.py --schedule`, `healthCheckPath: /ping`).
- Files: `health_server.py` (naya), `main.py`, `bot_listener.py`, `render.yaml` (naya)

### 4. 📰 Breaking News — Real-Time Hindi Market Headlines
- **Requirement:** Indian share market ki breaking news Hindi mein, turant jaise hi aaye.
- **Naya module:** `breaking_news.py` — background poller (har 3 min) jo 4 free RSS feeds check karta hai (Moneycontrol, Economic Times, Business Standard, LiveMint), sirf top 5 latest headlines per-feed dekhta hai ("top breaking news hi" requirement).
- Nayi headline milte hi turant existing `translator.py` (Hindi) se translate karke Telegram par bhej deta hai.
- **Dedup:** Ek baar bheja gaya headline dobara nahi bhejaga (link+title hash, `data/breaking_news_seen.json` mein persist hota hai).
- Har RSS source independent hai — ek feed down/blocked ho to baaki 3 phir bhi kaam karte rehte hain.
- ⚠️ **Note:** LiveMint ka exact RSS URL is environment se directly verify nahi ho paya (network restriction). Baaki 3 sources (Moneycontrol, ET, Business Standard) multiple independent sources se verify kiye gaye hain. Agar deploy ke baad logs mein "LiveMint: RSS malformed ya empty" baar-baar dikhe, `breaking_news.py` mein sirf uska URL update karna hoga.
- Files: `breaking_news.py` (naya), `main.py`, `bot_listener.py`

### 5. `requirements.txt` Update
- `feedparser` add kiya (breaking news RSS parsing ke liye)
- `flask`, `gunicorn` add kiye (health server ke liye)
- Baaki sab waisa hi (koi naya heavy dependency nahi — NSE/Stooq fetching `requests`+`pandas` se hi hua hai, jo already the)

---

## Kya WAISA hi hai (Unchanged from V8.1)

In files/features ko bilkul touch nahi kiya gaya:
- ✅ `dispatch_state.py` — `--force` lock already disabled tha (V8.1 mein hi ho chuka), verify kiya, sahi hai
- ✅ `resume_state.py` — crash recovery, bilkul waisa hi
- ✅ `nse_session.py` — cookie-session logic, naya fetcher isi ko REUSE karta hai (duplicate nahi banaya)
- ✅ `news.py` — per-stock cached news (alag feature hai breaking_news.py se, isko badalne ki zaroorat nahi thi)
- ✅ `scanner.py`, `indicators.py`, `patterns.py`, `targets.py`, `charts.py`, `database.py`, `nlu.py`, `ai_analysis.py`, `whatsapp_alert.py`, `email_alert.py`, `company_lookup.py`, `relative_strength.py`, `utils.py`, `logger.py`, `report.py` — sab bilkul waisa hi

## Known Limitation (Design Constraint, Bug Nahi)
- `mtf.py` (1H multi-timeframe confirmation) hamesha yfinance (secondary) use karega, kyunki koi bhi free NSE/Stooq source intraday/1H history nahi deta (sirf EOD daily). Agar future mein koi free intraday source milta hai, sirf `market_data_fetcher.py` ke `fetch_intraday()` function mein badlav karna hoga.

---

## Upgrade Steps
```bash
pip install -r requirements.txt      # feedparser, flask, gunicorn naye add hue hain
cp .env.example .env                 # NAYA (revoked-and-regenerated) bot token isme daalo
python main.py --morning             # test karo
python main.py --scan                # full scan test (primary/secondary chain dekhne ke liye)
python main.py --schedule            # full day automation + health server + breaking news poller
```

Render par deploy:
```bash
git push                             # apna GitHub repo update karo
# Render Dashboard -> Environment mein TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID set karo
# UptimeRobot mein https://your-app.onrender.com/ping monitor add karo (5 min interval)
```
