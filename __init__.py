"""
Charts Module Package (V9.2 Step 3)
====================================
Ultra-clean institutional chart generator.

PLOTS ONLY 4 ELEMENTS (everything else banned):
  1. Candlesticks (green/red, TradingView-style)
  2. 200 EMA (solid blue — macro trend baseline)
  3. 44 EMA (sharp gold/orange — dynamic reversal zone)
  4. Volume bars + 20-period volume MA (bottom subplot)

Exports:
  - ChartGenerator: Main class with save_to_file() + save_to_buffer()
  - ChartTheme: Visual configuration dataclass (colors, sizes, DPI)
  - generate_chart: Convenience function (file output)
  - generate_chart_buffer: Convenience function (BytesIO output)
"""

from .chart_generator import (
    ChartGenerator,
    ChartTheme,
    generate_chart,
    generate_chart_buffer,
)

__all__ = [
    "ChartGenerator",
    "ChartTheme",
    "generate_chart",
    "generate_chart_buffer",
]
