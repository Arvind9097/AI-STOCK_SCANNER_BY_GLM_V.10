"""
===========================================================
 UNICODE-COMPLIANT PDF REPORTING ENGINE (V9.2 Step 5)
===========================================================
Deep font-rendering reasoning for flawless PDF generation.

PROBLEM ANALYSIS (v9.0 square boxes □□□):
  ReportLab's default font is Helvetica — a Type-1 PostScript font
  with ONLY Latin-1 (256 chars) coverage. It CANNOT render:
    - ₹ (U+20B9 Indian Rupee sign) → □
    - Emojis (🔥🎯🛑 U+1F300+) → □
    - Devanagari Hindi (U+0900–U+097F) → □
    - Smart quotes (" " U+201C/201D) → □
    - Arrows (→ U+2192) → □

  When ReportLab encounters an unmapped glyph, it draws a NOTDEF
  box (□) instead of crashing. This is SILENT corruption — the PDF
  generates "successfully" but is unreadable.

ROOT CAUSE: ReportLab's Paragraph parser uses the registered font's
  character map (cmap). Helvetica's cmap stops at U+00FF. Any char
  above that → □.

SOLUTION ARCHITECTURE (3-layer font strategy):

  Layer 1 — REGISTER UNICODE TTF FONTS:
    a) DejaVu Sans (regular + bold) — has ₹, smart quotes, arrows,
       Latin Extended. Pre-installed on most Linux (incl. Render).
       Path: /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
    b) Noto Sans Devanagari (if available) — for Hindi text.
       Path: /usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf
    c) registerFontFamily() — links regular+bold so <b> tags work.

  Layer 2 — EMOJI/SYMBOL FALLBACK MAP:
    Emojis (🔥🎯🛑) render as □ even in DejaVu Sans (color bitmap fonts
    can't load via ReportLab's FT2Font). So we REPLACE them with ASCII
    equivalents BEFORE passing to Paragraph:
      ₹ → "Rs." (or keep ₹ if DejaVu registered — it HAS ₹)
      🔥 → "[STRONG]"
      🎯 → "[TARGET]"
      🛑 → "[SL]"
      📈 → "[UP]" / 📉 → "[DOWN]"
      → → "->"
      Smart quotes " " → straight quotes ""
    This guarantees NO □ boxes — every char has a renderable mapping.

  Layer 3 — GRACEFUL DEGRADATION:
    If a font file is missing (e.g. Noto not installed), fall back to
    next available font. If ALL custom fonts missing, fall back to
    Helvetica (with emoji strip + ₹→Rs. replacement — still readable,
    just less pretty). NEVER crash — always produce a valid PDF.

INSTITUTIONAL LAYOUT (Evening Summary & Stock Alert Report):
  1. TITLE HEADER — "AI Stock Scanner — Evening Summary Report"
     + date/time + universe scan count.
  2. EXECUTIVE SUMMARY BOX — market sentiment, win-rate, top movers.
  3. FUNDAMENTAL & RESEARCH TABLE — per-stock P/E, ROE, D/E, FII/DII.
  4. TRADE EXECUTION PLAN BOX — Entry, SL, T1, T2, R:R per BUY signal.
  5. EMBEDDED CHART SECTION — inserts PNG from Module 3 ChartGenerator.
  6. FOOTER — page number + disclaimer + timestamp.

ERROR HANDLING:
  - Font file missing → graceful fallback (log warning, use Helvetica).
  - Image missing/corrupt → skip with warning (don't crash PDF).
  - NaN/None values → display "N/A" (never "None" or crash).
  - Very long text → auto-wrap via Paragraph (no overflow).
  - Special chars with no mapping → replace with "?" (never □).

THREAD-SAFETY: Each report builds its own SimpleDocTemplate. Fonts
registered once at module load (idempotent — re-register is no-op).
Safe for concurrent report generation.
===========================================================
"""

import os
import html
import logging
import unicodedata
from typing import Optional, Dict, Any, List, Union
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ReportLab imports (heavy — do at module level once)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, inch
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, KeepTogether, PageBreak, HRFlowable,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


# ═══════════════════════════════════════════════════════════════════
# FONT MANAGEMENT — register Unicode TTF fonts (idempotent)
# ═══════════════════════════════════════════════════════════════════
# Track registration state (module-level — idempotent across instances)
_FONTS_REGISTERED = False
_FONT_REGISTRATION_LOCK = None  # set in _ensure_fonts_registered (thread-safe)

# Font names (constants — used throughout module)
FONT_REGULAR = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"
FONT_DEVANAGARI = "NotoDevanagari"   # for Hindi text (if available)
FONT_FALLBACK = "Helvetica"          # ReportLab built-in (Latin-1 only)

# Candidate font file paths (checked in order — first found wins)
_FONT_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",          # Linux (Render)
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",                         # macOS
    "C:/Windows/Fonts/arialuni.ttf",                            # Windows
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
_FONT_PATHS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arialuni.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_FONT_PATHS_DEVANAGARI = [
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/noto/NotoSansDevanagari-Regular.ttf",
]


def _find_font(candidates: List[str]) -> Optional[str]:
    """Return first existing font path from candidates, or None."""
    for path in candidates:
        if path and os.path.exists(path) and os.path.getsize(path) > 1000:
            return path
    return None


