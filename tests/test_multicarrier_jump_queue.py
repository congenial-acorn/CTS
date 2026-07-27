from __future__ import annotations

import datetime
import importlib
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from TraversalSystem.config import TraversalOptions
from TraversalSystem.runtime.controller import (
    TraversalRuntimeContext,
    TraversalRuntimeDependencies,
)
from TraversalSystem.sequence_queue import SequenceQueue


def _load_main_with_mocks(tmp_path: Path) -> Any:
    overrides: dict[str, ModuleType] = {}
    for mod_name in [
        "psutil",
        "pyautogui",
        "pyperclip",
        "pytz",
        "tzlocal",
        "discord_webhook",
    ]:
        module = ModuleType(mod_name)
        overrides[mod_name] = module

    cast(Any, overrides["pyautogui"]).FAILSAFE = False
    cast(Any, overrides["pytz"]).UTC = datetime.timezone.utc
    cast(Any, overrides["tzlocal"]).get_localzone = lambda: datetime.timezone.utc

    discord_mod = ModuleType("TraversalSystem.discordhandler")
    cast(Any, discord_mod).DiscordHandler = MagicMock
    overrides["TraversalSystem.discordhandler"] = discord_mod

    reshandler_mod = ModuleType("TraversalSystem.reshandler")
    cast(Any, reshandler_mod).Reshandler = MagicMock
    overrides["TraversalSystem.reshandler"] = reshandler_mod

    saved = {name: sys.modules.get(name) for name in overrides}
    saved_main = sys.modules.get("TraversalSystem.main")
    try:
        for name, module in overrides.items():
            sys.modules[name] = module
        _ = sys.modules.pop("TraversalSystem.main", None)
        module = importlib.import_module("TraversalSystem.main")
        cast(Any, module).BASE_DIR = tmp_path
        cast(Any, module).SEQUENCE_DIR = tmp_path
        cast(Any, module).SAVE_PATH = tmp_path / "save.txt"
        return module
    finally:
        _ = sys.modules.pop("TraversalSystem.main", None)
        if saved_main is not None:
            sys.modules["TraversalSystem.main"] = saved_main
        for name, original in saved.items():
            if original is None:
                _ = sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class _FakeResHandler:
    supported_res: bool = True

    def __init__(self, _width: object, _height: object) -> None:
        pass


class _RuntimeContextStub:
    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.wait_calls: list[float] = []

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise RuntimeError("cancelled")

    def wait(self, seconds: float) -> None:
        self.wait_calls.append(seconds)


class _BlockingRuntimeContext(_RuntimeContextStub):
    def __init__(self, *, wait_started: threading.Event, release_wait: threading.Event) -> None:
        super().__init__()
        self._wait_started = wait_started
        self._release_wait = release_wait

    def wait(self, seconds: float) -> None:
        super().wait(seconds)
        self._wait_started.set()
        assert self._release_wait.wait(2.0)


class _ManualJournal:
    def __init__(self, *, system_name: str, release_wait: threading.Event) -> None:
        self._system_name = system_name
        self._release_wait = release_wait
        self.requests_checked = 0

    def last_carrier_request(self) -> str | None:
        self.requests_checked += 1
        if not self._release_wait.is_set():
            return None
        return self._system_name

    def departure_time(self) -> str:
        return "2999-01-01T00:00:00Z"


class _InlineHandle:
    def __init__(self, run: Callable[[], object]) -> None:
        self._run = run
        self._resolved = False
        self._result: object | None = None
        self._error: BaseException | None = None
        self.done = threading.Event()

    def result(self, timeout: float | None = None) -> object:
        _ = timeout
        if not self._resolved:
            try:
                self._result = self._run()
            except BaseException as exc:
                self._error = exc
            finally:
                self._resolved = True
                self.done.set()
        if self._error is not None:
            raise self._error
        return self._result


class _InlineJumpQueue:
    def __init__(self) -> None:
        self.submit_jump_plot_calls: list[dict[str, object]] = []

    def submit_jump_plot(
        self,
        *,
        slot_id: str,
        run: Callable[[], object],
        deadline: float,
        estimated_duration: float,
        cancel_event: threading.Event | None = None,
    ) -> _InlineHandle:
        self.submit_jump_plot_calls.append({
            "slot_id": slot_id,
            "deadline": deadline,
            "estimated_duration": estimated_duration,
            "cancel_event": cancel_event,
        })
        return _InlineHandle(run)


class _ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


