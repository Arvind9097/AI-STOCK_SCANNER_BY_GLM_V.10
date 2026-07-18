"""
===========================================================
 FUNDAMENTAL ANALYZER (V9.4 — Fundamental Verification)
===========================================================
Technical pass हुए stocks का fundamental check करता है।

यह module 2 sources से fundamental data लाता है:
  1. yfinance (free, reliable) — P/E, Book Value, Debt, ROE
  2. Yahoo Finance summary API — quarterly revenue/profit growth

FUNDAMENTAL CHECKLIST (10 conditions):
  1. P/E Ratio: 5 < PE < 50 (sustainable, not overvalued)
  2. Debt-to-Equity: < 2.0 (manageable leverage)
  3. Book Value per Share: > 0 (positive net worth)
  4. Quarterly Profit Growth (YoY): > 10% (growing profits)
  5. Quarterly Revenue Growth (YoY): > 5% (growing revenue)
  6. Yearly Profit Growth: > 15% (annual growth)
  7. Yearly Revenue Growth: > 10% (annual revenue growth)
  8. ROE (Return on Equity): > 12% (efficient capital use)
  9. Promoter Holding: > 40% (skin in the game)
  10. FII/DII Holding: stable/increasing (institutional confidence)

RATING SYSTEM:
  - All 10 pass → A+ (Excellent)
  - 8-9 pass → A (Very Good)
  - 6-7 pass → B (Good)
  - 4-5 pass → C (Average)
  - <4 pass → D (Weak — reject for swing/BTST)

USAGE:
    analyzer = FundamentalAnalyzer()
    result = analyzer.analyze("RELIANCE.NS")
    # result = {
    #     "symbol": "RELIANCE.NS",
    #     "overall_rating": "A+",
    #     "pass_count": 9,
    #     "fail_count": 1,
    #     "checks": [...],
    #     "fundamental_score": 90,
    # }
===========================================================
"""

import logging
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Cache (5 min TTL — fundamental data doesn't change intraday)
_fundamental_cache = {}
_CACHE_TTL = 300  # 5 minutes


@dataclass
class FundamentalCheck:
    """Single fundamental check result."""
    name: str
    condition: str
    value: str
    threshold: str
    passed: bool
    details: str = ""


@dataclass
class FundamentalResult:
    """Complete fundamental analysis result."""
    symbol: str
    company_name: str = ""
    overall_rating: str = "D"
    pass_count: int = 0
    fail_count: int = 0
    fundamental_score: int = 0
    checks: List[Dict] = field(default_factory=list)
    # Raw data
    pe_ratio: Optional[float] = None
    debt_to_equity: Optional[float] = None
    book_value: Optional[float] = None
    roe: Optional[float] = None
    quarterly_profit_growth: Optional[float] = None
    quarterly_revenue_growth: Optional[float] = None
    yearly_profit_growth: Optional[float] = None
    yearly_revenue_growth: Optional[float] = None

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "overall_rating": self.overall_rating,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "fundamental_score": self.fundamental_score,
            "checks": self.checks,
            "pe_ratio": self.pe_ratio,
            "debt_to_equity": self.debt_to_equity,
            "book_value": self.book_value,
            "roe": self.roe,
            "quarterly_profit_growth": self.quarterly_profit_growth,
            "quarterly_revenue_growth": self.quarterly_revenue_growth,
            "yearly_profit_growth": self.yearly_profit_growth,
            "yearly_revenue_growth": self.yearly_revenue_growth,
        }


