from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol, TypeVar, cast

import pytest

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)


class _QueueType(Protocol):
    def __call__(self, *, time_fn: Callable[[], float]) -> object: ...


class _DoneSignal(Protocol):
    def wait(self, timeout: float | None = None) -> bool: ...

    def is_set(self) -> bool: ...


class _SubmissionHandle(Protocol[T_co]):
    slot_id: str
    deadline: float | None
    estimated_duration: float
    cancel_event: threading.Event
    done: _DoneSignal

    def result(self, timeout: float | None = None) -> T_co: ...


@dataclass(frozen=True)
class _QueueContract:
    queue_type: _QueueType
    cancelled_error: type[BaseException]


class _ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now: float = start
        self._lock: threading.Lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


def _contract_message() -> str:
    return (
        "Missing GUI coarse-block queue contract. Expected module "
        "TraversalSystem.sequence_queue with class SequenceQueue(*, time_fn=...) "
        "plus submit_jump_plot(slot_id, run, deadline, estimated_duration, "
        "cancel_event=None), submit_restock(slot_id, run, estimated_duration, "
        "cancel_event=None, deadline=None), and shutdown(wait=True). Each submit "
        "must return a handle exposing slot_id, deadline, estimated_duration, "
        "cancel_event, done, and result(timeout=None)."
    )


def _load_contract() -> _QueueContract:
    try:
        module = importlib.import_module("TraversalSystem.sequence_queue")
    except ModuleNotFoundError as exc:
        pytest.fail(_contract_message() + f" Import failed with: {exc}")
    assert isinstance(module, ModuleType)

    queue_type = getattr(module, "SequenceQueue", None)
    if not isinstance(queue_type, type):
        pytest.fail(_contract_message() + " SequenceQueue class is missing.")

    cancelled_error = getattr(module, "CancelledBlockError", CancelledError)
    if not isinstance(cancelled_error, type):
        pytest.fail(
            _contract_message()
            + " CancelledBlockError must be an exception type when provided."
        )

    return _QueueContract(
        queue_type=cast(_QueueType, queue_type),
        cancelled_error=cast(type[BaseException], cancelled_error),
    )


def _make_queue(clock: _ManualClock) -> tuple[object, type[BaseException]]:
    contract = _load_contract()
    try:
        queue = contract.queue_type(time_fn=clock.now)
    except TypeError as exc:
        pytest.fail(
            _contract_message()
            + " SequenceQueue constructor must accept keyword argument time_fn. "
            + f"Got: {exc}"
        )
    return queue, contract.cancelled_error


def _shutdown_queue(queue: object) -> None:
    shutdown = getattr(queue, "shutdown", None)
    if not callable(shutdown):
        pytest.fail(_contract_message() + " SequenceQueue.shutdown(wait=True) is missing.")
    _ = shutdown(wait=True)


def _assert_handle_contract(
    handle: object,
    *,
    slot_id: str,
    deadline: float | None,
    estimated_duration: float,
    cancel_event: threading.Event,
) -> _SubmissionHandle[object]:
    for attribute in ("slot_id", "deadline", "estimated_duration", "cancel_event", "done"):
        if not hasattr(handle, attribute):
            pytest.fail(_contract_message() + f" Submission handle is missing .{attribute}.")
    if not callable(getattr(handle, "result", None)):
        pytest.fail(_contract_message() + " Submission handle must provide result(timeout=None).")

    assert getattr(handle, "slot_id") == slot_id
    assert getattr(handle, "deadline") == deadline
    assert getattr(handle, "estimated_duration") == estimated_duration
    assert getattr(handle, "cancel_event") is cancel_event

    done = cast(_DoneSignal, getattr(handle, "done"))
    assert hasattr(done, "wait")
    assert hasattr(done, "is_set")
    assert done.is_set() is False
    return cast(_SubmissionHandle[object], handle)


