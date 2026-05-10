"""Build regular-trading-hours stock candles from Massive minute aggregates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
from zoneinfo import ZoneInfo
import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request

MASSIVE_BASE_URL = "https://api.massive.com"
MASSIVE_API_KEY_ENV = "MASSIVE_API_KEY"
USER_AGENT = "stockScreener Massive 4h RTH pipeline/0.1"
EASTERN_TZ = ZoneInfo("America/New_York")
REGULAR_TRADING_OPEN = time(9, 30)
REGULAR_TRADING_CLOSE = time(16, 0)
FOUR_HOUR_BUCKET_MINUTES = 240


@dataclass(frozen=True)
class MassiveMinuteBar:
    """A raw one-minute aggregate returned by Massive."""

    symbol: str
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    transactions: int | None = None


@dataclass(frozen=True)
class FourHourCandle:
    """A regular-trading-hours candle aggregated from minute bars."""

    symbol: str
    trading_date: date
    bucket_index: int
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None
    transactions: int | None
    minute_count: int


class MassiveClient:
    """Tiny Massive REST client for paginated stock aggregate requests."""

    def __init__(self, api_key: str, base_url: str = MASSIVE_BASE_URL) -> None:
        if not api_key:
            raise ValueError("A Massive API key is required.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_env(cls, env_var: str = MASSIVE_API_KEY_ENV) -> "MassiveClient":
        """Create a client using the API key stored in an environment variable or .env."""

        _load_dotenv()
        api_key = os.environ.get(env_var, "")
        if not api_key:
            raise ValueError(f"Set {env_var} in your environment or .env file.")
        return cls(api_key)

    def fetch_minute_bars(
        self,
        symbol: str,
        start_date: date | str,
        end_date: date | str,
        *,
        adjusted: bool = True,
        limit: int = 50_000,
    ) -> list[MassiveMinuteBar]:
        """Fetch sorted one-minute aggregate bars for a ticker and date range."""

        start = _date_arg(start_date)
        end = _date_arg(end_date)
        path = f"/v2/aggs/ticker/{symbol}/range/1/minute/{start.isoformat()}/{end.isoformat()}"
        url = f"{self.base_url}{path}?{urlencode({'adjusted': str(adjusted).lower(), 'sort': 'asc', 'limit': limit, 'apiKey': self.api_key})}"

        bars: list[MassiveMinuteBar] = []
        while url:
            payload = self._get_json(url)
            for raw_bar in payload.get("results", []) or []:
                bars.append(_minute_bar_from_payload(symbol, raw_bar))
            next_url = payload.get("next_url")
            url = self._with_api_key(next_url) if next_url else ""

        return bars

    def _get_json(self, url: str) -> dict:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return json.loads(response.read().decode(charset, errors="replace"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Massive API request failed with HTTP {error.code}: {body}"
            ) from error

    def _with_api_key(self, url: str) -> str:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("apiKey", self.api_key)
        return urlunparse(parsed._replace(query=urlencode(query)))


def _load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE pairs from a local .env file without overriding the environment."""

    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_value(value.strip())


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def build_rth_4h_candles(
    minute_bars: Iterable[MassiveMinuteBar],
) -> list[FourHourCandle]:
    """Filter to US regular trading hours and aggregate into 4-hour session buckets.

    Because the US regular session is 6.5 hours long, each full trading day can
    produce one 240-minute candle from 09:30-13:30 ET and one shorter closing
    candle from 13:30-16:00 ET.
    """

    buckets: dict[tuple[str, date, int], list[MassiveMinuteBar]] = {}
    for bar in minute_bars:
        local_start = _local_start(bar.timestamp_ms)
        if not _is_regular_trading_minute(local_start):
            continue
        bucket_index = _rth_bucket_index(local_start)
        key = (bar.symbol, local_start.date(), bucket_index)
        buckets.setdefault(key, []).append(bar)

    candles = [_aggregate_bucket(key, bars) for key, bars in buckets.items()]
    return sorted(candles, key=lambda candle: (candle.symbol, candle.start))


