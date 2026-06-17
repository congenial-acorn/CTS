from __future__ import annotations

import importlib
import logging
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
from TraversalSystem.multi_journal_router import CTSJournalFacade, MultiJournalRouter
from TraversalSystem.platform_utils import system_shutdown
from TraversalSystem.sequence_queue import SequenceQueue
from TraversalSystem.traversal_journal import JournalScanLoop
from TraversalSystem.window_manager import WindowBinding

logger = logging.getLogger(__name__)


class GlobalDependencyError(RuntimeError):
    pass


JournalRuntimeFactory = Callable[[UniversalSettings], "JournalRuntime"]
WindowDependencyFactory = Callable[[BindingSnapshot], object]
FocusDependencyFactory = Callable[[WindowBinding, UniversalSettings], object]
SequenceQueueDependencyFactory = Callable[[], object]


class JournalRuntime:
    """Owns a shared ``MultiJournalRouter`` and ``JournalScanLoop`` for one
    journal directory.  Provides per-FID facades via ``facade_for(fid)``.
    """

    def __init__(
        self,
        journal_dir: Path,
        *,
        error_callback: Callable[[Exception], None] | None = None,
        router: MultiJournalRouter | None = None,
    ) -> None:
        self.router = router if router is not None else MultiJournalRouter()
        self._journal_dir: Path = journal_dir
        self._scan_loop: JournalScanLoop = JournalScanLoop(
            self.router,
            journal_dir,
            error_callback=error_callback,
        )
        self._started: bool = False

    def start(self) -> None:
        """Start the background scan loop (idempotent)."""
        if not self._started:
            self._scan_loop.start()
            self._started = True

    def facade_for(self, fid: str) -> CTSJournalFacade:
        """Return a per-FID read-only facade over the shared router."""
        return CTSJournalFacade(self.router, fid)

    def stop(self) -> None:
        """Signal the scan loop to stop."""
        self._scan_loop.stop()

    @property
    def scan_loop(self) -> JournalScanLoop:
        return self._scan_loop