def _make_runtime_context(
    *,
    options: TraversalOptions,
    sequence_queue: object,
    journal: object,
    slot_id: int,
    sleep: Callable[[float], None],
    cancel_event: threading.Event | None = None,
) -> TraversalRuntimeContext:
    cancel = cancel_event or threading.Event()
    real_wait = cancel.wait
    cancel.wait = lambda timeout=None: real_wait(0)  # type: ignore[assignment]
    return TraversalRuntimeContext(
        options=options,
        dependencies=TraversalRuntimeDependencies(
            journal=journal,
            window=None,
            focus=None,
            sequence_queue=sequence_queue,
            slot_id=slot_id,
        ),
        cancel_event=cancel,
        status_callback=None,
        sleep=sleep,
    )


def _make_options(tmp_path: Path, *, auto_plot_jumps: bool = True) -> TraversalOptions:
    return TraversalOptions(
        webhook_url="",
        journal_directory=tmp_path,
        route_file=tmp_path / "route.txt",
        route_position=0,
        tritium_slot=0,
        auto_plot_jumps=auto_plot_jumps,
        disable_refuel=False,
        refuel_mode=0,
        single_discord_message=False,
        shutdown_on_complete=False,
    )


def _record_jump_submissions(queue: SequenceQueue) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    submit_jump_plot = queue.submit_jump_plot

    def wrapped_submit_jump_plot(
        *,
        slot_id: str,
        run: Callable[[], object],
        deadline: float,
        estimated_duration: float,
        cancel_event: threading.Event | None = None,
    ) -> object:
        calls.append({
            "slot_id": slot_id,
            "run": run,
            "deadline": deadline,
            "estimated_duration": estimated_duration,
            "cancel_event": cancel_event,
        })
        return submit_jump_plot(
            slot_id=slot_id,
            run=run,
            deadline=deadline,
            estimated_duration=estimated_duration,
            cancel_event=cancel_event,
        )

    setattr(queue, "submit_jump_plot", wrapped_submit_jump_plot)
    return calls


def _assert_pending_jump_restock_order(
    tmp_path: Path,
    *,
    jump_deadline: float,
    expected_order: list[str],
) -> list[dict[str, object]]:
    main = _load_main_with_mocks(tmp_path)
    clock = _ManualClock()
    queue = SequenceQueue(time_fn=clock.now)
    options = _make_options(tmp_path)
    runtime_context = _RuntimeContextStub()
    active_started = threading.Event()
    release_active = threading.Event()
    jump_submitted = threading.Event()
    jump_finished = threading.Event()
    execution_order: list[str] = []
    submit_calls: list[dict[str, object]] = []
    jump_result: dict[str, tuple[int, datetime.datetime | int]] = {}
    jump_error: dict[str, BaseException] = {}
    departure = datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc)

    submit_jump_plot = queue.submit_jump_plot

    def wrapped_submit_jump_plot(
        *,
        slot_id: str,
        run: Callable[[], object],
        deadline: float,
        estimated_duration: float,
        cancel_event: threading.Event | None = None,
    ) -> object:
        submit_calls.append({
            "slot_id": slot_id,
            "deadline": deadline,
            "estimated_duration": estimated_duration,
            "cancel_event": cancel_event,
        })
        if slot_id == "slot-jump":
            jump_submitted.set()
        return submit_jump_plot(
            slot_id=slot_id,
            run=run,
            deadline=deadline,
            estimated_duration=estimated_duration,
            cancel_event=cancel_event,
        )

    setattr(queue, "submit_jump_plot", wrapped_submit_jump_plot)

    def active_blocker() -> str:
        execution_order.append("active")
        active_started.set()
        assert release_active.wait(1.0)
        return "active"

    def fake_jump_to_system(
        system_name: str,
        _options: TraversalOptions,
        _res_handler: object,
        _journal: object,
        _sequence_dir: Path,
        _runtime_context: TraversalRuntimeContext,
        focus_handler: object | None = None,
    ) -> tuple[int, datetime.datetime]:
        _ = focus_handler
        execution_order.append("jump")
        return 42, departure

    def restock() -> str:
        execution_order.append("restock")
        clock.advance(60.0)
        return "restock"

    def run_jump() -> None:
        try:
            jump_result["result"] = main._run_coordinated_jump_plot(
                sequence_queue=queue,
                queue_slot_id="slot-jump",
                system_name="Sol",
                options=options,
                res_handler=MagicMock(),
                journal=MagicMock(),
                sequence_dir=tmp_path,
                runtime_context=runtime_context,
                focus_handler=None,
                deadline=jump_deadline,
            )
        except BaseException as exc:
            jump_error["error"] = exc
        finally:
            jump_finished.set()

    jump_thread = threading.Thread(target=run_jump)
    jump_thread_started = False
    restock_handle = None

    with patch.object(main, "jump_to_system", side_effect=fake_jump_to_system):
        try:
            active_handle = queue.submit_jump_plot(
                slot_id="slot-active",
                run=active_blocker,
                deadline=clock.now() + 1000.0,
                estimated_duration=30.0,
                cancel_event=threading.Event(),
            )
            assert active_started.wait(1.0)

            jump_thread.start()
            jump_thread_started = True

            deadline_limit = time.monotonic() + 1.0
            while (
                not jump_submitted.is_set()
                and not jump_finished.is_set()
                and time.monotonic() < deadline_limit
            ):
                _ = jump_finished.wait(0.01)

            if "error" in jump_error:
                raise jump_error["error"]

            assert jump_submitted.is_set()

            restock_handle = queue.submit_restock(
                slot_id="slot-restock",
                estimated_duration=60.0,
                run=restock,
                cancel_event=threading.Event(),
            )

            release_active.set()

            assert active_handle.done.wait(1.0)
            assert restock_handle.done.wait(1.0)
            jump_thread.join(timeout=2.0)

            assert active_handle.result(timeout=0.1) == "active"
            assert restock_handle.result(timeout=0.1) == "restock"
        finally:
            release_active.set()
            if jump_thread_started and jump_thread.is_alive():
                jump_thread.join(timeout=2.0)
            queue.shutdown(wait=True)

    if "error" in jump_error:
        raise jump_error["error"]

    assert jump_thread.is_alive() is False
    assert jump_result["result"] == (42, departure)
    assert execution_order == expected_order
    return submit_calls


