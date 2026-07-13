"""Rate-limited outbound email queue for Resend.

Resend enforces a per-second team limit (default often 2 req/s). When the
limit is reached, jobs wait in this queue and are flushed at most
RESEND_MAX_PER_SECOND times per second. 429 responses re-queue with Retry-After.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Conservative Resend default; override with RESEND_MAX_PER_SECOND.
_DEFAULT_MAX_PER_SECOND = 2


def _env_max_per_second() -> float:
    raw = os.getenv("RESEND_MAX_PER_SECOND", "").strip()
    if not raw:
        return float(_DEFAULT_MAX_PER_SECOND)
    try:
        value = float(raw)
    except ValueError:
        return float(_DEFAULT_MAX_PER_SECOND)
    return max(0.1, value)


@dataclass
class _EmailJob:
    to: str
    subject: str
    html: str
    attachments: list[dict[str, Any]] | None = None
    headers: dict[str, str] | None = None
    reply_to: str | list[str] | None = None
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: BaseException | None = None


class EmailSendQueue:
    """Single-worker queue that sends at most N emails per rolling second."""

    def __init__(self, deliver: Callable[..., dict[str, Any]], max_per_second: float | None = None) -> None:
        self._deliver = deliver
        self._max_per_second = max_per_second if max_per_second is not None else _env_max_per_second()
        self._q: queue.Queue[_EmailJob | None] = queue.Queue()
        self._send_times: list[float] = []
        self._rate_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._started = False
        self._worker = threading.Thread(target=self._run, name="email-send-queue", daemon=True)

    @property
    def max_per_second(self) -> float:
        return self._max_per_second

    def set_max_per_second(self, value: float) -> None:
        self._max_per_second = max(0.1, float(value))

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
            self._worker.start()
            logger.info(
                "Email send queue started (max %.2f/sec)",
                self._max_per_second,
            )

    def submit(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        attachments: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        reply_to: str | list[str] | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        """Enqueue an email and wait until it is sent (or fails)."""
        self.start()
        job = _EmailJob(
            to=to,
            subject=subject,
            html=html,
            attachments=attachments,
            headers=headers,
            reply_to=reply_to,
        )
        self._q.put(job)
        if not job.done.wait(timeout=timeout):
            raise RuntimeError(f"Email send timed out in queue for {to}")
        if job.error is not None:
            raise job.error
        return job.result or {"sent": False, "reason": "unknown"}

    def pending(self) -> int:
        return self._q.qsize()

    def _acquire_send_slot(self) -> None:
        """Block until sending another email stays within the per-second cap."""
        while True:
            with self._rate_lock:
                now = time.monotonic()
                window = 1.0
                self._send_times = [t for t in self._send_times if now - t < window]
                limit = max(1, int(self._max_per_second))
                # Allow fractional rates (e.g. 0.5/sec) via interval spacing.
                if self._max_per_second < 1:
                    if not self._send_times:
                        self._send_times.append(now)
                        return
                    wait = (1.0 / self._max_per_second) - (now - self._send_times[-1])
                elif len(self._send_times) < limit:
                    self._send_times.append(now)
                    return
                else:
                    wait = window - (now - self._send_times[0]) + 0.005
            time.sleep(max(0.005, wait))

    def _run(self) -> None:
        while True:
            job = self._q.get()
            try:
                if job is None:
                    return
                self._process(job)
            finally:
                self._q.task_done()

    def _process(self, job: _EmailJob) -> None:
        attempts = 0
        max_attempts = 8
        while attempts < max_attempts:
            attempts += 1
            self._acquire_send_slot()
            try:
                result = self._deliver(
                    to=job.to,
                    subject=job.subject,
                    html=job.html,
                    attachments=job.attachments,
                    headers=job.headers,
                    reply_to=job.reply_to,
                )
            except RateLimited as exc:
                delay = max(exc.retry_after, 1.0 / self._max_per_second)
                if exc.limit is not None and exc.limit > 0:
                    self.set_max_per_second(float(exc.limit))
                    logger.warning(
                        "Resend rate limit hit; adopted max %.0f/sec, retrying in %.2fs",
                        self._max_per_second,
                        delay,
                    )
                else:
                    logger.warning(
                        "Resend rate limit hit for %s; re-queue wait %.2fs (attempt %s)",
                        job.to,
                        delay,
                        attempts,
                    )
                time.sleep(delay)
                continue
            except Exception as exc:
                job.error = exc
                job.done.set()
                return

            job.result = result
            job.done.set()
            return

        job.error = RuntimeError(f"Email send rate-limited too many times for {job.to}")
        job.done.set()


class RateLimited(Exception):
    def __init__(self, retry_after: float, limit: float | None = None) -> None:
        super().__init__("rate_limited")
        self.retry_after = retry_after
        self.limit = limit


_queue: EmailSendQueue | None = None
_queue_lock = threading.Lock()


def get_email_queue(deliver: Callable[..., dict[str, Any]]) -> EmailSendQueue:
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = EmailSendQueue(deliver=deliver)
        return _queue
