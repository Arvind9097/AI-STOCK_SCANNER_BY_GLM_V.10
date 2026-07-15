# AI Stock Scanner V8.3.0

## V8.3.0 — NSE+BSE Universe + GLM AI Ranking + UI Super-Overhaul

V8.2.0 ke bug-fixes ke baad, is version mein 3 naye features + 4 UI overhauls:

### 🆕 Naye Features

| Feature | Detail |
|---------|--------|
| **Expanded Universe** | Pehle sirf NIFTY 500. Ab **NSE + BSE dono** ke sabhi listed equities (~1300 FAST mode, 7000+ FULL mode). 2-stage fast scan — Stage 1 mein price/volume filter, Stage 2 mein full indicators sirf survivors par. |
| **GLM AI Ranking** | Scanner poore universe ko score deta hai (free). Top 30 candidates **GLM API** ko jaate hain — GLM best 8 picks select karta hai with Hinglish rationale + confidence score (1-10) + action (ENTER_NOW/WAIT/AVOID). |
| **Market Breadth** | Advances/declines, breadth %, signal distribution, overall sentiment (🟢 BULLISH / 🟡 NEUTRAL / 🔴 BEARISH). Dashboard aur PDF ke top par. |

### 🎨 UI Overhauls

| Area | Kya badla |
|------|-----------|
| **Charts** | Compact trade-plan box (8 overlapping labels → 1 neat box), color hierarchy, cleaner title with signal badge, subtle watermark. Bloomberg-clean look. |
| **Telegram** | Stock names **hamesha bold + English** (`<b>RELIANCE</b>`). Standardized emoji vocabulary. 27-char dividers. Compact card format. Attention-grabbing alerts. |
| **News** | **Hinglish** (Roman script) with **English stock names** preserved. GLM API se best quality; fallback Devanagari Hindi with English names. Double-translate fix. |
| **PDF** | 5-section flow: Title → Market Sentiment → GLM Top Picks → Detailed Analysis (per-stock cards with bold names, emojis, trade table, chart, Hinglish analysis+news) → Disclaimer. Excel mein GLM Picks sheet. |

