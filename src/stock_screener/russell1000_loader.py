"""Load Russell 1000 components from Wikipedia into SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
import argparse
import html
import re
import sqlite3
import urllib.request

WIKIPEDIA_RUSSELL_1000_URL = "https://en.wikipedia.org/wiki/Russell_1000_Index"
USER_AGENT = "stockScreener Russell 1000 loader/0.1"


@dataclass(frozen=True)
class Russell1000Component:
    """A single Russell 1000 component from Wikipedia."""

    company: str
    symbol: str
    gics_sector: str | None = None
    gics_sub_industry: str | None = None


class WikipediaTableParser(HTMLParser):
    """Small HTML table parser focused on Wikipedia component tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._current_table = []
        elif tag == "tr" and self._table_depth == 1:
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
            self._in_cell = True
        elif tag == "br" and self._in_cell and self._current_cell is not None:
            self._current_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            cell = _clean_text("".join(self._current_cell))
            self._current_row.append(cell)
            self._current_cell = None
            self._in_cell = False
        elif tag == "tr" and self._current_row is not None and self._current_table is not None:
            if any(cell for cell in self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._table_depth > 0:
            if self._table_depth == 1 and self._current_table is not None:
                self.tables.append(self._current_table)
                self._current_table = None
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._current_cell is not None:
            self._current_cell.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._in_cell and self._current_cell is not None:
            self._current_cell.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if self._in_cell and self._current_cell is not None:
            self._current_cell.append(html.unescape(f"&#{name};"))


def fetch_russell1000_html(url: str = WIKIPEDIA_RUSSELL_1000_URL) -> str:
    """Fetch the Russell 1000 Wikipedia page HTML."""

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_russell1000_components(page_html: str) -> list[Russell1000Component]:
    """Parse Russell 1000 components from a Wikipedia page HTML document."""

    parser = WikipediaTableParser()
    parser.feed(page_html)

    for table in parser.tables:
        if not table:
            continue

        headers = [_normalise_header(cell) for cell in table[0]]
        if not _is_components_table(headers):
            continue

        components = [_component_from_row(headers, row) for row in table[1:]]
        components = [component for component in components if component is not None]
        if components:
            return components

    raise ValueError("Could not find the Russell 1000 components table on the Wikipedia page.")


def load_russell1000_to_sqlite(db_path: str, components: Iterable[Russell1000Component]) -> int:
    """Create/update the SQLite table and replace stale Russell 1000 rows."""

    component_list = list(components)
    symbols = [component.symbol for component in component_list]

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS russell_1000_components (
                symbol TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                gics_sector TEXT,
                gics_sub_industry TEXT,
                source_url TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO russell_1000_components (
                symbol, company, gics_sector, gics_sub_industry, source_url, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                company = excluded.company,
                gics_sector = excluded.gics_sector,
                gics_sub_industry = excluded.gics_sub_industry,
                source_url = excluded.source_url,
                fetched_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    component.symbol,
                    component.company,
                    component.gics_sector,
                    component.gics_sub_industry,
                    WIKIPEDIA_RUSSELL_1000_URL,
                )
                for component in component_list
            ],
        )

        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            connection.execute(
                f"DELETE FROM russell_1000_components WHERE symbol NOT IN ({placeholders})",
                symbols,
            )
        else:
            connection.execute("DELETE FROM russell_1000_components")

    return len(component_list)


def sync_russell1000_from_wikipedia(db_path: str, url: str = WIKIPEDIA_RUSSELL_1000_URL) -> int:
    """Fetch Wikipedia, parse the components table, and write it to SQLite."""

    page_html = fetch_russell1000_html(url)
    components = parse_russell1000_components(page_html)
    return load_russell1000_to_sqlite(db_path, components)


def _component_from_row(headers: list[str], row: list[str]) -> Russell1000Component | None:
    values = {header: row[index] if index < len(row) else "" for index, header in enumerate(headers)}
    company = values.get("company", "")
    symbol = values.get("symbol", "")

    if not company or not symbol:
        return None

    return Russell1000Component(
        company=company,
        symbol=symbol,
        gics_sector=values.get("gics_sector") or None,
        gics_sub_industry=values.get("gics_sub_industry") or None,
    )


def _is_components_table(headers: list[str]) -> bool:
    required_headers = {"company", "symbol"}
    return required_headers.issubset(headers) and (
        "gics_sector" in headers or "gics_sub_industry" in headers
    )


def _normalise_header(value: str) -> str:
    return _clean_text(value).lower().replace(" ", "_").replace("-", "_")


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load Russell 1000 components from Wikipedia into SQLite.")
    parser.add_argument("db_path", help="Path to the SQLite database to create or update.")
    args = parser.parse_args()

    count = sync_russell1000_from_wikipedia(args.db_path)
    print(f"Loaded {count} Russell 1000 components into {args.db_path}.")


if __name__ == "__main__":
    main()
