"""Screen Russell 1000 4-hour candles for yearly/earnings VWAP proximity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import sqrt
from typing import Iterable, Literal
import argparse
import csv
import sqlite3
import sys

AnchorKind = Literal["earnings", "yearly"]


@dataclass(frozen=True)
class StoredFourHourCandle:
    """A 4-hour regular-trading-hours candle loaded from SQLite."""

    symbol: str
    trading_date: date
    bucket_index: int
    start_ts_et: str
    high: float
    low: float
    close: float
    volume: float

    @property
    def hlc3(self) -> float:
        """Return the average price used by TradingView's hlc3 source."""

        return (self.high + self.low + self.close) / 3


@dataclass(frozen=True)
class VwapProximityHit:
    """A candle whose high or low is near an anchored VWAP in stdev units."""

    symbol: str
    trading_date: date
    bucket_index: int
    start_ts_et: str
    anchor: AnchorKind
    vwap: float
    stdev: float
    distance_stdev: float
    nearest_field: Literal["high", "low"]
    high: float
    low: float
    close: float
    volume: float


@dataclass
class _AnchoredVwapState:
    cumulative_price_volume: float = 0.0
    cumulative_volume: float = 0.0
    sn: float = 0.0
    previous_vwap: float = 0.0
    active: bool = False
    touched: bool = False

    def update(self, candle: StoredFourHourCandle, *, reset: bool) -> tuple[float, float]:
        """Update VWAP/stdev with the same incremental formula as the Pine script."""

        price = candle.hlc3
        if reset or not self.active:
            self.cumulative_price_volume = price * candle.volume
            self.cumulative_volume = candle.volume
            self.sn = 0.0
            self.previous_vwap = 0.0
            self.active = True
            self.touched = False
        else:
            self.cumulative_price_volume += price * candle.volume
            self.cumulative_volume += candle.volume

        vwap = self.cumulative_price_volume / self.cumulative_volume if self.cumulative_volume else 0.0
        if reset:
            self.sn = 0.0
        else:
            self.sn += candle.volume * (price - self.previous_vwap) * (price - vwap)
        self.previous_vwap = vwap
        stdev = sqrt(self.sn / self.cumulative_volume) if self.cumulative_volume and self.sn >= 0 else 0.0
        return vwap, stdev


def find_vwap_proximity_hits(
    candles: Iterable[StoredFourHourCandle],
    earnings_dates_by_symbol: dict[str, set[date]] | None = None,
    *,
    std_threshold: float = 0.1,
    only_first_touch: bool = False,
) -> list[VwapProximityHit]:
    """Find candles with high/low within ``std_threshold`` stdevs of yearly or earnings VWAP.

    The yearly anchor resets on the first candle of each calendar year. The
    earnings anchor resets on the first 4-hour regular-session candle whose
    trading date appears in ``earnings_dates_by_symbol`` for that symbol.
    """

    if std_threshold < 0:
        raise ValueError("std_threshold must be non-negative.")

    earnings_dates_by_symbol = earnings_dates_by_symbol or {}
    ordered = sorted(candles, key=lambda candle: (candle.symbol, candle.trading_date, candle.bucket_index))
    hits: list[VwapProximityHit] = []
    yearly_states: dict[str, _AnchoredVwapState] = {}
    earnings_states: dict[str, _AnchoredVwapState] = {}
    last_year_by_symbol: dict[str, int] = {}
    used_earnings_anchors: set[tuple[str, date]] = set()

    for candle in ordered:
        symbol = candle.symbol.upper()
        year_reset = last_year_by_symbol.get(symbol) != candle.trading_date.year
        last_year_by_symbol[symbol] = candle.trading_date.year
        yearly_state = yearly_states.setdefault(symbol, _AnchoredVwapState())
        yearly_vwap, yearly_stdev = yearly_state.update(candle, reset=year_reset)
        _append_hit_if_near(
            hits,
            candle,
            "yearly",
            yearly_state,
            yearly_vwap,
            yearly_stdev,
            std_threshold,
            only_first_touch,
        )

        earnings_dates = earnings_dates_by_symbol.get(symbol, set())
        earnings_state = earnings_states.setdefault(symbol, _AnchoredVwapState())
        earnings_key = (symbol, candle.trading_date)
        earnings_reset = candle.trading_date in earnings_dates and earnings_key not in used_earnings_anchors
        if earnings_reset:
            used_earnings_anchors.add(earnings_key)
        if earnings_reset or earnings_state.active:
            earnings_vwap, earnings_stdev = earnings_state.update(candle, reset=earnings_reset)
            _append_hit_if_near(
                hits,
                candle,
                "earnings",
                earnings_state,
                earnings_vwap,
                earnings_stdev,
                std_threshold,
                only_first_touch,
            )

    return hits


