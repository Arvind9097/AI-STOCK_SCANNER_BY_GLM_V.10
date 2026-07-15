# AI Stock Scanner — V8.3.0 Changelog
## (Base: V8.2.0 — 3 new modules, 9 files modified, expanded universe + GLM ranking + UI overhaul)

---

## 🎯 V8.3.0 — NSE+BSE Universe + GLM AI Ranking + UI Super-Overhaul

### 🆕 NAYA FEATURE 1: Expanded Universe (NSE + BSE full list)

**Pehle**: Sirf NIFTY 500 (~500 stocks) scan hota tha.
**Ab**: NSE + BSE dono exchanges ke sabhi listed equities (~1300 in FAST mode, 7000+ in FULL mode).

- Naya module `universe_fetcher.py`:
  - NSE: EQUITY_L.csv (sabhi NSE equities) + NIFTY 500 + NIFTY Total Market
  - BSE: List_Scrips.csv (sabhi BSE equities, Group A/B/T)
  - FAST mode (default): ~1300 symbols, scan time ~10-15 min
  - FULL mode: 7000+ symbols, scan time ~40-60 min
  - Fallback chain: NSE archives → BSE → local CSV → CUSTOM_STOCKS
- 2-stage fast scan (`scanner.py`):
  - **Stage 1**: Quick filter — price < ₹20 (penny) ya avg volume < 5 lakh (illiquid) skip. No indicator computation.
  - **Stage 2**: Full indicator scan (EMA/RSI/MACD/ADX/ATR/Supertrend/etc) sirf Stage-1 survivors par.
  - Result: 7000+ universe mein bhi sirf ~500-800 stocks par full scan → fast.

### 🆕 NAYA FEATURE 2: GLM AI Stock Ranking

**Pehle**: GLM sirf individual stock ka analysis text likhta tha.
**Ab**: GLM poore scan ke top 30 technical candidates ko dekh kar best 8 picks select karta hai.

- Naya module `glm_screener.py`:
  - Scanner poore universe (NSE+BSE ~1300) ko technical score deta hai (fast, free).
  - Top 30 candidates GLM API ko bheje jaate hain.
  - GLM inme se best 8 picks select karta hai with:
    - **Confidence score** (1-10 scale)
    - **Action**: ENTER_NOW / WAIT / AVOID
    - **Hinglish rationale** (2-3 lines, kyun ye stock best hai)
    - **Hinglish risk note** (1 line warning)
  - JSON response parsing with markdown-fence handling.
  - Fallback: agar ZAI_API_KEY nahi hai, technical score se ranking (GLM fields empty).
- Telegram par "🤖 GLM AI TOP PICKS" section bheja jaata hai.
- PDF mein "🤖 GLM AI TOP PICKS" section (highlighted boxes ke saath).

### 🆕 NAYA FEATURE 3: Market Breadth + Sentiment (mera idea)

Professional trading desks market breadth dekh kar overall sentiment judge karte hain. Ab ye bhi hai.

- Naya module `market_breadth.py`:
  - Advances vs Declines (kitne stocks upar, kitne neeche)
  - Market Breadth % (advances / total * 100)
  - New 20-day highs count
  - Signal distribution (Strong Buy / Buy / Watch / Sell)
  - Overall sentiment: 🟢 BULLISH / 🟡 NEUTRAL / 🔴 BEARISH
- Telegram dashboard ke top par "📊 MARKET SENTIMENT" section.
- PDF mein "📊 MARKET SENTIMENT" section (with visual advances/declines bar).

---

### 🎨 UI OVERHAUL 1: Charts Simple + Best (`charts.py`)