def test_future_jump_deadline_allows_restock_to_run_before_pending_jump(
    tmp_path: Path,
) -> None:
    jump_deadline = 300.0

    submit_calls = _assert_pending_jump_restock_order(
        tmp_path,
        jump_deadline=jump_deadline,
        expected_order=["active", "restock", "jump"],
    )

    jump_call = next(call for call in submit_calls if call["slot_id"] == "slot-jump")
    assert jump_call["deadline"] == jump_deadline


def test_soon_jump_deadline_defers_restock_until_after_pending_jump(
    tmp_path: Path,
) -> None:
    jump_deadline = 30.0

    submit_calls = _assert_pending_jump_restock_order(
        tmp_path,
        jump_deadline=jump_deadline,
        expected_order=["active", "jump", "restock"],
    )

    jump_call = next(call for call in submit_calls if call["slot_id"] == "slot-jump")
    assert jump_call["deadline"] == jump_deadline


def test_traversal_slot_passes_immediate_then_registered_cooldown_deadlines(
    tmp_path: Path,
) -> None:
    main = _load_main_with_mocks(tmp_path)
    clock = _ManualClock(start=1000.0)
    options = _make_options(tmp_path)
    journal = MagicMock()
    journal.reset_cancel.return_value = None
    journal.jump_cancelled.return_value = False
    journal.has_jumped.return_value = True
    departures = {
        "Sol": datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc),
        "Achenar": datetime.datetime(2999, 1, 2, tzinfo=datetime.timezone.utc),
    }
    queue = object()
    runtime_context = _make_runtime_context(
        options=options,
        sequence_queue=queue,
        journal=journal,
        slot_id=0,
        sleep=lambda _seconds: None,
    )
    runtime_context.cancel_event.wait = (  # type: ignore[assignment]
        lambda timeout=None: clock.advance(float(timeout or 0.0)) or False
    )
    jump_calls: list[dict[str, object]] = []
    restock_calls: list[dict[str, object]] = []
    registered_deadlines: list[float] = []
    cleared_deadlines: list[str] = []

    def fake_run_coordinated_jump_plot(
        *,
        system_name: str,
        deadline: float | None = None,
        **_kwargs: object,
    ) -> tuple[int, datetime.datetime]:
        jump_calls.append({
            "system_name": system_name,
            "deadline": deadline,
            "seen_at": clock.now(),
        })
        return 6, departures[system_name]

    def record_deadline(
        _sequence_queue: object,
        *,
        slot_id: str,
        deadline: float,
    ) -> None:
        assert slot_id == "slot-0"
        registered_deadlines.append(deadline)

    def record_restock(**kwargs: object) -> None:
        assert cleared_deadlines == []
        restock_calls.append(kwargs)

    def record_clear(_sequence_queue: object, *, slot_id: str) -> None:
        cleared_deadlines.append(slot_id)

    with patch("builtins.print"), \
         patch.object(main, "DiscordHandler", return_value=MagicMock()), \
         patch.object(main, "Reshandler", _FakeResHandler), \
         patch.object(main, "load_route_list", return_value=["Sol", "Achenar"]), \
         patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
         patch.object(main, "consume_save", return_value=None), \
         patch.object(main.time, "monotonic", side_effect=clock.now), \
         patch.object(main.time, "sleep", side_effect=clock.advance), \
         patch.object(main, "_run_coordinated_jump_plot", side_effect=fake_run_coordinated_jump_plot), \
         patch.object(main, "_run_coordinated_restock", side_effect=record_restock), \
         patch.object(main, "_register_jump_deadline", side_effect=record_deadline), \
         patch.object(main, "_clear_jump_deadline", side_effect=record_clear):
        assert main._run_traversal_slot(runtime_context) is True

    assert [call["system_name"] for call in jump_calls] == ["Sol", "Achenar"]
    assert jump_calls[0]["deadline"] == pytest.approx(jump_calls[0]["seen_at"])
    assert registered_deadlines
    assert jump_calls[1]["deadline"] == pytest.approx(registered_deadlines[-1])
    assert cast(float, jump_calls[1]["deadline"]) < cast(float, jump_calls[1]["seen_at"])
    assert len(restock_calls) == 1
    assert restock_calls[0]["queue_slot_id"] == "slot-0"
    assert restock_calls[0]["not_before"] == pytest.approx(
        cast(float, jump_calls[0]["seen_at"]) + 6.0 + main.RESTOCK_TRIGGER_REMAINING_SECONDS
    )
    assert cleared_deadlines


