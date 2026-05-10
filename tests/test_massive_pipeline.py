import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from stock_screener.massive_pipeline import (
    MassiveClient,
    MassiveMinuteBar,
    build_rth_4h_candles,
    load_massive_4h_candles_to_sqlite,
)

EASTERN = ZoneInfo("America/New_York")


def ms_at(year, month, day, hour, minute):
    return int(
        datetime(year, month, day, hour, minute, tzinfo=EASTERN)
        .astimezone(timezone.utc)
        .timestamp()
        * 1000
    )


def bar_at(
    hour,
    minute,
    open_price,
    close_price,
    volume=100,
    symbol="AAPL",
    vwap=None,
    transactions=1,
):
    high = max(open_price, close_price) + 1
    low = min(open_price, close_price) - 1
    return MassiveMinuteBar(
        symbol=symbol,
        timestamp_ms=ms_at(2026, 5, 4, hour, minute),
        open=open_price,
        high=high,
        low=low,
        close=close_price,
        volume=volume,
        vwap=vwap,
        transactions=transactions,
    )


def test_build_rth_4h_candles_filters_extended_hours_and_buckets_regular_session():
    candles = build_rth_4h_candles(
        [
            bar_at(9, 29, 1, 1),
            bar_at(9, 30, 10, 11, vwap=10.5, transactions=2),
            bar_at(13, 29, 12, 13, volume=300, vwap=12.5, transactions=3),
            bar_at(13, 30, 20, 21, vwap=20.5, transactions=4),
            bar_at(15, 59, 22, 23, volume=200, vwap=22.5, transactions=5),
            bar_at(16, 0, 99, 99),
        ]
    )

    assert len(candles) == 2
    assert candles[0].bucket_index == 0
    assert candles[0].start.isoformat() == "2026-05-04T09:30:00-04:00"
    assert candles[0].end.isoformat() == "2026-05-04T13:30:00-04:00"
    assert candles[0].open == 10
    assert candles[0].high == 14
    assert candles[0].low == 9
    assert candles[0].close == 13
    assert candles[0].volume == 400
    assert candles[0].vwap == 12.0
    assert candles[0].transactions == 5
    assert candles[0].minute_count == 2

    assert candles[1].bucket_index == 1
    assert candles[1].start.isoformat() == "2026-05-04T13:30:00-04:00"
    assert candles[1].end.isoformat() == "2026-05-04T16:00:00-04:00"
    assert candles[1].open == 20
    assert candles[1].close == 23
    assert candles[1].volume == 300


def test_load_massive_4h_candles_to_sqlite_upserts_rows(tmp_path):
    db_path = tmp_path / "stocks.sqlite3"
    initial = build_rth_4h_candles([bar_at(9, 30, 10, 11)])
    updated = build_rth_4h_candles([bar_at(9, 30, 15, 16)])

    assert load_massive_4h_candles_to_sqlite(str(db_path), initial) == 1
    assert load_massive_4h_candles_to_sqlite(str(db_path), updated) == 1

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("""
            SELECT symbol, trading_date, bucket_index, start_ts_et, end_ts_et, open, close,
                   volume, minute_count, source
            FROM massive_rth_4h_candles
            """).fetchall()

    assert rows == [
        (
            "AAPL",
            "2026-05-04",
            0,
            "2026-05-04T09:30:00-04:00",
            "2026-05-04T13:30:00-04:00",
            15.0,
            16.0,
            100.0,
            1,
            "massive",
        )
    ]


def test_massive_client_fetch_minute_bars_paginates_and_adds_api_key(monkeypatch):
    requested_urls = []
    payloads = [
        {
            "results": [
                {
                    "t": ms_at(2026, 5, 4, 9, 30),
                    "o": 1,
                    "h": 2,
                    "l": 0.5,
                    "c": 1.5,
                    "v": 10,
                }
            ],
            "next_url": "https://api.massive.com/next-page?cursor=abc",
        },
        {
            "results": [
                {
                    "t": ms_at(2026, 5, 4, 9, 31),
                    "o": 2,
                    "h": 3,
                    "l": 1.5,
                    "c": 2.5,
                    "v": 20,
                }
            ],
        },
    ]

    def fake_get_json(self, url):
        requested_urls.append(url)
        return payloads.pop(0)

    monkeypatch.setattr(MassiveClient, "_get_json", fake_get_json)

    bars = MassiveClient("secret").fetch_minute_bars("AAPL", "2026-05-04", "2026-05-04")

    assert [bar.close for bar in bars] == [1.5, 2.5]
    assert requested_urls[0].startswith(
        "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/minute/2026-05-04/2026-05-04?"
    )
    assert "apiKey=secret" in requested_urls[0]
    assert (
        requested_urls[1]
        == "https://api.massive.com/next-page?cursor=abc&apiKey=secret"
    )


def test_massive_client_from_env_loads_api_key_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        'MASSIVE_API_KEY="dotenv-secret"\n', encoding="utf-8"
    )

    client = MassiveClient.from_env()

    assert client.api_key == "dotenv-secret"