- **Compact trade-plan box** (top-right): 8 overlapping right-margin labels → 1 neat monospace box listing all 9 levels (Entry/SL/T1/T2/T3/Support/Resistance/Breakout).
- **Color hierarchy**: 6+ competing colors → 4-tier (solid bold Entry/SL/Target / muted dashed Support-Resistance / thin EMAs).
- **Cleaner title**: `SYMBOL • ₹XXX • ★ STRONG BUY` (signal badge).
- **Subtle watermark**: V8.3.0, low opacity, bottom-right.
- **No top/left spines** (TradingView "no border" look).
- **Volume panel**: dimmer, color-coded, no edge.
- Same TradingView dark theme (#131722). 300 DPI. Figure 15×9.

### 🎨 UI OVERHAUL 2: Telegram Messages Best (`telegram_alerts`, `master_dashboard`, `main`, `tracker`)

- **Stock names ALWAYS bold + English**: `<b>RELIANCE</b>` (never translated). M&M → `M&amp;M` → bold "M&M".
- **Standardized emoji vocabulary**: 🔥 STRONG BUY, ⚡ BUY, 👀 WATCH, ⚠️ SELL, 🟢 profit, 🔴 loss, 🎯 target, 🛑 stoploss, 💵 entry, 📊 score, 🚀 breakout, 📈 volume, 💡 analysis, 📰 news, 👑 master, 🤖 GLM, 🎉 target-hit, 🥇 best-performer.
- **27-char `━` divider** between sections/cards.
- **Compact card format** per stock (4-5 lines max).
- **Alert messages** attention-grabbing: `🎉 TARGET HIT! — RELIANCE hit T1 🎯`, `🛑 STOPLOSS HIT — RELIANCE 📉`.
- **Performance reports**: summary cards with win-rate, best/worst performer badges.
- All dynamic text `escape_html()`'d (HTML parse safety).

### 🎨 UI OVERHAUL 3: News Always Hinglish (`translator`, `news`, `breaking_news`)

- Naya `to_hinglish()` function (`translator.py`):
  - **GLM API path** (best, jab ZAI_API_KEY set ho): Roman Hinglish with English stock names preserved.
  - **Placeholder technique** (fallback): stock names extract → placeholder → Google Translate → restore English names.
  - **Stock name detection**: regex for tickers (RELIANCE, TCS, M&M), company names (Reliance Industries, Tata Steel), brands (Sensex, Nifty, Adani).
  - Thread-safe + LRU cached + never crashes.
- `format_news_text()` ab `to_hinglish()` use karta hai.
- `breaking_news.py` ab Hinglish path use karta hai.
- `main.py` morning briefing mein double-translate remove kiya gaya.

**Result**: "Reliance Industries reports strong Q2" → "Reliance Industries ne strong Q2 report kiya" (stock name English, common words Hinglish).

### 🎨 UI OVERHAUL 4: PDF Master AI Trading Dashboard (`report.py`)

5-section PDF flow:
1. **Title header**: `🤖 AI STOCK SCANNER V8.3.0` + `Master AI Trading Dashboard` + date/time + "Scanned N stocks across NSE + BSE".
2. **📊 MARKET SENTIMENT** (new): sentiment label + advances/declines visual bar + signal distribution grid.
3. **🤖 GLM AI TOP PICKS** (new): per-pick highlighted box — rank + bold stock + action badge + confidence meter + Hinglish rationale + risk note.
4. **📈 DETAILED STOCK ANALYSIS**: per-stock cards — dark-blue header bar (signal emoji + #rank + bold stock + signal label) + sub-bar (Score/CMP/R:R/RSI/ADX) + trade plan table (alternating rows, ₹ green targets / red SL) + chart image + 💡 AI Analysis (Hinglish) + 🤖 GLM View + 📰 Latest News (Hinglish).
5. **⚠️ DISCLAIMER** (new, last page): Hinglish disclaimer with 6 bullet points.

- **Bold stock names** everywhere (English, never translated).
- **Color palette**: dark blue headers, green BUY/targets, red SL, amber WATCH, light blue GLM highlight.
- **Footer on every page**: page number + "Not financial advice" + IST timestamp.
- **Excel**: Market Breadth info rows + new "GLM Picks" sheet (color-coded Action).
- **Backward-compatible**: old `save_report()` calls still work.

---

## 📊 Summary

| Metric | Value |
|--------|-------|
| New modules | 3 (`universe_fetcher`, `glm_screener`, `market_breadth`) |
| Files modified | 9 (`config`, `downloader`, `scanner`, `main`, `report`, `charts`, `translator`, `news`, `breaking_news`, `master_dashboard`, `tracker`, `telegram_alerts`) |
| Universe expansion | NIFTY 500 → NSE + BSE ~1300 (FAST) / 7000+ (FULL) |
| GLM ranking | Top 30 → 8 best picks (Hinglish rationale + confidence + action) |
| UI redesign | Charts + Telegram + News + PDF (all 4) |
| Syntax check | ✅ All 41 files pass |
| Functional tests | ✅ Universe import, GLM fallback, breadth, to_hinglish — all pass |

### Setup for new features
1. **Expanded universe**: Already on by default (FAST mode). For FULL mode, `config.py` mein `UNIVERSE_MODE = "FULL"`.
2. **GLM AI ranking**: Set `ZAI_API_KEY` in `.env` (from https://z.ai). Already enabled (`GLM_SCREENER_ENABLED = True`).
3. **Hinglish news**: Best results with `ZAI_API_KEY` (GLM Hinglish). Without key, falls back to Devanagari Hindi with English stock names.
4. **Market breadth**: Always on (no config needed).
