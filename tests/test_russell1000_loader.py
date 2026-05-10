import sqlite3

from stock_screener.russell1000_loader import (
    Russell1000Component,
    load_russell1000_to_sqlite,
    parse_russell1000_components,
)


def test_parse_russell1000_components_from_wikipedia_table_html():
    page_html = """
    <html><body>
      <table><tr><th>Other</th></tr><tr><td>Ignore me</td></tr></table>
      <table class="wikitable sortable">
        <tr>
          <th>Company</th><th>Symbol</th><th>GICS Sector</th><th>GICS Sub-Industry</th>
        </tr>
        <tr><td>3M</td><td>MMM</td><td>Industrials</td><td>Industrial Conglomerates</td></tr>
        <tr><td>Alphabet (Class A)</td><td>GOOGL</td><td>Communication Services</td><td>Interactive Media &amp; Services</td></tr>
      </table>
    </body></html>
    """

    components = parse_russell1000_components(page_html)

    assert components == [
        Russell1000Component("3M", "MMM", "Industrials", "Industrial Conglomerates"),
        Russell1000Component(
            "Alphabet (Class A)",
            "GOOGL",
            "Communication Services",
            "Interactive Media & Services",
        ),
    ]


def test_load_russell1000_to_sqlite_upserts_and_removes_stale_rows(tmp_path):
    db_path = tmp_path / "stocks.sqlite3"

    load_russell1000_to_sqlite(
        str(db_path),
        [Russell1000Component("Old Company", "OLD", "Old Sector", None)],
    )
    count = load_russell1000_to_sqlite(
        str(db_path),
        [Russell1000Component("New Company", "NEW", "New Sector", "New Industry")],
    )

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT symbol, company, gics_sector, gics_sub_industry, source_url
            FROM russell_1000_components
            """
        ).fetchall()

    assert count == 1
    assert rows == [
        (
            "NEW",
            "New Company",
            "New Sector",
            "New Industry",
            "https://en.wikipedia.org/wiki/Russell_1000_Index",
        )
    ]
