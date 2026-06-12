from __future__ import annotations

import importlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import final, cast

from PySide6.QtCore import QObject, QThread, Signal

from TraversalSystem.config import TraversalOptions
from TraversalSystem.focus_input_handler import FocusAwareInputHandler
from TraversalSystem.gui.binding_controller import (
    BindingSnapshot,
    classification_to_worker_state,
)
from TraversalSystem.gui.worker_state import FailureKind, SlotFailure, WorkerState, WorkerStateMachine
from TraversalSystem.gui.workers import (
    CarrierAutomationWorker,
    FailureClassifier,
    TraversalRunner,
    WorkerExecutionRequest,
)
from TraversalSystem.gui_config import CarrierSlotConfig, GuiConfig, UniversalSettings
from TraversalSystem.sequence_queue import SequenceQueue
from TraversalSystem.window_manager import WindowBinding


class GlobalDependencyError(RuntimeError):
    pass


JournalDependencyFactory = Callable[[UniversalSettings], object]
WindowDependencyFactory = Callable[[BindingSnapshot], object]
FocusDependencyFactory = Callable[[WindowBinding, UniversalSettings], object]
SequenceQueueDependencyFactory = Callable[[], object]


def default_journal_dependency_factory(universal: UniversalSettings) -> object:
    return Path(universal.journal_directory).expanduser()


def default_window_dependency_factory(snapshot: BindingSnapshot) -> object:
    if snapshot.window_binding is None:
        raise ValueError("Ready slot is missing a window binding.")
    return snapshot.window_binding


def default_focus_dependency_factory(
    binding: WindowBinding,
    universal: UniversalSettings,
) -> object:
    return FocusAwareInputHandler(
        binding,
        focus_timeout_seconds=float(universal.focus_timeout_seconds),
    )


def default_sequence_queue_dependency_factory() -> object:
    return SequenceQueue()


