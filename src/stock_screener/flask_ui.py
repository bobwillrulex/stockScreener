"""Flask UI for reviewing VWAP proximity hits sorted by market cap."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus
import argparse
import sqlite3

from typing import Any


DEFAULT_DB_PATH = "stocks.sqlite3"
TRADING_VIEW_CHART_URL = "https://www.tradingview.com/chart/?symbol={symbol}"


@dataclass(frozen=True)
class ProximityStockRow:
    """A stock with at least one stored VWAP proximity hit."""

    symbol: str
    company: str
    market_cap: float | None
    latest_trading_date: str | None
    latest_anchor: str | None
    hit_count: int

    @property
    def trading_view_url(self) -> str:
        """Return a TradingView chart URL for this row's ticker."""

        return TRADING_VIEW_CHART_URL.format(symbol=quote_plus(self.symbol))

    @property
    def formatted_market_cap(self) -> str:
        """Return a compact human-readable market cap value."""

        return format_market_cap(self.market_cap)


INDEX_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VWAP Proximity by Market Cap</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #0f172a;
      --card: #111827;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --border: #263244;
      --accent: #38bdf8;
      --row-hover: #1f2937;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: linear-gradient(135deg, #020617 0%, var(--bg) 55%, #172554 100%);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main { width: min(1100px, calc(100% - 32px)); margin: 48px auto; }
    .header { display: flex; justify-content: space-between; gap: 24px; align-items: end; margin-bottom: 24px; }
    h1 { margin: 0 0 8px; font-size: clamp(2rem, 5vw, 3.5rem); line-height: 1; letter-spacing: -0.05em; }
    p { margin: 0; color: var(--muted); }
    .card {
      overflow: hidden;
      background: rgba(17, 24, 39, 0.9);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
    }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 18px 20px; text-align: left; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); font-size: 0.78rem; letter-spacing: 0.08em; text-transform: uppercase; }
    tbody tr { cursor: pointer; transition: background 150ms ease, transform 150ms ease; }
    tbody tr:hover { background: var(--row-hover); }
    tbody tr:focus-within { outline: 2px solid var(--accent); outline-offset: -2px; }
    tbody tr:last-child td { border-bottom: 0; }
    a.row-link { color: inherit; text-decoration: none; display: block; font-weight: 700; }
    .ticker { color: var(--accent); font-size: 1.05rem; }
    .market-cap { font-variant-numeric: tabular-nums; white-space: nowrap; }
    .empty { padding: 40px; text-align: center; }
    .meta { color: var(--muted); font-size: 0.9rem; }
    @media (max-width: 720px) {
      main { margin: 24px auto; }
      .header { display: block; }
      th:nth-child(4), td:nth-child(4), th:nth-child(5), td:nth-child(5) { display: none; }
      th, td { padding: 14px 12px; }
    }
  </style>
