#!/bin/bash
# AI Stock Scanner V9.2 — Quick Start
set -e

echo "🚀 AI Stock Scanner V9.2 — Setup"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

echo "📥 Installing dependencies..."
pip install -r requirements.txt

echo "✅ Setup complete!"
echo ""
echo "Usage:"
echo "  python src/main.py --run evening     # Run evening pipeline once"
echo "  python src/main.py --run scheduler   # Start daily scheduler"
echo ""
echo "Output: output_reports/Evening_Summary_*.pdf"