### Setup for new features
1. **Expanded universe**: Default ON (FAST mode). FULL mode ke liye `config.py` mein `UNIVERSE_MODE = "FULL"`.
2. **GLM AI ranking**: `.env` mein `ZAI_API_KEY=<key>` (https://z.ai se). Default ON.
3. **Hinglish news**: `ZAI_API_KEY` se best (GLM Hinglish). Bina key ke Devanagari Hindi + English names.
4. **Market breadth**: Hamesha ON.

---

## V8.2.0 — Comprehensive Bug Fix + Claude → Z.AI GLM API Migration

Is version mein **~107 bugs fix** kiye gaye (4 CRITICAL, 25 HIGH, 42 MEDIUM,
36 LOW) aur Claude/Anthropic API ki jagah **Z.AI GLM API** set kiya gaya.
Poora detail `CHANGELOG_V8.2.0.md` mein hai.

### Sabse zaroori fixes (CRITICAL)

| Bug | Fix |
|-----|-----|
| Scheduler single-threaded — 9:20 scan lamba chalne se 9:30 intraday / 10:00 swing / 15:05 BTST **silently skip** | Har job alag thread mein chalta hai ab |
| Intraday RVOL 20-BAR (100-min) use karta tha, 20-DAY nahi — conviction scores meaningless | True 20-DAY daily-volume baseline |
| `sqlite3.Row.get()` crash — "Target Hit Stocks" bot command fail | `dict(r)` conversion |
| Breaking news RSS titles `&`/`<` bina escape — Telegram ~30% headlines reject | `html.escape()` after translate |

### Claude → Z.AI GLM API

V8.1.2 mein `AI_MODE="CLAUDE_API"` **kabhi kaam nahi karta tha** kyunki model
name `"claude-sonnet-4-6"` invalid tha. V8.2.0 mein:

- Claude poori tarah hata diya.
- Ab **Z.AI GLM API** (OpenAI-compatible, sasta, Hinglish mein behtar) use hota hai.
- Setup:
  1. https://z.ai par account banao, API key lo.
  2. `.env` mein `ZAI_API_KEY=<key>` daalo (default model `glm-4.5`).
  3. `config.py` mein `AI_MODE = "GLM_API"` set karo.
- Backward-compat: purana `AI_MODE="CLAUDE_API"` value bhi accept hota hai
  (warn ke saath GLM_API jaisa behave karega).

### Naye shared utilities (`utils.py`)

- `atomic_write_json()` / `atomic_write_bytes()` — crash-safe file writes
  (tempfile + `os.replace`). `resume_state`, `dispatch_state`, `cache`,
  `breaking_news` sab iska use karte hain.
- `chunk_text()` — long Telegram messages ko 4096-char limit ke andar todta
  hai. `telegram_alerts`, `master_dashboard`, `bot_listener` use karte hain.
- `db_transaction()` context manager (`database.py`) — auto-commit/rollback,
  connection-leak-proof. `tracker.py` use karta hai.

### IST timezone (poore project mein)

9 files mein `datetime.now()` timezone-naive tha (UTC servers par 1 din off).
Ab sab `ZoneInfo("Asia/Kolkata")` use karte hain. `requirements.txt` mein
`tzdata` add kiya (Windows portability ke liye).

---

## V6 - Dono Versions Ka Merge (comparison ke baad)

Aapne 2 versions upload/develop kiye the - is V6 mein dono ke best
parts merge kiye hain, jo aapne explicitly choose kiya:

| Decision | Kya rakha |
|---|---|
| **Chart style** | Purana matplotlib style (candlestick + EMA + RSI + Volume panels, **Entry/SL/Target1/Target2/FinalTarget/Support/Resistance lines chart par confirmed** - chart/Telegram/DB mein SAME numbers) |
| **Telegram dashboard formatting** | Naya `master_dashboard.py` (emoji cards, Hindi labels, 🎉 Target Hit / 🚨 SL Hit highlighting, 🟢/🔴 profit-loss) |
| **Bot group/channel support** | Aapka `bot_listener.py` wala fix rakha - Group ("message") **aur** Channel ("channel_post") dono handle karta hai |
| **Baaki features** | Aapke uploaded version jaisa hi (Bhavcopy/.env/profit-status wapas NAHI add kiye, jaisa aapne bola) |

### `master_dashboard.py` mein 2 bugs fix kiye (formatting same rakhi):
1. yfinance ka multi-index column order version ke hisab se badal sakta hai - purana code sirf EK order assume karta tha, dusre order mein saare stocks ka CMP silently Entry jaisा hi dikhta (0% galat data). Ab dono order check karta hai (test se verify kiya).
2. Apna alag bina-headers wala yfinance call kar raha tha - ab `tracker.py` wala shared session (browser headers ke saath) use karta hai, rate-limit risk kam.

### `main.py` mein 1 bug fix kiya:
`generate_charts_for_top()` ko galat arguments (`limit=` ki jagah `top_n=`, aur `all_data` missing) ke saath call kar raha tha jab charts.py switch hua - crash hota agar chalate. Ab sahi signature use hota hai.

⚠️ **Security reminder**: Aapke uploaded code mein ek naya bot token tha jo ab is chat mein bhi aa gaya hai - ise bhi revoke karke naya banao.

## v7 mein NAYA: Natural Language Chatbot

Ab group mein bot ko fixed commands ki zaroorat nahi - seedha normal
Hindi/English mixed sentence type karo:

| Type karo | Kya hota hai |
|---|---|
| "Reliance ka analysis bhejo" | RELIANCE ka snapshot + news |
| "Tata Steel entry kab milegi?" | TATASTEEL ka snapshot |
| "Aaj ke top stocks kya hain?" | Aaj ki top pick list |
| "Nifty ka trend kya hai?" | NIFTY 50 quick trend |
| "Mere active trades dikhao" | Saare OPEN positions |
| "Target hit stocks dikhao" | Jin stocks ka target hit hua |
| "Best risk reward stocks batao" | Sabse achha R:R wale OPEN positions |
| "Weekly report" / "Monthly report" | Performance summary |
| "PDF report bhejo" | Poori PDF report |
| "help" | Poori capability list |

**Kaam kaise karta hai (koi paid AI API nahi lagti):**
1. `nlu.py` - keyword-based intent classifier (rule-based, free)
2. `company_lookup.py` - company naam ("Reliance", "Tata Steel") ko
   symbol mein badalta hai. NIFTY 500 ki poori official company-name
   list (NSE se, `nifty_symbols.py` cache karta hai) ke against fuzzy
   match karta hai + common short-forms (TCS, SBI, L&T, M&M) ke liye
   alag alias list.

Agar koi intent match nahi hota, purana fallback chalta hai (seedha
symbol jaise "TCS" type karne par bhi kaam karta hai).

## v6 mein NAYA: Full Morning Briefing + FII/DII (8 PM)

Sabhi requested cheezein NSE ki apni FREE (unofficial) APIs se milti
hain - koi paid subscription nahi chahiye. In endpoints ko verify
kiya gaya hai (live response schema check kiya):

| Feature | Source | Command |
|---|---|---|
| **GIFT Nifty** | NSE `marketStatus` API (real futures data, koi proxy nahi) | `--morning` |
| **Bulk/Block Deal Stocks** | NSE `snapshot-capital-market-largedeal` API | `--morning` |
| **Pre-Market Movers** | NSE `market-data-pre-open` API | `--morning` |
| **Aaj ki Market News (Hindi)** | yfinance news + Hindi translation | `--morning` |
| **Watchlist/Top-Pick News (priority, Hindi)** | Sirf OPEN/recent DB stocks ki news, priority se | `--morning` |
| **FII/DII Report** | NSE `fiidiiTradeReact` API | `--evening` (8 PM) |

`python main.py --morning` (8 AM) - sab kuch ek Telegram message mein
(ya lamba ho to chunks mein). `--nifty` command bhi ab isी ko call
karta hai (backward-compatible).

**Har section independent hai** - agar NSE koi ek endpoint block/change
kar de, baaki sections phir bhi bhej diye jaate hain (poora message
fail nahi hota, sirf wo ek section "abhi available nahi" dikhata hai).

**Hindi translation**: `deep-translator` package use hota hai (free,
Google Translate wrap karta hai). Install: `pip install deep-translator`.
Agar install nahi hai, English text hi chala jaata hai (crash nahi
hota) - `config.py` mein `TRANSLATE_NEWS_TO_HINDI = False` karke bhi
band kar sakte ho.

⚠️ **Important honesty note**: Ye sab NSE ke UNOFFICIAL/undocumented
endpoints hain (NSE ka koi published public API contract nahi hai) -
isliye kabhi bhi bina notice ke change ho sakte hain ya block ho
sakte hain. Har function try/except mein hai, fail hone par gracefully
"data available nahi" dikhata hai, crash nahi karta.

## v5.2 mein NAYA: Poore Din Ka Schedule

Ab system pura trading day cover karta hai (jaisa architecture docs
mein bataya gaya tha), sab kuch duplicate-send guard ke saath:

| Time | Command | Kya karta hai |
|---|---|---|
| 8:00 AM | `--nifty` | NIFTY 50 live value |
| 9:20 AM | `--scan` | Poora scan + charts + Telegram |
| 9:15-3:30 PM | `--monitor` (har 15min) | Target/SL live tracking |
| **3:00 PM** | **`--closebuys`** | **Best Buys into Close** - subah ke cache se (koi naya download nahi, fast), sirf top 3-4 high-conviction setups agle din ke liye |
| 4:00 PM | `--report` | Din ka win-rate summary |
| **8:00 PM** | **`--evening`** | **Watchlist + Weekly (Friday) + Monthly (month-end)** performance report |

Sabkuch ek saath: `python main.py --schedule` (poora din auto-chalta hai).

Har command mein duplicate-send guard hai (`dispatch_state.py`) - same
din dobara chalao to skip ho jaata hai, `--force` se override kar sakte ho.

**"Best Buys into Close" (3pm) ki design choice**: Subah ka data cache
mein hota hai (`CACHE_MAX_AGE_HOURS` ke andar), isliye 3pm run **koi
naya Yahoo download nahi karta** - sirf dobara score karta hai aur
sirf top 3-4 candidates ke liye chhota 1H-confirmation check karta hai.
Isse fast rehta hai aur rate-limit risk nahi badhta.

## v5.1 mein kya fix hua

**1. Group chat ID update:** Bot ab channel ki jagah **"AI stocks for
swing trading"** GROUP mein kaam karta hai (chat_id `-1004375889188`,
`config.py` mein set hai).