def _submit_jump_plot(
    queue: object,
    *,
    slot_id: str,
    deadline: float,
    estimated_duration: float,
    run: Callable[[], T],
    cancel_event: threading.Event | None = None,
) -> _SubmissionHandle[T]:
    submit = getattr(queue, "submit_jump_plot", None)
    if not callable(submit):
        pytest.fail(_contract_message() + " SequenceQueue.submit_jump_plot(...) is missing.")
    event = cancel_event or threading.Event()
    try:
        handle = submit(
            slot_id=slot_id,
            run=run,
            deadline=deadline,
            estimated_duration=estimated_duration,
            cancel_event=event,
        )
    except TypeError as exc:
        pytest.fail(
            _contract_message()
            + " submit_jump_plot must accept slot_id/run/deadline/estimated_duration/"
            + f"cancel_event keywords. Got: {exc}"
        )
    typed_handle = cast(
        _SubmissionHandle[T],
        _assert_handle_contract(
            handle,
            slot_id=slot_id,
            deadline=deadline,
            estimated_duration=estimated_duration,
            cancel_event=event,
        ),
    )
    return typed_handle


def _submit_restock(
    queue: object,
    *,
    slot_id: str,
    estimated_duration: float,
    run: Callable[[], T],
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> _SubmissionHandle[T]:
    submit = getattr(queue, "submit_restock", None)
    if not callable(submit):
        pytest.fail(_contract_message() + " SequenceQueue.submit_restock(...) is missing.")
    event = cancel_event or threading.Event()
    try:
        handle = submit(
            slot_id=slot_id,
            run=run,
            estimated_duration=estimated_duration,
            cancel_event=event,
            deadline=deadline,
        )
    except TypeError as exc:
        pytest.fail(
            _contract_message()
            + " submit_restock must accept slot_id/run/estimated_duration/"
            + f"cancel_event/deadline keywords. Got: {exc}"
        )
    typed_handle = cast(
        _SubmissionHandle[T],
        _assert_handle_contract(
            handle,
            slot_id=slot_id,
            deadline=deadline,
            estimated_duration=estimated_duration,
            cancel_event=event,
        ),
    )
    return typed_handle


def test_one_active_block_at_a_time_and_result_propagation() -> None:
    clock = _ManualClock()
    queue, _cancelled_error = _make_queue(clock)
    release_first = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def enter() -> None:
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)

    def leave() -> None:
        nonlocal active
        with active_lock:
            active -= 1

    def jump_plot() -> str:
        enter()
        try:
            first_started.set()
            assert release_first.wait(1.0)
            return "jump-complete"
        finally:
            leave()

    def restock() -> str:
        enter()
        try:
            second_started.set()
            return "restock-complete"
        finally:
            leave()

    try:
        first = _submit_jump_plot(
            queue,
            slot_id="slot-0",
            deadline=clock.now() + 30.0,
            estimated_duration=30.0,
            run=jump_plot,
        )
        assert first_started.wait(1.0)

        second = _submit_restock(
            queue,
            slot_id="slot-1",
            estimated_duration=15.0,
            deadline=None,
            run=restock,
        )
        assert second_started.wait(0.05) is False

        release_first.set()

        assert first.done.wait(1.0)
        assert second.done.wait(1.0)
        assert first.result(timeout=0.1) == "jump-complete"
        assert second.result(timeout=0.1) == "restock-complete"
        assert max_active == 1
    finally:
        _shutdown_queue(queue)


def test_deadline_jump_plot_uses_earliest_deadline_first() -> None:
    clock = _ManualClock()
    queue, _cancelled_error = _make_queue(clock)
    active_started = threading.Event()
    release_active = threading.Event()
    execution_order: list[str] = []

    def active_jump() -> str:
        execution_order.append("active")
        active_started.set()
        assert release_active.wait(1.0)
        return "active"

    def make_jump(label: str) -> Callable[[], str]:
        def run() -> str:
            execution_order.append(label)
            return label

        return run

    try:
        _ = _submit_jump_plot(
            queue,
            slot_id="slot-active",
            deadline=clock.now() + 100.0,
            estimated_duration=30.0,
            run=active_jump,
        )
        assert active_started.wait(1.0)

        later = _submit_jump_plot(
            queue,
            slot_id="slot-late",
            deadline=clock.now() + 80.0,
            estimated_duration=30.0,
            run=make_jump("jump-late"),
        )
        earlier = _submit_jump_plot(
            queue,
            slot_id="slot-early",
            deadline=clock.now() + 10.0,
            estimated_duration=30.0,
            run=make_jump("jump-early"),
        )

        release_active.set()

        assert earlier.done.wait(1.0)
        assert later.done.wait(1.0)
        assert execution_order == ["active", "jump-early", "jump-late"]
    finally:
        _shutdown_queue(queue)