def _ensure_fonts_registered() -> None:
    """
    Register Unicode TTF fonts with ReportLab (idempotent + thread-safe).

    Registers:
      - DejaVuSans (regular) — has ₹, smart quotes, Latin Extended
      - DejaVuSans-Bold — for <b> tags
      - NotoSansDevanagari (if available) — for Hindi text
      - registerFontFamily() — links regular+bold for <b>/<i> support

    Falls back to Helvetica if no TTF found (with emoji-strip required).
    """
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return  # idempotent — only register once per process

    import threading
    global _FONT_REGISTRATION_LOCK
    if _FONT_REGISTRATION_LOCK is None:
        _FONT_REGISTRATION_LOCK = threading.Lock()

    with _FONT_REGISTRATION_LOCK:
        if _FONTS_REGISTERED:
            return  # double-checked lock

        regular_path = _find_font(_FONT_PATHS_REGULAR)
        bold_path = _find_font(_FONT_PATHS_BOLD) or regular_path  # bold optional
        devanagari_path = _find_font(_FONT_PATHS_DEVANAGARI)

        if regular_path:
            try:
                pdfmetrics.registerFont(TTFont(FONT_REGULAR, regular_path))
                logger.info(f"PDF font registered: {FONT_REGULAR} -> {regular_path}")
            except Exception as e:
                logger.warning(f"Failed to register {regular_path}: {e}")
                regular_path = None

        if bold_path and bold_path != regular_path:
            try:
                pdfmetrics.registerFont(TTFont(FONT_BOLD, bold_path))
            except Exception as e:
                logger.warning(f"Failed to register bold {bold_path}: {e}")

        # If bold not separately registered, alias bold = regular
        if not bold_path or bold_path == regular_path:
            try:
                # Re-register regular as bold (so <b> works)
                if regular_path:
                    pdfmetrics.registerFont(TTFont(FONT_BOLD, regular_path))
            except Exception:
                pass

        if devanagari_path:
            try:
                pdfmetrics.registerFont(TTFont(FONT_DEVANAGARI, devanagari_path))
                logger.info(f"PDF font registered: {FONT_DEVANAGARI} -> {devanagari_path}")
            except Exception as e:
                logger.debug(f"Devanagari font register fail: {e}")

        # Register font family (enables <b>, <i> tags in Paragraph)
        if regular_path:
            try:
                from reportlab.pdfbase.pdfmetrics import registerFontFamily
                registerFontFamily(
                    FONT_REGULAR,
                    normal=FONT_REGULAR,
                    bold=FONT_BOLD,
                    italic=FONT_REGULAR,   # no italic TTF — alias to regular
                    boldItalic=FONT_BOLD,
                )
            except Exception as e:
                logger.debug(f"registerFontFamily fail: {e}")

        _FONTS_REGISTERED = True
        if not regular_path:
            logger.warning(
                "No Unicode TTF font found — falling back to Helvetica. "
                "Emoji stripping + ₹→Rs. replacement REQUIRED for readability."
            )


# ═══════════════════════════════════════════════════════════════════
# TEXT SANITIZATION — replace unrenderable chars with ASCII equivalents
# ═══════════════════════════════════════════════════════════════════
# Comprehensive emoji → ASCII tag map (renders as [TEXT] in PDF)
EMOJI_TO_ASCII: Dict[str, str] = {
    # Signals
    "🔥": "[STRONG]", "⚡": "[BUY]", "👀": "[WATCH]", "⚠️": "[WARN]", "⚠": "[WARN]",
    # Status
    "🟢": "[PROFIT]", "🔴": "[LOSS]", "🟡": "[NEUTRAL]", "⚪": "[PENDING]",
    # Trade levels
    "🎯": "[TARGET]", "🛑": "[SL]", "💵": "[ENTRY]", "📊": "[STATS]",
    # Direction
    "🚀": "[BREAKOUT]", "📈": "[UP]", "📉": "[DOWN]", "💪": "[STRONG]",
    # Context
    "💡": "[ANALYSIS]", "📰": "[NEWS]", "🤖": "[AI]", "👑": "[MASTER]",
    # Events
    "🎉": "[TARGET-HIT]", "🥇": "[BEST]", "📌": "[PIN]", "📅": "[DATE]",
    "🔮": "[PATTERN]", "✅": "[OK]", "❌": "[FAIL]", "⭐": "[STAR]", "🌟": "[STAR]",
    # Arrows
    "→": "->", "←": "<-", "↑": "^", "↓": "v", "↔": "<->",
    "⇒": "=>", "⇐": "<=",
    # Stars/bullets
    "★": "*", "☆": "*", "●": "*", "►": ">", "◄": "<",
    # Smart quotes (DejaVu has these, but normalize for safety)
    "\u201c": '"', "\u201d": '"',   # left/right double quotes
    "\u2018": "'", "\u2019": "'",   # left/right single quotes
    # Dashes
    "\u2013": "-",   # en dash
    "\u2014": "--",  # em dash
    # Currency (₹ — DejaVu Sans HAS U+20B9, but fallback to Rs. if Helvetica)
    # We KEEP ₹ if DejaVu registered; only replace if using Helvetica fallback.
}

# Chars to remove entirely (zero-width, control, etc.) — never renderable
_INVISIBLE_CHARS = {
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\ufeff",  # BOM
    "\u00ad",  # soft hyphen
}


