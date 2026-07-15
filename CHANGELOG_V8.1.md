# AI Stock Scanner — V8.1 Changelog
### (Base: your uploaded V6 codebase — 30/30 original files untouched except the 3 listed below)

---

## Kya naya hai (What's New)

### 1. `config.py` — Optional `.env` Secret Management
- **Problem solve hua:** `ModuleNotFoundError: No module named 'dotenv'` aur `Missing required environment variables` — dono errors ab kabhi nahi aayenge.
- `.env` file na ho, `python-dotenv` install na ho — kuch bhi ho, **config.py kabhi crash nahi karega**. Purane hardcoded values fallback ki tarah kaam karte rahenge (exactly V6 jaisa zero-setup behavior).
- Agar `.env` file banate ho (`.env.example` se copy karke), to usme diye values (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`, Twilio, SMTP) hardcoded defaults ko **override** kar dete hain.
- ⚠️ **Security:** Aapka bot token pehle is conversation mein plaintext share ho chuka hai (`telegram_alerts.py` mein already isi ka warning comment mojood tha). **@BotFather se `/revoke` karke naya token banao**, phir `.env` file mein daal do.

### 2. `dispatch_state.py` — `--force` Lock Permanently Hataya Gaya
- Ye tha **Requirement #1** jo ab tak V6 mein pending tha.
- Pehle: koi bhi command (`--scan`, `--morning`, `--closebuys`, etc.) din mein sirf ek baar chalta tha — dobara chalane par kuch nahi hota tha jab tak `--force` na do.
- Ab: **har command, har baar, fresh data ke saath turant Telegram par bhejta hai** — chahe CLI se ho ya `--schedule` scheduler se, chahe usi din pehle bhi chal chuka ho.
- `--force` flag ab bhi accept hota hai (backward-compatible — purane scripts nahi tootenge), bas ab uska koi alag effect nahi hai.

### 3. `charts.py` — TradingView Premium Dark Theme
- Ye tha **Requirement #2**.
- Pehle: white/light background, RSI panel, Fibonacci retracement lines, candle-pattern text badges, trendline — retail-style chart.
- Ab:
  - `#131722` TradingView dark background, teal/red TV-style candles, **300 DPI**
  - **Sirf EMA 20 / EMA 44 / EMA 200** — koi aur indicator overlay nahi
  - **Naya Breakout Box** — 20-din ki highest high par orange dashed line (`BREAKOUT_LOOKBACK` config se)
  - Entry Zone / SL / Target 1-2-3 / Support / Resistance lines **retain** kiye (ye scanner.py ke trade-plan levels hain, retail "indicator" nahi — chart/Telegram/DB hamesha same number dikhayenge)
  - Volume panel neeche patla sa rakha hai
  - RSI panel, Fibonacci, pattern-badge, trendline — **hataye gaye**
- `main.py` mein chart caption bhi simplify kiya: ab sirf **"🔥 #SYMBOL - Signal Setup"** — koi boilerplate text nahi (poori detail wala text message alag se already jaata hai).

---

## Kya WAISA hi hai (Unchanged — already good in V6, verified)

In cheezon ko touch nahi kiya gaya kyunki ye already requirement fulfil kar rahi thi:
- ✅ Morning Briefing (GIFT Nifty + Bulk/Block deals + Pre-market movers + Hindi news) — `main.py`
- ✅ Best Buys into Close (3 PM) — `main.py`
- ✅ Database tracker + Daily/Weekly/Monthly scorecard + green/red P&L — `tracker.py`, `database.py`
- ✅ Hindi translation with graceful fallback — `translator.py`
- ✅ Resume-state crash recovery for interrupted downloads — `resume_state.py` (ye `dispatch_state.py` se bilkul alag cheez hai, isko touch nahi kiya)

## Aage Kya Ho Sakta Hai (Optional — batao to karenge)
- `news.py` abhi yfinance se news leta hai (already robust, dual-schema safe). Agar specifically Moneycontrol/ET/LiveMint RSS chahiye (original spec mein maanga gaya tha), wo ek alag, testable change hoga.

---

## Upgrade Steps
```bash
pip install -r requirements.txt      # python-dotenv naya add hua hai
python main.py --morning             # test karo
python main.py --scan                # full scan test
python main.py --schedule            # full day automation
```

`.env` use karna chaho to:
```bash
cp .env.example .env
# .env mein apna NAYA (revoked-and-regenerated) bot token daalo
```