def test_restock_fifo_when_feasible_before_next_jump_deadline() -> None:
    clock = _ManualClock()
    queue, _cancelled_error = _make_queue(clock)
    active_started = threading.Event()
    release_active = threading.Event()
    execution_order: list[str] = []

    def active_jump() -> str:
        execution_order.append("active")
        active_started.set()
        assert release_active.wait(1.0)
        return "active"

    def make_restock(label: str, duration: float) -> Callable[[], str]:
        def run() -> str:
            execution_order.append(label)
            clock.advance(duration)
            return label

        return run

    def pending_jump() -> str:
        execution_order.append("jump")
        return "jump"

    try:
        _ = _submit_jump_plot(
            queue,
            slot_id="slot-active",
            deadline=clock.now() + 100.0,
            estimated_duration=30.0,
            run=active_jump,
        )
        assert active_started.wait(1.0)

        first_restock = _submit_restock(
            queue,
            slot_id="slot-r1",
            estimated_duration=5.0,
            run=make_restock("restock-1", 5.0),
        )
        second_restock = _submit_restock(
            queue,
            slot_id="slot-r2",
            estimated_duration=5.0,
            run=make_restock("restock-2", 5.0),
        )
        jump = _submit_jump_plot(
            queue,
            slot_id="slot-jump",
            deadline=clock.now() + 8.0,
            estimated_duration=30.0,
            run=pending_jump,
        )

        release_active.set()

        assert first_restock.done.wait(1.0)
        assert jump.done.wait(1.0)
        assert second_restock.done.wait(1.0)
        assert execution_order == ["active", "restock-1", "jump", "restock-2"]
    finally:
        _shutdown_queue(queue)


def test_cancel_waiting_block_removes_it_and_signals_completion() -> None:
    clock = _ManualClock()
    queue, cancelled_error = _make_queue(clock)
    active_started = threading.Event()
    release_active = threading.Event()
    waiting_cancel = threading.Event()
    execution_order: list[str] = []

    def active_jump() -> str:
        execution_order.append("active")
        active_started.set()
        assert release_active.wait(1.0)
        return "active"

    def cancelled_restock() -> str:
        execution_order.append("cancelled-restock")
        return "cancelled-restock"

    def survivor_jump() -> str:
        execution_order.append("survivor")
        return "survivor"

    try:
        _ = _submit_jump_plot(
            queue,
            slot_id="slot-active",
            deadline=clock.now() + 100.0,
            estimated_duration=30.0,
            run=active_jump,
        )
        assert active_started.wait(1.0)

        cancelled_handle = _submit_restock(
            queue,
            slot_id="slot-cancel",
            estimated_duration=10.0,
            run=cancelled_restock,
            cancel_event=waiting_cancel,
        )
        survivor = _submit_jump_plot(
            queue,
            slot_id="slot-survivor",
            deadline=clock.now() + 5.0,
            estimated_duration=30.0,
            run=survivor_jump,
        )

        waiting_cancel.set()
        release_active.set()

        assert cancelled_handle.done.wait(1.0)
        assert survivor.done.wait(1.0)
        assert execution_order == ["active", "survivor"]
        with pytest.raises(cancelled_error):
            _ = cancelled_handle.result(timeout=0.1)
    finally:
        _shutdown_queue(queue)