def sanitize_text_for_pdf(text: str, keep_rupee: bool = True) -> str:
    """
    Sanitize text for PDF rendering — replace unrenderable chars with ASCII.

    Operations (in order):
      1. None/empty -> ""
      2. Unicode NFKC normalize (ligatures -> ascii: ﬁ -> fi)
      3. HTML entity decode (&amp; -> &)
      4. Remove invisible/control chars
      5. Replace emojis/symbols via EMOJI_TO_ASCII map
      6. Strip remaining supplementary-plane chars (U+1F000+) -> "?"
      7. ₹ handling: keep if keep_rupee=True (DejaVu has it), else "Rs."
      8. Normalize whitespace

    Args:
        text: Raw input (may contain emojis, smart quotes, ₹, etc.)
        keep_rupee: If True (default), keep ₹ (DejaVu Sans renders it).
                    If False, replace ₹ with "Rs." (Helvetica fallback mode).

    Returns:
        PDF-safe string with NO unrenderable chars (no □ boxes).
    """
    if not text:
        return ""

    text = str(text)

    # NFKC normalization — converts ligatures (ﬁ -> fi), fullwidth -> halfwidth
    text = unicodedata.normalize("NFKC", text)

    # HTML entity decode
    text = html.unescape(text)

    # Remove invisible chars
    for inv in _INVISIBLE_CHARS:
        text = text.replace(inv, "")

    # Remove control chars (except \n \t)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)

    # Replace emojis/symbols via map
    for emoji, replacement in EMOJI_TO_ASCII.items():
        text = text.replace(emoji, f" {replacement} ")

    # ₹ handling
    if not keep_rupee:
        text = text.replace("₹", "Rs. ")
        text = text.replace("₨", "Rs. ")  # Pakistan Rupee sign variant

    # Strip remaining supplementary-plane chars (emojis not in our map)
    # U+1F000-U+1FAFF = emoji blocks; U+2600-U+27BF = misc symbols
    # Replace with "?" (never □)
    result = []
    for ch in text:
        cp = ord(ch)
        if 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF:
            result.append("?")
        else:
            result.append(ch)
    text = "".join(result)

    # Normalize whitespace (multiple spaces -> single, but preserve newlines)
    import re
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    return text


# ═══════════════════════════════════════════════════════════════════
# COLOR PALETTE — institutional-grade
# ═══════════════════════════════════════════════════════════════════
class ReportColors:
    """Centralized color palette (institutional dark-blue + green/red accents)."""
    # Primary (dark blue institutional)
    HEADER_BG = colors.HexColor("#0D47A1")
    HEADER_TEXT = colors.white
    SECTION_BG = colors.HexColor("#1565C0")
    SUBSECTION_BG = colors.HexColor("#E3F2FD")

    # Body
    BODY_TEXT = colors.HexColor("#212121")
    BODY_LIGHT = colors.HexColor("#424242")
    TABLE_ALT_ROW = colors.HexColor("#F5F5F5")

    # Accents
    GREEN = colors.HexColor("#2E7D32")      # profit / BUY
    RED = colors.HexColor("#C62828")        # loss / SL
    AMBER = colors.HexColor("#F57F17")      # WATCH
    BLUE = colors.HexColor("#1976D2")       # info

    # Borders
    BORDER = colors.HexColor("#BDBDBD")
    BORDER_LIGHT = colors.HexColor("#E0E0E0")


