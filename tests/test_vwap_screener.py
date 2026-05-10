import sqlite3
from datetime import date

from stock_screener.vwap_screener import (
    StoredFourHourCandle,
    find_vwap_proximity_hits,
    load_earnings_dates_to_sqlite,
    screen_russell1000_vwap_proximity,
)


def candle(symbol, day, bucket, high, low, close, volume=100):
    return StoredFourHourCandle(
        symbol=symbol,
        trading_date=date.fromisoformat(day),
        bucket_index=bucket,
        start_ts_et=f"{day}T09:30:00-04:00",
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def test_find_vwap_proximity_hits_matches_high_or_low_against_yearly_vwap_stdev():
    hits = find_vwap_proximity_hits(
        [
            candle("AAPL", "2026-01-02", 0, 10, 10, 10),
            candle("AAPL", "2026-01-02", 1, 30, 30, 30),
            candle("AAPL", "2026-01-05", 0, 20, 20, 20),
        ],
        std_threshold=0.1,
    )

    assert len(hits) == 1
    assert hits[0].symbol == "AAPL"
    assert hits[0].anchor == "yearly"
    assert hits[0].nearest_field == "low"
    assert hits[0].trading_date == date(2026, 1, 5)
    assert hits[0].distance_stdev == 0


def test_find_vwap_proximity_hits_resets_earnings_anchor_and_can_emit_first_touch_only():
    hits = find_vwap_proximity_hits(
        [
            candle("MSFT", "2026-02-02", 0, 10, 10, 10),
            candle("MSFT", "2026-02-03", 0, 30, 30, 30),
            candle("MSFT", "2026-02-04", 0, 20, 20, 20),
            candle("MSFT", "2026-02-05", 0, 20, 20, 20),
        ],
        {"MSFT": {date(2026, 2, 2)}},
        std_threshold=0.1,
        only_first_touch=True,
    )

    earnings_hits = [hit for hit in hits if hit.anchor == "earnings"]
    assert len(earnings_hits) == 1
    assert earnings_hits[0].trading_date == date(2026, 2, 4)


def test_screen_russell1000_vwap_proximity_loads_symbols_events_and_stores_hits(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "stocks.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TABLE russell_1000_components (
                symbol TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                source_url TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
        connection.execute(
            "INSERT INTO russell_1000_components (symbol, company, source_url) VALUES ('AAPL', 'Apple', 'test')"
        )
        connection.execute("""
            CREATE TABLE massive_rth_4h_candles (
                symbol TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                bucket_index INTEGER NOT NULL,
                start_ts_et TEXT NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL
            )
            """)
        connection.executemany(
            """
            INSERT INTO massive_rth_4h_candles (
                symbol, trading_date, bucket_index, start_ts_et, high, low, close, volume
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("AAPL", "2026-01-02", 0, "2026-01-02T09:30:00-05:00", 10, 10, 10, 100),
                ("AAPL", "2026-01-02", 1, "2026-01-02T13:30:00-05:00", 30, 30, 30, 100),
                ("AAPL", "2026-01-05", 0, "2026-01-05T09:30:00-05:00", 20, 20, 20, 100),
            ],
        )
    assert load_earnings_dates_to_sqlite(str(db_path), [("AAPL", "2026-01-02")]) == 1
    sync_calls = []
    monkeypatch.setattr(
        "stock_screener.vwap_screener.sync_russell1000_from_wikipedia",
        sync_calls.append,
    )

    hits = screen_russell1000_vwap_proximity(str(db_path), std_threshold=0.1)

    assert {(hit.symbol, hit.anchor, hit.trading_date) for hit in hits} == {
        ("AAPL", "yearly", date(2026, 1, 5)),
        ("AAPL", "earnings", date(2026, 1, 5)),
    }
    with sqlite3.connect(db_path) as connection:
        stored = connection.execute(
            "SELECT symbol, anchor FROM vwap_proximity_hits ORDER BY anchor"
        ).fetchall()
    assert stored == [("AAPL", "earnings"), ("AAPL", "yearly")]
    assert sync_calls == []


def test_screen_russell1000_vwap_proximity_fetches_russell_when_components_are_missing(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "stocks.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TABLE massive_rth_4h_candles (
                symbol TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                bucket_index INTEGER NOT NULL,
                start_ts_et TEXT NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL
            )
            """)
        connection.executemany(
            """
            INSERT INTO massive_rth_4h_candles (
                symbol, trading_date, bucket_index, start_ts_et, high, low, close, volume
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("MSFT", "2026-01-02", 0, "2026-01-02T09:30:00-05:00", 10, 10, 10, 100),
                ("MSFT", "2026-01-02", 1, "2026-01-02T13:30:00-05:00", 30, 30, 30, 100),
                ("MSFT", "2026-01-05", 0, "2026-01-05T09:30:00-05:00", 20, 20, 20, 100),
            ],
        )

    sync_calls = []

    def fake_sync(db_file):
        sync_calls.append(db_file)
        with sqlite3.connect(db_file) as connection:
            connection.execute("""
                CREATE TABLE russell_1000_components (
                    symbol TEXT PRIMARY KEY,
                    company TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """)
            connection.execute(
                "INSERT INTO russell_1000_components (symbol, company, source_url) VALUES ('MSFT', 'Microsoft', 'test')"
            )
        return 1

    monkeypatch.setattr(
        "stock_screener.vwap_screener.sync_russell1000_from_wikipedia", fake_sync
    )

    hits = screen_russell1000_vwap_proximity(str(db_path), std_threshold=0.1)

    assert sync_calls == [str(db_path)]
    assert [(hit.symbol, hit.anchor, hit.trading_date) for hit in hits] == [
        ("MSFT", "yearly", date(2026, 1, 5))
    ]


def test_screen_russell1000_vwap_proximity_stores_current_and_new_symbol_lists(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "stocks.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TABLE russell_1000_components (
                symbol TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                source_url TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
        connection.executemany(
            "INSERT INTO russell_1000_components (symbol, company, source_url) VALUES (?, ?, 'test')",
            [("AAPL", "Apple"), ("MSFT", "Microsoft")],
        )
        connection.execute("""
            CREATE TABLE massive_rth_4h_candles (
                symbol TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                bucket_index INTEGER NOT NULL,
                start_ts_et TEXT NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL
            )
            """)
        connection.executemany(
            """
            INSERT INTO massive_rth_4h_candles (
                symbol, trading_date, bucket_index, start_ts_et, high, low, close, volume
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("AAPL", "2026-01-02", 0, "2026-01-02T09:30:00-05:00", 10, 10, 10, 100),
                ("AAPL", "2026-01-02", 1, "2026-01-02T13:30:00-05:00", 30, 30, 30, 100),
                ("AAPL", "2026-01-05", 0, "2026-01-05T09:30:00-05:00", 20, 20, 20, 100),
                ("MSFT", "2026-01-02", 0, "2026-01-02T09:30:00-05:00", 10, 10, 10, 100),
                ("MSFT", "2026-01-02", 1, "2026-01-02T13:30:00-05:00", 30, 30, 30, 100),
                ("MSFT", "2026-01-05", 0, "2026-01-05T09:30:00-05:00", 20, 20, 20, 100),
            ],
        )
        connection.execute("""
            CREATE TABLE vwap_proximity_current_symbols (
                symbol TEXT PRIMARY KEY,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
        connection.execute(
            "INSERT INTO vwap_proximity_current_symbols (symbol) VALUES ('AAPL')"
        )

    monkeypatch.setattr(
        "stock_screener.vwap_screener.sync_russell1000_from_wikipedia",
        lambda db_file: 0,
    )

    screen_russell1000_vwap_proximity(str(db_path), std_threshold=0.1)

    with sqlite3.connect(db_path) as connection:
        current_symbols = connection.execute(
            "SELECT symbol FROM vwap_proximity_current_symbols ORDER BY symbol"
        ).fetchall()
        new_symbols = connection.execute(
            "SELECT symbol FROM vwap_proximity_new_symbols ORDER BY symbol"
        ).fetchall()

    assert current_symbols == [("AAPL",), ("MSFT",)]
    assert new_symbols == [("MSFT",)]


def test_screen_russell1000_vwap_proximity_populates_missing_massive_candles(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "stocks.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TABLE russell_1000_components (
                symbol TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                source_url TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
        connection.execute(
            "INSERT INTO russell_1000_components (symbol, company, source_url) VALUES ('AAPL', 'Apple', 'test')"
        )

    sync_calls = []

    def fake_sync(db_file, symbols, start_date, end_date):
        sync_calls.append((db_file, symbols, start_date, end_date))
        with sqlite3.connect(db_file) as connection:
            connection.execute("""
                CREATE TABLE massive_rth_4h_candles (
                    symbol TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    bucket_index INTEGER NOT NULL,
                    start_ts_et TEXT NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL
                )
                """)
            connection.executemany(
                """
                INSERT INTO massive_rth_4h_candles (
                    symbol, trading_date, bucket_index, start_ts_et, high, low, close, volume
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "AAPL",
                        "2026-01-02",
                        0,
                        "2026-01-02T09:30:00-05:00",
                        10,
                        10,
                        10,
                        100,
                    ),
                    (
                        "AAPL",
                        "2026-01-02",
                        1,
                        "2026-01-02T13:30:00-05:00",
                        30,
                        30,
                        30,
                        100,
                    ),
                    (
                        "AAPL",
                        "2026-01-05",
                        0,
                        "2026-01-05T09:30:00-05:00",
                        20,
                        20,
                        20,
                        100,
                    ),
                ],
            )
        return 3

    monkeypatch.setattr(
        "stock_screener.vwap_screener.sync_massive_rth_4h_candles", fake_sync
    )

    hits = screen_russell1000_vwap_proximity(
        str(db_path),
        std_threshold=0.1,
        massive_start_date="2026-01-01",
        massive_end_date="2026-01-31",
    )

    assert sync_calls == [(str(db_path), ["AAPL"], date(2026, 1, 1), date(2026, 1, 31))]
    assert [(hit.symbol, hit.anchor, hit.trading_date) for hit in hits] == [
        ("AAPL", "yearly", date(2026, 1, 5))
    ]
