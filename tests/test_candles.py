import io
import json
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from candle_cache import Candle, CandleCache, price_crossings
from demo import STEP, SYMBOL, demo_feed, main, send_telegram


class CacheTest(TestCase):
    def test_fetches_only_missing_closed_candles_and_reopens(self):
        with TemporaryDirectory() as directory, demo_feed() as (feed, requests, start, end):
            path = Path(directory) / "history.sqlite3"
            cache = CandleCache(path)
            first = cache.load(feed, SYMBOL, STEP, start, start + 2 * STEP)
            self.assertEqual([candle.close for candle in first], [Decimal(99), Decimal(100)])
            all_candles = cache.load(feed, SYMBOL, STEP, start, end)
            self.assertEqual(requests, [(start, start + 2 * STEP), (start + 2 * STEP, end)])
            self.assertEqual(len(all_candles), 5)
            self.assertEqual(CandleCache(path).load(feed, SYMBOL, STEP, start, end), all_candles)
            self.assertEqual(len(requests), 2)

    def test_gap_detection_does_not_save_incomplete_response(self):
        with TemporaryDirectory() as directory, demo_feed() as (feed, requests, start, end):
            cache = CandleCache(Path(directory) / "history.sqlite3")
            limited = mock.Mock()
            limited.fetch.side_effect = lambda *args: feed.fetch(*args)[:-1]
            with self.assertRaisesRegex(ValueError, "пропуск"):
                cache.load(limited, SYMBOL, STEP, start, end)
            result = cache.load(feed, SYMBOL, STEP, start, end)
            self.assertEqual(len(result), 5)
            self.assertEqual(requests, [(start, end), (start, end)])

    def test_bad_numeric_values_and_invalid_interval_are_rejected(self):
        for value in ("NaN", "Infinity", "not-a-number"):
            with self.assertRaises(ValueError):
                Candle.from_values([0, value, "2", "0", "1", "10"])
        with TemporaryDirectory() as directory:
            cache = CandleCache(Path(directory) / "history.sqlite3")
            feed = mock.Mock()
            with self.assertRaises(ValueError):
                cache.load(feed, SYMBOL, STEP, 5, STEP)
            feed.fetch.assert_not_called()

    def test_threshold_crossing_in_both_directions(self):
        with demo_feed() as (feed, _, start, end):
            candles = feed.fetch(SYMBOL, STEP, start, end)
        crossings = list(price_crossings(candles, Decimal("105")))
        self.assertEqual([(direction, candle.close) for direction, candle in crossings],
                         [("вверх", Decimal("105")), ("вниз", Decimal("104"))])

    def test_default_cli_only_reports_locally(self):
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as console:
            path = str(Path(directory) / "history.sqlite3")
            with mock.patch.object(sys, "argv", ["demo.py", "--db", path]), mock.patch("demo.send_telegram") as send:
                self.assertEqual(main(), 0)
            send.assert_not_called()
        self.assertIn("Загружено свечей по HTTP: 3", console.getvalue())
        self.assertIn("HTTP-запросов: 0", console.getvalue())
        self.assertIn("пересекла 102 вверх", console.getvalue())


class TelegramTest(TestCase):
    def test_telegram_request_body(self):
        with mock.patch("demo.urlopen", return_value=io.BytesIO(b'{"ok":true}')) as request:
            send_telegram("000000000:" + "x" * 32, "42", "Тестовое событие")
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_method(), "POST")
        self.assertTrue(sent.full_url.endswith("/sendMessage"))
        self.assertEqual(json.loads(sent.data), {"chat_id": 42, "text": "Тестовое событие"})

    def test_invalid_telegram_settings_do_not_send(self):
        with mock.patch("demo.urlopen") as request:
            with self.assertRaises(ValueError):
                send_telegram("", "42", "Тест")
        request.assert_not_called()