⚠️ **Group mein chatbot ke reply karne ke liye ZAROORI setup:**
Telegram groups mein by default "Privacy Mode" ON hoti hai, jiski
wajah se bot ko sirf `/commands` dikhte hain, normal messages (jaise
"hello") nahi. Fix:
1. @BotFather ko message karo
2. `/setprivacy` bhejo → apna bot select karo → **Disable** choose karo
3. Bot ko group se remove karke wapas add karo

**2. Duplicate message/chart sending fix:** Pehle agar `python main.py`
galti se 2 baar chal jaata (manually test karte waqt, ya kisi aur
tarike se), to charts + dashboard message Telegram group mein DOBARA
chale jaate the. Ab ek **day-level idempotency guard** (`dispatch_state.py`)
hai - same din mein scan+charts sirf EK baar jaate hain. Dobara zaroorat
ho to `python main.py --scan --force` chalao.

**3. PDF Report ab on-demand hai (auto-duplicate nahi hota):**
`send_telegram_pdf()` function pehle bana hua tha lekin kabhi call
nahi hota tha. Ab ise seedhe scan pipeline mein add NAHI kiya (agar
karte, to charts DO BAAR aate - ek photo ki tarah, ek PDF ke andar).
Iske bajaye, PDF sirf tab jaati hai jab user bot se maange:
- Group mein **"Full PDF Report"** button (menu se)
- Ya `/pdf` command type karke

