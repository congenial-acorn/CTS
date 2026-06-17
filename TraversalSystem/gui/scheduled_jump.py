"""Scheduled jump controller — QTimer-based countdown and auto-click."""

from __future__ import annotations

import datetime
import threading
import time
from collections.abc import Callable
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from TraversalSystem import input_handler as _input_handler
from TraversalSystem.window_manager import FocusGuard


SCHEDULED_JUMP_ESTIMATE_SECONDS = 5.0
"""Estimated wall-clock seconds for a scheduled-jump focus+click block; used by the SequenceQueue feasibility gate so the block won't overlap a pending carrier jump."""


class ScheduledJumpController(QObject):
    """Manages a timed jump execution with countdown, focus guard, and click."""

    # -- Signals ---------------------------------------------------------------
    countdown_updated = Signal(str)  # "HH:MM:SS" remaining
    status_changed = Signal(str)     # "idle" | "scheduled" | "focusing" | "completed" | "failed" | "cancelled"

    # -- Constructor -----------------------------------------------------------

    def __init__(
        self,
        *,
        time_provider: Callable[[], datetime.datetime] | None = None,
        focus_func: Callable[..., None] | None = None,
        click_func: Callable[[int, int], None] | None = None,
        sleep_func: Callable[[float], None] | None = None,
        submit_func: Callable[[Callable[[], None], float, threading.Event], object] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)

        # Dependency injection with sensible defaults
        self._time_provider: Callable[[], datetime.datetime] = (
            time_provider
            or (lambda: datetime.datetime.now(datetime.timezone.utc))
        )
        self._focus_func: Callable[..., None] | None = (
            focus_func  # None ⇒ lazy resolve via FocusGuard at call time
        )
        self._click_func: Callable[[int, int], None] = (
            click_func or (lambda x, y: _input_handler.click(x, y))
        )
        self._sleep_func: Callable[[float], None] = sleep_func or time.sleep
        self._submit_func = submit_func
        """When set, the jump click+focus is submitted as a serialized block through the shared SequenceQueue so it cannot interleave with active worker automation. When None, the click fires directly (legacy behavior, safe only when no workers are active)."""

        # Internal timer (1-second tick)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)  # type: ignore[arg-type]

        # Scheduled state
        self._target_time: datetime.time | None = None
        self._button_x: int = 0
        self._button_y: int = 0
        self._binding: object | None = None
        self._focus_attempted: bool = False
        self._cancel_event = threading.Event()

    # -- Properties ------------------------------------------------------------

    @property
    def is_scheduled(self) -> bool:
        """Return True while a countdown is active."""
        return self._timer.isActive()

    @property
    def remaining_seconds(self) -> float:
        """Seconds until target; 0 if idle."""
        if self._target_time is None:
            return 0.0
        now = self._time_provider()
        target = self._build_target_datetime(now)
        delta = (target - now).total_seconds()
        return max(delta, 0.0)

    # -- Public API ------------------------------------------------------------

    def schedule(
        self,
        target_utc: datetime.time,
        button_x: int,
        button_y: int,
        binding: object,
    ) -> None:
        """Schedule a jump at *target_utc* (UTC time-of-day)."""
        self.cancel()  # clear any existing schedule

        now = self._time_provider()
        target_dt = datetime.datetime.combine(
            now.date(), target_utc, tzinfo=datetime.timezone.utc
        )
        if target_dt <= now:
            raise ValueError("Time is in the past")

        self._target_time = target_utc
        self._button_x = button_x
        self._button_y = button_y
        self._binding = binding
        self._focus_attempted = False
        self._cancel_event = threading.Event()

        self._timer.start(1000)
        self.status_changed.emit("scheduled")

        # Emit initial countdown immediately
        delta = (target_dt - now).total_seconds()
        self.countdown_updated.emit(self._format_remaining(delta))

    def cancel(self) -> None:
        """Cancel an active countdown."""
        self._cancel_event.set()
        self._timer.stop()
        self._target_time = None
        self._binding = None
        self._focus_attempted = False
        self.status_changed.emit("cancelled")

    # -- Private helpers -------------------------------------------------------

    def _tick(self) -> None:  # noqa: C901 — straightforward state machine
        """Called every second by the QTimer."""
        now = self._time_provider()
        target = self._build_target_datetime(now)
        delta = (target - now).total_seconds()

        if delta <= 0:
            # Time's up — click the button
            if self._submit_func is not None:
                # Submit a deadline slightly in the future (not bare "now"): the
                # queue's restock-feasibility gate excludes deadlines that are
                # already <= now (strict `> now`), so a bare time.monotonic()
                # deadline would fail to gate concurrent restocks and the click
                # could be delayed behind one (Bug C). A small positive offset
                # keeps it the earliest jump while still gating.
                self._submit_func(
                    self._make_fire_block(),
                    time.monotonic() + SCHEDULED_JUMP_ESTIMATE_SECONDS,
                    self._cancel_event,
                )
            else:
                self._click_func(self._button_x, self._button_y)
            self._cleanup()
            self.status_changed.emit("completed")
            return

        # Attempt focus when within 10 seconds
        if (
            self._submit_func is None
            and delta <= 10
            and not self._focus_attempted
        ):
            self._focus_attempted = True
            focus = self._resolve_focus_func()
            try:
                with _input_handler.dispatch_lock:
                    focus(self._binding)
            except Exception:
                # Retry once after a short sleep
                try:
                    self._sleep_func(1.0)
                    with _input_handler.dispatch_lock:
                        focus(self._binding)
                except Exception:
                    self._cleanup()
                    self.status_changed.emit("failed")
                    return
            self.status_changed.emit("focusing")

        self.countdown_updated.emit(self._format_remaining(delta))

    def _build_target_datetime(self, now: datetime.datetime) -> datetime.datetime:
        """Combine today's date with the stored target time (UTC).

        If the result is in the past relative to *now*, advance by one day.
        """
        assert self._target_time is not None, "No target time scheduled"
        target = datetime.datetime.combine(
            now.date(), self._target_time, tzinfo=datetime.timezone.utc
        )
        if target < now:
            target += datetime.timedelta(days=1)
        return target

    @staticmethod
    def _format_remaining(seconds: float) -> str:
        """Format *seconds* as ``HH:MM:SS``."""
        total = max(int(seconds), 0)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _cleanup(self) -> None:
        """Stop timer and reset scheduling state (no signal emitted)."""
        self._timer.stop()
        self._target_time = None
        self._binding = None
        self._focus_attempted = False

    def _make_fire_block(self) -> Callable[[], None]:
        binding = self._binding
        button_x = self._button_x
        button_y = self._button_y
        focus = self._resolve_focus_func()

        def run() -> None:
            # Hold the process-wide dispatch lock across focus + click so the
            # scheduled click cannot interleave with a worker's focus-then-input
            # critical section (Bug G).
            with _input_handler.dispatch_lock:
                focus(binding)
                self._click_func(button_x, button_y)

        return run

    def _resolve_focus_func(self) -> Callable[..., None]:
        """Return the focus callable, lazily wrapping FocusGuard if needed."""
        if self._focus_func is not None:
            return self._focus_func
        return lambda binding: FocusGuard(binding).ensure_focus()
