from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QApplication

from TraversalSystem.config import TraversalOptions
from TraversalSystem.gui.binding_controller import BindingSnapshot, SlotClassification
from TraversalSystem.gui.worker_controller import SlotRuntimeRecord, WorkerController
from TraversalSystem.gui.worker_state import FailureKind, SlotFailure, WorkerState
from TraversalSystem.gui_config import CarrierSlotConfig, GuiConfig, UniversalSettings
from TraversalSystem.runtime.controller import StatusCallback
from TraversalSystem.sequence_queue import CancelledBlockError, SequenceQueue
from TraversalSystem.window_manager import WindowBinding, WindowInfo
from typing import cast


_ = os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _wait_until(
    app: QApplication,
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    assert predicate()


def _wait_for_controller_idle(
    app: QApplication,
    controller: WorkerController,
    slot_indices: list[int],
) -> None:
    records = cast(dict[int, SlotRuntimeRecord], getattr(controller, "_records"))
    _wait_until(
        app,
        lambda: all(records[idx].thread is None for idx in slot_indices),
    )


def _shared_sequence_queue(controller: WorkerController) -> SequenceQueue | None:
    return cast(SequenceQueue | None, getattr(controller, "_shared_sequence_queue_dependency"))


def _queue_worker_is_alive(sequence_queue: SequenceQueue) -> bool:
    worker = cast(threading.Thread, getattr(sequence_queue, "_worker"))
    return worker.is_alive()


def _window_info(handle: int) -> WindowInfo:
    return WindowInfo(
        handle=handle,
        pid=handle,
        title=f"Elite Dangerous {handle}",
        window_class="elite",
        backend="x11",
        focusable=True,
    )


def _binding_snapshot(slot: CarrierSlotConfig) -> BindingSnapshot:
    window = _window_info(slot.slot_index + 100)
    return BindingSnapshot(
        classification=SlotClassification.READY,
        fid=slot.fid,
        commander_name=slot.commander_name,
        window_binding=WindowBinding.from_window(
            target_fid=slot.fid,
            startup_identity=f"slot:{slot.slot_index}",
            window=window,
        ),
        discovered_commander=None,
        candidate_windows=[window],
    )


def _config(tmp_path: Path, count: int) -> tuple[GuiConfig, dict[int, BindingSnapshot]]:
    route_file = tmp_path / "route.txt"
    _ = route_file.write_text("Sol\n", encoding="utf-8")
    slots = [
        CarrierSlotConfig(
            slot_index=index,
            fid=f"FID-{index}",
            commander_name=f"Cmdr {index}",
            route_file=str(route_file),
            state="ready",
        )
        for index in range(count)
    ]
    config = GuiConfig(
        universal=UniversalSettings(
            webhook_url="https://example.invalid/hook",
            journal_directory=str(tmp_path / "journals"),
            multi_commander_enabled=True,
            focus_timeout_seconds=7,
            single_discord_message=True,
            shutdown_on_complete=False,
        ),
        carrier_slots=slots,
    )
    bindings = {slot.slot_index: _binding_snapshot(slot) for slot in slots}
    return config, bindings


def test_single_slot_success_emits_status_and_injected_dependencies(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    _ = qapp
    config, bindings = _config(tmp_path, 1)
    seen: dict[str, object] = {}
    states: list[tuple[int, str]] = []
    logs: list[tuple[int, str]] = []
    finished: list[tuple[int, bool]] = []

    def traversal_runner(
        options: TraversalOptions,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        cancel_event: threading.Event | None = None,
        status_callback: StatusCallback | None = None,
        slot_id: int | None = None,
    ) -> bool:
        _ = slot_id
        seen["options"] = options
        seen["journal"] = journal
        seen["window"] = window
        seen["focus"] = focus
        seen["sequence_queue"] = sequence_queue
        assert cancel_event is not None
        assert status_callback is not None
        status_callback("starting")
        status_callback("running")
        status_callback("waiting")
        status_callback("running")
        status_callback("complete")
        return True

    controller = WorkerController(
        traversal_runner=traversal_runner,
        journal_dependency_factory=lambda universal: {
            "journal_dir": universal.journal_directory,
        },
        focus_dependency_factory=lambda binding, universal: {
            "binding": binding,
            "timeout": universal.focus_timeout_seconds,
        },
    )
    controller.sync_slots(config, bindings)

    def on_state(slot_index: int, state: str) -> None:
        states.append((slot_index, state))

    def on_log(slot_index: int, message: str) -> None:
        logs.append((slot_index, message))

    def on_finished(slot_index: int, success: bool) -> None:
        finished.append((slot_index, success))

    _ = controller.slot_state_changed.connect(on_state)
    _ = controller.slot_log.connect(on_log)
    _ = controller.slot_finished.connect(on_finished)

    assert controller.start_slot(0) is True
    _wait_until(qapp, lambda: finished == [(0, True)])

    assert controller.slot_state(0) is WorkerState.COMPLETE
    assert states == [
        (0, "starting"),
        (0, "running"),
        (0, "waiting"),
        (0, "running"),
        (0, "complete"),
    ]
    assert any("Worker started" in message for _, message in logs)
    assert any("Worker completed" in message for _, message in logs)
    options = seen["options"]
    assert isinstance(options, TraversalOptions)
    assert options.target_fid == "FID-0"
    assert options.multi_commander_enabled is True
    assert seen["journal"] == {"journal_dir": config.universal.journal_directory}
    assert seen["window"] == bindings[0].window_binding
    assert seen["focus"] == {
        "binding": bindings[0].window_binding,
        "timeout": 7,
    }
    assert isinstance(seen["sequence_queue"], SequenceQueue)
    _wait_for_controller_idle(qapp, controller, [0])
    controller.shutdown(wait=True)


def test_start_all_ready_runs_slots_concurrently(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    _ = qapp
    config, bindings = _config(tmp_path, 2)
    started_events: dict[str, threading.Event] = {
        "slot-0": threading.Event(),
        "slot-1": threading.Event(),
    }
    release = threading.Event()
    active = 0
    max_active = 0
    active_lock = threading.Lock()
    cancel_ids: dict[str, int] = {}
    finished: list[tuple[int, bool]] = []

    def traversal_runner(
        options: TraversalOptions,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        cancel_event: threading.Event | None = None,
        status_callback: StatusCallback | None = None,
        slot_id: int | None = None,
    ) -> bool:
        nonlocal active, max_active
        _ = slot_id
        slot_name = f"slot-{options.target_fid.rsplit('-', 1)[-1]}"
        _ = journal
        _ = window
        _ = focus
        _ = sequence_queue
        assert cancel_event is not None
        assert status_callback is not None
        cancel_ids[slot_name] = id(cancel_event)
        status_callback("running")
        started_events[slot_name].set()
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        _ = release.wait(2.0)
        with active_lock:
            active -= 1
        status_callback("complete")
        return True

    controller = WorkerController(traversal_runner=traversal_runner)
    controller.sync_slots(config, bindings)

    def on_finished(slot_index: int, success: bool) -> None:
        finished.append((slot_index, success))

    _ = controller.slot_finished.connect(on_finished)

    assert controller.start_all_ready()[0] == [0, 1]
    _wait_until(qapp, lambda: all(event.is_set() for event in started_events.values()))
    release.set()
    _wait_until(qapp, lambda: len(finished) == 2)

    assert controller.slot_state(0) is WorkerState.COMPLETE
    assert controller.slot_state(1) is WorkerState.COMPLETE
    assert max_active == 2
    assert cancel_ids["slot-0"] != cancel_ids["slot-1"]
    _wait_for_controller_idle(qapp, controller, [0, 1])
    controller.shutdown(wait=True)


def test_cancellation_is_independent_per_slot(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    _ = qapp
    config, bindings = _config(tmp_path, 2)
    running = {0: threading.Event(), 1: threading.Event()}
    allow_complete = threading.Event()
    finished: list[tuple[int, bool]] = []
    states: list[tuple[int, str]] = []

    def traversal_runner(
        options: TraversalOptions,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        cancel_event: threading.Event | None = None,
        status_callback: StatusCallback | None = None,
        slot_id: int | None = None,
    ) -> bool:
        _ = slot_id
        slot_index = int(options.target_fid.rsplit("-", 1)[-1])
        _ = journal
        _ = window
        _ = focus
        _ = sequence_queue
        assert cancel_event is not None
        assert status_callback is not None
        status_callback("running")
        running[slot_index].set()
        if slot_index == 0:
            while not cancel_event.is_set():
                time.sleep(0.01)
            status_callback("stopped")
            return False
        _ = allow_complete.wait(2.0)
        status_callback("complete")
        return True

    controller = WorkerController(traversal_runner=traversal_runner)
    controller.sync_slots(config, bindings)

    def on_state(slot_index: int, state: str) -> None:
        states.append((slot_index, state))

    def on_finished(slot_index: int, success: bool) -> None:
        finished.append((slot_index, success))

    _ = controller.slot_state_changed.connect(on_state)
    _ = controller.slot_finished.connect(on_finished)

    assert controller.start_all_ready()[0] == [0, 1]
    _wait_until(qapp, lambda: running[0].is_set() and running[1].is_set())
    assert controller.stop_slot(0) is True
    allow_complete.set()
    _wait_until(qapp, lambda: sorted(finished) == [(0, False), (1, True)])

    assert controller.slot_state(0) is WorkerState.STOPPED
    assert controller.slot_state(1) is WorkerState.COMPLETE
    assert (0, "stopping") in states
    assert (0, "stopped") in states
    _wait_for_controller_idle(qapp, controller, [0, 1])
    controller.shutdown(wait=True)


def test_one_worker_crash_is_isolated_to_that_slot(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    _ = qapp
    config, bindings = _config(tmp_path, 2)
    slot_zero_running = threading.Event()
    slot_one_release = threading.Event()
    errors: list[tuple[int, SlotFailure]] = []
    finished: list[tuple[int, bool]] = []

    def traversal_runner(
        options: TraversalOptions,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        cancel_event: threading.Event | None = None,
        status_callback: StatusCallback | None = None,
        slot_id: int | None = None,
    ) -> bool:
        _ = slot_id
        slot_index = int(options.target_fid.rsplit("-", 1)[-1])
        _ = journal
        _ = window
        _ = focus
        _ = sequence_queue
        _ = cancel_event
        assert status_callback is not None
        status_callback("running")
        if slot_index == 0:
            slot_zero_running.set()
            raise RuntimeError("boom")
        _ = slot_one_release.wait(2.0)
        status_callback("complete")
        return True

    controller = WorkerController(traversal_runner=traversal_runner)
    controller.sync_slots(config, bindings)

    def on_error(slot_index: int, failure: SlotFailure) -> None:
        errors.append((slot_index, failure))

    def on_finished(slot_index: int, success: bool) -> None:
        finished.append((slot_index, success))

    _ = controller.slot_error.connect(on_error)
    _ = controller.slot_finished.connect(on_finished)

    assert controller.start_all_ready()[0] == [0, 1]
    _wait_until(qapp, lambda: slot_zero_running.is_set() and len(errors) == 1)
    slot_one_release.set()
    _wait_until(qapp, lambda: sorted(finished) == [(0, False), (1, True)])

    assert controller.slot_state(0) is WorkerState.ERROR
    assert controller.slot_state(1) is WorkerState.COMPLETE
    assert errors[0][0] == 0
    assert errors[0][1].kind is FailureKind.SLOT_LOCAL
    assert errors[0][1].message == "boom"
    assert controller.slot_failure(0) == errors[0][1]
    _wait_for_controller_idle(qapp, controller, [0, 1])
    controller.shutdown(wait=True)


def test_shared_sequence_queue_serializes_concurrent_gui_slots(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    _ = qapp
    config, bindings = _config(tmp_path, 2)
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    finished: list[tuple[int, bool]] = []
    queue_ids: dict[str, int] = {}
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def traversal_runner(
        options: TraversalOptions,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        cancel_event: threading.Event | None = None,
        status_callback: StatusCallback | None = None,
        slot_id: int | None = None,
    ) -> bool:
        del journal, window, focus
        _ = slot_id
        assert isinstance(sequence_queue, SequenceQueue)
        assert cancel_event is not None
        assert status_callback is not None
        slot_index = int(options.target_fid.rsplit("-", 1)[-1])
        slot_name = f"slot-{slot_index}"
        queue_ids[slot_name] = id(sequence_queue)
        status_callback("running")

        def run_block() -> str:
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                if slot_index == 0:
                    first_started.set()
                    assert release_first.wait(1.0)
                else:
                    second_started.set()
                return slot_name
            finally:
                with active_lock:
                    active -= 1

        handle = sequence_queue.submit_jump_plot(
            slot_id=slot_name,
            run=run_block,
            deadline=10.0 + slot_index,
            estimated_duration=30.0,
            cancel_event=cancel_event,
        )
        assert handle.result(timeout=1.0) == slot_name
        status_callback("complete")
        return True

    controller = WorkerController(traversal_runner=traversal_runner)
    controller.sync_slots(config, bindings)

    def on_finished(slot_index: int, success: bool) -> None:
        finished.append((slot_index, success))

    _ = controller.slot_finished.connect(on_finished)

    assert controller.start_all_ready()[0] == [0, 1]
    _wait_until(qapp, lambda: first_started.is_set())
    assert second_started.wait(0.05) is False
    release_first.set()
    _wait_until(qapp, lambda: sorted(finished) == [(0, True), (1, True)])

    assert max_active == 1
    assert second_started.is_set() is True
    assert queue_ids["slot-0"] == queue_ids["slot-1"]
    _wait_for_controller_idle(qapp, controller, [0, 1])
    controller.shutdown(wait=True)


def test_shared_sequence_queue_shutdown_cancels_waiters(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    _ = qapp
    config, bindings = _config(tmp_path, 2)
    active_started = threading.Event()
    finished: list[tuple[int, bool]] = []
    failures: list[tuple[int, SlotFailure]] = []

    def traversal_runner(
        options: TraversalOptions,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        cancel_event: threading.Event | None = None,
        status_callback: StatusCallback | None = None,
        slot_id: int | None = None,
    ) -> bool:
        del journal, window, focus
        _ = slot_id
        assert isinstance(sequence_queue, SequenceQueue)
        assert cancel_event is not None
        assert status_callback is not None
        slot_index = int(options.target_fid.rsplit("-", 1)[-1])
        slot_name = f"slot-{slot_index}"
        status_callback("running")

        def run_block() -> str:
            if slot_index == 0:
                active_started.set()
                while not cancel_event.is_set():
                    time.sleep(0.01)
            return slot_name

        handle = sequence_queue.submit_jump_plot(
            slot_id=slot_name,
            run=run_block,
            deadline=1.0 + slot_index,
            estimated_duration=30.0,
            cancel_event=cancel_event,
        )
        try:
            _ = handle.result(timeout=1.5)
        except CancelledBlockError:
            status_callback("stopped")
            return False
        if cancel_event.is_set():
            status_callback("stopped")
            return False
        status_callback("complete")
        return True

    controller = WorkerController(traversal_runner=traversal_runner)
    controller.sync_slots(config, bindings)

    def on_finished(slot_index: int, success: bool) -> None:
        finished.append((slot_index, success))

    def on_error(slot_index: int, failure: SlotFailure) -> None:
        failures.append((slot_index, failure))

    _ = controller.slot_finished.connect(on_finished)
    _ = controller.slot_error.connect(on_error)

    assert controller.start_all_ready()[0] == [0, 1]
    _wait_until(qapp, lambda: active_started.is_set())
    assert controller.stop_all_active() == [0, 1]
    controller.shutdown(wait=True)
    _wait_until(qapp, lambda: sorted(finished) == [(0, False), (1, False)])

    assert controller.slot_state(0) is WorkerState.STOPPED
    assert controller.slot_state(1) is WorkerState.STOPPED
    assert failures == []
    _wait_for_controller_idle(qapp, controller, [0, 1])


def test_shared_sequence_queue_failure_and_release_isolated_per_slot(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    _ = qapp
    config, bindings = _config(tmp_path, 2)
    failure_started = threading.Event()
    finished: list[tuple[int, bool]] = []
    failures: list[tuple[int, SlotFailure]] = []
    execution_order: list[str] = []

    def traversal_runner(
        options: TraversalOptions,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        cancel_event: threading.Event | None = None,
        status_callback: StatusCallback | None = None,
        slot_id: int | None = None,
    ) -> bool:
        del journal, window, focus, slot_id
        assert isinstance(sequence_queue, SequenceQueue)
        assert cancel_event is not None
        assert status_callback is not None
        slot_index = int(options.target_fid.rsplit("-", 1)[-1])
        slot_name = f"slot-{slot_index}"
        status_callback("running")

        def run_block() -> str:
            if slot_index == 0:
                failure_started.set()
                execution_order.append("focus-failure")
                raise RuntimeError("focus/input failed")
            execution_order.append("slot-1-success")
            return slot_name

        handle = sequence_queue.submit_jump_plot(
            slot_id=slot_name,
            run=run_block,
            deadline=1.0 + slot_index,
            estimated_duration=30.0,
            cancel_event=cancel_event,
        )
        try:
            _ = handle.result(timeout=1.0)
        except RuntimeError:
            raise
        status_callback("complete")
        return True

    controller = WorkerController(traversal_runner=traversal_runner)
    controller.sync_slots(config, bindings)

    def on_finished(slot_index: int, success: bool) -> None:
        finished.append((slot_index, success))

    def on_error(slot_index: int, failure: SlotFailure) -> None:
        failures.append((slot_index, failure))

    _ = controller.slot_finished.connect(on_finished)
    _ = controller.slot_error.connect(on_error)

    assert controller.start_all_ready()[0] == [0, 1]
    _wait_until(qapp, lambda: failure_started.is_set())
    _wait_until(qapp, lambda: sorted(finished) == [(0, False), (1, True)])

    assert controller.slot_state(0) is WorkerState.ERROR
    assert controller.slot_state(1) is WorkerState.COMPLETE
    assert execution_order == ["focus-failure", "slot-1-success"]
    assert len(failures) == 1
    assert failures[0][0] == 0
    assert failures[0][1].slot_id == "slot-0"
    assert failures[0][1].kind is FailureKind.SLOT_LOCAL
    assert failures[0][1].message == "focus/input failed"
    _wait_for_controller_idle(qapp, controller, [0, 1])
    controller.shutdown(wait=True)


def test_controller_shutdown_cancels_waiting_and_active_queue_workers(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    _ = qapp
    config, bindings = _config(tmp_path, 2)
    active_started = threading.Event()
    finished: list[tuple[int, bool]] = []
    failures: list[tuple[int, SlotFailure]] = []

    def traversal_runner(
        options: TraversalOptions,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        cancel_event: threading.Event | None = None,
        status_callback: StatusCallback | None = None,
        slot_id: int | None = None,
    ) -> bool:
        del journal, window, focus, slot_id
        assert isinstance(sequence_queue, SequenceQueue)
        assert cancel_event is not None
        assert status_callback is not None
        slot_index = int(options.target_fid.rsplit("-", 1)[-1])
        slot_name = f"slot-{slot_index}"
        status_callback("running")

        def run_block() -> str:
            if slot_index == 0:
                active_started.set()
                while not cancel_event.is_set():
                    time.sleep(0.01)
            return slot_name

        handle = sequence_queue.submit_jump_plot(
            slot_id=slot_name,
            run=run_block,
            deadline=1.0 + slot_index,
            estimated_duration=30.0,
            cancel_event=cancel_event,
        )
        _ = handle.result(timeout=1.5)
        if cancel_event.is_set():
            status_callback("stopped")
            return False
        status_callback("complete")
        return True

    controller = WorkerController(traversal_runner=traversal_runner)
    controller.sync_slots(config, bindings)

    def on_finished(slot_index: int, success: bool) -> None:
        finished.append((slot_index, success))

    def on_error(slot_index: int, failure: SlotFailure) -> None:
        failures.append((slot_index, failure))

    _ = controller.slot_finished.connect(on_finished)
    _ = controller.slot_error.connect(on_error)

    assert controller.start_all_ready()[0] == [0, 1]
    _wait_until(qapp, lambda: active_started.is_set())

    sequence_queue = _shared_sequence_queue(controller)
    assert isinstance(sequence_queue, SequenceQueue)

    controller.shutdown(wait=True)
    _wait_until(qapp, lambda: sorted(finished) == [(0, False), (1, False)])
    _wait_for_controller_idle(qapp, controller, [0, 1])

    assert controller.slot_state(0) is WorkerState.STOPPED
    assert controller.slot_state(1) is WorkerState.STOPPED
    assert failures == []
    assert _queue_worker_is_alive(sequence_queue) is False
    _wait_for_controller_idle(qapp, controller, [0, 1])