## v5 mein NAYA: Multi-Timeframe Confluence + Entry Zone

Do professional-grade trading-desk architecture proposals padhne ke
baad, unse sabse valuable, REALISTICALLY-buildable (free yfinance data
par) idea implement ki: sirf Daily chart dekhna kaafi nahi hai - ab
system 3 timeframes ka confluence check karta hai:

| Timeframe | Kya check hota hai | Extra API call? |
|---|---|---|
| **Weekly** | Bada trend direction (BULLISH/BEARISH/NEUTRAL) - daily data ko hi resample karke nikalta hai | ❌ Nahi (free) |
| **Daily** | Actual setup/score (EMA/RSI/MACD/ADX/Volume/Breakout - jo pehle se tha) | Already download hota hai |
| **1 Hour** | "Abhi entry lene layak hai ya wait karna chahiye" - sirf TOP scoring candidates ke liye | ✅ Haan (isliye sirf `MTF_1H_TOP_N` stocks ke liye) |

**Entry Zone**: Single exact price ("Entry: 2952") ki jagah ab ek
REALISTIC RANGE milta hai ("Entry Zone: 2940-2955") - jaise professional
desks dete hain, kyunki market mein exact price par fill guarantee
nahi hoti. Zone ATR ke ek fraction se calculate hota hai
(`ENTRY_ZONE_ATR_FRACTION` in config.py).

Ye dono cheezein chart, Telegram message, PDF, Excel, aur database -
SABME consistent hain (single source: `mtf.py` + `targets.py`).