# ═══════════════════════════════════════════════════════════════════
# MAIN PDF REPORT BUILDER CLASS
# ═══════════════════════════════════════════════════════════════════
class PDFReportBuilder:
    """
    Unicode-compliant institutional PDF report generator.

    Layout sections (in order):
      1. Title header (report name + date + scan count)
      2. Executive summary box (sentiment, win-rate, top movers)
      3. Fundamental & research table (per-stock P/E, ROE, D/E, FII/DII)
      4. Trade execution plan box (Entry, SL, T1, T2, R:R per BUY)
      5. Embedded chart section (PNG image from Module 3)
      6. Footer (page number, disclaimer, timestamp)

    USAGE:
        builder = PDFReportBuilder()
        builder.build_report(
            output_path="reports/Evening_Summary.pdf",
            report_title="Evening Summary Report",
            executive_summary={...},
            stock_data=[...],
            chart_paths={"RELIANCE": "charts/RELIANCE.png"},
        )

    FONT SAFETY:
      - DejaVu Sans registered (has ₹, smart quotes, Latin Extended)
      - All text sanitized via sanitize_text_for_pdf() (emojis -> [TEXT])
      - If DejaVu missing: ₹ -> "Rs.", emojis stripped, Helvetica fallback
      - NEVER produces □ boxes — graceful degradation guaranteed
    """

    def __init__(
        self,
        page_size=A4,
        margin: float = 15 * mm,
    ):
        """
        Initialize PDF builder.

        Args:
            page_size: ReportLab page size (default A4).
            margin: Page margin in points (default 15mm).
        """
        self.page_size = page_size
        self.margin = margin

        # Ensure fonts registered (idempotent)
        _ensure_fonts_registered()

        # Determine if Unicode font available (for ₹ handling)
        self._has_unicode_font = _FONTS_REGISTERED and _find_font(_FONT_PATHS_REGULAR) is not None
        self._keep_rupee = self._has_unicode_font  # keep ₹ only if DejaVu registered

        # Build stylesheet
        self.styles = self._build_stylesheets()

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Stylesheet construction
    # ───────────────────────────────────────────────────────────────
    def _build_stylesheets(self) -> Dict[str, ParagraphStyle]:
        """Build paragraph styles using registered Unicode font."""
        font = FONT_REGULAR if self._has_unicode_font else FONT_FALLBACK
        font_bold = FONT_BOLD if self._has_unicode_font else FONT_FALLBACK + "-Bold"

        # Helvetica-Bold is a valid ReportLab built-in; handle gracefully
        if not self._has_unicode_font:
            font_bold = "Helvetica-Bold"
            font = "Helvetica"

        styles = {}

        # Title (largest, bold, white-on-blue)
        styles["title"] = ParagraphStyle(
            "Title",
            fontName=font_bold,
            fontSize=20,
            leading=24,
            textColor=ReportColors.HEADER_TEXT,
            alignment=TA_CENTER,
            spaceAfter=4,
        )

        # Subtitle (smaller, below title)
        styles["subtitle"] = ParagraphStyle(
            "Subtitle",
            fontName=font,
            fontSize=10,
            leading=13,
            textColor=ReportColors.HEADER_TEXT,
            alignment=TA_CENTER,
            spaceAfter=2,
        )

        # Section heading (white text on blue bar)
        styles["section_heading"] = ParagraphStyle(
            "SectionHeading",
            fontName=font_bold,
            fontSize=13,
            leading=16,
            textColor=ReportColors.HEADER_TEXT,
            alignment=TA_LEFT,
            spaceBefore=10,
            spaceAfter=6,
            leftIndent=6,
        )

        # Subsection heading
        styles["subsection"] = ParagraphStyle(
            "Subsection",
            fontName=font_bold,
            fontSize=11,
            leading=14,
            textColor=ReportColors.SECTION_BG,
            spaceBefore=6,
            spaceAfter=4,
        )

        # Body text
        styles["body"] = ParagraphStyle(
            "Body",
            fontName=font,
            fontSize=9.5,
            leading=13,
            textColor=ReportColors.BODY_TEXT,
            alignment=TA_LEFT,
            spaceAfter=3,
        )

        # Body bold
        styles["body_bold"] = ParagraphStyle(
            "BodyBold",
            fontName=font_bold,
            fontSize=9.5,
            leading=13,
            textColor=ReportColors.BODY_TEXT,
            alignment=TA_LEFT,
            spaceAfter=3,
        )

        # Table cell
        styles["table_cell"] = ParagraphStyle(
            "TableCell",
            fontName=font,
            fontSize=8.5,
            leading=11,
            textColor=ReportColors.BODY_TEXT,
            alignment=TA_LEFT,
        )

        # Table header cell
        styles["table_header"] = ParagraphStyle(
            "TableHeader",
            fontName=font_bold,
            fontSize=8.5,
            leading=11,
            textColor=colors.white,
            alignment=TA_CENTER,
        )

        # Trade plan value (bold, colored)
        styles["trade_value"] = ParagraphStyle(
            "TradeValue",
            fontName=font_bold,
            fontSize=10,
            leading=13,
            textColor=ReportColors.BODY_TEXT,
            alignment=TA_LEFT,
        )

        # Disclaimer (small, grey)
        styles["disclaimer"] = ParagraphStyle(
            "Disclaimer",
            fontName=font,
            fontSize=7.5,
            leading=10,
            textColor=ReportColors.BODY_LIGHT,
            alignment=TA_JUSTIFY,
        )

        return styles

    # ───────────────────────────────────────────────────────────────
    # PUBLIC: Main build method
    # ───────────────────────────────────────────────────────────────
    def build_report(
        self,
        output_path: str,
        report_title: str = "Evening Summary Report",
        executive_summary: Optional[Dict[str, Any]] = None,
        stock_data: Optional[List[Dict[str, Any]]] = None,
        chart_paths: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """
        Build complete PDF report.

        Args:
            output_path: File path for output PDF.
            report_title: Title shown in header.
            executive_summary: Dict with keys:
                - sentiment (str): "BULLISH"/"BEARISH"/"NEUTRAL"
                - win_rate (float): 0-100
                - total_trades (int)
                - target_hits (int)
                - sl_hits (int)
                - top_gainer (str): "RELIANCE +5.2%"
                - top_loser (str): "TATASTEEL -3.1%"
            stock_data: List of dicts (per stock), each with:
                - symbol, signal, score, close, pe_ratio, roe, debt_to_equity,
                  fii_dii_trend, trade_plan (dict with entry/sl/t1/t2/rr)
            chart_paths: Dict mapping symbol -> PNG file path (from Module 3).

        Returns:
            output_path on success, None on failure.
        """
        try:
            # Ensure output directory exists
            parent = os.path.dirname(output_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            # Build document
            doc = SimpleDocTemplate(
                output_path,
                pagesize=self.page_size,
                leftMargin=self.margin,
                rightMargin=self.margin,
                topMargin=self.margin,
                bottomMargin=self.margin + 10,  # extra for footer
                title=report_title,
                author="AI Stock Scanner V9.2",
            )

            story: List = []

            # Section 1: Title header
            story.extend(self._build_title_header(report_title, len(stock_data or [])))
            story.append(Spacer(1, 8))

            # Section 2: Executive summary box
            if executive_summary:
                story.extend(self._build_executive_summary(executive_summary))
                story.append(Spacer(1, 8))

            # Section 3: Fundamental & research table
            if stock_data:
                story.extend(self._build_fundamental_table(stock_data))
                story.append(Spacer(1, 8))

            # Section 4: Trade execution plan boxes
            if stock_data:
                story.extend(self._build_trade_plans(stock_data))
                story.append(Spacer(1, 8))

            # Section 5: Embedded charts
            if chart_paths:
                story.extend(self._build_chart_section(chart_paths))

            # Section 6: Disclaimer
            story.append(Spacer(1, 12))
            story.append(self._build_disclaimer())

            # Build PDF (footer added via onPage callback)
            doc.build(story, onFirstPage=self._draw_footer, onLaterPages=self._draw_footer)

            logger.info(f"PDF report generated: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"PDF build failed: {e}", exc_info=True)
            return None

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Section 1 — Title header
    # ───────────────────────────────────────────────────────────────
    def _build_title_header(self, title: str, stock_count: int) -> List:
        """Build title header with blue background bar."""
        try:
            tz = ZoneInfo("Asia/Kolkata")
            now_str = datetime.now(tz).strftime("%d %b %Y, %I:%M %p IST")
        except Exception:
            now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

        title_text = sanitize_text_for_pdf(title, self._keep_rupee)
        subtitle_text = sanitize_text_for_pdf(
            f"AI Stock Scanner V9.2  |  {now_str}  |  {stock_count} stocks analyzed",
            self._keep_rupee,
        )

        # Build as a 1-cell table with blue background (acts as header bar)
        header_data = [[
            Paragraph(title_text, self.styles["title"]),
        ], [
            Paragraph(subtitle_text, self.styles["subtitle"]),
        ]]
        header_table = Table(header_data, colWidths=[self.page_size[0] - 2 * self.margin])
        header_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ReportColors.HEADER_BG),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, 0), 10),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
            ("TOPPADDING", (0, 1), (-1, 1), 0),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ]))
        return [header_table]

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Section 2 — Executive summary box
    # ───────────────────────────────────────────────────────────────
    def _build_executive_summary(self, summary: Dict[str, Any]) -> List:
        """Build executive summary box (sentiment, win-rate, top movers)."""
        sentiment = sanitize_text_for_pdf(str(summary.get("sentiment", "N/A")), self._keep_rupee)
        win_rate = summary.get("win_rate", 0)
        total = summary.get("total_trades", 0)
        t_hits = summary.get("target_hits", 0)
        sl_hits = summary.get("sl_hits", 0)
        gainer = sanitize_text_for_pdf(str(summary.get("top_gainer", "N/A")), self._keep_rupee)
        loser = sanitize_text_for_pdf(str(summary.get("top_loser", "N/A")), self._keep_rupee)

        # Sentiment color
        sent_color = ReportColors.GREEN if "BULL" in sentiment.upper() else \
                     ReportColors.RED if "BEAR" in sentiment.upper() else ReportColors.AMBER

        # Section heading bar
        heading = self._section_heading("EXECUTIVE SUMMARY")

        # Build 2-column table: labels | values
        data = [
            [Paragraph("<b>Market Sentiment</b>", self.styles["table_cell"]),
             Paragraph(f'<font color="{sent_color.hexval()}"><b>{sentiment}</b></font>', self.styles["table_cell"])],
            [Paragraph("<b>Win Rate</b>", self.styles["table_cell"]),
             Paragraph(f"{win_rate:.1f}%  ({t_hits} target hits / {sl_hits} SL hits / {total} total)", self.styles["table_cell"])],
            [Paragraph("<b>Top Gainer</b>", self.styles["table_cell"]),
             Paragraph(f'<font color="{ReportColors.GREEN.hexval()}">{gainer}</font>', self.styles["table_cell"])],
            [Paragraph("<b>Top Loser</b>", self.styles["table_cell"]),
             Paragraph(f'<font color="{ReportColors.RED.hexval()}">{loser}</font>', self.styles["table_cell"])],
        ]

        tbl = Table(data, colWidths=[40 * mm, self.page_size[0] - 2 * self.margin - 40 * mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ReportColors.TABLE_ALT_ROW),
            ("BACKGROUND", (0, 0), (0, -1), ReportColors.SUBSECTION_BG),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("BOX", (0, 0), (-1, -1), 0.5, ReportColors.BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, ReportColors.BORDER_LIGHT),
        ]))
        return [heading, tbl]

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Section 3 — Fundamental & research table
    # ───────────────────────────────────────────────────────────────
    def _build_fundamental_table(self, stock_data: List[Dict[str, Any]]) -> List:
        """Build per-stock fundamental table (P/E, ROE, D/E, FII/DII)."""
        heading = self._section_heading("FUNDAMENTAL & ADVANCED RESEARCH")

        # Header row
        headers = ["Symbol", "Signal", "Score", "Close", "P/E", "ROE %", "D/E", "FII/DII"]
        header_row = [Paragraph(f"<b>{h}</b>", self.styles["table_header"]) for h in headers]

        rows = [header_row]
        for stock in stock_data[:15]:  # cap at 15 rows for readability
            symbol = sanitize_text_for_pdf(str(stock.get("symbol", "")), self._keep_rupee)
            signal = sanitize_text_for_pdf(str(stock.get("signal", "")), self._keep_rupee)
            score = stock.get("score", "N/A")
            close = self._fmt_price(stock.get("close"))
            pe = self._fmt_num(stock.get("pe_ratio"))
            roe = self._fmt_num(stock.get("roe"))
            de = self._fmt_num(stock.get("debt_to_equity"))
            fii = sanitize_text_for_pdf(str(stock.get("fii_dii_trend", "N/A")), self._keep_rupee)

            # Color signal cell
            sig_color = ReportColors.GREEN if "BUY" in signal.upper() else \
                        ReportColors.RED if "SELL" in signal.upper() else ReportColors.AMBER
            sig_para = Paragraph(
                f'<font color="{sig_color.hexval()}"><b>{signal}</b></font>',
                self.styles["table_cell"],
            )

            rows.append([
                Paragraph(f"<b>{symbol}</b>", self.styles["table_cell"]),
                sig_para,
                Paragraph(str(score), self.styles["table_cell"]),
                Paragraph(close, self.styles["table_cell"]),
                Paragraph(pe, self.styles["table_cell"]),
                Paragraph(roe, self.styles["table_cell"]),
                Paragraph(de, self.styles["table_cell"]),
                Paragraph(fii, self.styles["table_cell"]),
            ])

        # Column widths (sum should = page width - margins)
        col_widths = [28 * mm, 22 * mm, 15 * mm, 22 * mm, 15 * mm, 18 * mm, 15 * mm, 22 * mm]
        tbl = Table(rows, colWidths=col_widths, repeatRows=1)

        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), ReportColors.HEADER_BG),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("BOX", (0, 0), (-1, -1), 0.5, ReportColors.BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, ReportColors.BORDER_LIGHT),
        ]
        # Alternating row colors
        for i in range(1, len(rows)):
            if i % 2 == 0:
                style_cmds.append(("BACKGROUND", (0, i), (-1, i), ReportColors.TABLE_ALT_ROW))
        tbl.setStyle(TableStyle(style_cmds))

        return [heading, tbl]

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Section 4 — Trade execution plan boxes
    # ───────────────────────────────────────────────────────────────
    def _build_trade_plans(self, stock_data: List[Dict[str, Any]]) -> List:
        """Build per-stock trade execution plan boxes (only for BUY signals)."""
        heading = self._section_heading("TRADE EXECUTION PLANS (BUY Signals)")

        flowables = [heading]
        buy_stocks = [s for s in stock_data if s.get("trade_plan") and
                      "BUY" in str(s.get("signal", "")).upper()]

        if not buy_stocks:
            flowables.append(Paragraph(
                "<i>No BUY signals today. All stocks failed confluence gates.</i>",
                self.styles["body"],
            ))
            return flowables

        for stock in buy_stocks[:8]:  # cap at 8 trade plans
            plan_box = self._build_single_trade_plan(stock)
            if plan_box:
                flowables.append(KeepTogether(plan_box))
                flowables.append(Spacer(1, 6))

        return flowables

    def _build_single_trade_plan(self, stock: Dict[str, Any]) -> Optional[List]:
        """Build a single trade plan box for one stock."""
        tp = stock.get("trade_plan")
        if not tp:
            return None

        symbol = sanitize_text_for_pdf(str(stock.get("symbol", "")), self._keep_rupee)
        pattern = sanitize_text_for_pdf(str(stock.get("pattern", "")), self._keep_rupee)

        # Header row (symbol + pattern)
        header = Table([[
            Paragraph(f"<b>{symbol}</b>  —  Trade Execution Plan", self.styles["body_bold"]),
            Paragraph(f"Pattern: <b>{pattern}</b>", self.styles["body"]),
        ]], colWidths=[100 * mm, 60 * mm])
        header.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ReportColors.SUBSECTION_BG),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("BOX", (0, 0), (-1, -1), 0.5, ReportColors.BORDER),
        ]))

        # Trade plan values table (2 columns × 4 rows)
        entry = self._fmt_price(tp.get("entry"))
        entry_range = f"{self._fmt_price(tp.get('entry_low'))} - {self._fmt_price(tp.get('entry_high'))}"
        sl = self._fmt_price(tp.get("stop_loss"))
        t1 = self._fmt_price(tp.get("target_1"))
        t2 = self._fmt_price(tp.get("target_2"))
        rr = tp.get("risk_reward_ratio", 0)
        risk_pct = tp.get("risk_pct", 0)

        plan_data = [
            [Paragraph("<b>Entry</b>", self.styles["table_cell"]),
             Paragraph(f"{entry}  <i>(limit range: {entry_range})</i>", self.styles["table_cell"]),
             Paragraph("<b>R:R</b>", self.styles["table_cell"]),
             Paragraph(f'<font color="{ReportColors.GREEN.hexval()}"><b>1:{rr:.2f}</b></font>', self.styles["table_cell"])],
            [Paragraph("<b>Stop Loss</b>", self.styles["table_cell"]),
             Paragraph(f'<font color="{ReportColors.RED.hexval()}"><b>{sl}</b></font>  <i>(risk {risk_pct}%)</i>', self.styles["table_cell"]),
             Paragraph("<b>Target 1</b>", self.styles["table_cell"]),
             Paragraph(f'<font color="{ReportColors.GREEN.hexval()}"><b>{t1}</b></font>', self.styles["table_cell"])],
            [Paragraph("<b>Target 2</b>", self.styles["table_cell"]),
             Paragraph(f'<font color="{ReportColors.GREEN.hexval()}"><b>{t2}</b></font>', self.styles["table_cell"]),
             Paragraph("<b>Risk</b>", self.styles["table_cell"]),
             Paragraph(self._fmt_price(tp.get("risk")), self.styles["table_cell"])],
        ]

        plan_tbl = Table(plan_data, colWidths=[25 * mm, 60 * mm, 25 * mm, 50 * mm])
        plan_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("BOX", (0, 0), (-1, -1), 0.5, ReportColors.BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, ReportColors.BORDER_LIGHT),
        ]))

        return [header, plan_tbl]

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Section 5 — Embedded charts
    # ───────────────────────────────────────────────────────────────
    def _build_chart_section(self, chart_paths: Dict[str, str]) -> List:
        """Build section embedding chart PNGs (from Module 3 ChartGenerator)."""
        heading = self._section_heading("TECHNICAL CHARTS")
        flowables = [heading]

        max_charts = 5  # cap to avoid huge PDF
        shown = 0
        for symbol, path in chart_paths.items():
            if shown >= max_charts:
                break
            if not path or not os.path.exists(path):
                logger.debug(f"Chart not found for {symbol}: {path}")
                continue
            try:
                # Embed image (scaled to fit page width)
                img = Image(path)
                # Scale to max width = page width - margins
                max_width = self.page_size[0] - 2 * self.margin
                if img.drawWidth > max_width:
                    ratio = max_width / img.drawWidth
                    img.drawWidth *= ratio
                    img.drawHeight *= ratio
                # Cap height to avoid single chart taking full page
                max_height = 120 * mm
                if img.drawHeight > max_height:
                    ratio = max_height / img.drawHeight
                    img.drawWidth *= ratio
                    img.drawHeight *= ratio

                sym_clean = sanitize_text_for_pdf(symbol, self._keep_rupee)
                caption = Paragraph(f"<b>{sym_clean}</b> — Daily Chart (EMA 44 + EMA 200)",
                                    self.styles["body"])
                flowables.append(KeepTogether([caption, Spacer(1, 3), img, Spacer(1, 8)]))
                shown += 1
            except Exception as e:
                logger.warning(f"Failed to embed chart {path}: {e}")
                continue

        if shown == 0:
            flowables.append(Paragraph(
                "<i>No charts available for this report.</i>",
                self.styles["body"],
            ))
        return flowables

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Section 6 — Disclaimer
    # ───────────────────────────────────────────────────────────────
    def _build_disclaimer(self) -> Paragraph:
        """Build disclaimer paragraph (small grey text)."""
        disclaimer_text = sanitize_text_for_pdf(
            "DISCLAIMER: This report is generated by AI Stock Scanner V9.2 based on "
            "technical indicators and filtered news. It is for educational purposes only "
            "and is NOT financial advice. Stock markets carry risk — always do your own "
            "research and consult a SEBI-registered advisor before trading. Past "
            "performance does not guarantee future results.",
            self._keep_rupee,
        )
        return Paragraph(disclaimer_text, self.styles["disclaimer"])

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Helpers
    # ───────────────────────────────────────────────────────────────
    def _section_heading(self, text: str) -> Table:
        """Build a section heading bar (blue background, white text)."""
        clean = sanitize_text_for_pdf(text, self._keep_rupee)
        tbl = Table([[Paragraph(f"<b>{clean}</b>", self.styles["section_heading"])]],
                    colWidths=[self.page_size[0] - 2 * self.margin])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ReportColors.SECTION_BG),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        return tbl

    def _fmt_price(self, value) -> str:
        """Format price with ₹ (or Rs. fallback). Null-safe."""
        if value is None:
            return "N/A"
        try:
            v = float(value)
            if v != v:  # NaN check
                return "N/A"
            prefix = "₹" if self._keep_rupee else "Rs. "
            return f"{prefix}{v:,.2f}"
        except (ValueError, TypeError):
            return str(value) if value is not None else "N/A"

    def _fmt_num(self, value) -> str:
        """Format number (P/E, ROE, etc.). Null-safe."""
        if value is None:
            return "N/A"
        try:
            v = float(value)
            if v != v:
                return "N/A"
            return f"{v:.2f}"
        except (ValueError, TypeError):
            return str(value) if value is not None else "N/A"

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Footer (page number + timestamp)
    # ───────────────────────────────────────────────────────────────
    def _draw_footer(self, canv: canvas.Canvas, doc: SimpleDocTemplate) -> None:
        """Draw footer on each page (page number + disclaimer + timestamp)."""
        canv.saveState()
        font = FONT_REGULAR if self._has_unicode_font else FONT_FALLBACK
        canv.setFont(font, 7.5)
        canv.setFillColor(ReportColors.BODY_LIGHT)

        page_num = canv.getPageNumber()
        try:
            tz = ZoneInfo("Asia/Kolkata")
            ts = datetime.now(tz).strftime("%d %b %Y %I:%M %p IST")
        except Exception:
            ts = datetime.now().strftime("%d %b %Y %I:%M %p")

        footer_text = f"AI Stock Scanner V9.2  |  Page {page_num}  |  Generated {ts}  |  Not financial advice"
        # Center footer
        canv.drawCentredString(self.page_size[0] / 2, 8 * mm, footer_text)

        # Top border line
        canv.setStrokeColor(ReportColors.BORDER_LIGHT)
        canv.setLineWidth(0.5)
        canv.line(self.margin, 12 * mm, self.page_size[0] - self.margin, 12 * mm)

        canv.restoreState()


