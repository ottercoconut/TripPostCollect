"""One monotonic manual-checkpoint budget shared by an XHS crawler run."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Optional

from .exception import PlatformRuntimeError


XHS_MANUAL_WAIT_BUDGET_ENV = "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS"
XHS_MANUAL_WAIT_BUDGET_SECONDS = 600.0
XHS_MANUAL_WAIT_BUDGET_EXHAUSTED = "xhs_manual_checkpoint_budget_exhausted"


class XHSManualWaitBudgetExhausted(PlatformRuntimeError):
    """The one per-run operator wait budget has been consumed."""

    def __init__(self) -> None:
        super().__init__(
            XHS_MANUAL_WAIT_BUDGET_EXHAUSTED,
            code=XHS_MANUAL_WAIT_BUDGET_EXHAUSTED,
        )


class XHSManualWaitTicket:
    """One active waiter; concurrent tickets are charged as a time union."""

    def __init__(self, budget: XHSManualWaitBudget, token: int, stage: str) -> None:
        self._budget = budget
        self._token = token
        self.stage = stage
        self._closed = False

    @property
    def remaining_seconds(self) -> float:
        return self._budget.remaining_seconds

    @property
    def manual_elapsed_seconds(self) -> float:
        return self._budget.manual_elapsed_seconds

    def raise_if_exhausted(self) -> None:
        self._budget.raise_if_exhausted()

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Exclude an automated sub-operation unless another waiter is active."""
        if self._closed:
            raise RuntimeError("xhs_manual_wait_ticket_closed")
        self._budget.raise_if_exhausted()
        self._budget._set_paused(self._token, True)
        try:
            yield
        finally:
            if not self._closed:
                self._budget._set_paused(self._token, False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._budget._close(self._token)

    def __enter__(self) -> XHSManualWaitTicket:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class XHSManualWaitBudget:
    """Charge only intervals containing at least one active manual waiter."""

    def __init__(
        self,
        *,
        limit_seconds: float,
        monotonic: Callable[[], float],
    ) -> None:
        if not math.isfinite(limit_seconds):
            limit_seconds = XHS_MANUAL_WAIT_BUDGET_SECONDS
        self.limit_seconds = min(
            XHS_MANUAL_WAIT_BUDGET_SECONDS,
            max(0.0, float(limit_seconds)),
        )
        self._monotonic = monotonic
        self._manual_elapsed = 0.0
        self._active_tokens: set[int] = set()
        self._pause_depths: dict[int, int] = {}
        self._charged_since: Optional[float] = None
        self._next_token = 0

    @classmethod
    def from_environment(
        cls,
        *,
        monotonic: Callable[[], float],
    ) -> XHSManualWaitBudget:
        raw_value = os.environ.get(
            XHS_MANUAL_WAIT_BUDGET_ENV,
            str(XHS_MANUAL_WAIT_BUDGET_SECONDS),
        )
        try:
            limit_seconds = float(raw_value)
        except (TypeError, ValueError):
            limit_seconds = XHS_MANUAL_WAIT_BUDGET_SECONDS
        if not math.isfinite(limit_seconds):
            limit_seconds = XHS_MANUAL_WAIT_BUDGET_SECONDS
        return cls(limit_seconds=limit_seconds, monotonic=monotonic)

    def now(self) -> float:
        return float(self._monotonic())

    def _is_charging(self) -> bool:
        return any(
            self._pause_depths.get(token, 0) == 0
            for token in self._active_tokens
        )

    def _transition(self, mutate: Callable[[], None]) -> None:
        now = self.now()
        if self._charged_since is not None:
            self._manual_elapsed += max(0.0, now - self._charged_since)
        mutate()
        self._charged_since = now if self._is_charging() else None

    @property
    def manual_elapsed_seconds(self) -> float:
        elapsed = self._manual_elapsed
        if self._charged_since is not None:
            elapsed += max(0.0, self.now() - self._charged_since)
        return elapsed

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limit_seconds - self.manual_elapsed_seconds)

    def raise_if_exhausted(self) -> None:
        if self.remaining_seconds <= 0.0:
            raise XHSManualWaitBudgetExhausted()

    def start(self, stage: str) -> XHSManualWaitTicket:
        self.raise_if_exhausted()
        self._next_token += 1
        token = self._next_token
        self._transition(lambda: self._active_tokens.add(token))
        return XHSManualWaitTicket(self, token, stage)

    def _set_paused(self, token: int, paused: bool) -> None:
        if token not in self._active_tokens:
            raise RuntimeError("xhs_manual_wait_ticket_inactive")

        def mutate() -> None:
            if paused:
                self._pause_depths[token] = self._pause_depths.get(token, 0) + 1
            else:
                pause_depth = self._pause_depths.get(token, 0)
                if pause_depth <= 1:
                    self._pause_depths.pop(token, None)
                else:
                    self._pause_depths[token] = pause_depth - 1

        self._transition(mutate)

    def _close(self, token: int) -> None:
        if token not in self._active_tokens:
            return

        def mutate() -> None:
            self._active_tokens.discard(token)
            self._pause_depths.pop(token, None)

        self._transition(mutate)