**Important honesty note**: Dono documents jo aapne bheje the, unmein
paid broker APIs (Zerodha/Upstox/Angel One), options Greeks/IV/PCR,
FastAPI+Celery+Redis+PostgreSQL infra, aur ML models suggest hue the.
Aapne free yfinance route choose kiya, isliye:
- Options analytics is scope mein NAHI hai (free reliable source nahi hai)
- Infra abhi bhi single-script + SQLite hai (aapke PC/VPS par cron/scheduler se chalta hai) - poora Celery/Redis/Postgres stack nahi hai, jo ki free-data scanner ke liye zaroorat se zyada bhi hai
- 1H confirmation sirf top candidates ke liye hai (na ki 5m real-time) - yfinance ke free-tier rate-limits ko respect karne ke liye

⚠️ **SECURITY - PEHLE YE KARO:** Is project mein ek Telegram bot token
tha jo humari chat mein plaintext share ho gaya tha - isliye use
COMPROMISED maano. **@BotFather ko Telegram par `/revoke` bhejo, naya
token banao, aur `config.py` mein `TELEGRAM_BOT_TOKEN` update karo.**
Jab tak purana revoke nahi karte, koi bhi jisne wo token dekha hai
tumhare channel par messages bhej sakta hai / bot control kar sakta hai.

## v4.2 mein kya BUGS fix hue

| Bug | Kya tha | Fix |
|---|---|---|
| **Database schema mismatch** | `tracker.py` `target_1/2/3`, `t1_hit` etc columns use karta tha jo table mein kabhi the hi nahi - har recommendation save karna silently FAIL ho raha tha | Schema fix + auto-migration (purana data safe rehta hai) |
| **Telegram silent failures** | `sendPhoto`/`sendDocument` ka error response kabhi check hi nahi hota tha | Sabhi calls ab status check + log karte hain |
| **Markdown parse errors** | Stock names/AI text mein `_`, `&`, etc. se poora message reject ho jaata tha | HTML parse_mode + proper escaping |
| **Duplicate hardcoded credentials** | Token 2 files mein alag-alag hardcoded tha | `config.py` ab single source of truth |
| **Chart vs Telegram vs DB mismatch** | `charts.py` apna ALAG Entry/SL/Target (Fibonacci swing se) calculate karta tha - chart, Telegram message, aur database teeno mein ALAG numbers dikhte the | Sab jagah scanner.py ka SAME Entry/SL + shared `targets.py` se Target1/2/3 |
| **Boilerplate AI Analysis (PDF)** | PDF mein har stock ka EXACT SAME hardcoded paragraph tha | Ab asli `AI_Analysis` (scanner.py se, per-stock indicators based) |
| **".NS" dikhna** | Chart title, PDF, Telegram sab jagah "RELIANCE.NS" dikhta tha | `clean_symbol()` helper - sab jagah "RELIANCE" |
| **News dead code** | `fetch_stock_news()` function tha lekin kahin call hi nahi hota tha | Ab shared `news.py` (cached, dono yfinance schema handle karta hai) Telegram + PDF dono mein use hota hai |
| **Windows-only PDF font** | Hindi font sirf `C:\Windows\Fonts\mangal.ttf` check karta tha | Multiple OS paths + `config.py` mein override option |

## v4.2 mein NAYE FEATURES

- **Interactive Telegram bot** (`python bot_listener.py`) - "hello"/"/start" par button menu; free-text mein koi stock naam type karo, turant snapshot + news milta hai
- **Daily tracking**: `python main.py --monitor` (live Target/SL check), `--report` (win-rate summary), `--nifty` (NIFTY 50 live value)
- **Full-day automation**: `python main.py --schedule` (8am Nifty, 9:20 scan, market-hours monitor, 4pm summary - sab ek process mein)

## Is version mein KYA NAHI hai (honesty)

- **FII/DII data, Block Deals, Volume Shockers** - ye NSE ki alag "bhavcopy"/reports se aata hai, yfinance mein available nahi hai. Alag se scrape karna padega (NSE frequently blocks scripted access, fragile hoga).
- **Har trending news ka real-time push** - abhi news sirf scan/monitor ke time fetch hoti hai, continuous news-monitoring alag background service hoga.

