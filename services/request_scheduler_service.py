"""Keyed request scheduling service."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import sleep, time

from domain.keys import PairKey


@dataclass
class RequestSchedulerState:
    """Mutable scheduler state for the previous-key bypass model."""

    base_delay: float = 3.5
    adaptive_delay: float = 3.5
    last_request_time: float = 0.0
    last_request_key: str | None = None
    success_count: int = 0
    error_count: int = 0
    metadata: dict = field(default_factory=dict)


def build_request_key(asset_id: int, segment_code: str, expiry: str | None = None) -> str:
    """Build a segment-safe request key."""
    return PairKey(asset_id=asset_id, segment_code=segment_code, expiry=expiry or "__meta__").value


def should_bypass_delay(state: RequestSchedulerState, request_key: str) -> bool:
    """Replicate the current previous-key bypass rule in a pure helper."""
    return state.error_count == 0 and request_key != state.last_request_key


def calculate_adaptive_delay(state: RequestSchedulerState) -> float:
    """Calculate the next effective delay based on request outcomes."""
    if state.error_count > 0:
        error_ratio = state.error_count / max(1, state.success_count)
        return min(10.0, state.base_delay * (1 + error_ratio))
    return max(state.base_delay, state.adaptive_delay * 0.9)


def calculate_wait_time(
    state: RequestSchedulerState,
    request_key: str,
    now: float | None = None,
) -> float:
    """Return how long the caller should wait before issuing the request."""
    current_time = time() if now is None else now
    adaptive_delay = calculate_adaptive_delay(state)
    reference_last_request = 0.0 if should_bypass_delay(state, request_key) else state.last_request_time
    elapsed = current_time - reference_last_request
    return max(0.0, adaptive_delay - elapsed)


def mark_request(state: RequestSchedulerState, request_key: str, success: bool) -> RequestSchedulerState:
    """Return a new scheduler state after a request outcome."""
    return RequestSchedulerState(
        base_delay=state.base_delay,
        adaptive_delay=calculate_adaptive_delay(state),
        last_request_time=time(),
        last_request_key=request_key,
        success_count=state.success_count + (1 if success else 0),
        error_count=max(0, state.error_count - 1) if success else state.error_count + 1,
        metadata=dict(state.metadata),
    )


class KeyedRequestScheduler:
    """Runtime scheduler implementing the previous-key bypass semantics."""

    def __init__(
        self,
        state: RequestSchedulerState | None = None,
        sleep_fn=sleep,
        time_fn=time,
    ) -> None:
        self.state = state or RequestSchedulerState()
        self._sleep_fn = sleep_fn
        self._time_fn = time_fn

    def wait_for_slot(self, request_key: str) -> float:
        wait_seconds = calculate_wait_time(self.state, request_key, now=self._time_fn())
        if wait_seconds > 0:
            self._sleep_fn(wait_seconds)
        return wait_seconds

    def record_result(self, request_key: str, success: bool) -> RequestSchedulerState:
        self.state = RequestSchedulerState(
            base_delay=self.state.base_delay,
            adaptive_delay=calculate_adaptive_delay(self.state),
            last_request_time=self._time_fn(),
            last_request_key=request_key,
            success_count=self.state.success_count + (1 if success else 0),
            error_count=max(0, self.state.error_count - 1) if success else self.state.error_count + 1,
            metadata=dict(self.state.metadata),
        )
        return self.state

    def note_rate_limit(self) -> RequestSchedulerState:
        self.state = RequestSchedulerState(
            base_delay=self.state.base_delay,
            adaptive_delay=min(10.0, max(self.state.base_delay, self.state.adaptive_delay) * 2),
            last_request_time=self._time_fn(),
            last_request_key=self.state.last_request_key,
            success_count=self.state.success_count,
            error_count=self.state.error_count + 1,
            metadata=dict(self.state.metadata),
        )
        return self.state