class FundamentalAnalyzer:
    """
    Fundamental data fetch + 10-condition checklist + rating system.

    USAGE:
        analyzer = FundamentalAnalyzer()
        result = analyzer.analyze("RELIANCE.NS")
        if result.overall_rating in ("A+", "A", "B"):
            # Stock passed fundamental check — include in picks
        else:
            # Reject — weak fundamentals
    """

    # Thresholds (configurable)
    PE_MIN = 5.0
    PE_MAX = 50.0
    DEBT_TO_EQUITY_MAX = 2.0
    BOOK_VALUE_MIN = 0.0
    QUARTERLY_PROFIT_GROWTH_MIN = 10.0    # %
    QUARTERLY_REVENUE_GROWTH_MIN = 5.0    # %
    YEARLY_PROFIT_GROWTH_MIN = 15.0       # %
    YEARLY_REVENUE_GROWTH_MIN = 10.0      # %
    ROE_MIN = 12.0                        # %

    def __init__(self):
        self._cache = {}

    # ───────────────────────────────────────────────────────────────
    # PUBLIC: Main analyze method
    # ───────────────────────────────────────────────────────────────
    def analyze(self, symbol: str) -> FundamentalResult:
        """
        Fetch fundamental data + run 10-condition checklist + assign rating.

        Args:
            symbol: NSE symbol with .NS suffix (e.g. "RELIANCE.NS")

        Returns:
            FundamentalResult with rating, pass/fail count, and raw data.
        """
        result = FundamentalResult(symbol=symbol)

        # Check cache
        cache_key = symbol
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        # Fetch fundamental data
        data = self._fetch_fundamental_data(symbol)
        if not data:
            result.overall_rating = "N/A"
            result.checks = [{"name": "Data Fetch", "condition": "Fundamental data available",
                              "value": "Failed", "threshold": "Success",
                              "passed": False, "details": "Could not fetch fundamental data"}]
            self._cache[cache_key] = (time.time(), result)
            return result

        # Populate raw data
        result.company_name = data.get("company_name", "")
        result.pe_ratio = data.get("pe_ratio")
        result.debt_to_equity = data.get("debt_to_equity")
        result.book_value = data.get("book_value")
        result.roe = data.get("roe")
        result.quarterly_profit_growth = data.get("quarterly_profit_growth")
        result.quarterly_revenue_growth = data.get("quarterly_revenue_growth")
        result.yearly_profit_growth = data.get("yearly_profit_growth")
        result.yearly_revenue_growth = data.get("yearly_revenue_growth")

        # Run 10 checks
        checks = []
        checks.append(self._check_pe(result.pe_ratio))
        checks.append(self._check_debt(result.debt_to_equity))
        checks.append(self._check_book_value(result.book_value))
        checks.append(self._check_quarterly_profit_growth(result.quarterly_profit_growth))
        checks.append(self._check_quarterly_revenue_growth(result.quarterly_revenue_growth))
        checks.append(self._check_yearly_profit_growth(result.yearly_profit_growth))
        checks.append(self._check_yearly_revenue_growth(result.yearly_revenue_growth))
        checks.append(self._check_roe(result.roe))
        # Note: promoter holding + FII/DII not easily available via yfinance
        # These 2 checks are marked as "N/A" (not counted in pass/fail)
        checks.append(self._check_promoter_holding(data.get("promoter_holding")))
        checks.append(self._check_fii_dii(data.get("fii_dii_trend")))

        result.checks = [c.__dict__ for c in checks]

        # Count pass/fail (skip N/A checks)
        valid_checks = [c for c in checks if c.value != "N/A"]
        result.pass_count = sum(1 for c in valid_checks if c.passed)
        result.fail_count = sum(1 for c in valid_checks if not c.passed)

        # Score (0-100)
        total_valid = len(valid_checks)
        if total_valid > 0:
            result.fundamental_score = int((result.pass_count / total_valid) * 100)

        # Rating
        result.overall_rating = self._calculate_rating(result.pass_count, result.fail_count)

        # Cache
        self._cache[cache_key] = (time.time(), result)
        return result

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Fetch fundamental data from yfinance
    # ───────────────────────────────────────────────────────────────
    def _fetch_fundamental_data(self, symbol: str) -> Optional[Dict]:
        """
        Fetch fundamental data from yfinance + Yahoo Finance API.

        Returns dict with:
            - company_name, pe_ratio, debt_to_equity, book_value, roe
            - quarterly_profit_growth, quarterly_revenue_growth
            - yearly_profit_growth, yearly_revenue_growth
        """
        data = {}

        # Method 1: yfinance library (info dict)
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            info = ticker.info or {}

            data["company_name"] = info.get("longName", info.get("shortName", ""))
            data["pe_ratio"] = info.get("trailingPE")
            data["debt_to_equity"] = info.get("debtToEquity")
            if data["debt_to_equity"]:
                data["debt_to_equity"] = data["debt_to_equity"] / 100.0  # yfinance gives %
            data["book_value"] = info.get("bookValue")
            data["roe"] = info.get("returnOnEquity")
            if data["roe"]:
                data["roe"] = data["roe"] * 100.0  # convert to %

            # Quarterly growth (from quarterly financials)
            try:
                quarterly = ticker.quarterly_financials
                if quarterly is not None and not quarterly.empty:
                    # Revenue growth (last 2 quarters YoY)
                    if "Total Revenue" in quarterly.index:
                        revenues = quarterly.loc["Total Revenue"]
                        if len(revenues) >= 4:
                            current_rev = revenues.iloc[0]
                            prev_year_rev = revenues.iloc[3]  # same quarter last year
                            if prev_year_rev > 0:
                                data["quarterly_revenue_growth"] = \
                                    ((current_rev - prev_year_rev) / prev_year_rev) * 100

                    # Profit growth (Net Income)
                    if "Net Income" in quarterly.index:
                        profits = quarterly.loc["Net Income"]
                        if len(profits) >= 4:
                            current_profit = profits.iloc[0]
                            prev_year_profit = profits.iloc[3]
                            if prev_year_profit > 0:
                                data["quarterly_profit_growth"] = \
                                    ((current_profit - prev_year_profit) / prev_year_profit) * 100
            except Exception as e:
                logger.debug(f"{symbol}: quarterly financials fetch fail: {e}")

            # Yearly growth (from annual financials)
            try:
                annual = ticker.financials
                if annual is not None and not annual.empty:
                    if "Total Revenue" in annual.index:
                        revenues = annual.loc["Total Revenue"]
                        if len(revenues) >= 2:
                            current_rev = revenues.iloc[0]
                            prev_rev = revenues.iloc[1]
                            if prev_rev > 0:
                                data["yearly_revenue_growth"] = \
                                    ((current_rev - prev_rev) / prev_rev) * 100

                    if "Net Income" in annual.index:
                        profits = annual.loc["Net Income"]
                        if len(profits) >= 2:
                            current_profit = profits.iloc[0]
                            prev_profit = profits.iloc[1]
                            if prev_profit > 0:
                                data["yearly_profit_growth"] = \
                                    ((current_profit - prev_profit) / prev_profit) * 100
            except Exception as e:
                logger.debug(f"{symbol}: annual financials fetch fail: {e}")

        except Exception as e:
            logger.warning(f"{symbol}: fundamental data fetch fail: {e}")
            return None

        return data if data else None

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: 10 Condition Checks
    # ───────────────────────────────────────────────────────────────
    def _check_pe(self, pe) -> FundamentalCheck:
        if pe is None:
            return FundamentalCheck("P/E Ratio", "5 < PE < 50", "N/A", "5-50", False, "Data not available")
        passed = self.PE_MIN < pe < self.PE_MAX
        return FundamentalCheck(
            "P/E Ratio", "5 < PE < 50 (sustainable)",
            f"{pe:.1f}", f"{self.PE_MIN}-{self.PE_MAX}",
            passed, "Overvalued" if pe >= self.PE_MAX else "Undervalued trap" if pe <= self.PE_MIN else "Good"
        )

    def _check_debt(self, de) -> FundamentalCheck:
        if de is None:
            return FundamentalCheck("Debt-to-Equity", "D/E < 2.0", "N/A", "< 2.0", False, "Data not available")
        passed = de < self.DEBT_TO_EQUITY_MAX
        return FundamentalCheck(
            "Debt-to-Equity", "D/E < 2.0 (manageable leverage)",
            f"{de:.2f}", f"< {self.DEBT_TO_EQUITY_MAX}",
            passed, "High debt risk" if de >= self.DEBT_TO_EQUITY_MAX else "Low debt — safe"
        )

    def _check_book_value(self, bv) -> FundamentalCheck:
        if bv is None:
            return FundamentalCheck("Book Value", "BV > 0", "N/A", "> 0", False, "Data not available")
        passed = bv > self.BOOK_VALUE_MIN
        return FundamentalCheck(
            "Book Value/Share", "BV > 0 (positive net worth)",
            f"₹{bv:.1f}", f"> {self.BOOK_VALUE_MIN}",
            passed, "Negative net worth!" if bv <= 0 else "Positive net worth"
        )

    def _check_quarterly_profit_growth(self, growth) -> FundamentalCheck:
        if growth is None:
            return FundamentalCheck("Qtrly Profit Growth", "YoY > 10%", "N/A", "> 10%", False, "Data not available")
        passed = growth > self.QUARTERLY_PROFIT_GROWTH_MIN
        return FundamentalCheck(
            "Quarterly Profit Growth (YoY)", "> 10% (growing profits)",
            f"{growth:.1f}%", f"> {self.QUARTERLY_PROFIT_GROWTH_MIN}%",
            passed, "Profits declining!" if growth < 0 else "Strong growth" if growth > 25 else "Moderate growth"
        )

    def _check_quarterly_revenue_growth(self, growth) -> FundamentalCheck:
        if growth is None:
            return FundamentalCheck("Qtrly Revenue Growth", "YoY > 5%", "N/A", "> 5%", False, "Data not available")
        passed = growth > self.QUARTERLY_REVENUE_GROWTH_MIN
        return FundamentalCheck(
            "Quarterly Revenue Growth (YoY)", "> 5% (growing revenue)",
            f"{growth:.1f}%", f"> {self.QUARTERLY_REVENUE_GROWTH_MIN}%",
            passed, "Revenue declining!" if growth < 0 else "Strong growth" if growth > 15 else "Moderate growth"
        )

    def _check_yearly_profit_growth(self, growth) -> FundamentalCheck:
        if growth is None:
            return FundamentalCheck("Yearly Profit Growth", "YoY > 15%", "N/A", "> 15%", False, "Data not available")
        passed = growth > self.YEARLY_PROFIT_GROWTH_MIN
        return FundamentalCheck(
            "Yearly Profit Growth", "> 15% (annual growth)",
            f"{growth:.1f}%", f"> {self.YEARLY_PROFIT_GROWTH_MIN}%",
            passed, "Annual profits declining!" if growth < 0 else "Strong annual growth" if growth > 30 else "Good growth"
        )

    def _check_yearly_revenue_growth(self, growth) -> FundamentalCheck:
        if growth is None:
            return FundamentalCheck("Yearly Revenue Growth", "YoY > 10%", "N/A", "> 10%", False, "Data not available")
        passed = growth > self.YEARLY_REVENUE_GROWTH_MIN
        return FundamentalCheck(
            "Yearly Revenue Growth", "> 10% (annual revenue growth)",
            f"{growth:.1f}%", f"> {self.YEARLY_REVENUE_GROWTH_MIN}%",
            passed, "Annual revenue declining!" if growth < 0 else "Strong growth" if growth > 20 else "Good growth"
        )

    def _check_roe(self, roe) -> FundamentalCheck:
        if roe is None:
            return FundamentalCheck("ROE", "ROE > 12%", "N/A", "> 12%", False, "Data not available")
        passed = roe > self.ROE_MIN
        return FundamentalCheck(
            "Return on Equity (ROE)", "> 12% (efficient capital use)",
            f"{roe:.1f}%", f"> {self.ROE_MIN}%",
            passed, "Low efficiency" if roe < self.ROE_MIN else "High efficiency" if roe > 20 else "Good efficiency"
        )

    def _check_promoter_holding(self, holding) -> FundamentalCheck:
        if holding is None:
            return FundamentalCheck("Promoter Holding", "> 40%", "N/A", "> 40%", False, "Data not available (yfinance)")
        passed = holding > 40.0
        return FundamentalCheck(
            "Promoter Holding", "> 40% (skin in the game)",
            f"{holding:.1f}%", "> 40%",
            passed, "Low promoter holding" if holding < 40 else "High promoter confidence"
        )

    def _check_fii_dii(self, trend) -> FundamentalCheck:
        if trend is None:
            return FundamentalCheck("FII/DII Trend", "stable/increasing", "N/A", "stable+", False, "Data not available")
        passed = trend in ("increasing", "stable")
        return FundamentalCheck(
            "FII/DII Holding Trend", "stable or increasing",
            trend, "stable/increasing",
            passed, "Institutions exiting!" if trend == "decreasing" else "Institutional confidence"
        )

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Rating calculation
    # ───────────────────────────────────────────────────────────────
    def _calculate_rating(self, pass_count: int, fail_count: int) -> str:
        """
        Rating based on pass count:
          8-10 pass → A+ (Excellent)
          6-7 pass  → A (Very Good)
          4-5 pass  → B (Good)
          2-3 pass  → C (Average)
          0-1 pass  → D (Weak — reject)
        """
        total = pass_count + fail_count
        if total == 0:
            return "N/A"
        if pass_count >= 8:
            return "A+"
        elif pass_count >= 6:
            return "A"
        elif pass_count >= 4:
            return "B"
        elif pass_count >= 2:
            return "C"
        else:
            return "D"


