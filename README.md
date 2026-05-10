# Stock Screener

A Python stock-screening project that stores Russell 1000 symbols, aggregates Massive.com market data into regular-trading-hours four-hour candles, screens those candles for yearly and earnings-anchored VWAP proximity, and displays the resulting tickers in a Flask UI sorted by market cap.

## What the program does

The project is split into four main functions/workflows:

1. **Load Russell 1000 components**
   - Fetches the current Russell 1000 components table from Wikipedia.
   - Stores symbols and company metadata in the `russell_1000_components` SQLite table.

2. **Load four-hour candle data**
   - Fetches Massive.com minute aggregates for selected symbols and dates.
   - Keeps regular trading hours only.
   - Rolls minute bars into four-hour candle buckets.
   - Stores the results in the `massive_rth_4h_candles` SQLite table.

3. **Run the VWAP proximity scanner**
   - Reads Russell 1000 symbols and stored four-hour candles from SQLite.
   - Calculates anchored VWAP and standard deviation from yearly resets and optional earnings reset dates.
   - Stores matching proximity hits in the `vwap_proximity_hits` SQLite table.

4. **Launch the web UI**
   - Shows stored VWAP proximity hits sorted from largest to smallest market cap when market-cap data is available.
   - Each ticker links to its TradingView chart.
   - Launches even when the configured SQLite database does not exist yet.
   - Includes a **Run Scan** button that creates the database if needed, runs the VWAP scanner against the configured database, and reports success or failure in the page.

## Requirements

- Python 3.11 or newer
- Flask, installed through this package's dependencies
- A Massive.com API key in `MASSIVE_API_KEY` when fetching market data

## Setup

Create and activate a virtual environment, then install the project in editable mode:

```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows PowerShell
pip install -e .
```

If you use the Windows Python Launcher, you can replace `python` with `py` in most commands.

## How to run the program

### 1. Load Russell 1000 symbols

```bash
load-russell1000 stocks.sqlite3
```

### 2. Fetch Massive four-hour candle data

Set your Massive API key first:

```bash
export MASSIVE_API_KEY="your-api-key"  # macOS/Linux
# setx MASSIVE_API_KEY "your-api-key"  # Windows PowerShell for future shells
```

Then fetch candles:

```bash
load-massive-rth-4h stocks.sqlite3 --start 2026-01-01 --end 2026-01-31
```

Useful options:

- `--symbols AAPL,MSFT,NVDA` scans specific symbols instead of every stored Russell 1000 component.
- `--symbol-limit 25` limits the number of symbols for smoke tests.
- `--unadjusted` requests unadjusted Massive bars.

### 3. Optional: load earnings reset dates

Create a CSV with `symbol` and `earnings_date` columns, then run:

```bash
screen-russell1000-vwap load-earnings stocks.sqlite3 earnings.csv
```

### 4. Run the VWAP scanner from the command line

```bash
screen-russell1000-vwap screen stocks.sqlite3
```

Useful options:

- `--std-threshold 0.1` controls how close a high/low must be to anchored VWAP in standard-deviation units.
- `--only-first-touch` stores only the first touch for each symbol/reset anchor.
- `--start YYYY-MM-DD` and `--end YYYY-MM-DD` limit the scan date range.
- `--symbol-limit 25` limits symbols for faster smoke tests.
- `--no-store` prints hits without writing `vwap_proximity_hits`.

### 5. Launch the UI with `py main.py`

After setup, start the Flask UI from the repository root:

```bash
py main.py stocks.sqlite3
```

The database argument is optional. If omitted, the UI uses `stocks.sqlite3` in the current directory. The UI can start before this file exists; simply open the page and click **Run Scan** when you are ready to create/populate the database:

```bash
py main.py
```

On macOS/Linux, use this equivalent command if `py` is not installed:

```bash
python main.py stocks.sqlite3
```

Then open <http://127.0.0.1:5000> in your browser.

Additional UI options:

```bash
py main.py stocks.sqlite3 --host 0.0.0.0 --port 8080 --debug
```

## UI Run Scan button

The **Run Scan** button posts to `/run-scan`. If the configured SQLite database file does not exist, the file is created by the scan workflow rather than by simply loading the UI. The button runs the same default scanner as:

```bash
screen-russell1000-vwap screen stocks.sqlite3
```

Use the command-line scanner when you need custom options such as `--start`, `--end`, `--std-threshold`, or `--only-first-touch`.

## Testing

Run the automated tests with:

```bash
pytest
```
