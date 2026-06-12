from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import final

from PySide6.QtCore import QObject, Signal, Slot

from TraversalSystem.config import TraversalOptions
from TraversalSystem.gui.worker_state import FailureKind, SlotFailure


TraversalRunner = Callable[..., bool]
FailureClassifier = Callable[[BaseException], FailureKind]


@dataclass(slots=True)
class WorkerExecutionRequest:
    slot_id: str
    options: TraversalOptions
    journal_dependency: object
    window_dependency: object
    focus_dependency: object
    sequence_queue_dependency: object
    cancel_event: threading.Event


def default_failure_classifier(_error: BaseException) -> FailureKind:
    return FailureKind.SLOT_LOCAL


@final
class CarrierAutomationWorker(QObject):

    runtime_status = Signal(str)
    failure = Signal(object)
    log = Signal(str)
    finished = Signal(bool)

    def __init__(
        self,
        request: WorkerExecutionRequest,
        *,
        traversal_runner: TraversalRunner,
        failure_classifier: FailureClassifier | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._request = request
        self._traversal_runner = traversal_runner
        self._failure_classifier = failure_classifier or default_failure_classifier

    @Slot()
    def run(self) -> None:
        self.log.emit(f"Worker started for {self._request.slot_id}.")

        try:
            _slot_idx: int | None = None
            try:
                _slot_idx = int(self._request.slot_id.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                pass
            success = bool(
                self._traversal_runner(
                    self._request.options,
                    journal=self._request.journal_dependency,
                    window=self._request.window_dependency,
                    focus=self._request.focus_dependency,
                    sequence_queue=self._request.sequence_queue_dependency,
                    cancel_event=self._request.cancel_event,
                    status_callback=self.runtime_status.emit,
                    slot_id=_slot_idx,
                )
            )
        except BaseException as exc:
            if self._request.cancel_event.is_set():
                self.log.emit(
                    f"Worker stopped for {self._request.slot_id}: "
                    + (str(exc) or exc.__class__.__name__)
                )
                self.finished.emit(False)
                return
            failure = SlotFailure(
                self._request.slot_id,
                self._failure_classifier(exc),
                str(exc) or exc.__class__.__name__,
            )
            self.log.emit(
                f"Worker crashed for {self._request.slot_id}: {failure.message}"
            )
            self.failure.emit(failure)
            self.finished.emit(False)
            return

        if success:
            self.log.emit(f"Worker completed for {self._request.slot_id}.")
            self.finished.emit(True)
            return

        if self._request.cancel_event.is_set():
            self.log.emit(f"Worker stopped for {self._request.slot_id}.")
            self.finished.emit(False)
            return

        failure = SlotFailure(
            self._request.slot_id,
            FailureKind.SLOT_LOCAL,
            "Traversal runner returned False.",
        )
        self.log.emit(f"Worker failed for {self._request.slot_id}: {failure.message}")
        self.failure.emit(failure)
        self.finished.emit(False)