def test_concurrent_auto_jump_plot_serializes_queue_blocks(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    queue = SequenceQueue()
    submit_calls = _record_jump_submissions(queue)
    options = _make_options(tmp_path)
    runtime_context = _RuntimeContextStub()
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    results: dict[str, tuple[int, datetime.datetime | int]] = {}
    run_order: list[str] = []
    active = 0
    max_active = 0
    active_lock = threading.Lock()
    departures = {
        "Sol": datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc),
        "Achenar": datetime.datetime(2999, 1, 2, tzinfo=datetime.timezone.utc),
    }

    def fake_jump_to_system(
        system_name: str,
        passed_options: TraversalOptions,
        _res_handler: object,
        _journal: object,
        passed_sequence_dir: Path,
        passed_runtime_context: _RuntimeContextStub,
        focus_handler: object | None = None,
    ) -> tuple[int, datetime.datetime]:
        nonlocal active, max_active
        assert passed_options is options
        assert passed_sequence_dir == tmp_path
        assert passed_runtime_context is runtime_context
        assert focus_handler is None
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            run_order.append(system_name)
            if system_name == "Sol":
                first_started.set()
                assert release_first.wait(2.0)
            else:
                second_started.set()
            return 42, departures[system_name]
        finally:
            with active_lock:
                active -= 1

    def run_attempt(system_name: str, slot_id: str) -> None:
        results[system_name] = main._run_coordinated_jump_plot(
            sequence_queue=queue,
            queue_slot_id=slot_id,
            system_name=system_name,
            options=options,
            res_handler=MagicMock(),
            journal=MagicMock(),
            sequence_dir=tmp_path,
            runtime_context=runtime_context,
            focus_handler=None,
        )

    with patch.object(main, "jump_to_system", side_effect=fake_jump_to_system):
        first_thread = threading.Thread(target=run_attempt, args=("Sol", "slot-0"))
        second_thread = threading.Thread(target=run_attempt, args=("Achenar", "slot-1"))
        try:
            first_thread.start()
            assert first_started.wait(1.0)

            second_thread.start()
            assert second_started.wait(0.1) is False

            release_first.set()
            first_thread.join(timeout=2.0)
            second_thread.join(timeout=2.0)
        finally:
            queue.shutdown(wait=True)

    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False
    assert max_active == 1
    assert run_order == ["Sol", "Achenar"]
    assert results == {
        "Sol": (42, departures["Sol"]),
        "Achenar": (42, departures["Achenar"]),
    }
    assert [call["slot_id"] for call in submit_calls] == ["slot-0", "slot-1"]
    assert all(
        call["estimated_duration"] == main.DEFAULT_JUMP_PLOT_ESTIMATE_SECONDS
        for call in submit_calls
    )
    assert all(call["cancel_event"] is runtime_context.cancel_event for call in submit_calls)


