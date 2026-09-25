"""Message dispatcher: the daemon's main loop.

Inbound (sender, text) pairs come from the mesh adapter's queue:

- Commands and gossip are handled inline on the dispatcher thread. They
  never touch the LLM, so they stay instant even while a question is
  being answered.
- Questions (and !retry) pass a per-sender rate limit, then join a
  bounded queue served by one worker thread. One LLM call at a time is
  all small hardware can afford; everyone else gets a "you're next"
  notice instead of silence.

Protocol-agnostic: the dispatcher only needs a send(dest, text) callable,
so every mesh adapter gets the same rate limiting and queueing.
"""

import logging
import math
import queue
import threading
import time
from collections.abc import Callable

from del_fi.core.formatter import byte_len

log = logging.getLogger("del_fi.core.dispatcher")

# Pause between auto-sent consecutive chunks (reduces channel congestion).
AUTO_SEND_DELAY = 0.5

DEFAULT_QUERY_QUEUE_SIZE = 10


def command_name(text: str) -> str:
    """'!more 2' -> '!more' (lowercased)."""
    parts = text.split(None, 1)
    return parts[0].lower() if parts else ""


class Dispatcher:
    """Routes inbound messages, rate-limits questions, runs the query worker."""

    def __init__(
        self,
        cfg: dict,
        router,
        send: Callable[[str, str], bool],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.router = router
        self._send = send
        self._clock = clock
        self._sleep = sleep

        self.rate_limit: float = cfg.get("rate_limit_seconds", 30)
        self.rate_limit_notice: bool = cfg.get("rate_limit_notice", True)
        self.busy_notice: bool = cfg.get("busy_notice", True)
        self.query_queue_size: int = cfg.get("query_queue_size", DEFAULT_QUERY_QUEUE_SIZE)

        self.query_queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker_busy = threading.Event()
        self._worker: threading.Thread | None = None

        self._state_lock = threading.Lock()
        self._pending: dict[str, int] = {}          # sender -> queued + in-flight
        self._last_accepted: dict[str, float] = {}  # sender -> last question accepted
        self._last_notice: dict[str, float] = {}    # sender -> last rate-limit notice

    # --- Lifecycle ---

    def start(self) -> None:
        """Start the query worker thread."""
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._worker_loop, name="query-worker", daemon=True
            )
            self._worker.start()

    def stop(self) -> None:
        """Ask the loops to exit. An in-flight LLM call is abandoned."""
        self._stop.set()

    def run(self, inbox: queue.Queue) -> None:
        """Main loop: handle inbound messages until stop() is called."""
        while not self._stop.is_set():
            try:
                sender_id, text = inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            self.handle(sender_id, text)

    # --- Inbound messages ---

    def handle(self, sender_id: str, text: str) -> None:
        """Handle one inbound message. Never raises."""
        try:
            self._handle(sender_id, text.strip())
        except Exception:
            log.exception(f"dispatcher error handling message from {sender_id}")
            self._safe_send(sender_id, "I hit an error processing that. Try again.")

    def _handle(self, sender_id: str, text: str) -> None:
        kind = self.router.classify(text)
        if kind == "empty":
            return
        if kind == "gossip":
            self.router.route(sender_id, text)
            return
        if kind == "command":
            if command_name(text) != "!retry":
                self._send_all(sender_id, self.router.route_multi(sender_id, text))
                return
            # !retry re-asks the last question: it runs on the worker and
            # counts against the rate limit like any other question.
            last = self.router.prepare_retry(sender_id)
            if last is None:
                self._safe_send(sender_id, "No previous query to retry. Ask a question first.")
                return
            text = last
        self._submit_question(sender_id, text)

    def _submit_question(self, sender_id: str, text: str) -> None:
        name = self.cfg["node_name"]
        now = self._clock()

        with self._state_lock:
            last = self._last_accepted.get(sender_id)
            limited = (
                self.rate_limit > 0
                and last is not None
                and now - last < self.rate_limit
            )
            notify = False
            if limited:
                # One notice per window: only if none was sent since the
                # last accepted question, so a flood gets one reply.
                notify = self.rate_limit_notice and self._last_notice.get(sender_id, -1.0) < last
                if notify:
                    self._last_notice[sender_id] = now
            else:
                queue_full = self.query_queue.qsize() >= self.query_queue_size
                if not queue_full:
                    self._last_accepted[sender_id] = now
                    already_pending = self._pending.get(sender_id, 0) > 0
                    self._pending[sender_id] = self._pending.get(sender_id, 0) + 1

        if limited:
            wait = max(1, math.ceil(self.rate_limit - (now - last)))
            log.info(f"rate limited: {sender_id} ({wait}s left)")
            if notify:
                self._safe_send(
                    sender_id,
                    f"{name}: One question per {int(self.rate_limit)}s, please. "
                    f"Try again in {wait}s. Commands still work.",
                )
            return

        if queue_full:
            log.warning(f"query queue full ({self.query_queue_size}) — turned away {sender_id}")
            self._safe_send(
                sender_id,
                f"{name}: Too many questions queued right now. Try again in a few minutes.",
            )
            return

        if self.busy_notice and self._worker_busy.is_set() and not already_pending:
            position = self.query_queue.qsize() + 1
            self._safe_send(sender_id, self.router.busy_message(position))
            log.info(f"  ⏳ busy notice → {sender_id} (position {position})")

        self.query_queue.put((sender_id, text))

    # --- Worker ---

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sender_id, text = self.query_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._worker_busy.set()
            try:
                self._send_all(sender_id, self.router.route_multi(sender_id, text))
            except Exception:
                log.exception(f"error processing query from {sender_id}")
                self._safe_send(sender_id, "I hit an error processing that. Try again.")
            finally:
                with self._state_lock:
                    remaining = self._pending.get(sender_id, 1) - 1
                    if remaining > 0:
                        self._pending[sender_id] = remaining
                    else:
                        self._pending.pop(sender_id, None)
                self._worker_busy.clear()
                self.query_queue.task_done()

    # --- Sending ---

    def _send_all(self, sender_id: str, messages: list[str] | None) -> None:
        if not messages:
            return
        for i, msg in enumerate(messages):
            if i > 0:
                self._sleep(AUTO_SEND_DELAY)
            self._safe_send(sender_id, msg)
        total = sum(byte_len(m) for m in messages)
        log.info(f"  ✓ response: {len(messages)} msg(s), {total}B → {sender_id}")

    def _safe_send(self, sender_id: str, text: str) -> None:
        try:
            self._send(sender_id, text)
        except Exception:
            log.exception(f"send to {sender_id} failed")
