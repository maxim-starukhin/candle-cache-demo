"""Run the cache demo against an in-process HTTP API with fictional candles."""
import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from candle_cache import CandleCache, HttpFeed, price_crossings

SYMBOL = "DEMO-USD"
STEP = 60
DATA_PATH = Path(__file__).with_name("demo_candles.json")


@contextmanager
def demo_feed():
    rows = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path != "/candles":
                self.send_error(404)
                return
            params = parse_qs(url.query)
            try:
                symbol, step, start, end = (params[name][0] for name in ("symbol", "step", "start", "end"))
                step, start, end = int(step), int(start), int(end)
                if symbol != SYMBOL or step != STEP or start >= end:
                    raise ValueError
            except (KeyError, ValueError):
                self.send_error(400)
                return
            requests.append((start, end))
            content = json.dumps([row for row in rows if start <= row[0] < end]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        host, port = server.server_address
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield HttpFeed(f"http://{host}:{port}"), requests, rows[0][0], rows[-1][0] + STEP
        finally:
            server.shutdown()
            thread.join()


def send_telegram(token: str, chat_id: str, text: str) -> None:
    if not re.fullmatch(r"\d{6,15}:[A-Za-z0-9_-]{25,}", token) or not re.fullmatch(r"-?\d+", chat_id):
        raise ValueError("Задайте TG_BOT_TOKEN и TG_CHAT_ID.")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": int(chat_id), "text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Telegram вернул HTTP {error.code}.") from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError("Ответ Telegram недоступен.") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram отклонил отправку.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Демо кэша вымышленных биржевых свечей.")
    parser.add_argument("--db", type=Path, default=Path("candles.sqlite3"), help="Путь к базе SQLite.")
    parser.add_argument("--threshold", default="102", help="Порог цены.")
    parser.add_argument("--send-telegram", action="store_true", help="Отправить тестовые уведомления в Telegram.")
    args = parser.parse_args()
    try:
        threshold = Decimal(args.threshold)
        if not threshold.is_finite() or threshold <= 0:
            raise ValueError("Порог должен быть положительным числом.")
        cache = CandleCache(args.db)
        with demo_feed() as (feed, requests, start, end):
            stages = (("Первые две свечи", start + 2 * STEP), ("Весь интервал", end), ("Повтор интервала", end))
            for label, stop in stages:
                before = len(requests)
                candles = cache.load(feed, SYMBOL, STEP, start, stop)
                fresh = requests[before:]
                print(f"{label}: {len(candles)} свечей, HTTP-запросов: {len(fresh)}")
                for first, last in fresh:
                    print(f"  Загружено свечей по HTTP: {(last - first) // STEP}")
        for direction, candle in price_crossings(candles, threshold):
            timestamp = datetime.fromtimestamp(candle.timestamp, UTC).strftime("%H:%M UTC")
            alert = f"{SYMBOL}: цена пересекла {threshold} {direction}, закрытие {candle.close} в {timestamp}."
            print(alert)
            if args.send_telegram:
                send_telegram(os.getenv("TG_BOT_TOKEN", ""), os.getenv("TG_CHAT_ID", ""), alert)
    except (InvalidOperation, OSError, RuntimeError, ValueError) as error:
        print(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
