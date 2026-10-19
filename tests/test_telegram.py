import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from rsi.queue import QueueStore
from rsi_telegram import Bot, BotAPI, BotError
from test_seed import TemporaryWorkspace


TOKEN = "12345:private-test-token"


def update(update_id, text, *, user_id=42, chat_type="private"):
    return {"update_id": update_id, "message": {
        "from": {"id": user_id}, "chat": {"id": user_id, "type": chat_type}, "text": text,
    }}


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryWorkspace()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.queue = QueueStore(self.path)
        self.client = Mock()
        self.handlers = {"status": Mock(return_value="status"), "best": Mock(return_value="best")}
        self.bot = Bot(self.queue, self.handlers, TOKEN, 42, client=self.client)

    def test_numeric_allowlist_is_mandatory(self):
        for invalid in (None, "42", 0, -42, True):
            with self.assertRaises(ValueError):
                Bot(self.queue, {}, TOKEN, invalid, client=self.client)

    def test_rejects_strangers_groups_and_spoofed_chat_before_reading_history(self):
        self.queue.status = Mock(side_effect=AssertionError("private history read"))
        stranger = update(1, "/status", user_id=99)
        group = update(2, "/status", chat_type="group")
        spoof = update(3, "/status")
        spoof["message"]["chat"]["id"] = 999
        for item in (stranger, group, spoof):
            self.bot.process_update(item)
        self.handlers["status"].assert_not_called()
        self.queue.status.assert_not_called()
        self.client.send_message.assert_not_called()
        self.assertEqual(self.queue.telegram_offset(), 4)

    def test_job_dispatch_deduplicates_across_restart(self):
        item = update(5, "/task develop a helper")
        self.bot.process_update(item)
        restarted = Bot(QueueStore(self.path), {}, TOKEN, 42, client=self.client)
        restarted.process_update(item)
        self.assertEqual(len(self.queue.list()), 1)
        job = self.queue.list()[0]
        self.assertEqual(job["kind"], "task")
        self.assertEqual(job["payload"], {"task": "develop a helper"})
        self.client.send_message.assert_called_once()

    def test_response_failure_cannot_repeat_accepted_command(self):
        self.client.send_message.side_effect = URLError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        with self.assertRaises(BotError) as error:
            self.bot.process_update(update(10, "/experiment try lexical ranking"))
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertNotIn("https", str(error.exception))
        self.assertEqual(self.queue.telegram_offset(), 11)
        self.assertFalse(self.queue.accept_update(10)["accepted"])
        self.assertEqual(len(self.queue.list()), 1)

    def test_controls_are_persistent_and_mutations_enqueue(self):
        self.bot.process_update(update(1, "/pause"))
        self.assertTrue(QueueStore(self.path).status()["paused"])
        self.bot.process_update(update(2, "/resume"))
        self.assertFalse(self.queue.status()["paused"])
        self.bot.process_update(update(3, "/task example"))
        self.bot.process_update(update(4, "/cancel telegram-3"))
        self.assertEqual(self.queue.get("telegram-3")["status"], "cancelled")
        self.bot.process_update(update(5, "/rollback accepted-previous"))
        self.assertEqual(self.queue.get("telegram-5")["payload"], {"version_id": "accepted-previous"})
        self.assertEqual(self.queue.get("telegram-5")["status"], "queued")
        self.bot.process_update(update(6, "/promote candidate"))
        self.assertEqual(self.queue.get("telegram-6")["kind"], "promote")

    def test_handler_errors_are_redacted_and_bounded(self):
        self.handlers["status"].side_effect = RuntimeError(f"secret {TOKEN} https://example.invalid")
        self.bot.process_update(update(1, "/status"))
        reply = self.client.send_message.call_args.args[1]
        self.assertNotIn(TOKEN, reply)
        self.assertNotIn("https", reply)
        self.handlers["best"].return_value = TOKEN + "a" * 5000
        self.bot.process_update(update(2, "/best"))
        reply = self.client.send_message.call_args.args[1]
        self.assertNotIn(TOKEN, reply)
        self.assertLessEqual(len(reply), 4000)

    def test_bot_token_cannot_enter_task_payload(self):
        self.bot.process_update(update(1, f"/task configure {TOKEN}"))
        self.assertEqual(self.queue.list(), [])
        self.assertNotIn(TOKEN, self.client.send_message.call_args.args[1])

    def test_invalid_command_is_acknowledged_without_partial_action(self):
        self.bot.process_update(update(1, "/task"))
        self.assertEqual(self.queue.list(), [])
        self.assertEqual(self.queue.telegram_offset(), 2)
        self.bot.process_update(update(2, "/cancel missing"))
        self.assertEqual(self.queue.telegram_offset(), 3)

    def test_poll_uses_durable_offset_and_finite_timeout(self):
        self.queue.accept_update(10)
        self.client.get_updates.return_value = [update(12, "/help"), update(11, "/status")]
        self.bot.poll_once()
        self.client.get_updates.assert_called_once_with(11, 20)
        self.assertEqual(self.queue.telegram_offset(), 13)
        self.assertEqual(self.client.send_message.call_count, 2)

    def test_finite_transient_retry_and_interruptible_shutdown(self):
        self.client.get_updates.side_effect = BotError("transient")
        stop = Mock()
        stop.is_set.return_value = False
        with self.assertRaises(BotError):
            self.bot.run(stop)
        self.assertEqual(self.client.get_updates.call_count, 3)
        self.assertEqual(stop.wait.call_count, 2)
        stop.is_set.return_value = True
        self.bot.run(stop)
        self.assertEqual(self.client.get_updates.call_count, 3)

    def test_telegram_auth_failure_pauses_scheduling(self):
        self.client.get_updates.side_effect = BotError("authentication")
        with self.assertRaises(BotError):
            self.bot.run()
        self.assertTrue(self.queue.status()["paused"])

    def test_http_client_never_exposes_token_url_or_error_body(self):
        for error in (URLError(TOKEN), RuntimeError(TOKEN),
                      HTTPError(f"https://api.telegram.org/bot{TOKEN}", 401, TOKEN, {}, None)):
            with self.subTest(error=type(error).__name__):
                client = BotAPI(TOKEN, opener=Mock(side_effect=error))
                with self.assertRaises(BotError) as raised:
                    client.get_updates(0)
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertNotIn("https", str(raised.exception))

    def test_http_request_is_post_with_bounded_timeout_and_json(self):
        opener = Mock(return_value=io.BytesIO(json.dumps({"ok": True, "result": []}).encode()))
        client = BotAPI(TOKEN, opener=opener)
        self.assertEqual(client.get_updates(123, 10), [])
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data)["offset"], 123)
        self.assertEqual(opener.call_args.kwargs["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
