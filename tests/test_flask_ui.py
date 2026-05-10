import sqlite3

import pytest

from stock_screener.flask_ui import (
    format_market_cap,
    load_new_proximity_rows,
    load_proximity_rows,
)


def seed_database(db_path):
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TABLE russell_1000_components (
                symbol TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                market_cap REAL,
                source_url TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
        connection.executemany(
            "INSERT INTO russell_1000_components (symbol, company, market_cap, source_url) VALUES (?, ?, ?, 'test')",
            [
                ("SMALL", "Small Co", 1_000_000_000),
                ("MEGA", "Mega Corp", 3_200_000_000_000),
            ],
        )
        connection.execute("""
            CREATE TABLE vwap_proximity_hits (
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
                screened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
        connection.executemany(
            """
            INSERT INTO vwap_proximity_hits (
                symbol, trading_date, bucket_index, start_ts_et, anchor, vwap, stdev,
                distance_stdev, nearest_field, high, low, close, volume, std_threshold, only_first_touch
            )
            VALUES (?, ?, ?, ?, ?, 1, 1, 0, 'low', 1, 1, 1, 100, 0.1, 0)
            """,
            [
                ("SMALL", "2026-01-02", 0, "2026-01-02T09:30:00-05:00", "yearly"),
                ("MEGA", "2026-01-03", 0, "2026-01-03T09:30:00-05:00", "earnings"),
                ("MEGA", "2026-01-04", 1, "2026-01-04T13:30:00-05:00", "yearly"),
            ],
        )
        connection.execute("""
            CREATE TABLE vwap_proximity_new_symbols (
                symbol TEXT PRIMARY KEY,
                detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
        connection.execute(
            "INSERT INTO vwap_proximity_new_symbols (symbol, detected_at) VALUES ('MEGA', '2026-01-05 12:00:00')"
        )


def test_load_proximity_rows_sorts_by_market_cap_and_formats_values(tmp_path):
    db_path = tmp_path / "stocks.sqlite3"
    seed_database(db_path)

    rows = load_proximity_rows(str(db_path))

    assert [row.symbol for row in rows] == ["MEGA", "SMALL"]
    assert rows[0].company == "Mega Corp"
    assert rows[0].formatted_market_cap == "$3.20T"
    assert rows[0].latest_trading_date == "2026-01-04"
    assert rows[0].latest_anchor == "yearly"
    assert rows[0].hit_count == 2
    assert rows[0].trading_view_url == "https://www.tradingview.com/chart/?symbol=MEGA"


def test_format_market_cap_handles_missing_values():
    assert format_market_cap(None) == "—"
    assert format_market_cap(2500) == "$2.50K"


def test_load_new_proximity_rows_returns_latest_scan_additions(tmp_path):
    db_path = tmp_path / "stocks.sqlite3"
    seed_database(db_path)

    rows = load_new_proximity_rows(str(db_path))

    assert [row.symbol for row in rows] == ["MEGA"]
    assert rows[0].company == "Mega Corp"
    assert rows[0].formatted_market_cap == "$3.20T"
    assert rows[0].detected_at == "2026-01-05 12:00:00"


def test_flask_index_renders_clickable_trading_view_rows(tmp_path):
    pytest.importorskip("flask")
    from stock_screener.flask_ui import create_app

    db_path = tmp_path / "stocks.sqlite3"
    seed_database(db_path)
    app = create_app(str(db_path))

    response = app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "VWAP Proximity" in html
    assert "Mega Corp" in html
    assert "$3.20T" in html
    assert "https://www.tradingview.com/chart/?symbol=MEGA" in html


def test_flask_index_renders_run_scan_button(tmp_path):
    pytest.importorskip("flask")
    from stock_screener.flask_ui import create_app

    db_path = tmp_path / "stocks.sqlite3"
    seed_database(db_path)
    app = create_app(str(db_path))

    response = app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'action="/run-scan"' in html
    assert 'method="post"' in html
    assert "Run Scan" in html


def test_flask_index_renders_new_proximity_stocks(tmp_path):
    pytest.importorskip("flask")
    from stock_screener.flask_ui import create_app

    db_path = tmp_path / "stocks.sqlite3"
    seed_database(db_path)
    app = create_app(str(db_path))

    response = app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "New proximity stocks" in html
    assert "Stocks in the latest proximity list" in html


def test_run_scan_button_invokes_screener_and_reports_status(tmp_path):
    pytest.importorskip("flask")
    from stock_screener.flask_ui import create_app

    db_path = tmp_path / "stocks.sqlite3"
    seed_database(db_path)
    calls = []

    def fake_runner(db_file):
        calls.append(db_file)
        return [object(), object()]

    app = create_app(str(db_path), scan_runner=fake_runner)

    response = app.test_client().post("/run-scan", follow_redirects=True)

    assert response.status_code == 200
    assert calls == [str(db_path)]
    assert "Scan complete. Stored 2 proximity hits." in response.get_data(as_text=True)
