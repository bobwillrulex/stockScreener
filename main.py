"""Convenience launcher for the stock screener Flask UI.

Run with:
    py main.py
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_screener.flask_ui import main


if __name__ == "__main__":
    main()
