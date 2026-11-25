"""Cache a closed-candle interval and detect crossings of a fixed price."""
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode
from urllib.request import urlopen


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: int
    opening: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def from_values(cls, values: list | tuple) -> "Candle":
        if len(values) != 6 or type(values[0]) is not int:
            raise ValueError("Ожидается время открытия и пять значений OHLCV.")
        try:
            opening, high, low, close, volume = (Decimal(str(value)) for value in values[1:])
        except InvalidOperation as error:
            raise ValueError("Свеча содержит некорректное число.") from error
        if (
            not all(value.is_finite() for value in (opening, high, low, close, volume))
            or not 0 <= low <= min(opening, close) <= max(opening, close) <= high
            or volume < 0
        ):
            raise ValueError("Свеча содержит недопустимые цены или объём.")
        return cls(values[0], opening, high, low, close, volume)

    def as_values(self) -> tuple:
        return self.timestamp, *(str(value) for value in (
            self.opening, self.high, self.low, self.close, self.volume,
        ))


class HttpFeed:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def fetch(self, symbol: str, step: int, start: int, end: int) -> list[Candle]:
        query = urlencode(dict(symbol=symbol, step=step, start=start, end=end))
        with urlopen(f"{self.base_url}/candles?{query}", timeout=4) as response:
            payload = json.load(response)
        if not isinstance(payload, list) or any(not isinstance(row, list) for row in payload):
            raise ValueError("Источник вернул данные в неожиданном формате.")
        return [Candle.from_values(row) for row in payload]


def missing_intervals(timestamps: range, cached: dict[int, Candle]) -> Iterable[tuple[int, int]]:
    first = None
    for timestamp in timestamps:
        if timestamp not in cached:
            if first is None:
                first = timestamp
        elif first is not None:
            yield first, timestamp
            first = None
    if first is not None:
        yield first, timestamps.stop


class CandleCache:
    def __init__(self, path: str | Path):
        self.path = path
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL, step INTEGER NOT NULL, opened INTEGER NOT NULL,
                    opening TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
                    close TEXT NOT NULL, volume TEXT NOT NULL,
                    PRIMARY KEY (symbol, step, opened)
                ) WITHOUT ROWID
            """)

    def load(self, feed: HttpFeed, symbol: str, step: int, start: int, end: int) -> list[Candle]:
        """Return closed candles for [start, end), fetching only missing intervals."""
        if step <= 0 or start < 0 or end <= start or start % step or end % step:
            raise ValueError("Границы интервала должны совпадать с началом свечей.")
        timestamps = range(start, end, step)
        with closing(sqlite3.connect(self.path)) as database:
            rows = database.execute(
                """SELECT opened, opening, high, low, close, volume
                   FROM candles WHERE symbol=? AND step=? AND opened>=? AND opened<?""",
                (symbol, step, start, end),
            )
            cached = {row[0]: Candle.from_values(row) for row in rows}
            fresh = {}
            for first, last in missing_intervals(timestamps, cached):
                candles = feed.fetch(symbol, step, first, last)
                found = {candle.timestamp: candle for candle in candles}
                expected = range(first, last, step)
                if len(found) != len(candles) or set(found) != set(expected):
                    raise ValueError("Источник вернул пропуск, лишние свечи или дубликаты.")
                fresh.update(found)
            if fresh:
                with database:
                    database.executemany(
                        "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        ((symbol, step, *candle.as_values()) for candle in fresh.values()),
                    )
            cached.update(fresh)
            return [cached[timestamp] for timestamp in timestamps]


def price_crossings(candles: Iterable[Candle], threshold: Decimal) -> Iterable[tuple[str, Candle]]:
    """Return upward and downward crossings between consecutive closing prices."""
    if not threshold.is_finite() or threshold <= 0:
        raise ValueError("Порог должен быть конечным положительным числом.")
    previous = None
    for candle in candles:
        if previous is not None:
            if previous.close < threshold <= candle.close:
                yield "вверх", candle
            elif candle.close < threshold <= previous.close:
                yield "вниз", candle
        previous = candle