NIFTY 500 stocks ko technical indicators (EMA, RSI, MACD, ADX, ATR,
Supertrend, Volume Spike, Breakout, Consolidation, Support/Resistance,
Pivot Points, Fibonacci, VWAP), chart patterns (Double Top/Bottom,
Head & Shoulders, Bull/Bear Flag), aur Relative Strength (vs NIFTY)
ke basis par scan karke score deta hai, Risk:Reward calculate karta
hai, AI-style analysis likhta hai, Excel + PDF report banata hai,
chart images generate karta hai, aur Telegram / WhatsApp / Email par
alert bhej deta hai.

## v2.2 mein naya kya hai

| Feature | Detail |
|---|---|
| **Local Cache** | `data/cache/` mein data cache hota hai, same din dubara download nahi |
| **Resume Download** | Beech mein रुके to agli baar wahin se aage badhta hai |
| **Smart Random Delay** | Har batch ke beech random gap (rate-limit se bachne ke liye) |
| **Pattern Detection** | Double Top/Bottom, Head & Shoulders, Bull/Bear Flag |
| **Pivot Points, Fibonacci, VWAP** | Support/Resistance upgrade |
| **RVOL + Volume Dry-up** | Volume analysis upgrade |
| **Relative Strength** | Stock vs NIFTY 50 performance comparison |
| **WhatsApp Alert** | Twilio ke through |
| **Email Alert** | SMTP ke through, Excel+PDF attachment ke saath |
| **Daily Scheduler** | `python main.py --schedule` |

## 1. Install (pehli baar)

```bash
pip install -r requirements.txt
```

## 2. Chalao

```bash
python main.py              # ek baar turant scan
python main.py --now        # same as upar
python main.py --schedule   # roz fixed time par auto-run (process chalta rehna chahiye)
```

Output:
- `reports/AI_Report.xlsx` - poora scan result + "Top Buy List" sheet
- `reports/AI_Report.pdf` - formatted report with charts
- `charts/*.png` - top stocks ki chart images
- `logs/scanner.log` - run history

## 3. Settings (`config.py`)

Sab kuch `config.py` mein hai:

| Setting | Kya karta hai |
|---|---|
| `SYMBOL_SOURCE` | `"NIFTY500"` = auto NSE se poori list, `"CUSTOM"` = neeche di list |
| `MIN_RSI`, `MIN_ADX`, etc. | scoring thresholds |
| `SCORE_WEIGHTS` | har indicator ka weight (total 100) |
| `SIGNAL_THRESHOLDS` | kitne score par STRONG BUY / BUY / WATCH |
| `AI_MODE` | `"RULE_BASED"` (free, default) ya `"GLM_API"` (real AI via Z.AI, API key chahiye) |
| `TELEGRAM_*` | Telegram alert settings |

## 4. NIFTY 500 auto-list

Code pehle NSE se live list download karne ki koshish karta hai. Agar
NSE block kar de (kabhi kabhi scripted requests block hoti hain) ya
internet na ho, to ye `data/nifty500.csv` file se list leta hai
(agar pehle se ek successful run ho chuka ho to ye file auto-cache ho
jaati hai). Pehli baar agar dono fail ho jaayein, to `CUSTOM_STOCKS`
(config.py) use hota hai taaki scan kam se kam chal to sake.

Agar NSE hamesha block karta hai, to manually CSV yahan se download
karo aur `data/nifty500.csv` mein rakh do (column name "Symbol" hona
chahiye, bina ".NS" ke):
https://www.nseindia.com/products-services/indices-nifty500-index

## 5. Telegram Alerts (optional)

1. Telegram par **@BotFather** ko `/newbot` bhejo, bot ka naam do.
2. Jo token mile, `config.py` mein `TELEGRAM_BOT_TOKEN` mein daalo.
3. Apna chat id nikalne ke liye **@userinfobot** ko `/start` bhejo
   (personal alert ke liye), ya group mein bot add karke
   `https://api.telegram.org/bot<TOKEN>/getUpdates` open karo.
4. `config.py` mein `TELEGRAM_ENABLED = True` aur `TELEGRAM_CHAT_ID` set karo.

## 6. AI Analysis mode