def test_active_failure_propagates_and_releases_next_waiter() -> None:
    clock = _ManualClock()
    queue, _cancelled_error = _make_queue(clock)
    failure_started = threading.Event()
    execution_order: list[str] = []

    def failing_jump() -> str:
        execution_order.append("boom")
        failure_started.set()
        raise RuntimeError("jump plot failed")

    def recovery_restock() -> str:
        execution_order.append("recovery")
        return "recovery"

    try:
        failing = _submit_jump_plot(
            queue,
            slot_id="slot-fail",
            deadline=clock.now() + 1.0,
            estimated_duration=30.0,
            run=failing_jump,
        )
        assert failure_started.wait(1.0)

        recovery = _submit_restock(
            queue,
            slot_id="slot-next",
            estimated_duration=10.0,
            run=recovery_restock,
        )

        assert failing.done.wait(1.0)
        with pytest.raises(RuntimeError, match="jump plot failed"):
            _ = failing.result(timeout=0.1)

        assert recovery.done.wait(1.0)
        assert recovery.result(timeout=0.1) == "recovery"
        assert execution_order == ["boom", "recovery"]
    finally:
        _shutdown_queue(queue)


def test_shutdown_cancels_waiting_handle_and_sets_cancel_event() -> None:
    clock = _ManualClock()
    queue, cancelled_error = _make_queue(clock)
    active_started = threading.Event()
    release_active = threading.Event()
    waiting_cancel = threading.Event()

    def active_jump() -> str:
        active_started.set()
        assert release_active.wait(1.0)
        return "active"

    def cancelled_restock() -> str:
        return "cancelled-restock"

    try:
        _ = _submit_jump_plot(
            queue,
            slot_id="slot-active",
            deadline=clock.now() + 100.0,
            estimated_duration=30.0,
            run=active_jump,
        )
        assert active_started.wait(1.0)

        cancelled_handle = _submit_restock(
            queue,
            slot_id="slot-cancel",
            estimated_duration=10.0,
            run=cancelled_restock,
            cancel_event=waiting_cancel,
        )

        release_active.set()
        _shutdown_queue(queue)

        assert cancelled_handle.done.wait(1.0)
        assert waiting_cancel.is_set() is True
        with pytest.raises(cancelled_error):
            _ = cancelled_handle.result(timeout=0.1)
    finally:
        _shutdown_queue(queue)


def test_deadline_registered_lookahead_defers_restock_until_cleanup() -> None:
    clock = _ManualClock()
    queue, _cancelled_error = _make_queue(clock)
    started = threading.Event()

    register_deadline = getattr(queue, "register_jump_deadline", None)
    clear_deadline = getattr(queue, "clear_jump_deadline", None)
    if not callable(register_deadline) or not callable(clear_deadline):
        pytest.fail(
            "SequenceQueue must expose register_jump_deadline(slot_id, deadline) "
            + "and clear_jump_deadline(slot_id) for deadline look-ahead scheduling."
        )

    def restock() -> str:
        started.set()
        return "restock"

    try:
        _ = register_deadline(slot_id="slot-jump", deadline=clock.now() + 50.0)
        handle = _submit_restock(
            queue,
            slot_id="slot-restock",
            estimated_duration=60.0,
            run=restock,
        )

        assert started.wait(0.05) is False
        _ = clear_deadline(slot_id="slot-jump")

        assert handle.done.wait(1.0)
        assert handle.result(timeout=0.1) == "restock"
    finally:
        _shutdown_queue(queue)


def test_deadline_cleanup_reopens_restock_feasibility() -> None:
    clock = _ManualClock()
    queue, _cancelled_error = _make_queue(clock)

    can_start_restock = getattr(queue, "can_start_restock", None)
    register_deadline = getattr(queue, "register_jump_deadline", None)
    clear_deadline = getattr(queue, "clear_jump_deadline", None)
    if (
        not callable(can_start_restock)
        or not callable(register_deadline)
        or not callable(clear_deadline)
    ):
        pytest.fail(
            "SequenceQueue must expose can_start_restock(), register_jump_deadline(), "
            + "and clear_jump_deadline() for deadline cleanup checks."
        )

    try:
        _ = register_deadline(slot_id="slot-a", deadline=clock.now() + 30.0)
        assert can_start_restock(estimated_duration=60.0) is False

        _ = clear_deadline(slot_id="slot-a")
        assert can_start_restock(estimated_duration=60.0) is True
    finally:
        _shutdown_queue(queue)


