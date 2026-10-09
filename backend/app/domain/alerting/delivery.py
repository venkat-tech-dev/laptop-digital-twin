"""Delivery reliability: provider contract, failure classification and the retry schedule.

attempt 1 immediately, then 5 s, 30 s, 2 min (each +-20 % jitter); after the last failure the
notification is FAILED (dead letter, kept for troubleshooting).
transient    timeouts, connection errors, 5xx            -> retry on the schedule
rate_limited 429 / provider throttling (Retry-After)     -> retry no earlier than Retry-After
permanent    4xx, invalid / missing configuration        -> FAILED at once (never retried)
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.alerting.models import Notification

RETRY_DELAYS_S = (5.0, 30.0, 120.0)  # after attempts 1, 2, 3; attempt 4 failing -> dead letter
MAX_ATTEMPTS = len(RETRY_DELAYS_S) + 1


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ok: bool
    failure: str | None = None  # transient | rate_limited | permanent | deferred
    error: str | None = None  # short, non-sensitive description
    provider_message_id: str | None = None
    retry_after_s: float | None = None
    pending_pickup: bool = False  # accepted, delivered when the client/agent fetches it (windows)


class NotificationProvider(ABC):
    """One delivery channel. Implementations must be idempotent per ``notification.idempotency_key``,
    honour a timeout, never raise for delivery failures (return a classified DeliveryResult) and never
    put credentials into payloads or logs."""

    channel: str
    name: str

    @abstractmethod
    async def send(self, notification: Notification) -> DeliveryResult: ...

    def available(self) -> tuple[bool, str | None]:
        return True, None


def next_retry(
    attempt: int, result: DeliveryResult, now: datetime, rng: random.Random | None = None
) -> datetime | None:
    """When to try again after ``attempt`` (1-based) failed; None = dead letter.
    ``deferred`` (e.g. the user has no open session yet) is not a failure: it does not use up attempts
    and is retried after ``retry_after_s``; the channel's lifetime limit expires it instead."""
    if result.failure == "deferred":
        return now + timedelta(seconds=result.retry_after_s or 60.0)
    if result.failure == "permanent" or attempt >= MAX_ATTEMPTS:
        return None
    base = RETRY_DELAYS_S[min(attempt - 1, len(RETRY_DELAYS_S) - 1)]
    jitter = (rng or random).uniform(0.8, 1.2)
    delay = base * jitter
    if result.failure == "rate_limited" and result.retry_after_s:
        delay = max(delay, result.retry_after_s)
    return now + timedelta(seconds=delay)