Default `AI_MODE = "RULE_BASED"` hai - koi API key nahi chahiye,
turant kaam karta hai, 500 stocks ke liye bhi free hai.

Agar real AI se natural-language analysis chahiye (V8.2.0 mein **Z.AI GLM API**):
1. https://z.ai par account banao, API key lo.
2. `.env` file mein `ZAI_API_KEY=<aapki_key>` daalo.
   (Optional: `ZAI_MODEL=glm-4.5` — default. Agar future mein `glm-5.2`
   available ho to bas yahan set kar do.)
3. `config.py` mein `AI_MODE = "GLM_API"` karo.
4. Note: cost/speed control karne ke liye ye sirf STRONG BUY / BUY
   signal wale stocks ke liye hi real API call karta hai; baaki sab
   automatically rule-based rehte hain. Koi API error aaye to gracefully
   rule-based fallback chalta hai (crash nahi hota).

> **V8.2.0 note**: Pehle ye Claude/Anthropic API use karta tha (mode
> `"CLAUDE_API"`) lekin model name galat tha isliye kabhi kaam nahi karta
> tha. Ab Z.AI GLM API use hota hai. Purana `AI_MODE="CLAUDE_API"` value
> bhi accept hota hai (backward-compat, warn ke saath GLM_API jaisa).

## 6b. WhatsApp Alerts (optional, Twilio ke through)

1. https://www.twilio.com par free account banao.
2. Console se `ACCOUNT_SID` aur `AUTH_TOKEN` copy karo -> `config.py`.
3. Twilio Console -> Messaging -> Try it out -> "Send a WhatsApp message"
   mein sandbox join karo (apne WhatsApp se join code bhejna hoga).
4. `TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"` (sandbox number)
5. `WHATSAPP_TO = "whatsapp:+91XXXXXXXXXX"` (tumhara number)
6. `WHATSAPP_ENABLED = True` karo.

Note: sandbox free hai lekin session window 24hr ki hoti hai;
production ke liye Twilio se apna verified WhatsApp number lena hoga.

## 6c. Email Alerts (optional, SMTP ke through)

Gmail example:
1. Google Account -> Security -> 2-Step Verification enable karo.
2. Google Account -> Security -> App Passwords -> naya app password banao.
3. `config.py` mein `SMTP_USER` = tumhara gmail, `SMTP_PASSWORD` = wo app password.
4. `EMAIL_FROM`, `EMAIL_TO` set karo, `EMAIL_ENABLED = True`.

Email mein Top Buy List table (HTML) + Excel/PDF attachment dono jaate hain.

## 6d. Daily Scheduler (optional)

```bash
python main.py --schedule
```

Ye process foreground mein chalta rehta hai aur roz `SCHEDULE_TIME`
(config.py, default `"09:20"`) par automatically scan chalata hai.
Isko chalne ke liye PC/server on rehna chahiye.

**Zyada reliable option (production ke liye recommended):** OS-level
scheduling use karo:
- **Windows**: Task Scheduler mein `python main.py --now` ko daily trigger par add karo
- **Linux/Mac**: crontab mein ye line add karo (roz 9:20 AM, Mon-Fri):
  ```
  20 9 * * 1-5 cd /path/to/AI_Stock_Scanner_V2 && python3 main.py --now
  ```

## 7. Project Structure

```
AI_Stock_Scanner_V2/
├── main.py               # entry point - ye file run karo
├── config.py              # sab settings yahan
├── nifty_symbols.py        # NIFTY 500 list fetch (NSE + fallback)
├── downloader.py            # Yahoo Finance batch download (cache+resume aware)
├── cache.py                  # local data caching
├── resume_state.py             # interrupted-run resume tracking
├── indicators.py                 # EMA/RSI/MACD/ADX/ATR/Supertrend/Pivot/Fib/VWAP/etc.
├── patterns.py                     # chart pattern detection
├── relative_strength.py              # stock vs NIFTY comparison
├── scanner.py                          # scoring + risk-reward + signal logic
├── ai_analysis.py                        # rule-based / Claude API analysis text
├── charts.py                               # chart image generation
├── report.py                                 # Excel + PDF report
├── telegram_alert.py                           # Telegram bot integration
├── whatsapp_alert.py                             # WhatsApp (Twilio) integration
├── email_alert.py                                  # Email (SMTP) integration
├── scheduler.py                                      # daily auto-run scheduler
├── logger.py                                           # logging setup
├── requirements.txt
├── data/           # NIFTY 500 CSV cache + downloaded data cache + resume state
├── reports/        # Excel + PDF output
├── charts/         # PNG chart images
└── logs/           # run logs
```

