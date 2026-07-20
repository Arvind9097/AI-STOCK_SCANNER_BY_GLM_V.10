"""
===================================================================
 INSTITUTIONAL DARK-THEME CHART GENERATOR (V8.3.0 — "Bloomberg clean")
===================================================================
V8.3.0 UPGRADE — UI simplification ("Charts ka UI simple or best"):

1. COMPACT TRADE-PLAN BOX (top-right): Pehle 8 alag-alag right-margin
   labels (Entry Zone / SL / T1 / T2 / T3 / Support / Resistance /
   Breakout) ek dusre ke saath overlap karte the aur messy lagte the.
   Ab sirf ek semi-transparent monospace box top-right corner mein
   hai jisme saare levels neatly listed hain (label + value pairs).
   Horizontal lines abhi bhi chart par hain (visual reference ke
   liye), par unke paas koi messy right-side text nahi hai.
2. CLEAR COLOR HIERARCHY:
   • Entry / SL / Target  → solid bold (blue / red / teal-green)
   • Support / Resistance → muted dashed grey (#787b86)
   • Breakout             → muted dashed orange (#ff9800)
   • EMAs                 → thin subtle lines (gold / pink / blue)
   Pehle 6+ competing colors the, ab clear hierarchy hai - koi
   attention ke liye compete nahi karta.
3. CLEANER TITLE: "SYMBOL  •  ₹XXX  •  ★ STRONG BUY" format.
   Larger font (14pt), bullet separators (•) instead of pipes (|),
   optional signal badge with geometric marker (★/▲/●/▼) jab
   row.Signal available ho. NOTE: color emojis (🔥/👀) use glyphs
   above U+FFFF (Supplementary Multilingual Plane) jo DejaVu Sans
   (matplotlib default font) mein missing hain — tofu boxes (□)
   render karte. Isliye institutional geometric markers use kiye
   (★=STRONG BUY, ▲=BUY, ●=WATCH, ▼=SELL/AVOID) jo BMP mein hain
   aur har environment (Linux/Render/Mac/Windows) mein reliably
   render hote hain. Telegram caption (main.py) abhi bhi real
   emojis use karta hai — Telegram native UI mein emoji support
   perfect hai.
4. WATERMARK: "AI Scanner V8.3.0" (was V8.2.0), bottom-right, very
   low opacity (0.35) — subtle, distracting nahi.
5. VOLUME: thin bars, alpha 0.55, green/red color-coded (verified
   clean). Width 0.6, no edge.
6. GRID: subtle dotted (':'), alpha 0.5. Top + left spines hidden
   (TradingView "no border" look). Right + bottom spines subtle.
   axisbelow=True taaki grid candles ke peeche rahe.
7. CANDLESTICKS: thinner wicks (lw=0.9, was 1.3), cleaner bodies
   (width=0.6, was 0.62, no edge). TV teal/red unchanged.
8. LEGEND: compact top-left, only EMA 20/44/200, framealpha=0.7,
   smaller fontsize (7.5), tighter padding.
9. DPI 300 (unchanged — crisp for PDF/Telegram).
10. Figure 15x9 (unchanged — good aspect ratio for Telegram).

V8.2.0 FIXES PRESERVED (no regression):
- try/finally around savefig + plt.close(fig)  [bug #14, memory leak]
- date formatting try/except                   [bug #15]
- watermark version consistency                 [bug #31 — now V8.3.0]

Return signature UNCHANGED:
    generate_chart(...)          -> (path, metrics_dict)
    generate_charts_for_top(...) -> (chart_paths_dict, metrics_db_dict)
    generate_simple_chart(...)   -> path
===================================================================
"""

import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config import CHARTS_DIR, BREAKOUT_LOOKBACK
from targets import calculate_targets
from utils import clean_symbol
from logger import logger

# -----------------------------------------------------------
# TRADINGVIEW DARK THEME — V9.6 (improved for TV-like look)
# -----------------------------------------------------------
BG_COLOR         = "#0d1117"   # TradingView dark background (darker)
GRID_COLOR       = "#1e222d"   # subtle grid
TEXT_COLOR       = "#d1d4dc"   # TV light-grey label text
TEXT_DIM_COLOR   = "#787b86"   # TV dim-grey (secondary labels)
TITLE_COLOR      = "#ffffff"
PANEL_BG         = "#131722"   # candlestick panel background

CANDLE_UP        = "#089981"   # TV green (v2 — more vibrant)
CANDLE_DOWN      = "#f23645"   # TV red (v2 — more vibrant)
WICK_UP          = "#089981"
WICK_DOWN        = "#f23645"

