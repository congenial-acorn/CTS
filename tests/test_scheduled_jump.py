"""Comprehensive tests for ScheduledJumpController.

Exercises the full public API: schedule(), cancel(), is_scheduled,
remaining_seconds, countdown_updated signal, status_changed signal,
focus retry logic, click execution, and re-scheduling.
"""

from __future__ import annotations

import datetime
import re
import time

import pytest
from unittest.mock import Mock, MagicMock, call

from PySide6.QtCore import QObject

from TraversalSystem.gui.scheduled_jump import ScheduledJumpController
from TraversalSystem.window_manager import FocusError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_until(app, predicate, *, timeout=2.0):
    """Spin the Qt event loop until *predicate* is true or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "Condition not met within timeout"


def _utc(year, month, day, hour, minute, second=0):
    """Shorthand for constructing an aware UTC datetime."""
    return datetime.datetime(year, month, day, hour, minute, second,
                             tzinfo=datetime.timezone.utc)


def _utc_time(hour, minute, second=0):
    """Shorthand for constructing a UTC time."""
    return datetime.time(hour, minute, second)


# ---------------------------------------------------------------------------
# Signal collector — gathers emissions into a list
# ---------------------------------------------------------------------------

class _SignalCollector:
    """Connects to a Signal and records every emission."""

    def __init__(self):
        self.calls: list[object] = []

    def slot(self, *args):
        self.calls.append(args if len(args) != 1 else args[0])

    def __contains__(self, item):
        return item in self.calls

    def __len__(self):
        return len(self.calls)


# ===========================================================================
# Test class
# ===========================================================================

class TestScheduledJumpController:
    """10 comprehensive tests for ScheduledJumpController."""

    # -- 1. schedule() starts timer and emits "scheduled" ---------------------

    def test_schedule_starts_timer(self, qapp):
        now = _utc(2026, 1, 1, 12, 0, 0)
        controller = ScheduledJumpController(
            time_provider=lambda: now,
            parent=qapp,
        )

        status = _SignalCollector()
        controller.status_changed.connect(status.slot)

        controller.schedule(
            target_utc=_utc_time(12, 5, 0),  # 5 min in the future
            button_x=100,
            button_y=200,
            binding=Mock(),
        )

        assert controller.is_scheduled is True
        assert "scheduled" in status

    # -- 2. schedule() with an elapsed time-of-day rolls to tomorrow ----------

    def test_schedule_with_past_time_rolls_to_tomorrow(self, qapp):
        # A time-of-day earlier than "now" must schedule for the same time
        # tomorrow (consistent with _build_target_datetime), not raise.
        now = _utc(2026, 1, 1, 12, 0, 0)
        controller = ScheduledJumpController(
            time_provider=lambda: now,
            parent=qapp,
        )

        controller.schedule(
            target_utc=_utc_time(11, 0, 0),  # 1 hour earlier today
            button_x=100,
            button_y=200,
            binding=Mock(),
        )

        assert controller.is_scheduled is True
        # 11:00 tomorrow is 23h from 12:00 today.
        assert controller.remaining_seconds == pytest.approx(23 * 3600, abs=2)

    # -- 3. countdown_updated emits formatted strings -------------------------

    def test_countdown_updates(self, qapp):
        # Time sequence: schedule uses idx 0, tick 1 uses idx 1, tick 2 uses idx 2
        tick_times = [
            _utc(2026, 1, 1, 12, 0, 0),   # idx 0: schedule call (T-30)
            _utc(2026, 1, 1, 12, 0, 1),   # idx 1: tick 1  (T-29)
            _utc(2026, 1, 1, 12, 0, 2),   # idx 2: tick 2  (T-28)
        ]
        idx = 0

        def time_fn():
            nonlocal idx
            t = tick_times[min(idx, len(tick_times) - 1)]
            idx += 1
            return t

        controller = ScheduledJumpController(
            time_provider=time_fn,
            click_func=Mock(),
            parent=qapp,
        )

        countdown = _SignalCollector()
        controller.countdown_updated.connect(countdown.slot)

        # Schedule for 30 seconds in future relative to first time value
        controller.schedule(
            target_utc=_utc_time(12, 0, 30),
            button_x=100,
            button_y=200,
            binding=Mock(),
        )

        # Fire two ticks — both well within countdown range
        controller._tick()
        controller._tick()

        # At least one emission should match HH:MM:SS format
        hhmmss = re.compile(r"^\d{2}:\d{2}:\d{2}$")
        assert any(hhmmss.match(str(c)) for c in countdown.calls), (
            f"Expected HH:MM:SS format in countdown emissions, got {countdown.calls}"
        )

    # -- 4. Focus is attempted at T-minus-10 ---------------------------------

    def test_focus_at_t_minus_10(self, qapp):
        focus_func = Mock()
        click_func = Mock()

        # Time sequence:
        #  0: T-12 → schedule call (time_provider returns this initially)
        #  1: T-10 → first tick triggers focus
        times = [
            _utc(2026, 1, 1, 12, 0, 0),   # T-12 (schedule)
            _utc(2026, 1, 1, 12, 0, 2),   # T-10 (first tick)
        ]
        idx = 0

        def time_fn():
            nonlocal idx
            t = times[min(idx, len(times) - 1)]
            idx += 1
            return t

        controller = ScheduledJumpController(
            time_provider=time_fn,
            focus_func=focus_func,
            click_func=click_func,
            parent=qapp,
        )

        binding = Mock()
        controller.schedule(
            target_utc=_utc_time(12, 0, 12),  # target at T=12s
            button_x=100,
            button_y=200,
            binding=binding,
        )

        # Tick with time at T-10 → should trigger focus
        controller._tick()

        focus_func.assert_called_once_with(binding)
        click_func.assert_not_called()

    # -- 5. Click fires at T-zero and emits "completed" ----------------------

    def test_click_at_t_zero(self, qapp):
        click_func = Mock()
        focus_func = Mock()

        # Use a mutable container to switch time between schedule and tick phases.
        # schedule() must see now < target for validation.
        # _tick() must see now >= target for click to fire.
        # schedule() validates target > now, so use T-1 for schedule phase.
        # _tick() needs now == target for delta=0 and click to fire.
        # (target < now triggers the day-wrap safety net in _build_target_datetime.)
        schedule_time = _utc(2026, 1, 1, 11, 59, 59)
        tick_time = _utc(2026, 1, 1, 12, 0, 0)

        schedule_done = [False]

        def time_fn():
            if not schedule_done[0]:
                return schedule_time
            return tick_time

        controller = ScheduledJumpController(
            time_provider=time_fn,
            focus_func=focus_func,
            click_func=click_func,
            parent=qapp,
        )

        status = _SignalCollector()
        controller.status_changed.connect(status.slot)

        button_x, button_y = 150, 250
        controller.schedule(
            target_utc=_utc_time(12, 0, 0),
            button_x=button_x,
            button_y=button_y,
            binding=Mock(),
        )

        schedule_done[0] = True

        controller._tick()

        click_func.assert_called_once_with(button_x, button_y)
        assert "completed" in status
        assert controller.is_scheduled is False

    def test_click_at_t_zero_submits_shared_queue_block(self, qapp):
        submit_func = Mock()
        click_func = Mock()
        focus_func = Mock()
        binding = Mock()

        schedule_time = _utc(2026, 1, 1, 11, 59, 59)
        tick_time = _utc(2026, 1, 1, 12, 0, 0)
        schedule_done = [False]

        def time_fn():
            if not schedule_done[0]:
                return schedule_time
            return tick_time

        controller = ScheduledJumpController(
            time_provider=time_fn,
            focus_func=focus_func,
            click_func=click_func,
            submit_func=submit_func,
            parent=qapp,
        )

        controller.schedule(
            target_utc=_utc_time(12, 0, 0),
            button_x=150,
            button_y=250,
            binding=binding,
        )

        schedule_done[0] = True

        controller._tick()

        submit_func.assert_called_once()
        args = submit_func.call_args.args
        assert len(args) == 3
        run_callable, deadline, cancel_event = args
        assert callable(run_callable)
        assert isinstance(deadline, float)
        assert cancel_event is controller._cancel_event
        click_func.assert_not_called()

        run_callable()

        focus_func.assert_called_once_with(binding)
        click_func.assert_called_once_with(150, 250)

    def test_click_at_t_zero_without_submit_func_uses_direct_click(self, qapp):
        click_func = Mock()
        focus_func = Mock()

        schedule_time = _utc(2026, 1, 1, 11, 59, 59)
        tick_time = _utc(2026, 1, 1, 12, 0, 0)
        schedule_done = [False]

        def time_fn():
            if not schedule_done[0]:
                return schedule_time
            return tick_time

        controller = ScheduledJumpController(
            time_provider=time_fn,
            focus_func=focus_func,
            click_func=click_func,
            submit_func=None,
            parent=qapp,
        )

        controller.schedule(
            target_utc=_utc_time(12, 0, 0),
            button_x=150,
            button_y=250,
            binding=Mock(),
        )

        schedule_done[0] = True

        controller._tick()

        click_func.assert_called_once_with(150, 250)

    def test_cancel_sets_active_queue_cancel_event(self, qapp):
        now = _utc(2026, 1, 1, 12, 0, 0)
        controller = ScheduledJumpController(
            time_provider=lambda: now,
            submit_func=Mock(),
            parent=qapp,
        )

        controller.schedule(
            target_utc=_utc_time(12, 0, 30),
            button_x=100,
            button_y=200,
            binding=Mock(),
        )

        cancel_event = controller._cancel_event
        assert cancel_event.is_set() is False

        controller.cancel()

        assert cancel_event.is_set() is True

    def test_submit_func_skips_early_focus_branch(self, qapp):
        focus_func = Mock()
        click_func = Mock()
        submit_func = Mock()

        times = [
            _utc(2026, 1, 1, 12, 0, 0),
            _utc(2026, 1, 1, 12, 0, 2),
        ]
        idx = 0

        def time_fn():
            nonlocal idx
            t = times[min(idx, len(times) - 1)]
            idx += 1
            return t

        controller = ScheduledJumpController(
            time_provider=time_fn,
            focus_func=focus_func,
            click_func=click_func,
            submit_func=submit_func,
            parent=qapp,
        )

        controller.schedule(
            target_utc=_utc_time(12, 0, 12),
            button_x=100,
            button_y=200,
            binding=Mock(),
        )

        controller._tick()

        focus_func.assert_not_called()
        click_func.assert_not_called()
        submit_func.assert_not_called()

    # -- 6. cancel() stops timer and emits "cancelled" ------------------------

    def test_cancel_stops_timer(self, qapp):
        now = _utc(2026, 1, 1, 12, 0, 0)
        controller = ScheduledJumpController(
            time_provider=lambda: now,
            parent=qapp,
        )

        status = _SignalCollector()
        controller.status_changed.connect(status.slot)

        controller.schedule(
            target_utc=_utc_time(12, 0, 30),  # 30s in future
            button_x=100,
            button_y=200,
            binding=Mock(),
        )
        assert controller.is_scheduled is True

        controller.cancel()

        assert "cancelled" in status
        assert controller.is_scheduled is False

    # -- 7. Focus failure retries once, then succeeds -------------------------

    def test_focus_failure_retries_once(self, qapp):
        focus_func = Mock(
            side_effect=[FocusError("fail1"), None]  # 1st fails, 2nd succeeds
        )
        click_func = Mock()

        times = [
            _utc(2026, 1, 1, 12, 0, 0),   # T-12 (schedule)
            _utc(2026, 1, 1, 12, 0, 2),   # T-10 (first tick → focus)
            _utc(2026, 1, 1, 12, 0, 3),   # T-9 (retry moment)
        ]
        idx = 0

        def time_fn():
            nonlocal idx
            t = times[min(idx, len(times) - 1)]
            idx += 1
            return t

        sleep_func = Mock()

        controller = ScheduledJumpController(
            time_provider=time_fn,
            focus_func=focus_func,
            click_func=click_func,
            sleep_func=sleep_func,
            parent=qapp,
        )

        status = _SignalCollector()
        controller.status_changed.connect(status.slot)

        binding = Mock()
        controller.schedule(
            target_utc=_utc_time(12, 0, 12),
            button_x=100,
            button_y=200,
            binding=binding,
        )

        controller._tick()

        assert focus_func.call_count == 2
        assert "focusing" in status
        assert "failed" not in status

    # -- 8. Focus failure on both attempts emits "failed" --------------------

    def test_focus_failure_both_attempts_fails(self, qapp):
        focus_func = Mock(side_effect=FocusError("fatal"))
        click_func = Mock()

        times = [
            _utc(2026, 1, 1, 12, 0, 0),   # T-12 (schedule)
            _utc(2026, 1, 1, 12, 0, 2),   # T-10 (tick → focus attempt)
            _utc(2026, 1, 1, 12, 0, 3),   # retry moment
        ]
        idx = 0

        def time_fn():
            nonlocal idx
            t = times[min(idx, len(times) - 1)]
            idx += 1
            return t

        sleep_func = Mock()

        controller = ScheduledJumpController(
            time_provider=time_fn,
            focus_func=focus_func,
            click_func=click_func,
            sleep_func=sleep_func,
            parent=qapp,
        )

        status = _SignalCollector()
        controller.status_changed.connect(status.slot)

        controller.schedule(
            target_utc=_utc_time(12, 0, 12),
            button_x=100,
            button_y=200,
            binding=Mock(),
        )

        controller._tick()

        assert focus_func.call_count == 2
        assert "failed" in status
        assert controller.is_scheduled is False

    # -- 9. Cancel before focus prevents any focus/click calls ----------------

    def test_cancel_before_focus(self, qapp):
        focus_func = Mock()
        click_func = Mock()

        now = _utc(2026, 1, 1, 12, 0, 0)
        controller = ScheduledJumpController(
            time_provider=lambda: now,
            focus_func=focus_func,
            click_func=click_func,
            parent=qapp,
        )

        # Schedule 30s in future (focus threshold is T-10, so at T-0=now
        # we're at T-30 — well before focus)
        controller.schedule(
            target_utc=_utc_time(12, 0, 30),
            button_x=100,
            button_y=200,
            binding=Mock(),
        )

        controller.cancel()

        focus_func.assert_not_called()
        click_func.assert_not_called()
        assert controller.is_scheduled is False

    # -- 10. Re-scheduling replaces the previous countdown --------------------

    def test_re_schedule_replaces_previous(self, qapp):
        click_func = Mock()

        # Time sequence for first schedule + tick + second schedule + tick
        times = [
            _utc(2026, 1, 1, 12, 0, 0),   # 1st schedule
            _utc(2026, 1, 1, 12, 0, 0),   # 2nd schedule (cancel + re-schedule)
            _utc(2026, 1, 1, 12, 0, 1),   # tick after re-schedule
        ]
        idx = 0

        def time_fn():
            nonlocal idx
            t = times[min(idx, len(times) - 1)]
            idx += 1
            return t

        controller = ScheduledJumpController(
            time_provider=time_fn,
            click_func=click_func,
            parent=qapp,
        )

        countdown = _SignalCollector()
        status = _SignalCollector()
        controller.countdown_updated.connect(countdown.slot)
        controller.status_changed.connect(status.slot)

        # First schedule: 30s ahead
        controller.schedule(
            target_utc=_utc_time(12, 0, 30),
            button_x=100,
            button_y=200,
            binding=Mock(),
        )

        # Second schedule: 60s ahead — replaces the first
        controller.schedule(
            target_utc=_utc_time(12, 1, 0),  # 60s from T=0
            button_x=100,
            button_y=200,
            binding=Mock(),
        )

        # Only one active schedule
        assert controller.is_scheduled is True

        # "cancelled" emitted by cancel() inside schedule(), then "scheduled"
        # Index 0: "scheduled" from 1st schedule
        # Index 1: "cancelled" from cancel() inside 2nd schedule
        # Index 2: "scheduled" from 2nd schedule
        assert status.calls.count("scheduled") == 2
        assert "cancelled" in status

        # Countdown should reflect 60s target, not 30s
        # The last countdown emission before any tick is from the 2nd schedule
        initial_countdowns = countdown.calls  # "00:01:00" expected
        assert "00:01:00" in initial_countdowns, (
            f"Expected 60s countdown, got {initial_countdowns}"
        )

        # Tick at T+1: remaining should be 59s → "00:00:59"
        controller._tick()
        assert "00:00:59" in countdown.calls, (
            f"Expected 59s countdown after tick, got {countdown.calls}"
        )

        # Click should NOT have fired (still 59s to go)
        click_func.assert_not_called()