def load_massive_4h_candles_to_sqlite(
    db_path: str, candles: Iterable[FourHourCandle]
) -> int:
    """Create/update the SQLite table for Massive regular-hours 4-hour candles."""

    candle_list = list(candles)
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS massive_rth_4h_candles (
                symbol TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                bucket_index INTEGER NOT NULL,
                start_ts_utc TEXT NOT NULL,
                end_ts_utc TEXT NOT NULL,
                start_ts_et TEXT NOT NULL,
                end_ts_et TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                vwap REAL,
                transactions INTEGER,
                minute_count INTEGER NOT NULL,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, trading_date, bucket_index)
            )
            """)
        connection.executemany(
            """
            INSERT INTO massive_rth_4h_candles (
                symbol, trading_date, bucket_index, start_ts_utc, end_ts_utc,
                start_ts_et, end_ts_et, open, high, low, close, volume, vwap,
                transactions, minute_count, source, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'massive', CURRENT_TIMESTAMP)
            ON CONFLICT(symbol, trading_date, bucket_index) DO UPDATE SET
                start_ts_utc = excluded.start_ts_utc,
                end_ts_utc = excluded.end_ts_utc,
                start_ts_et = excluded.start_ts_et,
                end_ts_et = excluded.end_ts_et,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                vwap = excluded.vwap,
                transactions = excluded.transactions,
                minute_count = excluded.minute_count,
                source = excluded.source,
                fetched_at = CURRENT_TIMESTAMP
            """,
            [_sqlite_row(candle) for candle in candle_list],
        )
    return len(candle_list)


def sync_massive_rth_4h_candles(
    db_path: str,
    symbols: Sequence[str],
    start_date: date | str,
    end_date: date | str,
    *,
    client: MassiveClient | None = None,
    adjusted: bool = True,
) -> int:
    """Fetch Massive minute bars, build RTH 4-hour candles, and store them in SQLite."""

    api_client = client or MassiveClient.from_env()
    all_candles: list[FourHourCandle] = []
    for symbol in symbols:
        minute_bars = api_client.fetch_minute_bars(
            symbol, start_date, end_date, adjusted=adjusted
        )
        all_candles.extend(build_rth_4h_candles(minute_bars))
    return load_massive_4h_candles_to_sqlite(db_path, all_candles)


def load_symbols_from_sqlite(db_path: str, limit: int | None = None) -> list[str]:
    """Load symbols from the Russell 1000 component table."""

    query = "SELECT symbol FROM russell_1000_components ORDER BY symbol"
    params: tuple[int, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    with sqlite3.connect(db_path) as connection:
        return [row[0] for row in connection.execute(query, params).fetchall()]


def _aggregate_bucket(
    key: tuple[str, date, int], bars: list[MassiveMinuteBar]
) -> FourHourCandle:
    symbol, trading_day, bucket_index = key
    ordered = sorted(bars, key=lambda bar: bar.timestamp_ms)
    local_start = _local_start(ordered[0].timestamp_ms)
    bucket_start = _rth_bucket_start(trading_day, bucket_index)
    bucket_end = min(
        bucket_start + timedelta(minutes=FOUR_HOUR_BUCKET_MINUTES),
        datetime.combine(trading_day, REGULAR_TRADING_CLOSE, EASTERN_TZ),
    )
    total_volume = sum(bar.volume for bar in ordered)
    weighted_vwap = sum(
        (bar.vwap or 0) * bar.volume for bar in ordered if bar.vwap is not None
    )
    vwap_volume = sum(bar.volume for bar in ordered if bar.vwap is not None)
    transactions = [bar.transactions for bar in ordered if bar.transactions is not None]

    return FourHourCandle(
        symbol=symbol,
        trading_date=trading_day,
        bucket_index=bucket_index,
        start=bucket_start if local_start >= bucket_start else local_start,
        end=bucket_end,
        open=ordered[0].open,
        high=max(bar.high for bar in ordered),
        low=min(bar.low for bar in ordered),
        close=ordered[-1].close,
        volume=total_volume,
        vwap=weighted_vwap / vwap_volume if vwap_volume else None,
        transactions=sum(transactions) if transactions else None,
        minute_count=len(ordered),
    )


def _sqlite_row(candle: FourHourCandle) -> tuple:
    return (
        candle.symbol,
        candle.trading_date.isoformat(),
        candle.bucket_index,
        candle.start.astimezone(timezone.utc).isoformat(),
        candle.end.astimezone(timezone.utc).isoformat(),
        candle.start.isoformat(),
        candle.end.isoformat(),
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume,
        candle.vwap,
        candle.transactions,
        candle.minute_count,
    )


def _minute_bar_from_payload(symbol: str, raw_bar: dict) -> MassiveMinuteBar:
    return MassiveMinuteBar(
        symbol=symbol.upper(),
        timestamp_ms=int(raw_bar["t"]),
        open=float(raw_bar["o"]),
        high=float(raw_bar["h"]),
        low=float(raw_bar["l"]),
        close=float(raw_bar["c"]),
        volume=float(raw_bar.get("v", 0)),
        vwap=float(raw_bar["vw"]) if raw_bar.get("vw") is not None else None,
        transactions=int(raw_bar["n"]) if raw_bar.get("n") is not None else None,
    )


def _date_arg(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _local_start(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone(
        EASTERN_TZ
    )


def _is_regular_trading_minute(local_start: datetime) -> bool:
    local_time = local_start.timetz().replace(tzinfo=None)
    return (
        local_start.weekday() < 5
        and REGULAR_TRADING_OPEN <= local_time < REGULAR_TRADING_CLOSE
    )


def _rth_bucket_index(local_start: datetime) -> int:
    session_open = datetime.combine(
        local_start.date(), REGULAR_TRADING_OPEN, EASTERN_TZ
    )
    elapsed_minutes = int((local_start - session_open).total_seconds() // 60)
    return elapsed_minutes // FOUR_HOUR_BUCKET_MINUTES


def _rth_bucket_start(trading_day: date, bucket_index: int) -> datetime:
    return datetime.combine(trading_day, REGULAR_TRADING_OPEN, EASTERN_TZ) + timedelta(
        minutes=bucket_index * FOUR_HOUR_BUCKET_MINUTES
    )


def _parse_symbols(args: argparse.Namespace) -> list[str]:
    symbols = (
        [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
        if args.symbols
        else []
    )
    if symbols:
        return symbols
    return load_symbols_from_sqlite(args.db_path, args.symbol_limit)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Massive minute aggregates and store regular-trading-hours 4-hour candles."
    )
    parser.add_argument(
        "db_path", help="Path to the SQLite database to create or update."
    )
    parser.add_argument(
        "--start", required=True, help="Start date in YYYY-MM-DD format."
    )
    parser.add_argument("--end", required=True, help="End date in YYYY-MM-DD format.")
    parser.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to russell_1000_components.",
    )
    parser.add_argument(
        "--symbol-limit",
        type=int,
        help="Limit symbols loaded from russell_1000_components.",
    )
    parser.add_argument(
        "--unadjusted", action="store_true", help="Request unadjusted Massive bars."
    )
    args = parser.parse_args()

    symbols = _parse_symbols(args)
    if not symbols:
        raise SystemExit(
            "No symbols were provided or found in russell_1000_components."
        )

    count = sync_massive_rth_4h_candles(
        args.db_path,
        symbols,
        args.start,
        args.end,
        adjusted=not args.unadjusted,
    )
    print(f"Loaded {count} Massive RTH 4-hour candles into {args.db_path}.")


if __name__ == "__main__":
    main()