def load_earnings_dates_to_sqlite(db_path: str, rows: Iterable[tuple[str, date | str]]) -> int:
    """Create/update the earnings event table used to reset earnings VWAP."""

    parsed_rows = [(symbol.strip().upper(), _date_arg(earnings_date).isoformat()) for symbol, earnings_date in rows]
    with sqlite3.connect(db_path) as connection:
        _ensure_earnings_table(connection)
        connection.executemany(
            """
            INSERT INTO earnings_events (symbol, earnings_date, source, fetched_at)
            VALUES (?, ?, 'csv', CURRENT_TIMESTAMP)
            ON CONFLICT(symbol, earnings_date) DO UPDATE SET
                source = excluded.source,
                fetched_at = CURRENT_TIMESTAMP
            """,
            parsed_rows,
        )
    return len(parsed_rows)


def load_earnings_csv_to_sqlite(db_path: str, csv_path: str) -> int:
    """Load earnings dates from a CSV with ``symbol`` and ``earnings_date`` columns."""

    with open(csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not {"symbol", "earnings_date"}.issubset(reader.fieldnames):
            raise ValueError("CSV must include symbol and earnings_date columns.")
        rows = [(row["symbol"], row["earnings_date"]) for row in reader]
    return load_earnings_dates_to_sqlite(db_path, rows)


def screen_russell1000_vwap_proximity(
    db_path: str,
    *,
    std_threshold: float = 0.1,
    only_first_touch: bool = False,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    symbol_limit: int | None = None,
    store_results: bool = True,
) -> list[VwapProximityHit]:
    """Run the screener against Russell 1000 symbols and stored Massive 4-hour candles."""

    with sqlite3.connect(db_path) as connection:
        symbols = _load_russell_symbols(connection, symbol_limit)
        candles = _load_candles(connection, symbols, start_date=start_date, end_date=end_date)
        earnings_dates = _load_earnings_dates(connection, symbols)
        hits = find_vwap_proximity_hits(
            candles,
            earnings_dates,
            std_threshold=std_threshold,
            only_first_touch=only_first_touch,
        )
        if store_results:
            _store_hits(connection, hits, std_threshold=std_threshold, only_first_touch=only_first_touch)
        return hits


def _append_hit_if_near(
    hits: list[VwapProximityHit],
    candle: StoredFourHourCandle,
    anchor: AnchorKind,
    state: _AnchoredVwapState,
    vwap: float,
    stdev: float,
    std_threshold: float,
    only_first_touch: bool,
) -> None:
    if stdev <= 0:
        return

    low_distance = abs(candle.low - vwap) / stdev
    high_distance = abs(candle.high - vwap) / stdev
    distance, nearest_field = (low_distance, "low") if low_distance <= high_distance else (high_distance, "high")
    if distance > std_threshold:
        return
    if only_first_touch and state.touched:
        return

    hits.append(
        VwapProximityHit(
            symbol=candle.symbol,
            trading_date=candle.trading_date,
            bucket_index=candle.bucket_index,
            start_ts_et=candle.start_ts_et,
            anchor=anchor,
            vwap=vwap,
            stdev=stdev,
            distance_stdev=distance,
            nearest_field=nearest_field,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
        )
    )
    state.touched = True


def _ensure_earnings_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS earnings_events (
            symbol TEXT NOT NULL,
            earnings_date TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, earnings_date)
        )
        """
    )


def _load_russell_symbols(connection: sqlite3.Connection, limit: int | None) -> list[str]:
    query = "SELECT symbol FROM russell_1000_components ORDER BY symbol"
    params: tuple[int, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    return [row[0] for row in connection.execute(query, params).fetchall()]


def _load_candles(
    connection: sqlite3.Connection,
    symbols: list[str],
    *,
    start_date: date | str | None,
    end_date: date | str | None,
) -> list[StoredFourHourCandle]:
    if not symbols:
        return []
    params: list[str] = symbols.copy()
    filters = [f"symbol IN ({','.join('?' for _ in symbols)})"]
    if start_date is not None:
        filters.append("trading_date >= ?")
        params.append(_date_arg(start_date).isoformat())
    if end_date is not None:
        filters.append("trading_date <= ?")
        params.append(_date_arg(end_date).isoformat())

    rows = connection.execute(
        f"""
        SELECT symbol, trading_date, bucket_index, start_ts_et, high, low, close, volume
        FROM massive_rth_4h_candles
        WHERE {' AND '.join(filters)}
        ORDER BY symbol, trading_date, bucket_index
        """,
        params,
    ).fetchall()
    return [
        StoredFourHourCandle(
            symbol=row[0],
            trading_date=date.fromisoformat(row[1]),
            bucket_index=row[2],
            start_ts_et=row[3],
            high=row[4],
            low=row[5],
            close=row[6],
            volume=row[7],
        )
        for row in rows
    ]


def _load_earnings_dates(connection: sqlite3.Connection, symbols: list[str]) -> dict[str, set[date]]:
    _ensure_earnings_table(connection)
    if not symbols:
        return {}
    rows = connection.execute(
        f"""
        SELECT symbol, earnings_date
        FROM earnings_events
        WHERE symbol IN ({','.join('?' for _ in symbols)})
        """,
        symbols,
    ).fetchall()
    earnings_dates: dict[str, set[date]] = {}
    for symbol, earnings_date in rows:
        earnings_dates.setdefault(symbol.upper(), set()).add(date.fromisoformat(earnings_date))
    return earnings_dates


def _store_hits(
    connection: sqlite3.Connection,
    hits: list[VwapProximityHit],
    *,
    std_threshold: float,
    only_first_touch: bool,
) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS vwap_proximity_hits (
            symbol TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            bucket_index INTEGER NOT NULL,
            start_ts_et TEXT NOT NULL,
            anchor TEXT NOT NULL,
            vwap REAL NOT NULL,
            stdev REAL NOT NULL,
            distance_stdev REAL NOT NULL,
            nearest_field TEXT NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            std_threshold REAL NOT NULL,
            only_first_touch INTEGER NOT NULL,
            screened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, trading_date, bucket_index, anchor, std_threshold, only_first_touch)
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO vwap_proximity_hits (
            symbol, trading_date, bucket_index, start_ts_et, anchor, vwap, stdev,
            distance_stdev, nearest_field, high, low, close, volume,
            std_threshold, only_first_touch, screened_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(symbol, trading_date, bucket_index, anchor, std_threshold, only_first_touch) DO UPDATE SET
            start_ts_et = excluded.start_ts_et,
            vwap = excluded.vwap,
            stdev = excluded.stdev,
            distance_stdev = excluded.distance_stdev,
            nearest_field = excluded.nearest_field,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            screened_at = CURRENT_TIMESTAMP
        """,
        [
            (
                hit.symbol,
                hit.trading_date.isoformat(),
                hit.bucket_index,
                hit.start_ts_et,
                hit.anchor,
                hit.vwap,
                hit.stdev,
                hit.distance_stdev,
                hit.nearest_field,
                hit.high,
                hit.low,
                hit.close,
                hit.volume,
                std_threshold,
                int(only_first_touch),
            )
            for hit in hits
        ],
    )


