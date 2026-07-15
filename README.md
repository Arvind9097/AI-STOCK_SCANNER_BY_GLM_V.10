# AI Stock Scanner V9.2

Production-grade Indian stock market scanner with 5 modular components.

## 📦 Modules

| Module | Path | Purpose |
|--------|------|---------|
| 1. Network | `network/` | NSE data fetcher (anti-403) + API retry (429 backoff) |
| 2. News | `news/` | Indian market news filter + sentiment engine |
| 3. Charts | `charts/` | Ultra-clean charts (candles + EMA44 + EMA200 + volume) |
| 4. Strategy | `strategy/` | 3-layer confluence (trend + entry + fundamentals) |
| 5. Reports | `reports/` | Unicode-safe PDF (no □ boxes) |
| Master | `src/main.py` | Pipeline orchestrator + scheduler |

## 🚀 Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run evening summary pipeline (one-time)
python src/main.py --run evening --max-stocks 50

# 3. Start daily scheduler (runs at 08:00, 09:20, 16:00, 20:00 IST)
python src/main.py --run scheduler
```

## 📁 Output

- `output_reports/Evening_Summary_*.pdf` — PDF report
- `output_reports/charts/*.png` — Clean technical charts
- `logs/master.log` — Pipeline logs

## 🧪 Self-Tests

Each module has a built-in self-test:
```bash
python network/api_retry.py      # Test 429 retry logic
python news/filter_engine.py     # Test news filtering
python charts/chart_generator.py # Test chart rendering
python strategy/strategy_evaluator.py  # Test strategy evaluation
python reports/pdf_builder.py    # Test PDF generation
```

## 📋 Architecture

```
┌─────────────┐   ┌──────────┐   ┌──────────┐   ┌─────────┐   ┌──────┐
│  Module 1   │→  │ Module 2 │→  │ Module 4 │→  │Module 3 │→  │Mod 5 │
│ Network+API │   │ News     │   │ Strategy │   │ Charts  │   │ PDF  │
│ (NSE+retry) │   │ (filter) │   │ (3-layer)│   │ (clean) │   │(UTF8)│
└─────────────┘   └──────────┘   └──────────┘   └─────────┘   └──────┘
                                                                     ↓
                                                            output_reports/
```

## ⚠️ Requirements

- Python 3.9+ (for `zoneinfo`)
- Linux/Mac recommended (DejaVu Sans font pre-installed for PDF)
- Internet access (NSE + RSS + yfinance)

## 📝 License

Educational use only. NOT financial advice.