</head>
<body>
  <main>
    <section class="header">
      <div>
        <h1>VWAP Proximity</h1>
        <p>Stocks with stored proximity hits, sorted from largest to smallest market cap.</p>
      </div>
      <p>{{ rows|length }} stocks</p>
    </section>
    <section class="card">
      {% if rows %}
      <table aria-label="VWAP proximity stocks by market cap">
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Name</th>
            <th>Market Cap</th>
            <th>Latest Hit</th>
            <th>Hits</th>
          </tr>
        </thead>
        <tbody>
          {% for row in rows %}
          <tr onclick="window.location='{{ row.trading_view_url }}'" title="Open {{ row.symbol }} in TradingView">
            <td><a class="row-link ticker" href="{{ row.trading_view_url }}">{{ row.symbol }}</a></td>
            <td>{{ row.company }}</td>
            <td class="market-cap">{{ row.formatted_market_cap }}</td>
            <td class="meta">{{ row.latest_trading_date or '—' }}{% if row.latest_anchor %} · {{ row.latest_anchor }}{% endif %}</td>
            <td class="meta">{{ row.hit_count }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="empty">
        <h2>No proximity hits found</h2>
        <p>Run the screener first so <code>vwap_proximity_hits</code> contains rows.</p>
      </div>
      {% endif %}
    </section>
  </main>
</body>
</html>
"""


def create_app(db_path: str = DEFAULT_DB_PATH) -> Any:
    """Create the Flask application for the proximity dashboard."""

    from flask import Flask, abort, render_template_string

    app = Flask(__name__)
    app.config["STOCK_SCREENER_DB_PATH"] = db_path

    @app.get("/")
    def index() -> str:
        db_file = app.config["STOCK_SCREENER_DB_PATH"]
        if not Path(db_file).exists():
            abort(404, description=f"SQLite database not found: {db_file}")
        rows = load_proximity_rows(db_file)
        return render_template_string(INDEX_TEMPLATE, rows=rows)

    return app


def load_proximity_rows(db_path: str) -> list[ProximityStockRow]:
    """Load unique proximity tickers sorted by descending market cap."""

    with sqlite3.connect(db_path) as connection:
        if not _table_exists(connection, "vwap_proximity_hits"):
            return []
        has_components = _table_exists(connection, "russell_1000_components")
        market_cap_expr = _market_cap_expression(connection) if has_components else "NULL"
        if has_components:
            rows = connection.execute(
                f"""
                SELECT
                    h.symbol,
                    COALESCE(r.company, h.symbol) AS company,
                    {market_cap_expr} AS market_cap,
                    MAX(h.trading_date) AS latest_trading_date,
                    (
                        SELECT h2.anchor
                        FROM vwap_proximity_hits h2
                        WHERE h2.symbol = h.symbol
                        ORDER BY h2.trading_date DESC, h2.bucket_index DESC, h2.screened_at DESC
                        LIMIT 1
                    ) AS latest_anchor,
                    COUNT(*) AS hit_count
                FROM vwap_proximity_hits h
                LEFT JOIN russell_1000_components r ON r.symbol = h.symbol
                GROUP BY h.symbol, company, market_cap
                ORDER BY market_cap IS NULL, market_cap DESC, h.symbol ASC
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT
                    h.symbol,
                    h.symbol AS company,
                    NULL AS market_cap,
                    MAX(h.trading_date) AS latest_trading_date,
                    (
                        SELECT h2.anchor
                        FROM vwap_proximity_hits h2
                        WHERE h2.symbol = h.symbol
                        ORDER BY h2.trading_date DESC, h2.bucket_index DESC, h2.screened_at DESC
                        LIMIT 1
                    ) AS latest_anchor,
                    COUNT(*) AS hit_count
                FROM vwap_proximity_hits h
                GROUP BY h.symbol
                ORDER BY h.symbol ASC
                """
            ).fetchall()
    return [
        ProximityStockRow(
            symbol=row[0],
            company=row[1],
            market_cap=row[2],
            latest_trading_date=row[3],
            latest_anchor=row[4],
            hit_count=row[5],
        )
        for row in rows
    ]


def format_market_cap(market_cap: float | None) -> str:
    """Format raw market cap dollars into T/B/M/K notation."""

    if market_cap is None:
        return "—"
    abs_value = abs(market_cap)
    for suffix, threshold in (("T", 1_000_000_000_000), ("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs_value >= threshold:
            return f"${market_cap / threshold:.2f}{suffix}"
    return f"${market_cap:,.0f}"


def _market_cap_expression(connection: sqlite3.Connection) -> str:
    columns = _table_columns(connection, "russell_1000_components")
    for column in ("market_cap", "marketcap", "market_capitalization"):
        if column in columns:
            return f"r.{column}"
    return "NULL"


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the stock screener Flask UI.")
    parser.add_argument("db_path", nargs="?", default=DEFAULT_DB_PATH, help="SQLite database path.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind.")
    parser.add_argument("--debug", action="store_true", help="Run Flask in debug mode.")
    args = parser.parse_args()

    app = create_app(args.db_path)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