## 9. "Rate Limited / Too Many Requests" aaye to?

Yahoo Finance zyada requests par temporarily block kar deta hai. Code
ab automatically:
- Stocks ko **batches** (`CHUNK_SIZE`, default 20) mein download karta hai, ek se zyada parallel calls nahi karta.
- Rate-limit error par khud **exponential backoff** karta hai (30s → 60s → 120s...).
- Har batch ke beech **random gap** (`RANDOM_DELAY_MIN_SEC` 4s / `MAX_SEC` 9s) leta hai.
- **Local cache** use karta hai - same din dobara chalao to already-downloaded stocks Yahoo se dubara nahi mangwata.
- **Resume** karta hai - beech mein रुके to agli baar sirf bache hue stocks download karta hai.

**v7.1 fix**: Naye yfinance versions rate-limit hone par exception
raise nahi karte - internally hi catch karke khaali dataframe de dete
hain (isliye pehle "batch empty response" errors sirf 5 sec wait karke
retry hoti thi, exponential backoff nahi). Ab khaali response ko
HAMESHA rate-limit maana jaata hai. Plus ek **circuit breaker** hai:
agar 3 batches lagatar poori tarah fail ho jaayein, poora process 5
minute ruk jaata hai (rate-limit window reset hone dene ke liye).

Agar phir bhi baar-baar aaye to `config.py` mein:
- `CHUNK_SIZE` ko aur chhota karo (jaise 10)
- `RANDOM_DELAY_MIN_SEC` / `RANDOM_DELAY_MAX_SEC` badhao (jaise 8-15)
- `CIRCUIT_BREAKER_COOLDOWN_SEC` badhao (jaise 600 = 10 minute)
- Ya poore NIFTY 500 ki jagah pehle `CUSTOM_STOCKS` ki chhoti list se test karo

## 10. Important Notes

- NIFTY 500 ka poora scan (500 stocks) internet speed ke hisaab se
  ~5-12 minute le sakta hai (pehli baar; cache hit hone par bahut fast).
- `CHARTS_FOR_TOP_N` (config.py) control karta hai kitne stocks ke
  liye chart image banegi - sabke liye banana slow hoga.
- Risk:Reward calculation ATR-based stoploss aur pichle
  support/resistance par based hai - ye purely technical hai, koi
  financial advice nahi hai. Apna khud ka research zaroor karo.
- **Pattern Detection** (Double Top/Bottom, H&S, Flags) rule-based/
  heuristic hai, ML-trained nahi - ek "extra confirmation signal"
  samjho, akela decision maker nahi.

## 11. Is version mein kya JAAN-BUJH KAR nahi hai

Honesty ke liye - ye cheezein is scope mein include nahi hain kyunki
inke liye alag data source ya poori alag infrastructure chahiye:

- **Machine Learning score** - isko train karne ke liye historical
  labeled data + backtesting infra chahiye, ye khud ek alag project hai
- **Sector Rotation ranking** - sector-mapping data NSE se alag se
  leni padegi (yfinance mein nahi hoti)
- **Delivery Volume** - ye NSE ki alag "bhavcopy" file se aata hai,
  Yahoo Finance data mein include nahi hota
- **Dashboard / Backtesting / Portfolio tracking** - ye web-app level
  ka kaam hai (Streamlit ya similar + database chahiye)

Agar in mein se koi cheez chahiye, batao - alag se (v3 jaisa) bana
sakte hain.
