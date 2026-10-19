"""Finite HTTP requests, persistent update acceptance, and queued mutations.

The bot never runs development or promotion inline. A separate supervisor
worker consumes accepted commands, so polling remains responsive.
"""

from contextlib import suppress
import json
import re
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from rsi.queue import QueueError, QueueStore


class BotError(RuntimeError):
    def __init__(self, category="infrastructure", *, retry_after=1):
        self.category = category
        self.retry_after = min(30, max(1, retry_after))
        super().__init__(f"Telegram request failed ({category}); credentials and response details omitted.")


class BotAPI:
    """Small injectable client. Never propagate a token-bearing URL or body."""

    def __init__(self, token: str, *, opener=urlopen):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("A Telegram bot token is required.")
        self._token = token
        self._opener = opener

    def _request(self, method, payload, timeout):
        try:
            request = Request(
                f"https://api.telegram.org/bot{self._token}/{method}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with self._opener(request, timeout=timeout) as response:
                # Bot requests return small JSON responses; cap malformed bodies.
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise BotError("protocol")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise BotError("protocol")
            if result.get("ok") is not True:
                code = result.get("error_code")
                category = "authentication" if code in (401, 403) else "transient" if code == 429 or isinstance(code, int) and code >= 500 else "protocol"
                retry = result.get("parameters", {}).get("retry_after", 1)
                raise BotError(category, retry_after=retry if type(retry) is int else 1)
            return result.get("result")
        except HTTPError as exc:
            category = "authentication" if exc.code in (401, 403) else "transient" if exc.code == 429 or exc.code >= 500 else "protocol"
            with suppress(Exception):
                exc.close()
            raise BotError(category) from None
        except (URLError, TimeoutError, OSError):
            raise BotError("transient") from None
        except (ValueError, TypeError, KeyError):
            raise BotError("protocol") from None
        except BotError:
            raise
        except Exception:
            raise BotError("protocol") from None

    def get_updates(self, offset: int, timeout=20):
        result = self._request("getUpdates", {
            "offset": offset, "timeout": timeout, "limit": 50,
            "allowed_updates": ["message"],
        }, timeout + 5)
        if not isinstance(result, list):
            raise BotError("protocol")
        return result

    def send_message(self, chat_id: int, text: str):
        return self._request("sendMessage", {"chat_id": chat_id, "text": text[:4000]}, 10)


HELP = (
    "RSI controls:\n/status\n/task TEXT\n/experiment [GUIDANCE]\n"
    "/pause\n/resume\n/cancel JOB_ID\n/best\n/diff VERSION_ID\n"
    "/rollback VERSION_ID\n/promote VERSION_ID\n"
    "Tasks propose changes. Promotion is an explicit command; experiments never auto-promote."
)


class Bot:
    def __init__(self, queue: QueueStore, handlers: dict, token: str,
                 allowed_user_id: int, *, client=None, poll_timeout=20,
                 max_transient_failures=3):
        if type(allowed_user_id) is not int or allowed_user_id <= 0:
            raise ValueError("A positive numeric Telegram user ID is required.")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("A Telegram bot token is required.")
        if type(poll_timeout) is not int or not 1 <= poll_timeout <= 30:
            raise ValueError("Polling timeout must be between 1 and 30 seconds.")
        if type(max_transient_failures) is not int or max_transient_failures < 1:
            raise ValueError("A positive transient failure limit is required.")
        self.queue = queue
        self.handlers = handlers
        self._token = token
        self.allowed_user_id = allowed_user_id
        self.client = client if client is not None else BotAPI(token)
        self.poll_timeout = poll_timeout
        self.max_transient_failures = max_transient_failures

    def _sanitize(self, text):
        text = str(text).replace(self._token, "[REDACTED]")
        return re.sub(r"https?://api\.telegram\.org/bot[^\s]+", "[REDACTED TELEGRAM URL]", text)[:4000]

    def process_update(self, update: dict):
        update_id = update.get("update_id")
        if type(update_id) is not int or update_id < 0:
            return
        message = update.get("message") or {}
        if not isinstance(message, dict):
            self.queue.accept_update(update_id)
            return
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        # Check authorization before calling any status/history handler.
        if (not isinstance(sender, dict) or not isinstance(chat, dict)
                or type(sender.get("id")) is not int or sender.get("id") != self.allowed_user_id
                or chat.get("type") != "private" or type(chat.get("id")) is not int
                or chat["id"] != self.allowed_user_id):
            self.queue.accept_update(update_id)
            return
        text = message.get("text")
        if not isinstance(text, str):
            self.queue.accept_update(update_id)
            return
        parts = text.strip().split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lstrip("/").lower() if parts else ""
        argument = parts[1].strip() if len(parts) == 2 else ""
        if not text.lstrip().startswith("/"):
            command = "help"
        if self._token in text:
            accepted = self.queue.accept_update(update_id)
            reply = "Credentials cannot be submitted as a task. Configure them outside agent prompts."
        else:
            try:
                accepted = self.queue.accept_update(update_id, command, argument)
            except QueueError as exc:
                # Invalid control commands are acknowledged so restarts do not
                # repeatedly attempt them. QueueError messages contain no input.
                accepted = self.queue.accept_update(update_id)
                reply = str(exc)
            else:
                if not accepted["accepted"]:
                    return
                job = accepted["result"]
                if job:
                    reply = f"Queued {job['kind']} job {job['id']}."
                elif command in ("pause", "resume", "cancel"):
                    reply = {"pause": "Scheduling paused; the active job may finish.",
                             "resume": "Scheduling resumed.",
                             "cancel": "Cancellation recorded."}[command]
                elif command in self.handlers and command in ("status", "best", "diff"):
                    try:
                        reply = self.handlers[command](argument)
                    except Exception:
                        reply = "Unable to read the requested state. Inspect local supervisor logs."
                elif command == "status":
                    state = self.queue.status()
                    active = state["active"]["id"] if state["active"] else "none"
                    reply = f"Paused: {state['paused']}\nQueued: {state['queued']}\nActive: {active}"
                else:
                    reply = HELP
        if accepted["accepted"]:
            try:
                self.client.send_message(chat["id"], self._sanitize(reply))
            except BotError:
                raise
            except Exception:
                raise BotError("transient") from None

    def poll_once(self):
        try:
            updates = self.client.get_updates(self.queue.telegram_offset(), self.poll_timeout)
        except BotError:
            raise
        except Exception:
            raise BotError("transient") from None
        if not isinstance(updates, list):
            raise BotError("protocol")
        for update in sorted((item for item in updates if isinstance(item, dict)
                              and type(item.get("update_id")) is int),
                             key=lambda item: item["update_id"]):
            self.process_update(update)

    def run(self, stop_event: threading.Event | None = None):
        """Run until signalled; network failure retries are finite and interruptible."""
        stop_event = stop_event if stop_event is not None else threading.Event()
        failures = 0
        while not stop_event.is_set():
            try:
                self.poll_once()
                failures = 0
            except BotError as exc:
                if exc.category == "authentication":
                    self.queue.pause("telegram_authentication")
                    raise
                failures += 1
                if exc.category != "transient" or failures >= self.max_transient_failures:
                    raise
                stop_event.wait(min(30, max(exc.retry_after, 2 ** (failures - 1))))