# EMAs — TradingView style colors
EMA20_COLOR      = "#FFD700"   # gold (bright)
EMA44_COLOR      = "#FF6B35"   # sharp orange (reversal zone)
EMA200_COLOR     = "#4FC3F7"   # light blue (trend baseline)

FIG_W            = 16   # wider figure for more candles
FIG_H            = 9
DPI              = 150  # balanced quality + speed

# V9.7 FIX: Box colors (were removed in V9.6 cleanup — caused chart crash)
BOX_BG_COLOR     = "#1a1e2a"
BOX_BORDER_COLOR = "#2a2e39"


# -----------------------------------------------------------
# Private helpers (shared styling — DRY across both chart funcs)
# -----------------------------------------------------------
def _fmt_price(v):
    """Format price for trade-plan box: 2 decimals, None/NaN -> '—'."""
    if v is None:
        return "—"
    try:
        f = float(v)
        if f != f:  # NaN check
            return "—"
        return f"{f:.2f}"
    except (TypeError, ValueError):
        return "—"


def _signal_badge(signal):
    """
    Map scanner Signal string to a visual badge MARKER for the chart title.

    Uses geometric markers (★ ▲ ● ▼) instead of color emojis (🔥 ⚡ 👀 ⚠)
    because DejaVu Sans (matplotlib's default font) lacks U+1F525 (🔥) and
    U+1F440 (👀) — these glyphs are in the Supplementary Multilingual
    Plane (above U+FFFF) and DejaVu Sans only covers the BMP. They would
    render as tofu boxes (□) on the chart. Noto Color Emoji cannot be
    loaded by matplotlib's FT2Font (color bitmap fonts unsupported).

    Geometric markers ARE in DejaVu Sans's BMP coverage and render
    reliably across all environments (Linux / Render / Mac / Windows).
    Bonus: they look more institutional (Bloomberg-style) than emojis.

    The Telegram caption (handled in main.py, NOT here) can still use
    real emojis — Telegram's native UI renders them fine. Only the
    server-side matplotlib chart needs ASCII markers.

    Mapping:
        STRONG BUY  →  ★   (star = premium pick)
        BUY         →  ▲   (up triangle = bullish)
        WATCH       →  ●   (circle = neutral / observe)
        SELL / AVOID →  ▼   (down triangle = bearish)
    """
    if not signal or not isinstance(signal, str):
        return ""
    s = signal.upper().strip()
    if "STRONG" in s and "BUY" in s:
        return "★"
    if "BUY" in s:
        return "▲"
    if "WATCH" in s:
        return "●"
    if "SELL" in s or "AVOID" in s:
        return "▼"
    return ""


def _style_axes(ax):
    """Apply TradingView-clean styling to an axes (shared by both charts)."""
    ax.set_facecolor(BG_COLOR)
    ax.set_axisbelow(True)  # grid behind data (TV style)
    ax.tick_params(axis='both', colors=TEXT_COLOR, labelsize=9,
                   length=3, width=0.6)
    ax.grid(color=GRID_COLOR, linestyle=':', linewidth=0.5, alpha=0.5)
    ax.yaxis.set_label_position("right")
    ax.yaxis.tick_right()
    # TradingView "no border" look: hide top + left spines, keep right + bottom subtle
    ax.spines['top'].set_visible(False)
    ax.spines['left'].set_visible(False)
    ax.spines['right'].set_edgecolor("#2a2e39")
    ax.spines['right'].set_linewidth(0.6)
    ax.spines['bottom'].set_edgecolor("#2a2e39")
    ax.spines['bottom'].set_linewidth(0.6)


def _draw_trade_plan_box(ax, lines):
    """
    Compact semi-transparent trade-plan box in the top-right corner
    of the axes. `lines` is a list of (label, value) tuples — only
    non-None entries should be passed. Monospace font for neat alignment.
    """
    if not lines:
        return
    # Build the multi-line text block
    text_block = "TRADE PLAN\n" + "─" * 22 + "\n"
    for label, value in lines:
        # Pad label to fixed width for column alignment (monospace)
        text_block += f"{label:<11} {value}\n"
    text_block = text_block.rstrip("\n")

    ax.text(
        0.995, 0.985, text_block,
        transform=ax.transAxes,
        fontsize=8.2, color=TEXT_COLOR,
        ha='right', va='top',
        family='DejaVu Sans Mono',
        zorder=12,
        bbox=dict(
            boxstyle="round,pad=0.55",
            facecolor=BOX_BG_COLOR,
            edgecolor=BOX_BORDER_COLOR,
            alpha=0.92,
            linewidth=0.7,
        ),
    )