def default_journal_runtime_factory(
    universal: UniversalSettings,
    *,
    error_callback: Callable[[Exception], None] | None = None,
) -> JournalRuntime:
    return JournalRuntime(
        Path(universal.journal_directory).expanduser(),
        error_callback=error_callback,
    )


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
    slot_id: int | None = None,
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
            slot_id=slot_id,
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
        journal_runtime_factory: JournalRuntimeFactory | None = None,
        window_dependency_factory: WindowDependencyFactory = default_window_dependency_factory,
        focus_dependency_factory: FocusDependencyFactory = default_focus_dependency_factory,
        sequence_queue_dependency_factory: SequenceQueueDependencyFactory = default_sequence_queue_dependency_factory,
        failure_classifier: FailureClassifier | None = None,
        router: MultiJournalRouter | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._traversal_runner = traversal_runner
        self._journal_runtime_factory: JournalRuntimeFactory = (
            journal_runtime_factory or default_journal_runtime_factory
        )
        self._window_dependency_factory = window_dependency_factory
        self._focus_dependency_factory = focus_dependency_factory
        self._sequence_queue_dependency_factory = sequence_queue_dependency_factory
        self._failure_classifier = failure_classifier
        self._config: GuiConfig | None = None
        self._records: dict[int, SlotRuntimeRecord] = {}
        self._journal_runtime: JournalRuntime | None = None
        self._shared_sequence_queue_dependency: object | None = None
        self._shared_router: MultiJournalRouter | None = router

    def sync_slots(
        self,
        config: GuiConfig,
        binding_snapshots: Mapping[int, BindingSnapshot],
    ) -> None:
        self._config = config
        has_active_records = any(
            record.thread is not None or record.cancel_event is not None
            for record in self._records.values()
        )
        if not has_active_records:
            self._shutdown_shared_sequence_queue(wait=True)
            self._stop_journal_runtime()
            self._shared_sequence_queue_dependency = None
        next_records: dict[int, SlotRuntimeRecord] = {}
        for slot in config.carrier_slots:
            snapshot = binding_snapshots[slot.slot_index]
            worker_state = classification_to_worker_state(snapshot.classification)
            existing = self._records.get(slot.slot_index)
            is_active = existing is not None and (
                existing.thread is not None or existing.cancel_event is not None
            )
            if is_active:
                assert existing is not None
                next_records[slot.slot_index] = SlotRuntimeRecord(
                    slot=slot,
                    binding=snapshot,
                    state_machine=existing.state_machine,
                    last_failure=existing.last_failure,
                    thread=existing.thread,
                    worker=existing.worker,
                    cancel_event=existing.cancel_event,
                )
                continue

            next_records[slot.slot_index] = SlotRuntimeRecord(
                slot=slot,
                binding=snapshot,
                state_machine=WorkerStateMachine(
                    self._slot_id(slot.slot_index),
                    worker_state,
                ),
                last_failure=existing.last_failure if existing is not None else None,
            )
        self._records = next_records

    def start_slot(self, slot_index: int) -> bool:
        """Start a single slot's worker (per-slot Start button).

        Unlike ``start_all_ready``, this path does NOT arm the first-cycle
        ordering barrier: strict slot-index jump ordering across a batch is only
        well-defined when the full set of starting slots is known up front, which
        a single manual click is not. Per-slot starts therefore dispatch in
        submission (click) order, which is the operator's explicit choice; use
        "Start All" for strict slot-index ordering (Bug D). We do clear any stale
        shared first-cycle base left by a prior batch so each manual start
        captures a fresh base and ordering stays deterministic by construction.
        """
        record = self._records[slot_index]
        if record.thread is not None:
            return False
        if record.state_machine.state is not WorkerState.READY:
            return False

        queue = self.peek_shared_sequence_queue()
        reset_base = getattr(queue, "reset_first_cycle_base", None)
        if callable(reset_base):
            reset_base()

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

        def on_finished(
            success: bool,
            idx: int = slot_index,
            finished_worker: CarrierAutomationWorker = worker,
            finished_thread: QThread = thread,
        ) -> None:
            self._on_worker_finished(idx, success, finished_worker, finished_thread)

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
        # Arm the shared queue's first-cycle ordering barrier for this batch so
        # the first jumps dispatch in strict slot-index order regardless of
        # per-worker submission timing or OS scheduling skew.
        ready_count = sum(
            1
            for record in self._records.values()
            if record.slot.enabled
            and record.state_machine.state is WorkerState.READY
        )
        self._arm_first_cycle_batch(ready_count)
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

    def peek_shared_sequence_queue(self) -> object | None:
        return self._shared_sequence_queue_dependency

    def _arm_first_cycle_batch(self, expected_count: int) -> None:
        """Prepare the shared queue's first-cycle ordering barrier for a start
        batch of *expected_count* slots.

        With two or more slots, force the shared queue into existence and arm
        the barrier so the first jumps dispatch in strict slot-index order. With
        fewer, ordering is moot, so do not force-create the queue (matches prior
        behavior); just reset any stale base on an existing queue.
        """
        if expected_count >= 2:
            queue = self._get_shared_sequence_queue_dependency()
            arm = getattr(queue, "arm_first_cycle_barrier", None)
            if callable(arm):
                arm(expected_count=expected_count)
                return
        queue = self.peek_shared_sequence_queue()
        reset_base = getattr(queue, "reset_first_cycle_base", None)
        if callable(reset_base):
            reset_base()

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
        runtime = self._get_shared_journal_runtime(config.universal)
        journal_facade = runtime.facade_for(record.slot.fid)
        sequence_queue_dependency = self._get_shared_sequence_queue_dependency()
        window_dependency = self._window_dependency_factory(record.binding)
        focus_dependency = self._focus_dependency_factory(binding, config.universal)
        return WorkerExecutionRequest(
            slot_id=self._slot_id(record.slot.slot_index),
            options=self._build_options(config.universal, record.slot),
            journal_dependency=journal_facade,
            window_dependency=window_dependency,
            focus_dependency=focus_dependency,
            sequence_queue_dependency=sequence_queue_dependency,
            cancel_event=threading.Event(),
        )

    def _get_shared_journal_runtime(self, universal: UniversalSettings) -> JournalRuntime:
        """Return (and lazily create) the shared JournalRuntime for the
        configured journal directory.  The scan loop is started on first
        access so that journal state is populated before traversal begins.
        """
        if self._journal_runtime is not None:
            return self._journal_runtime
        if self._shared_router is not None:
            runtime = JournalRuntime(
                Path(universal.journal_directory),
                router=self._shared_router,
            )
        else:
            runtime = self._journal_runtime_factory(universal)
        runtime.start()
        self._journal_runtime = runtime
        return runtime

    def _stop_journal_runtime(self) -> None:
        runtime = self._journal_runtime
        if runtime is not None:
            runtime.stop()
            self._journal_runtime = None

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

    def _on_worker_finished(
        self,
        slot_index: int,
        success: bool,
        worker: CarrierAutomationWorker,
        thread: QThread,
    ) -> None:
        record = self._records[slot_index]
        _ = thread
        if record.worker is not worker:
            return
        state = record.state_machine.state
        transitioned = False
        if success:
            if state is WorkerState.COMPLETE:
                record.thread = None
                record.worker = None
                record.cancel_event = None
                transitioned = True
            elif state in (WorkerState.STARTING, WorkerState.RUNNING, WorkerState.WAITING):
                if state in (WorkerState.STARTING, WorkerState.WAITING):
                    _ = record.state_machine.try_transition(WorkerState.RUNNING)
                if record.state_machine.try_transition(WorkerState.COMPLETE):
                    record.thread = None
                    record.worker = None
                    record.cancel_event = None
                    self.slot_state_changed.emit(slot_index, WorkerState.COMPLETE.value)
                    transitioned = True
        else:
            if state in (WorkerState.ERROR, WorkerState.STOPPED):
                record.thread = None
                record.worker = None
                record.cancel_event = None
                transitioned = True
            elif state is WorkerState.STOPPING:
                if record.state_machine.try_transition(WorkerState.STOPPED):
                    record.thread = None
                    record.worker = None
                    record.cancel_event = None
                    self.slot_state_changed.emit(slot_index, WorkerState.STOPPED.value)
                    transitioned = True
        if transitioned and all(item.thread is None for item in self._records.values()):
            self._stop_journal_runtime()
            self._shutdown_shared_sequence_queue(wait=True)
            self._shared_sequence_queue_dependency = None
            self._maybe_shutdown_system()
        self.slot_finished.emit(slot_index, success)

    def _maybe_shutdown_system(self) -> None:
        """Trigger system shutdown when all carriers completed successfully
        and the universal ``shutdown_on_complete`` setting is enabled."""
        if self._config is None:
            return
        if not self._config.universal.shutdown_on_complete:
            return
        all_complete = all(
            record.state_machine.state is WorkerState.COMPLETE
            for record in self._records.values()
        )
        if all_complete and self._records:
            logger.info("All carriers complete — initiating system shutdown.")
            system_shutdown(30)

    def shutdown(self, *, wait: bool = True) -> None:
        _ = self.stop_all_active()
        self._stop_journal_runtime()
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
            refuel_mode=slot.refuel_mode,
            single_discord_message=slot.single_discord_message,
            shutdown_on_complete=False,
            multi_commander_enabled=universal.multi_commander_enabled,
            target_fid=slot.fid,
            auto_detect_window=universal.auto_detect_window,
            focus_timeout_seconds=universal.focus_timeout_seconds,
            ambiguous_window_policy=universal.ambiguous_window_policy,
        )