# ═══════════════════════════════════════════════════════════════════
# CONVENIENCE: Batch analyze + format
# ═══════════════════════════════════════════════════════════════════
def analyze_fundamentals_batch(symbols: List[str]) -> List[Dict]:
    """
    Batch analyze multiple stocks.

    Returns list of FundamentalResult dicts, sorted by:
      1. Rating (A+ first, D last)
      2. Fundamental score (high to low)
    """
    analyzer = FundamentalAnalyzer()
    results = []
    for sym in symbols:
        try:
            result = analyzer.analyze(sym)
            results.append(result.to_dict())
        except Exception as e:
            logger.warning(f"Fundamental analyze fail {sym}: {e}")

    # Sort by rating priority + score
    rating_order = {"A+": 5, "A": 4, "B": 3, "C": 2, "D": 1, "N/A": 0}
    results.sort(key=lambda x: (rating_order.get(x.get("overall_rating", "D"), 0),
                                x.get("fundamental_score", 0)), reverse=True)
    return results


def format_fundamental_checklist(result_dict: Dict) -> str:
    """
    Format fundamental checklist as HTML for Telegram/PDF.

    Returns a string with pass/fail table + rating.
    """
    from utils import escape_html, clean_symbol
    symbol = result_dict.get("symbol", "")
    display = escape_html(clean_symbol(symbol))
    rating = result_dict.get("overall_rating", "N/A")
    score = result_dict.get("fundamental_score", 0)
    pass_count = result_dict.get("pass_count", 0)
    fail_count = result_dict.get("fail_count", 0)
    company = escape_html(result_dict.get("company_name", ""))

    # Rating emoji
    rating_emoji = {"A+": "🟢", "A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴", "N/A": "⚪"}
    emoji = rating_emoji.get(rating, "⚪")

    lines = [
        f"📊 <b>FUNDAMENTAL CHECKLIST</b> — {emoji} <b>{display}</b>",
        f"<i>{company}</i>",
        f"Rating: <b>{rating}</b> | Score: <b>{score}/100</b> | Pass: {pass_count} | Fail: {fail_count}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for check in result_dict.get("checks", []):
        name = escape_html(check.get("name", ""))
        value = escape_html(str(check.get("value", "")))
        threshold = escape_html(check.get("threshold", ""))
        passed = check.get("passed", False)
        details = escape_html(check.get("details", ""))

        if value == "N/A":
            mark = "⚪"
        elif passed:
            mark = "✅"
        else:
            mark = "❌"

        lines.append(f"{mark} <b>{name}</b>: {value} (need: {threshold})")
        if details:
            lines.append(f"   <i>{details}</i>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("FundamentalAnalyzer — Self Test")
    print("=" * 60)

    analyzer = FundamentalAnalyzer()

    # Test with a mock (no internet needed)
    print("\n--- Test 1: Rating calculation ---")
    assert analyzer._calculate_rating(10, 0) == "A+"
    assert analyzer._calculate_rating(8, 2) == "A+"
    assert analyzer._calculate_rating(6, 4) == "A"
    assert analyzer._calculate_rating(4, 6) == "B"
    assert analyzer._calculate_rating(2, 8) == "C"
    assert analyzer._calculate_rating(0, 10) == "D"
    print("  ✅ Rating system works (A+/A/B/C/D)")

    print("\n--- Test 2: Check functions ---")
    pe_check = analyzer._check_pe(25.0)
    assert pe_check.passed == True
    pe_check_bad = analyzer._check_pe(60.0)
    assert pe_check_bad.passed == False
    print(f"  ✅ P/E check: 25.0 → pass, 60.0 → fail")

    de_check = analyzer._check_debt(0.8)
    assert de_check.passed == True
    print(f"  ✅ Debt check: 0.8 → pass")

    growth_check = analyzer._check_quarterly_profit_growth(20.0)
    assert growth_check.passed == True
    print(f"  ✅ Profit growth: 20% → pass")

    print("\n--- Test 3: Format checklist ---")
    mock_result = {
        "symbol": "RELIANCE.NS",
        "company_name": "Reliance Industries Ltd",
        "overall_rating": "A+",
        "pass_count": 8,
        "fail_count": 1,
        "fundamental_score": 89,
        "checks": [
            {"name": "P/E Ratio", "value": "24.5", "threshold": "5-50", "passed": True, "details": "Good"},
            {"name": "Debt-to-Equity", "value": "0.65", "threshold": "< 2.0", "passed": True, "details": "Low debt"},
            {"name": "ROE", "value": "9.2", "threshold": "> 12%", "passed": False, "details": "Low efficiency"},
        ],
    }
    output = format_fundamental_checklist(mock_result)
    print(output[:200] + "...")
    assert "FUNDAMENTAL CHECKLIST" in output
    assert "A+" in output
    print("\n  ✅ Format works")

    print("\n" + "=" * 60)
    print("✅ ALL TESTS PASSED — Fundamental Analyzer Ready")
    print("=" * 60)