def _draw_watermark(ax):
    """Subtle bottom-right watermark (V8.3.0)."""
    ax.text(
        0.995, 0.015, "AI Scanner V8.3.0",
        transform=ax.transAxes,
        fontsize=6.5, color="#3d4466",
        ha='right', va='bottom', alpha=0.35,
        zorder=11,
    )


def _draw_title(ax, display_name, close, signal=None):
    """Clean institutional title: SYMBOL  •  ₹XXX  •  ★ SIGNAL"""
    title = f"  {display_name}   •   ₹{close:.2f}"
    if signal:
        badge = _signal_badge(signal)
        if badge:
            title += f"   •   {badge}  {signal}"
        else:
            title += f"   •   {signal}"
    ax.set_title(title, color=TITLE_COLOR, fontsize=14, loc='left',
                 fontweight='bold', pad=12)


def _draw_legend(ax):
    """Compact top-left legend, only EMAs, semi-transparent background."""
    leg = ax.legend(
        loc="upper left", facecolor=BOX_BG_COLOR, edgecolor=BOX_BORDER_COLOR,
        fontsize=7.5, labelcolor=TEXT_COLOR, framealpha=0.7,
        borderpad=0.6, handlelength=1.8, handletextpad=0.5,
    )
    if leg is not None:
        leg.get_frame().set_linewidth(0.5)


def _draw_candles(ax, x, df):
    """TV-style candlesticks: thin wicks, clean bodies (shared by both charts)."""
    colors = [CANDLE_UP if c >= o else CANDLE_DOWN
              for c, o in zip(df['Close'], df['Open'])]
    ax.vlines(x, df['Low'], df['High'], color=colors, linewidth=0.9, zorder=3)
    bottoms = np.minimum(df['Open'], df['Close'])
    heights = np.abs(df['Open'] - df['Close'])
    ax.bar(
        x, np.where(heights == 0, 0.05, heights),
        width=0.6, bottom=bottoms, color=colors, edgecolor='none', zorder=3,
    )
    return colors


def _draw_volume(ax, x, colors, volumes):
    """Thin color-coded volume panel (shared by both charts)."""
    ax.bar(x, volumes, color=colors, width=0.6, alpha=0.55,
           edgecolor='none', zorder=2)
    ax.set_ylabel("Volume", color=TEXT_DIM_COLOR, fontsize=8.5, fontweight='bold')
    ax.tick_params(axis='y', labelsize=7, colors=TEXT_DIM_COLOR)


