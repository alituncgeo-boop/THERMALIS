"""Sınırlı yeniden deneme: üstel geri çekilme + tam rastgele sapma (full jitter), iptal edilebilir bekleme.

Yalnız :class:`TransientError` (ağ hatası, zaman aşımı, 429, 5xx) yeniden denenir. Koşul hataları
(412), kilit (409), yetki (403) ve bulunamadı (404) hiçbir zaman yeniden denenmez.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, TypeVar

from .base import NEVER_CANCELLED, CancelToken
from .errors import Cancelled, TransientError

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 20.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be within 1..10")

    def delay(self, attempt: int, rng: random.Random) -> float:
        return rng.uniform(0.0, min(self.max_delay, self.base_delay * (2 ** attempt)))


def call_with_retry(fn: Callable[[int], T], *, policy: RetryPolicy, cancel: CancelToken | None = None,
                    key: str | None = None, rng: random.Random | None = None,
                    on_retry: Callable[[int, TransientError, float], None] | None = None) -> T:
    """``fn(attempt)`` çağrılır; geçici hatada bekleyip tekrar dener, son denemede hatayı yükseltir."""
    cancel = cancel or NEVER_CANCELLED
    rng = rng or random.Random()
    for attempt in range(policy.max_attempts):
        cancel.raise_if_cancelled(key)
        try:
            return fn(attempt)
        except TransientError as exc:
            if attempt == policy.max_attempts - 1:
                raise
            delay = policy.delay(attempt, rng)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            if cancel.wait(delay):
                raise Cancelled("cancelled_during_backoff", key=key) from exc
    raise AssertionError("unreachable")  # pragma: no cover