def test_stale_registered_deadline_does_not_wedge_restock() -> None:
    clock = _ManualClock(start=100.0)
    queue, _cancelled_error = _make_queue(clock)
    started = threading.Event()

    can_start_restock = getattr(queue, "can_start_restock", None)
    register_deadline = getattr(queue, "register_jump_deadline", None)
    if not callable(can_start_restock) or not callable(register_deadline):
        pytest.fail(
            "SequenceQueue must expose can_start_restock() and "
            + "register_jump_deadline() for stale deadline cleanup checks."
        )

    def restock() -> str:
        started.set()
        return "restock"

    try:
        _ = register_deadline(slot_id="slot-stale", deadline=clock.now())

        assert can_start_restock(estimated_duration=60.0) is True

        handle = _submit_restock(
            queue,
            slot_id="slot-restock",
            estimated_duration=60.0,
            run=restock,
        )

        assert handle.done.wait(1.0)
        assert started.is_set() is True
        assert handle.result(timeout=0.1) == "restock"
    finally:
        _shutdown_queue(queue)


def test_later_feasible_restock_runs_when_fifo_head_cannot_fit() -> None:
    clock = _ManualClock()
    queue, _cancelled_error = _make_queue(clock)
    execution_order: list[str] = []

    register_deadline = getattr(queue, "register_jump_deadline", None)
    clear_deadline = getattr(queue, "clear_jump_deadline", None)
    if not callable(register_deadline) or not callable(clear_deadline):
        pytest.fail(
            "SequenceQueue must expose register_jump_deadline(slot_id, deadline) "
            + "and clear_jump_deadline(slot_id) for FIFO restock checks."
        )

    def make_restock(label: str, duration: float) -> Callable[[], str]:
        def run() -> str:
            execution_order.append(label)
            clock.advance(duration)
            return label

        return run

    try:
        _ = register_deadline(slot_id="slot-jump", deadline=clock.now() + 8.0)

        blocked = _submit_restock(
            queue,
            slot_id="slot-blocked",
            estimated_duration=10.0,
            run=make_restock("restock-blocked", 10.0),
        )
        feasible = _submit_restock(
            queue,
            slot_id="slot-feasible",
            estimated_duration=5.0,
            run=make_restock("restock-feasible", 5.0),
        )

        assert feasible.done.wait(1.0)
        _ = clear_deadline(slot_id="slot-jump")

        assert blocked.done.wait(1.0)
        assert feasible.result(timeout=0.1) == "restock-feasible"
        assert blocked.result(timeout=0.1) == "restock-blocked"
        assert execution_order == ["restock-feasible", "restock-blocked"]
    finally:
        _shutdown_queue(queue)


def test_cancelling_blocked_restock_signals_completion_and_releases_later_work() -> None:
    clock = _ManualClock()
    queue, cancelled_error = _make_queue(clock)
    active_started = threading.Event()
    release_active = threading.Event()
    blocked_cancel = threading.Event()
    execution_order: list[str] = []

    register_deadline = getattr(queue, "register_jump_deadline", None)
    if not callable(register_deadline):
        pytest.fail(
            "SequenceQueue must expose register_jump_deadline(slot_id, deadline) "
            + "for blocked restock cancellation checks."
        )

    def active_jump() -> str:
        execution_order.append("active")
        active_started.set()
        assert release_active.wait(1.0)
        return "active"

    def blocked_restock() -> str:
        execution_order.append("restock-blocked")
        return "restock-blocked"

    def feasible_restock() -> str:
        execution_order.append("restock-feasible")
        return "restock-feasible"

    try:
        _ = _submit_jump_plot(
            queue,
            slot_id="slot-active",
            deadline=clock.now() + 100.0,
            estimated_duration=30.0,
            run=active_jump,
        )
        assert active_started.wait(1.0)

        _ = register_deadline(slot_id="slot-jump", deadline=clock.now() + 8.0)

        blocked = _submit_restock(
            queue,
            slot_id="slot-blocked",
            estimated_duration=10.0,
            run=blocked_restock,
            cancel_event=blocked_cancel,
        )
        feasible = _submit_restock(
            queue,
            slot_id="slot-feasible",
            estimated_duration=5.0,
            run=feasible_restock,
        )

        assert feasible.done.wait(0.05) is False

        blocked_cancel.set()
        release_active.set()

        assert blocked.done.wait(1.0)
        assert feasible.done.wait(1.0)
        assert feasible.result(timeout=0.1) == "restock-feasible"
        with pytest.raises(cancelled_error):
            _ = blocked.result(timeout=0.1)
        assert execution_order == ["active", "restock-feasible"]
    finally:
        _shutdown_queue(queue)