def _run_default_traversal(
    options: TraversalOptions,
    *,
    journal: object = None,
    window: object = None,
    focus: object = None,
    sequence_queue: object = None,
    cancel_event: threading.Event | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> bool:
    from TraversalSystem.runtime.controller import TraversalController

    main_module = importlib.import_module("TraversalSystem.main")
    traversal_slot = cast(Callable[[object], bool], getattr(main_module, "_run_traversal_slot"))
    return bool(
        TraversalController().run(
            traversal_slot,
            options,
            journal=journal,
            window=window,
            focus=focus,
            sequence_queue=sequence_queue,
            cancel_event=cancel_event,
            status_callback=status_callback,
        )
    )


@dataclass(slots=True)
class SlotRuntimeRecord:
    slot: CarrierSlotConfig
    binding: BindingSnapshot
    state_machine: WorkerStateMachine
    last_failure: SlotFailure | None = None
    thread: QThread | None = None
    worker: CarrierAutomationWorker | None = None
    cancel_event: threading.Event | None = None


@final
class WorkerController(QObject):

    slot_state_changed = Signal(int, str)
    slot_error = Signal(int, object)
    slot_log = Signal(int, str)
    slot_finished = Signal(int, bool)

    def __init__(
        self,
        *,
        traversal_runner: TraversalRunner = _run_default_traversal,
        journal_dependency_factory: JournalDependencyFactory = default_journal_dependency_factory,
        window_dependency_factory: WindowDependencyFactory = default_window_dependency_factory,
        focus_dependency_factory: FocusDependencyFactory = default_focus_dependency_factory,
        sequence_queue_dependency_factory: SequenceQueueDependencyFactory = default_sequence_queue_dependency_factory,
        failure_classifier: FailureClassifier | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._traversal_runner = traversal_runner
        self._journal_dependency_factory = journal_dependency_factory
        self._window_dependency_factory = window_dependency_factory
        self._focus_dependency_factory = focus_dependency_factory
        self._sequence_queue_dependency_factory = sequence_queue_dependency_factory
        self._failure_classifier = failure_classifier
        self._config: GuiConfig | None = None
        self._records: dict[int, SlotRuntimeRecord] = {}
        self._shared_journal_dependency: object | None = None
        self._shared_sequence_queue_dependency: object | None = None

    def sync_slots(
        self,
        config: GuiConfig,
        binding_snapshots: Mapping[int, BindingSnapshot],
    ) -> None:
        self._config = config
        self._shutdown_shared_sequence_queue(wait=True)
        self._shared_journal_dependency = None
        self._shared_sequence_queue_dependency = None
        next_records: dict[int, SlotRuntimeRecord] = {}
        for slot in config.carrier_slots:
            snapshot = binding_snapshots[slot.slot_index]
            worker_state = classification_to_worker_state(snapshot.classification)
            next_records[slot.slot_index] = SlotRuntimeRecord(
                slot=slot,
                binding=snapshot,
                state_machine=WorkerStateMachine(
                    self._slot_id(slot.slot_index),
                    worker_state,
                ),
            )
        self._records = next_records

    def start_slot(self, slot_index: int) -> bool:
        record = self._records[slot_index]
        if record.thread is not None:
            return False
        if record.state_machine.state is not WorkerState.READY:
            return False

        try:
            request = self._build_request(record)
        except GlobalDependencyError as exc:
            self._apply_failure(
                slot_index,
                SlotFailure(
                    self._slot_id(slot_index),
                    FailureKind.GLOBAL_DEPENDENCY,
                    str(exc) or exc.__class__.__name__,
                ),
            )
            self._stop_peers_for_global_failure(slot_index)
            return False
        except Exception as exc:
            self._apply_failure(
                slot_index,
                SlotFailure(
                    self._slot_id(slot_index),
                    FailureKind.SLOT_LOCAL,
                    str(exc) or exc.__class__.__name__,
                ),
            )
            return False

        if not record.state_machine.try_transition(WorkerState.STARTING):
            return False
        self.slot_state_changed.emit(slot_index, WorkerState.STARTING.value)

        thread = QThread(self)
        worker = CarrierAutomationWorker(
            request,
            traversal_runner=self._traversal_runner,
            failure_classifier=self._failure_classifier,
        )
        _ = worker.moveToThread(thread)
        def on_runtime_status(status: str, idx: int = slot_index) -> None:
            self._on_runtime_status(idx, status)

        def on_failure(failure: object, idx: int = slot_index) -> None:
            self._on_worker_failure(idx, failure)

        def on_log(message: str, idx: int = slot_index) -> None:
            self.slot_log.emit(idx, message)

        def on_finished(success: bool, idx: int = slot_index) -> None:
            self._on_worker_finished(idx, success)

        _ = thread.started.connect(worker.run)
        _ = worker.runtime_status.connect(on_runtime_status)
        _ = worker.failure.connect(on_failure)
        _ = worker.log.connect(on_log)
        _ = worker.finished.connect(on_finished)
        _ = worker.finished.connect(thread.quit)
        _ = worker.finished.connect(worker.deleteLater)
        _ = thread.finished.connect(thread.deleteLater)

        record.thread = thread
        record.worker = worker
        record.cancel_event = request.cancel_event
        thread.start()
        return True

    def start_all_ready(self) -> tuple[list[int], dict[int, str]]:
        started: list[int] = []
        skip_reasons: dict[int, str] = {}
        for slot_index in sorted(self._records):
            record = self._records[slot_index]
            if not record.slot.enabled:
                skip_reasons[slot_index] = "Disabled by user"
            elif record.state_machine.state is not WorkerState.READY:
                # Use binding classification for more descriptive reason
                from TraversalSystem.gui.binding_controller import SlotClassification
                cls = record.binding.classification
                if cls is SlotClassification.UNBOUND:
                    skip_reasons[slot_index] = "Slot is unbound"
                elif cls is SlotClassification.NEEDS_MANUAL_BINDING:
                    skip_reasons[slot_index] = "Needs manual binding"
                elif cls in (SlotClassification.STALE, SlotClassification.UNAVAILABLE):
                    skip_reasons[slot_index] = "Invalid configuration or unavailable"
                else:
                    skip_reasons[slot_index] = f"State is {record.state_machine.state.value}"
            elif self.start_slot(slot_index):
                started.append(slot_index)
            else:
                skip_reasons[slot_index] = "Failed to start"
        return started, skip_reasons

    def stop_slot(self, slot_index: int) -> bool:
        record = self._records[slot_index]
        cancel_event = record.cancel_event
        if cancel_event is None:
            return False
        if not record.state_machine.request_stop():
            return False
        cancel_event.set()
        self.slot_state_changed.emit(slot_index, WorkerState.STOPPING.value)
        return True

    def stop_all_active(self) -> list[int]:
        stopped: list[int] = []
        for slot_index in sorted(self._records):
            if self.stop_slot(slot_index):
                stopped.append(slot_index)
        return stopped

    def slot_state(self, slot_index: int) -> WorkerState:
        return self._records[slot_index].state_machine.state

    def slot_failure(self, slot_index: int) -> SlotFailure | None:
        return self._records[slot_index].last_failure

    def _build_request(self, record: SlotRuntimeRecord) -> WorkerExecutionRequest:
        config = self._config
        if config is None:
            raise RuntimeError("Slots have not been synchronized.")
        binding = record.binding.window_binding
        if binding is None:
            raise ValueError("Ready slot requires a resolved window binding.")
        journal_dependency = self._get_shared_journal_dependency(config.universal)
        sequence_queue_dependency = self._get_shared_sequence_queue_dependency()
        window_dependency = self._window_dependency_factory(record.binding)
        focus_dependency = self._focus_dependency_factory(binding, config.universal)
        return WorkerExecutionRequest(
            slot_id=self._slot_id(record.slot.slot_index),
            options=self._build_options(config.universal, record.slot),
            journal_dependency=journal_dependency,
            window_dependency=window_dependency,
            focus_dependency=focus_dependency,
            sequence_queue_dependency=sequence_queue_dependency,
            cancel_event=threading.Event(),
        )

    def _get_shared_journal_dependency(self, universal: UniversalSettings) -> object:
        if self._shared_journal_dependency is not None:
            return self._shared_journal_dependency
        dependency = self._journal_dependency_factory(universal)
        self._shared_journal_dependency = dependency
        return dependency

    def _on_runtime_status(self, slot_index: int, status: str) -> None:
        record = self._records[slot_index]
        target = {
            "starting": WorkerState.STARTING,
            "running": WorkerState.RUNNING,
            "waiting": WorkerState.WAITING,
            "error": WorkerState.ERROR,
            "complete": WorkerState.COMPLETE,
            "stopped": WorkerState.STOPPED,
        }.get(status)
        if target is None:
            return
        if target is WorkerState.STARTING:
            return
        if record.state_machine.state is target:
            return
        if record.state_machine.try_transition(target):
            self.slot_state_changed.emit(slot_index, target.value)

    def _on_worker_failure(self, slot_index: int, failure: object) -> None:
        if not isinstance(failure, SlotFailure):
            failure = SlotFailure(
                self._slot_id(slot_index),
                FailureKind.SLOT_LOCAL,
                str(failure),
            )
        self._apply_failure(slot_index, failure)
        if failure.kind is FailureKind.GLOBAL_DEPENDENCY:
            self._stop_peers_for_global_failure(slot_index)

    def _on_worker_finished(self, slot_index: int, success: bool) -> None:
        record = self._records[slot_index]
        state = record.state_machine.state
        if success and state in (WorkerState.STARTING, WorkerState.RUNNING, WorkerState.WAITING):
            if record.state_machine.try_transition(WorkerState.COMPLETE):
                self.slot_state_changed.emit(slot_index, WorkerState.COMPLETE.value)
        elif not success and state is WorkerState.STOPPING:
            if record.state_machine.try_transition(WorkerState.STOPPED):
                self.slot_state_changed.emit(slot_index, WorkerState.STOPPED.value)
        record.thread = None
        record.worker = None
        record.cancel_event = None
        if all(item.thread is None for item in self._records.values()):
            self._shutdown_shared_sequence_queue(wait=True)
            self._shared_sequence_queue_dependency = None
        self.slot_finished.emit(slot_index, success)

    def shutdown(self, *, wait: bool = True) -> None:
        _ = self.stop_all_active()
        self._shutdown_shared_sequence_queue(wait=wait)

    def _apply_failure(self, slot_index: int, failure: SlotFailure) -> None:
        record = self._records[slot_index]
        record.last_failure = failure
        state = record.state_machine.state
        if state is WorkerState.STOPPING:
            if record.state_machine.try_transition(WorkerState.ERROR):
                self.slot_state_changed.emit(slot_index, WorkerState.ERROR.value)
        elif state is not WorkerState.ERROR:
            if record.state_machine.try_transition(WorkerState.ERROR):
                self.slot_state_changed.emit(slot_index, WorkerState.ERROR.value)
        self.slot_error.emit(slot_index, failure)

    def _stop_peers_for_global_failure(self, failed_slot_index: int) -> None:
        for slot_index in sorted(self._records):
            if slot_index == failed_slot_index:
                continue
            _ = self.stop_slot(slot_index)

    def _get_shared_sequence_queue_dependency(self) -> object:
        if self._shared_sequence_queue_dependency is not None:
            return self._shared_sequence_queue_dependency
        dependency = self._sequence_queue_dependency_factory()
        self._shared_sequence_queue_dependency = dependency
        return dependency

    def _shutdown_shared_sequence_queue(self, *, wait: bool) -> None:
        dependency = self._shared_sequence_queue_dependency
        if dependency is None:
            return
        shutdown = getattr(dependency, "shutdown", None)
        if callable(shutdown):
            _ = shutdown(wait=wait)

    @staticmethod
    def _slot_id(slot_index: int) -> str:
        return f"slot-{slot_index}"

    @staticmethod
    def _build_options(
        universal: UniversalSettings,
        slot: CarrierSlotConfig,
    ) -> TraversalOptions:
        return TraversalOptions(
            webhook_url=universal.webhook_url,
            journal_directory=Path(universal.journal_directory).expanduser(),
            route_file=Path(slot.route_file).expanduser(),
            route_position=slot.route_position,
            tritium_slot=slot.tritium_slot,
            auto_plot_jumps=slot.auto_plot_jumps,
            disable_refuel=slot.disable_refuel,
            power_saving=universal.power_saving,
            refuel_mode=slot.refuel_mode,
            single_discord_message=universal.single_discord_message,
            shutdown_on_complete=universal.shutdown_on_complete,
            multi_commander_enabled=universal.multi_commander_enabled,
            target_fid=slot.fid,
            auto_detect_window=universal.auto_detect_window,
            focus_timeout_seconds=universal.focus_timeout_seconds,
            ambiguous_window_policy=universal.ambiguous_window_policy,
        )