def test_manual_mode_releases_queue_before_journal_wait(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    queue = SequenceQueue()
    submit_calls = _record_jump_submissions(queue)
    wait_started = threading.Event()
    release_wait = threading.Event()
    runtime_context = _BlockingRuntimeContext(
        wait_started=wait_started,
        release_wait=release_wait,
    )
    options = _make_options(tmp_path, auto_plot_jumps=False)
    journal = _ManualJournal(system_name="Sol", release_wait=release_wait)
    result_holder: dict[str, tuple[int, datetime.datetime | int]] = {}
    other_started = threading.Event()

    def run_manual_attempt() -> None:
        result_holder["result"] = main._run_coordinated_jump_plot(
            sequence_queue=queue,
            queue_slot_id="slot-manual",
            system_name="Sol",
            options=options,
            res_handler=MagicMock(),
            journal=journal,
            sequence_dir=tmp_path,
            runtime_context=runtime_context,
            focus_handler=None,
        )

    with patch.object(main.pyperclip, "copy", create=True) as copy_mock:
        manual_thread = threading.Thread(target=run_manual_attempt)
        try:
            manual_thread.start()
            assert wait_started.wait(1.0)

            other_handle = queue.submit_jump_plot(
                slot_id="slot-other",
                run=lambda: other_started.set() or "other",
                deadline=time.monotonic(),
                estimated_duration=1.0,
                cancel_event=threading.Event(),
            )

            assert manual_thread.is_alive() is True
            assert other_started.wait(1.0)
            release_wait.set()

            manual_thread.join(timeout=2.0)
            assert other_handle.result(timeout=0.1) == "other"
        finally:
            queue.shutdown(wait=True)

    assert manual_thread.is_alive() is False
    copy_mock.assert_called_once_with("sol")
    assert result_holder["result"][0] > 0
    assert isinstance(result_holder["result"][1], datetime.datetime)
    manual_calls = [call for call in submit_calls if call["slot_id"] == "slot-manual"]
    assert len(manual_calls) == 1
    assert manual_calls[0]["estimated_duration"] == main.DEFAULT_JUMP_PLOT_ESTIMATE_SECONDS
    assert manual_calls[0]["cancel_event"] is runtime_context.cancel_event
    assert journal.requests_checked >= 1


def test_retry_failed_jump_submits_separate_queue_blocks(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    queue = _InlineJumpQueue()
    options = _make_options(tmp_path)
    attempts = 0
    executed_systems: list[str] = []
    journal = MagicMock()
    journal.reset_cancel.return_value = None

    def fake_jump_to_system(
        system_name: str,
        _options: TraversalOptions,
        _res_handler: object,
        _journal: object,
        _sequence_dir: Path,
        _runtime_context: TraversalRuntimeContext,
        focus_handler: object | None = None,
    ) -> tuple[int, datetime.datetime | int]:
        nonlocal attempts
        _ = focus_handler
        attempts += 1
        executed_systems.append(system_name)
        if attempts <= 3:
            return 0, 0
        raise KeyboardInterrupt

    runtime_context = _make_runtime_context(
        options=options,
        sequence_queue=queue,
        journal=journal,
        slot_id=0,
        sleep=lambda _seconds: None,
    )

    with patch("builtins.print"), \
         patch.object(main, "DiscordHandler", return_value=MagicMock()), \
         patch.object(main, "Reshandler", _FakeResHandler), \
         patch.object(main, "load_route_list", return_value=["Sol"]), \
         patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
         patch.object(main, "consume_save", return_value=None), \
         patch.object(main, "save_progress"), \
         patch.object(main, "jump_to_system", side_effect=fake_jump_to_system):
        assert main._run_traversal_slot(runtime_context) is False

    assert executed_systems == ["Sol", "Sol", "Sol"]
    assert len(queue.submit_jump_plot_calls) == 3
    assert all(call["slot_id"] == "slot-0" for call in queue.submit_jump_plot_calls)
    assert all(
        call["estimated_duration"] == main.DEFAULT_JUMP_PLOT_ESTIMATE_SECONDS
        for call in queue.submit_jump_plot_calls
    )
    assert all(
        call["cancel_event"] is runtime_context.cancel_event
        for call in queue.submit_jump_plot_calls
    )


@pytest.mark.parametrize(
    ("sequence_queue", "label"),
    [
        (None, "none"),
        (object(), "missing-api"),
    ],
)
def test_cli_fallback_uses_direct_jump_to_system_without_queue(
    tmp_path: Path,
    sequence_queue: object | None,
    label: str,
) -> None:
    _ = label
    main = _load_main_with_mocks(tmp_path)
    options = _make_options(tmp_path)
    runtime_context = _RuntimeContextStub()
    expected_departure = datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc)
    expected_result = (12, expected_departure)

    with patch.object(main, "jump_to_system", return_value=expected_result) as jump_to_system_mock:
        result = main._run_coordinated_jump_plot(
            sequence_queue=sequence_queue,
            queue_slot_id="slot-0",
            system_name="Sol",
            options=options,
            res_handler=MagicMock(),
            journal=MagicMock(),
            sequence_dir=tmp_path,
            runtime_context=runtime_context,
            focus_handler=None,
        )

    assert result == expected_result
    jump_to_system_mock.assert_called_once()


def test_estimate_restock_duration_scales_with_tritium_slot(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)

    per_slot = main.RESTOCK_PER_SLOT_SECONDS
    focus_overhead = main.RESTOCK_FOCUS_OVERHEAD_PER_INPUT_SECONDS
    fixed_inputs = main.RESTOCK_INPUTS_FIXED
    inputs_per_slot = main.RESTOCK_INPUTS_PER_SLOT
    assert main.estimate_restock_duration(0) == pytest.approx(
        main.RESTOCK_FIXED_OVERHEAD_SECONDS + focus_overhead * fixed_inputs
    )

    assert main.estimate_restock_duration(100) == pytest.approx(
        main.RESTOCK_FIXED_OVERHEAD_SECONDS
        + per_slot * 100
        + focus_overhead * (fixed_inputs + inputs_per_slot * 100)
    )
    assert main.estimate_restock_duration(500) == pytest.approx(
        main.RESTOCK_FIXED_OVERHEAD_SECONDS
        + per_slot * 500
        + focus_overhead * (fixed_inputs + inputs_per_slot * 500)
    )


def test_estimate_restock_duration_includes_focus_overhead(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)

    duration = main.estimate_restock_duration(0)

    assert duration > main.RESTOCK_FIXED_OVERHEAD_SECONDS
    assert duration == pytest.approx(
        main.RESTOCK_FIXED_OVERHEAD_SECONDS
        + main.RESTOCK_FOCUS_OVERHEAD_PER_INPUT_SECONDS * main.RESTOCK_INPUTS_FIXED
    )


def test_coordinated_restock_passes_dynamic_estimate_to_queue(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    options = _make_options(tmp_path)
    options.tritium_slot = 200
    queue = MagicMock()
    pending_handle = MagicMock()
    queue.submit_restock.return_value = pending_handle

    with patch.object(main, "restock_tritium"):
        result = main._run_coordinated_restock(
            sequence_queue=queue,
            queue_slot_id="slot-0",
            cancel_event=threading.Event(),
            runtime_context=None,
            options=options,
            sequence_dir=tmp_path,
            focus_handler=None,
            not_before=1234.0,
        )

    assert result is pending_handle
    queue.submit_restock.assert_called_once()
    assert queue.submit_restock.call_args.kwargs["not_before"] == 1234.0
    submitted_duration = queue.submit_restock.call_args.kwargs["estimated_duration"]
    expected = (
        main.RESTOCK_FIXED_OVERHEAD_SECONDS
        + main.RESTOCK_PER_SLOT_SECONDS * 200
        + main.RESTOCK_FOCUS_OVERHEAD_PER_INPUT_SECONDS
        * (main.RESTOCK_INPUTS_FIXED + main.RESTOCK_INPUTS_PER_SLOT * 200)
    )
    assert submitted_duration == pytest.approx(expected)
    assert submitted_duration != main.DEFAULT_RESTOCK_ESTIMATE_SECONDS
    pending_handle.result.assert_not_called()


def test_coordinated_restock_skipped_when_auto_plot_jumps_disabled(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    options = _make_options(tmp_path, auto_plot_jumps=False)
    options.disable_refuel = False
    queue = MagicMock()
    queue.submit_restock.return_value = MagicMock()

    result = main._run_coordinated_restock(
        sequence_queue=queue,
        queue_slot_id="slot-0",
        cancel_event=threading.Event(),
        runtime_context=None,
        options=options,
        sequence_dir=tmp_path,
        focus_handler=None,
        not_before=1234.0,
    )

    assert result is None
    queue.submit_restock.assert_not_called()


def test_coordinated_restock_returns_pending_handle_without_waiting(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    options = _make_options(tmp_path)
    runtime_context = _RuntimeContextStub()
    captured: dict[str, threading.Event] = {}

    class _PendingHandle:
        def __init__(self) -> None:
            self.done = threading.Event()

        def result(self, timeout: float | None = None) -> object:
            _ = timeout
            raise AssertionError("asynchronous submission must not wait for result")

        def cancel(self) -> None:
            captured["cancel_event"].set()

    pending_handle = _PendingHandle()

    def _submit_restock(
        *, slot_id: str, run: Callable[[], object],
        estimated_duration: float, cancel_event: threading.Event,
        not_before: float,
    ) -> object:
        _ = slot_id, run, estimated_duration
        assert not_before == 1234.0
        captured["cancel_event"] = cancel_event
        return pending_handle

    queue = MagicMock()
    queue.submit_restock.side_effect = _submit_restock

    with patch.object(main, "restock_tritium"):
        result = main._run_coordinated_restock(
            sequence_queue=queue,
            queue_slot_id="slot-0",
            cancel_event=threading.Event(),
            runtime_context=runtime_context,
            options=options,
            sequence_dir=tmp_path,
            focus_handler=None,
            not_before=1234.0,
        )

    assert result is pending_handle
    assert runtime_context.cancel_event.is_set() is False
    assert captured["cancel_event"] is not runtime_context.cancel_event
    assert captured["cancel_event"].is_set() is False
    queue.submit_restock.assert_called_once()
    pending_handle.cancel()
    assert captured["cancel_event"].is_set() is True


def test_first_cycle_deadline_is_deterministic_slot_order(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    queue = SequenceQueue()
    try:
        # The shared base is captured once atomically; every slot deadline then
        # derives from that identical base, so ordering is purely slot-index.
        slot_0 = main._resolve_first_cycle_jump_deadline(queue, slot_id=0, next_jump_plot_deadline=None)
        slot_1 = main._resolve_first_cycle_jump_deadline(queue, slot_id=1, next_jump_plot_deadline=None)
        slot_2 = main._resolve_first_cycle_jump_deadline(queue, slot_id=2, next_jump_plot_deadline=None)
        slot_5 = main._resolve_first_cycle_jump_deadline(queue, slot_id=5, next_jump_plot_deadline=None)
    finally:
        queue.shutdown()

    assert slot_1 > slot_0
    assert slot_1 - slot_0 == pytest.approx(main.FIRST_CYCLE_SLOT_ORDER_OFFSET_SECONDS)
    assert slot_5 > slot_2
    assert (slot_5 - slot_0) == pytest.approx(5 * main.FIRST_CYCLE_SLOT_ORDER_OFFSET_SECONDS)

    # next_jump_plot_deadline passes through unchanged on subsequent cycles.
    explicit_deadline = 4321.0
    assert (
        main._resolve_first_cycle_jump_deadline(
            queue, slot_id=99, next_jump_plot_deadline=explicit_deadline,
        )
        == explicit_deadline
    )

    fallback = main._resolve_first_cycle_jump_deadline(None, slot_id=3, next_jump_plot_deadline=None)
    assert isinstance(fallback, float)


def test_first_cycle_deadline_orders_concurrent_workers_by_slot(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    num_slots = 6
    queue = SequenceQueue()
    # The batch initiator arms the first-cycle barrier so the queue withholds
    # dispatch until every sibling jump block is pending; only then can the
    # deadline sort impose slot-index order.
    queue.arm_first_cycle_barrier(expected_count=num_slots, timeout_seconds=5.0)
    execution_order: list[int] = []
    order_lock = threading.Lock()
    barrier = threading.Barrier(num_slots)

    def worker(slot_id: int) -> None:
        # Block until every thread is ready, then release them all at once so
        # the claim/submit calls race under real OS scheduling skew.
        barrier.wait()
        deadline = main._resolve_first_cycle_jump_deadline(
            queue, slot_id=slot_id, next_jump_plot_deadline=None,
        )

        def block_run(slot: int = slot_id) -> None:
            with order_lock:
                execution_order.append(slot)

        queue.submit_jump_plot(
            slot_id=f"slot-{slot_id}",
            run=block_run,
            deadline=deadline,
            estimated_duration=1.0,
            cancel_event=threading.Event(),
        )

    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_slots)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wait_deadline = time.monotonic() + 5.0
        while len(execution_order) < num_slots and time.monotonic() < wait_deadline:
            time.sleep(0.01)
    finally:
        queue.shutdown()

    assert execution_order == list(range(num_slots)), (
        f"Expected slot-index execution order [0..{num_slots - 1}], got {execution_order}"
    )


def test_first_cycle_barrier_holds_head_block_until_siblings_arrive(
    tmp_path: Path,
) -> None:
    # The regression: the head (first-submitted) block must NOT run while the
    # queue is idle before its later-indexed siblings are submitted. Submit
    # slot-2 first with the latest deadline, then slots 1 and 0; the barrier
    # must still dispatch them strictly by slot index.
    _ = _load_main_with_mocks(tmp_path)
    queue = SequenceQueue()
    queue.arm_first_cycle_barrier(expected_count=3, timeout_seconds=5.0)
    execution_order: list[int] = []
    order_lock = threading.Lock()

    def make_block(slot: int) -> Callable[[], None]:
        def block_run() -> None:
            with order_lock:
                execution_order.append(slot)

        return block_run

    try:
        base = time.monotonic()
        # Submit the highest-index slot first; if the barrier were absent it
        # would run immediately while the queue is idle.
        for slot in (2, 1, 0):
            queue.submit_jump_plot(
                slot_id=f"slot-{slot}",
                run=make_block(slot),
                deadline=base + slot * 0.001,
                estimated_duration=1.0,
                cancel_event=threading.Event(),
            )
            # Give a would-be premature dispatch a chance to misfire.
            time.sleep(0.02)

        wait_deadline = time.monotonic() + 5.0
        while len(execution_order) < 3 and time.monotonic() < wait_deadline:
            time.sleep(0.01)
    finally:
        queue.shutdown()

    assert execution_order == [0, 1, 2], (
        f"Expected slot-index order [0, 1, 2], got {execution_order}"
    )


def test_first_cycle_barrier_releases_on_timeout_when_sibling_missing(
    tmp_path: Path,
) -> None:
    # Safety valve: if an expected sibling never submits (e.g. its worker failed
    # to start), the already-pending block must still dispatch once the barrier
    # timeout elapses rather than stalling forever.
    _ = _load_main_with_mocks(tmp_path)
    queue = SequenceQueue()
    queue.arm_first_cycle_barrier(expected_count=3, timeout_seconds=0.2)
    ran = threading.Event()

    try:
        handle = queue.submit_jump_plot(
            slot_id="slot-0",
            run=lambda: ran.set(),
            deadline=time.monotonic(),
            estimated_duration=1.0,
            cancel_event=threading.Event(),
        )
        # Only one of three expected blocks submitted: must wait for timeout.
        assert ran.wait(0.1) is False
        assert ran.wait(2.0) is True
        assert handle.result(timeout=1.0) is None
    finally:
        queue.shutdown()


def test_traversal_slot_skips_restock_on_final_route_element(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    clock = _ManualClock(start=1000.0)
    options = _make_options(tmp_path)
    journal = MagicMock()
    journal.reset_cancel.return_value = None
    journal.reset_jump.return_value = None
    journal.jump_cancelled.return_value = False
    journal.has_jumped.return_value = True
    departure = datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc)
    runtime_context = _make_runtime_context(
        options=options,
        sequence_queue=object(),
        journal=journal,
        slot_id=0,
        sleep=lambda _seconds: None,
    )
    runtime_context.cancel_event.wait = (  # type: ignore[assignment]
        lambda timeout=None: clock.advance(float(timeout or 0.0)) or False
    )

    def fake_run_coordinated_jump_plot(**_kwargs: object) -> tuple[int, datetime.datetime]:
        return 6, departure

    with patch("builtins.print"), \
         patch.object(main, "DiscordHandler", return_value=MagicMock()), \
         patch.object(main, "Reshandler", _FakeResHandler), \
         patch.object(main, "load_route_list", return_value=["Sol"]), \
         patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
         patch.object(main, "consume_save", return_value=None), \
         patch.object(main.time, "monotonic", side_effect=clock.now), \
         patch.object(main.time, "sleep", side_effect=clock.advance), \
         patch.object(main, "_run_coordinated_jump_plot", side_effect=fake_run_coordinated_jump_plot), \
         patch.object(main, "_run_coordinated_restock") as restock_mock:
        assert main._run_traversal_slot(runtime_context) is True

    restock_mock.assert_not_called()


def test_journal_confirmation_timeout_stops_slot(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)
    clock = _ManualClock(start=1000.0)
    options = _make_options(tmp_path)
    journal = MagicMock()
    journal.reset_cancel.return_value = None
    journal.jump_cancelled.return_value = False
    journal.has_jumped.return_value = False
    departure = datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc)
    queue = object()
    runtime_context = _make_runtime_context(
        options=options,
        sequence_queue=queue,
        journal=journal,
        slot_id=0,
        sleep=lambda _seconds: None,
    )
    runtime_context.cancel_event.wait = (  # type: ignore[assignment]
        lambda timeout=None: clock.advance(float(timeout or 0.0)) or False
    )

    def fake_run_coordinated_jump_plot(**_kwargs: object) -> tuple[int, datetime.datetime]:
        return 6, departure

    with patch("builtins.print"), \
         patch.object(main, "DiscordHandler", return_value=MagicMock()), \
         patch.object(main, "Reshandler", _FakeResHandler), \
         patch.object(main, "load_route_list", return_value=["Sol"]), \
         patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
         patch.object(main, "consume_save", return_value=None), \
         patch.object(main, "save_progress"), \
         patch.object(main.time, "monotonic", side_effect=clock.now), \
         patch.object(main.time, "sleep", side_effect=clock.advance), \
         patch.object(main, "_run_coordinated_jump_plot", side_effect=fake_run_coordinated_jump_plot), \
         patch.object(main, "_run_coordinated_restock"), \
         patch.object(main, "JOURNAL_CONFIRMATION_TIMEOUT_SECONDS", 5.0):
        result = main._run_traversal_slot(runtime_context)

    assert result is False


def test_timing_constants_are_documented_and_correct(tmp_path: Path) -> None:
    main = _load_main_with_mocks(tmp_path)

    assert main.CARRIER_COOLDOWN_SECONDS == 362
    assert main.RESTOCK_TRIGGER_REMAINING_SECONDS == 300
    assert main.ESTIMATED_CYCLE_SECONDS == 1320