def test_concurrent_carrier_submissions_preserve_serialization() -> None:
    """Simultaneous submissions from multiple carriers must never overlap.

    Four blocks (alternating jump/restock from different slot IDs) are
    submitted concurrently. max_active must remain 1, proving the queue
    serializes all automation blocks regardless of carrier origin.
    """
    clock = _ManualClock()
    queue, _cancelled_error = _make_queue(clock)
    release = threading.Event()
    num_workers = 4
    started_events = [threading.Event() for _ in range(num_workers)]
    active = 0
    max_active = 0
    lock = threading.Lock()

    def make_work(label: str, started: threading.Event) -> Callable[[], str]:
        def run() -> str:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                started.set()
                assert release.wait(2.0)
                return label
            finally:
                with lock:
                    active -= 1

        return run

    handles: list[_SubmissionHandle[str]] = []
    try:
        for i in range(num_workers):
            if i % 2 == 0:
                h = _submit_jump_plot(
                    queue,
                    slot_id=f"carrier-{i}",
                    deadline=clock.now() + 100.0 + i * 10,
                    estimated_duration=30.0,
                    run=make_work(f"jump-{i}", started_events[i]),
                )
            else:
                h = _submit_restock(
                    queue,
                    slot_id=f"carrier-{i}",
                    estimated_duration=15.0,
                    run=make_work(f"restock-{i}", started_events[i]),
                )
            handles.append(h)

        first_started = False
        deadline_check = 1.0
        for _ in range(20):
            if any(ev.is_set() for ev in started_events):
                first_started = True
                break
            time.sleep(0.05)
        assert first_started

        num_started = sum(1 for ev in started_events if ev.is_set())
        assert num_started == 1

        release.set()

        for h in handles:
            assert h.done.wait(2.0)

        assert max_active == 1
    finally:
        _shutdown_queue(queue)


def test_stale_deadline_pruning_prevents_indefinite_restock_starvation() -> None:
    """Past-due registered deadlines must be pruned across cycles.

    Verifies that stale deadlines from earlier cycles are removed by the
    queue's internal pruning, preventing indefinite restock starvation.
    Also confirms that over-estimate durations (restock longer than the
    gap before the jump deadline) cannot register a past deadline that
    wedges future restocks.
    """
    clock = _ManualClock(start=0.0)
    queue, _cancelled_error = _make_queue(clock)

    can_start_restock = getattr(queue, "can_start_restock", None)
    register_deadline = getattr(queue, "register_jump_deadline", None)
    clear_deadline = getattr(queue, "clear_jump_deadline", None)
    if not callable(can_start_restock) or not callable(register_deadline):
        pytest.fail("SequenceQueue must expose can_start_restock and register_jump_deadline")

    try:
        register_deadline(slot_id="slot-1", deadline=clock.now() + 30.0)
        assert can_start_restock(estimated_duration=60.0) is False

        clock.advance(40.0)
        assert can_start_restock(estimated_duration=60.0) is True

        register_deadline(slot_id="slot-2", deadline=clock.now() + 20.0)
        assert can_start_restock(estimated_duration=60.0) is False

        clock.advance(30.0)
        assert can_start_restock(estimated_duration=60.0) is True

        register_deadline(slot_id="slot-3", deadline=clock.now() + 100.0)
        assert can_start_restock(estimated_duration=200.0) is False
        assert can_start_restock(estimated_duration=10.0) is True

        if callable(clear_deadline):
            clear_deadline(slot_id="slot-3")
        assert can_start_restock(estimated_duration=60.0) is True
    finally:
        _shutdown_queue(queue)