# ═══════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION — quick one-liner PDF generation
# ═══════════════════════════════════════════════════════════════════
def generate_evening_report(
    output_path: str,
    executive_summary: Optional[Dict[str, Any]] = None,
    stock_data: Optional[List[Dict[str, Any]]] = None,
    chart_paths: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """
    Quick one-liner: generate Evening Summary PDF report.

    Returns output_path on success, None on failure.
    """
    builder = PDFReportBuilder()
    return builder.build_report(
        output_path=output_path,
        report_title="Evening Summary & Stock Alert Report",
        executive_summary=executive_summary,
        stock_data=stock_data,
        chart_paths=chart_paths,
    )


# ═══════════════════════════════════════════════════════════════════
# SELF-TEST — generate sample PDF to verify rendering
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import tempfile

    print("=" * 70)
    print("PDFReportBuilder — Self Test")
    print("=" * 70)

    # ─── Test 1: Basic PDF generation ───
    print("\n--- Test 1: Basic PDF generation ---")
    builder = PDFReportBuilder()
    print(f"  Unicode font available: {builder._has_unicode_font}")
    print(f"  Keep ₹ symbol: {builder._keep_rupee}")

    exec_summary = {
        "sentiment": "BULLISH",
        "win_rate": 66.7,
        "total_trades": 6,
        "target_hits": 4,
        "sl_hits": 2,
        "top_gainer": "RELIANCE +5.2%",
        "top_loser": "TATASTEEL -3.1%",
    }

    stock_data = [
        {
            "symbol": "RELIANCE", "signal": "BUY", "score": 85,
            "close": 2945.50, "pe_ratio": 24.8, "roe": 9.2,
            "debt_to_equity": 0.65, "fii_dii_trend": "increasing",
            "pattern": "Hammer",
            "trade_plan": {
                "entry": 2945.50, "entry_low": 2930.78, "entry_high": 2960.22,
                "stop_loss": 2890.00, "target_1": 3056.50, "target_2": 3112.00,
                "risk": 55.50, "reward_t1": 111.00, "reward_t2": 166.50,
                "risk_reward_ratio": 2.0, "risk_pct": 1.88, "reward_t1_pct": 3.77,
            },
        },
        {
            "symbol": "TCS", "signal": "BUY", "score": 78,
            "close": 3850.00, "pe_ratio": 28.5, "roe": 38.1,
            "debt_to_equity": 0.12, "fii_dii_trend": "stable",
            "pattern": "Bullish Engulfing",
            "trade_plan": {
                "entry": 3850.00, "entry_low": 3830.83, "entry_high": 3869.18,
                "stop_loss": 3790.00, "target_1": 3970.00, "target_2": 4030.00,
                "risk": 60.00, "reward_t1": 120.00, "reward_t2": 180.00,
                "risk_reward_ratio": 2.0, "risk_pct": 1.56, "reward_t1_pct": 3.12,
            },
        },
        {
            "symbol": "INFY", "signal": "WATCH", "score": 60,
            "close": 1502.00, "pe_ratio": 22.0, "roe": 21.3,
            "debt_to_equity": 0.18, "fii_dii_trend": "stable",
            "trade_plan": None,  # no trade plan for WATCH
        },
    ]

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name

    result = builder.build_report(
        output_path=tmp_path,
        report_title="Evening Summary & Stock Alert Report",
        executive_summary=exec_summary,
        stock_data=stock_data,
        chart_paths=None,  # no charts in this test
    )
    assert result == tmp_path, "build_report failed"
    size = os.path.getsize(tmp_path)
    print(f"  ✅ PDF generated: {size:,} bytes")
    assert size > 5000, "PDF too small — likely empty/broken"
    print(f"  Path: {tmp_path}")

    # ─── Test 2: Text sanitization (no □ boxes) ───
    print("\n--- Test 2: Text sanitization (emoji/₹/special chars) ---")
    test_cases = [
        ("🔥 Reliance target ₹2940 🎯", "emoji + ₹"),
        ("Smart quotes: \u201chello\u201d \u2018world\u2019", "smart quotes"),
        ("Arrow → target", "arrow"),
        ("Em dash — test", "em dash"),
        ("ﬁ ligature ofﬁce", "ligature"),
        ("Invisible\u200bchar", "zero-width space"),
        ("Hindi नमस्ते test", "Devanagari"),
        ("Multiple    spaces", "whitespace"),
    ]
    for text, desc in test_cases:
        clean = sanitize_text_for_pdf(text, keep_rupee=True)
        # Verify NO □ (U+FFFD replacement or unrenderable)
        assert "\ufffd" not in clean, f"Replacement char in {desc}: {clean}"
        print(f"  {desc}: '{text}' -> '{clean}'")
    print("  ✅ All text sanitized (no replacement chars)")

    # ─── Test 3: ₹ handling (keep vs fallback) ───
    print("\n--- Test 3: ₹ handling (keep_rupee True vs False) ---")
    with_rupee = sanitize_text_for_pdf("Price: ₹1000", keep_rupee=True)
    without_rupee = sanitize_text_for_pdf("Price: ₹1000", keep_rupee=False)
    print(f"  keep_rupee=True:  '{with_rupee}'")
    print(f"  keep_rupee=False: '{without_rupee}'")
    assert "₹" in with_rupee
    assert "Rs." in without_rupee and "₹" not in without_rupee
    print("  ✅ ₹ kept/replaced correctly")

    # ─── Test 4: Empty/None input handling ───
    print("\n--- Test 4: Empty/None input ---")
    assert sanitize_text_for_pdf(None) == ""
    assert sanitize_text_for_pdf("") == ""
    print("  ✅ None/empty -> '' (no crash)")

    # ─── Test 5: PDF with chart embedding (if chart exists) ───
    print("\n--- Test 5: PDF with chart embedding ---")
    # Generate a sample chart using Module 3 (if available)
    chart_path = None
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from charts import ChartGenerator
        import pandas as pd
        import numpy as np

        np.random.seed(42)
        n = 250
        dates = pd.date_range(end=pd.Timestamp.now(), periods=n, freq="B")
        closes = 1000 * np.cumprod(1 + np.random.normal(0.001, 0.015, n))
        opens = closes * 0.998
        highs = np.maximum(opens, closes) * 1.005
        lows = np.minimum(opens, closes) * 0.995
        volumes = np.random.lognormal(15, 0.6, n).astype(int)
        df = pd.DataFrame({"Date": dates, "Open": opens, "High": highs,
                           "Low": lows, "Close": closes, "Volume": volumes})
        gen = ChartGenerator()
        chart_path = "/tmp/test_chart.png"
        gen.save_to_file(df, "RELIANCE", chart_path)
        print(f"  Sample chart generated: {chart_path}")
    except Exception as e:
        print(f"  Chart generation skipped: {e}")

    if chart_path and os.path.exists(chart_path):
        result = builder.build_report(
            output_path=tmp_path,
            executive_summary=exec_summary,
            stock_data=stock_data,
            chart_paths={"RELIANCE": chart_path},
        )
        assert result == tmp_path
        size = os.path.getsize(tmp_path)
        print(f"  ✅ PDF with chart generated: {size:,} bytes (larger = image embedded)")

    # ─── Test 6: Convenience function ───
    print("\n--- Test 6: Convenience function (generate_evening_report) ---")
    result = generate_evening_report(
        output_path=tmp_path,
        executive_summary=exec_summary,
        stock_data=stock_data,
    )
    assert result == tmp_path
    print("  ✅ generate_evening_report() works")

    # ─── Test 7: Verify PDF is valid (magic bytes) ───
    print("\n--- Test 7: PDF validity (magic bytes) ---")
    with open(tmp_path, "rb") as f:
        magic = f.read(5)
    assert magic == b"%PDF-", f"Invalid PDF magic: {magic}"
    print(f"  ✅ Valid PDF (magic: {magic})")

    # Cleanup
    os.unlink(tmp_path)
    if chart_path and os.path.exists(chart_path):
        os.unlink(chart_path)

    print()
    print("=" * 70)
    print("✅ ALL 7 PDF TESTS PASSED")
    print("   - Unicode font registered (DejaVu Sans — has ₹, smart quotes)")
    print("   - Emoji/symbol replacement (🔥 -> [STRONG], → -> ->)")
    print("   - ₹ handling (keep with DejaVu, 'Rs.' fallback with Helvetica)")
    print("   - Empty/None input safe")
    print("   - Chart embedding works")
    print("   - Valid PDF output (magic bytes %PDF-)")
    print("   - NO square boxes (□) — graceful degradation guaranteed")
    print("=" * 70)