# -----------------------------------------------------------
# Main swing chart
# -----------------------------------------------------------
def generate_chart(stock, original_df, row=None, save_dir=CHARTS_DIR, lookback_days=120):
    """
    stock: e.g. "RELIANCE.NS"
    original_df: raw OHLCV dataframe
    row: scanner.py ka scan() result row for this stock (dict) - agar diya
         gaya to Entry/Stoploss/Target/Support/Resistance YAHI SE liye
         jaate hain (single source of truth - chart/Telegram/DB match).

    Return: (path, metrics_dict) — signature unchanged.
    """
    os.makedirs(save_dir, exist_ok=True)
    df = original_df.copy()

    # ---- 3 EMAs (V9.5: EMA20 wapas add kiya gaya user request par) ----
    df['EMA20']  = df['Close'].ewm(span=20,  adjust=False).mean()
    df['EMA44']  = df['Close'].ewm(span=44,  adjust=False).mean()
    df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()

    # V9.6 FIX: dropna sirf Close pe (EMA200 NaN hone par rows mat hatao).
    # Pehle dropna(subset=["Close","EMA200"]) tha — agar stock ka data
    # 210 days ka tha, EMA200 ke liye sirf 10 rows non-NaN, baaki sab
    # drop ho jaate the → chart pe sirf 3-10 candles dikhte the!
    # Ab sirf Close pe dropna, EMA200 NaN rows plot nahi hongi (matplotlib
    # NaN skip karta hai) lekin candles + EMA20 + EMA44 sab dikhte hain.
    df = df.dropna(subset=["Close"]).tail(lookback_days).reset_index(drop=True)
    if df.empty or len(df) < 10:
        logger.warning(f"{stock}: chart skip — only {len(df)} rows (need ≥10 for meaningful chart)")
        return None, {}

    last_close = float(df['Close'].iloc[-1])

    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(FIG_W, FIG_H), sharex=True,
        gridspec_kw={"height_ratios": [4.2, 1]},
    )
    fig.patch.set_facecolor(BG_COLOR)
    _style_axes(ax_price)
    _style_axes(ax_vol)

    x = np.arange(len(df))

    # ---- 1. CANDLESTICKS (TV-style: thin wick, clean body) ----
    colors = _draw_candles(ax_price, x, df)

    # ---- 2. 3 EMAs (V9.5: EMA20 + EMA44 + EMA200, nothing else) ----
    ax_price.plot(x, df['EMA20'],  color=EMA20_COLOR,  linewidth=1.3,
                  label='EMA 20',  zorder=4)
    ax_price.plot(x, df['EMA44'],  color=EMA44_COLOR,  linewidth=1.6,
                  label='EMA 44 (Reversal)',  zorder=4)
    ax_price.plot(x, df['EMA200'], color=EMA200_COLOR, linewidth=2.0,
                  label='EMA 200 (Trend)', zorder=5)

    # V9.4: REMOVED all of these (user request — sirf EMA rakhna hai):
    # - Breakout level line
    # - Entry zone shading
    # - Stop Loss line
    # - Target 1/2/3 lines
    # - Support line
    # - Resistance line
    # - Trade plan box

    signal = row.get("Signal") if row else None

    # ---- 3. TITLE + LEGEND ----
    display_name = clean_symbol(stock)
    _draw_title(ax_price, display_name, last_close, signal)
    _draw_legend(ax_price)

    # ---- 7. VOLUME (thin panel, color-coded, semi-transparent) ----
    _draw_volume(ax_vol, x, colors, df['Volume'].values)

    # ---- 8. X-AXIS DATE LABELS ----
    date_col = df['Date'] if 'Date' in df.columns else df.index
    step = max(1, len(df) // 8)
    ax_vol.set_xticks(x[::step])
    # V8.2.0 FIX (bug #15): try/except around date formatting. Agar
    # df.index RangeIndex (integers) hai aur 'Date' column missing hai
    # to pd.to_datetime(integers) unhe ns-since-epoch samajh ke 1970+
    # dates banata tha, .strftime() bhi weird labels deta tha ya throw.
    try:
        ax_vol.set_xticklabels(
            [pd.to_datetime(date_col).iloc[i].strftime('%d %b') for i in x[::step]],
            color=TEXT_COLOR, fontsize=8.5, fontweight='bold',
        )
    except Exception as e:
        logger.debug(f"{display_name}: date formatting fail (default labels used): {e}")

    # Tighter right margin (no more right-side labels to fit)
    ax_vol.set_xlim(-1.5, len(df) + 2)

    # ---- 9. WATERMARK ----
    _draw_watermark(ax_price)

    plt.tight_layout()
    plt.subplots_adjust(hspace=0.05)

    safe_name = stock.replace(".", "_")
    path = os.path.join(save_dir, f"{safe_name}.png")
    # V8.2.0 FIX (bug #14): try/finally around savefig + close. Pehle
    # savefig throw karte hi plt.close(fig) nahi chalta tha - matplotlib
    # figures global registry mein accumulate hote hain (memory leak,
    # ~5-20MB per fig). Long-running 500-stock scan mein significant leak.
    try:
        fig.savefig(path, dpi=DPI, facecolor=BG_COLOR, edgecolor="none",
                    bbox_inches="tight")
    except Exception as e:
        logger.warning(f"{display_name}: chart savefig fail ({e})")
        plt.close(fig)
        return None, {}
    finally:
        # Always close figure (even on success path) to prevent leak.
        # close() idempotent hai - agar fig already close hai to no-op.
        plt.close(fig)

    metrics = {
        'entry':          row.get("Entry") if row else None,
        'sl':             row.get("Stoploss") if row else None,
        'target':         row.get("Target") if row else None,
        'support':        row.get("Support") if row else None,
        'resistance':     row.get("Resistance") if row else None,
        'breakout_level': round(breakout_level, 2),
    }
    return path, metrics


def generate_charts_for_top(all_data, ranked_result, top_n):
    """
    ranked_result: scanner.py ka scan() output (Score se sorted) - har
    row mein Entry/Stoploss/Target/Support/Resistance already hain.
    Return: (chart_paths_dict, metrics_db_dict) - signature unchanged.
    """
    chart_paths, metrics_db = {}, {}
    for item in ranked_result[:top_n]:
        stock = item.get("Stock")
        if stock in all_data:
            try:
                path, metrics = generate_chart(stock, all_data[stock], row=item)
                if path:
                    chart_paths[stock], metrics_db[stock] = path, metrics
            except Exception as e:
                logger.warning(f"{stock} Chart Error: {e}")
    return chart_paths, metrics_db


# -----------------------------------------------------------
# Intraday / BTST simplified chart
# -----------------------------------------------------------
def generate_simple_chart(stock, df, entry=None, sl=None, target=None,
                          save_dir=CHARTS_DIR, lookback_days=120):
    """
    V8.1.2 NAYA: Intraday scanner aur BTST scanner ke liye simplified
    chart - inka result-dict format scanner.py (Swing) se ALAG hai
    (koi Entry_Low/Entry_High/Support/Resistance nahi hota, kyunki
    ORB/VWAP/last-hour-price-action based hai, ATR-based zones nahi).

    V8.3.0: Same "Bloomberg clean" UI as generate_chart — compact
    trade-plan box (top-right), cleaner title, subtle watermark,
    thin wicks, muted grid, hidden spines. Only EMA 20 plotted.

    Return: path (str) ya None (fail hone par)
    """
    os.makedirs(save_dir, exist_ok=True)
    if df is None or df.empty:
        return None

    data = df.copy()
    # V9.5: 3 EMAs (EMA20 + EMA44 + EMA200, nothing else)
    data['EMA20']  = data['Close'].ewm(span=20, adjust=False).mean()
    data['EMA44']  = data['Close'].ewm(span=44, adjust=False).mean()
    if len(data) >= 200:
        data['EMA200'] = data['Close'].ewm(span=200, adjust=False).mean()
    data = data.dropna(subset=["Close"]).tail(lookback_days).reset_index(drop=True)
    if data.empty:
        return None

    last_close = float(data['Close'].iloc[-1])

    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(FIG_W, FIG_H), sharex=True,
        gridspec_kw={"height_ratios": [4.2, 1]},
    )
    fig.patch.set_facecolor(BG_COLOR)
    _style_axes(ax_price)
    _style_axes(ax_vol)

    x = np.arange(len(data))

    # Candlesticks (thin wicks, clean bodies)
    colors = _draw_candles(ax_price, x, data)

    # V9.5: 3 EMAs (EMA20 + EMA44 + EMA200, no trade plan lines)
    ax_price.plot(x, data['EMA20'], color=EMA20_COLOR, linewidth=1.3,
                  label='EMA 20', zorder=4)
    ax_price.plot(x, data['EMA44'], color=EMA44_COLOR, linewidth=1.6,
                  label='EMA 44 (Reversal)', zorder=4)
    if 'EMA200' in data.columns:
        ax_price.plot(x, data['EMA200'], color=EMA200_COLOR, linewidth=2.0,
                      label='EMA 200 (Trend)', zorder=5)

    # V9.4: REMOVED all trade plan lines + trade plan box (user request)

    display_name = clean_symbol(stock)
    _draw_title(ax_price, display_name, last_close, signal=None)
    _draw_legend(ax_price)

    # Volume
    _draw_volume(ax_vol, x, colors, data['Volume'].values)

    date_col = data['Date'] if 'Date' in data.columns else data.index
    step = max(1, len(data) // 8)
    ax_vol.set_xticks(x[::step])
    try:
        ax_vol.set_xticklabels(
            [pd.to_datetime(date_col).iloc[i].strftime('%d %b %H:%M') for i in x[::step]],
            color=TEXT_COLOR, fontsize=8.5, fontweight='bold', rotation=30, ha='right',
        )
    except Exception:
        pass  # date formatting fail ho to bhi chart bane, bas x-labels default rahenge
    ax_vol.set_xlim(-1.5, len(data) + 2)

    _draw_watermark(ax_price)

    plt.tight_layout()
    plt.subplots_adjust(hspace=0.05)

    safe_name = stock.replace(".", "_") + "_intraday"
    path = os.path.join(save_dir, f"{safe_name}.png")
    # V8.2.0 FIX (bug #14): try/finally around savefig + close - same
    # as generate_chart (prevents matplotlib figure leak on savefig exception).
    try:
        fig.savefig(path, dpi=DPI, facecolor=BG_COLOR, edgecolor="none",
                    bbox_inches="tight")
    except Exception as e:
        logger.warning(f"{display_name}: intraday chart savefig fail ({e})")
        plt.close(fig)
        return None
    finally:
        plt.close(fig)

    return path