def _date_arg(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _print_hits(hits: list[VwapProximityHit]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(
        [
            "symbol",
            "trading_date",
            "bucket_index",
            "start_ts_et",
            "anchor",
            "nearest_field",
            "distance_stdev",
            "vwap",
            "stdev",
            "high",
            "low",
            "close",
            "volume",
        ]
    )
    for hit in hits:
        writer.writerow(
            [
                hit.symbol,
                hit.trading_date.isoformat(),
                hit.bucket_index,
                hit.start_ts_et,
                hit.anchor,
                hit.nearest_field,
                f"{hit.distance_stdev:.6f}",
                f"{hit.vwap:.6f}",
                f"{hit.stdev:.6f}",
                hit.high,
                hit.low,
                hit.close,
                hit.volume,
            ]
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Screen Russell 1000 4-hour RTH candles for yearly/earnings VWAP proximity."
    )
    subparsers = parser.add_subparsers(dest="command")

    load_earnings = subparsers.add_parser("load-earnings", help="Load earnings reset dates from CSV.")
    load_earnings.add_argument("db_path", help="SQLite database path.")
    load_earnings.add_argument("csv_path", help="CSV with symbol and earnings_date columns.")

    screen = subparsers.add_parser("screen", help="Run the VWAP proximity screener.")
    screen.add_argument("db_path", help="SQLite database path.")
    screen.add_argument("--std-threshold", type=float, default=0.1, help="Max high/low distance in stdev units.")
    screen.add_argument(
        "--only-first-touch",
        action="store_true",
        help="Emit only the first touch per symbol/anchor reset.",
    )
    screen.add_argument("--start", help="Optional start date in YYYY-MM-DD format.")
    screen.add_argument("--end", help="Optional end date in YYYY-MM-DD format.")
    screen.add_argument("--symbol-limit", type=int, help="Limit Russell 1000 symbols for smoke tests.")
    screen.add_argument("--no-store", action="store_true", help="Print hits without writing vwap_proximity_hits.")

    args = parser.parse_args()
    if args.command == "load-earnings":
        count = load_earnings_csv_to_sqlite(args.db_path, args.csv_path)
        print(f"Loaded {count} earnings events into {args.db_path}.")
        return
    if args.command == "screen":
        hits = screen_russell1000_vwap_proximity(
            args.db_path,
            std_threshold=args.std_threshold,
            only_first_touch=args.only_first_touch,
            start_date=args.start,
            end_date=args.end,
            symbol_limit=args.symbol_limit,
            store_results=not args.no_store,
        )
        _print_hits(hits)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
