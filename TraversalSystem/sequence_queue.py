from __future__ import annotations

import threading
import time
from concurrent.futures import CancelledError, TimeoutError
from dataclasses import dataclass, field
from types import TracebackType
from typing import Callable, Generic, Literal, TypeVar, cast, final

T = TypeVar("T")
DEFAULT_RESTOCK_ESTIMATE_SECONDS = 60.0
DEFAULT_FIRST_CYCLE_BARRIER_TIMEOUT_SECONDS = 30.0
"""Safety valve for the first-cycle ordering barrier: if an expected sibling
never submits its jump block (e.g. its worker failed to start), dispatch the
blocks already pending once this many seconds elapse rather than stalling."""


class CancelledBlockError(CancelledError):
    pass


@dataclass(slots=True)
class SubmissionHandle(Generic[T]):
    slot_id: str
    deadline: float | None
    not_before: float | None
    estimated_duration: float
    cancel_event: threading.Event
    done: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _result: T | None = field(default=None, init=False, repr=False)
    _exception: BaseException | None = field(default=None, init=False, repr=False)
    _traceback: TracebackType | None = field(default=None, init=False, repr=False)
    _cancelled: bool = field(default=False, init=False, repr=False)

    def result(self, timeout: float | None = None) -> T:
        if not self.done.wait(timeout):
            raise TimeoutError()

        with self._lock:
            if self._cancelled:
                raise CancelledBlockError(f"GUI block cancelled for {self.slot_id}.")
            if self._exception is not None:
                exception = self._exception
                traceback = self._traceback
            else:
                return self._result  # pyright: ignore[reportReturnType]

        if traceback is not None:
            raise exception.with_traceback(traceback)
        raise exception

    def set_result(self, result: T) -> None:
        with self._lock:
            self._result = result
        self.done.set()

    def set_exception(
        self,
        exception: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        with self._lock:
            self._exception = exception
            self._traceback = traceback
        self.done.set()

    def cancel(self) -> None:
        with self._lock:
            self.cancel_event.set()
            self._cancelled = True
        self.done.set()


@dataclass(slots=True)
class _PendingBlock(Generic[T]):
    kind: Literal["jump_plot", "restock"]
    sequence: int
    handle: SubmissionHandle[T]
    run: Callable[[], T]


@final
class SequenceQueue:
    """A thread-safe sequence queue for serializing multi-carrier automation phases.

    While worker threads remain concurrent, automation blocks (jump/restock) are serialized
    at coarse boundaries. Retries stay outside queue blocks. Manual mode queues only
    preparation.
    """
    def __init__(self, *, time_fn: Callable[[], float] = time.monotonic) -> None:
        self._time_fn: Callable[[], float] = time_fn
        self._condition: threading.Condition = threading.Condition()
        self._pending: list[_PendingBlock[object]] = []
        self._active: _PendingBlock[object] | None = None
        self._registered_jump_deadlines: dict[str, float] = {}
        self._next_sequence: int = 0
        self._shutdown: bool = False
        # Shared base + barrier for deterministic first-cycle jump ordering; see
        # claim_first_cycle_deadline / arm_first_cycle_barrier.
        self._first_cycle_base: float | None = None
        self._first_cycle_expected: int = 0
        self._first_cycle_arrived: int = 0
        self._first_cycle_satisfied: bool = True
        self._first_cycle_barrier_deadline: float | None = None
        self._worker: threading.Thread = threading.Thread(
            target=self._worker_main,
            name="cts-sequence-queue",
            daemon=False,
        )
        self._worker.start()

    def submit_jump_plot(
        self,
        *,
        slot_id: str,
        run: Callable[[], T],
        deadline: float,
        estimated_duration: float,
        cancel_event: threading.Event | None = None,
    ) -> SubmissionHandle[T]:
        return self._submit(
            kind="jump_plot",
            slot_id=slot_id,
            run=run,
            deadline=deadline,
            not_before=None,
            estimated_duration=estimated_duration,
            cancel_event=cancel_event,
        )

    def submit_restock(
        self,
        *,
        slot_id: str,
        run: Callable[[], T],
        estimated_duration: float,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
        not_before: float | None = None,
    ) -> SubmissionHandle[T]:
        return self._submit(
            kind="restock",
            slot_id=slot_id,
            run=run,
            deadline=deadline,
            not_before=not_before,
            estimated_duration=estimated_duration,
            cancel_event=cancel_event,
        )

    def shutdown(self, wait: bool = True) -> None:
        with self._condition:
            if self._shutdown:
                already_stopped = True
            else:
                already_stopped = False
                self._shutdown = True
                self._cancel_all_pending_locked()
                self._condition.notify_all()
        if wait and not already_stopped:
            self._worker.join()

    def register_jump_deadline(self, *, slot_id: str, deadline: float) -> None:
        with self._condition:
            if self._shutdown:
                return
            self._registered_jump_deadlines[slot_id] = deadline
            self._condition.notify_all()

    def clear_jump_deadline(self, *, slot_id: str) -> None:
        with self._condition:
            removed = self._registered_jump_deadlines.pop(slot_id, None)
            if removed is not None:
                self._condition.notify_all()

    def claim_first_cycle_deadline(
        self, slot_index: int, *, offset_seconds: float,
    ) -> float:
        """Return a deterministic first-cycle jump deadline for *slot_index*.

        The base monotonic timestamp is captured once, atomically under the
        lock, on the first claim; every worker in the same start batch then
        shares that identical base, so only the per-slot offset varies. The
        offset alone does NOT guarantee ordering, because the worker thread
        would execute whichever block is submitted first while the queue is
        idle. Pair this with ``arm_first_cycle_barrier`` (called by the batch
        initiator before workers start): the barrier holds dispatch until every
        sibling jump block is pending, at which point the ``(deadline,
        sequence)`` sort in ``_select_next_locked`` resolves to strict
        slot-index order regardless of per-worker submission timing or OS
        scheduling skew.
        """
        with self._condition:
            if self._first_cycle_base is None:
                self._first_cycle_base = self._time_fn()
            return self._first_cycle_base + slot_index * offset_seconds

    def arm_first_cycle_barrier(
        self,
        *,
        expected_count: int,
        timeout_seconds: float = DEFAULT_FIRST_CYCLE_BARRIER_TIMEOUT_SECONDS,
    ) -> None:
        """Arm the first-cycle ordering barrier for a start batch.

        Holds all dispatch until *expected_count* ``jump_plot`` blocks are
        pending simultaneously (or *timeout_seconds* elapses), so unrelated
        work cannot bypass the batch while the deadline sort orders jumps by
        slot index. Resets the shared base so each batch re-captures it. Call
        once, before the workers begin claiming deadlines; pairs with
        ``claim_first_cycle_deadline``. With one or zero expected blocks
        ordering is moot, so the barrier opens immediately.
        """
        with self._condition:
            self._first_cycle_base = None
            self._first_cycle_arrived = 0
            self._first_cycle_expected = max(0, expected_count)
            self._first_cycle_satisfied = self._first_cycle_expected <= 1
            self._first_cycle_barrier_deadline = (
                None
                if self._first_cycle_satisfied
                else self._time_fn() + timeout_seconds
            )
            self._condition.notify_all()

    def reset_first_cycle_base(self) -> None:
        """Clear the shared first-cycle base so the next batch re-captures it.

        Does not arm the ordering barrier; use ``arm_first_cycle_barrier`` when
        two or more workers start concurrently and need strict slot ordering.
        """
        with self._condition:
            self._first_cycle_base = None

    def _first_cycle_barrier_open_locked(self) -> bool:
        """Return whether queued blocks may now be dispatched.

        Latches open once every expected sibling has arrived or the barrier
        timeout passes, so it never re-closes within a batch. Caller must hold
        ``self._condition``.
        """
        if self._first_cycle_satisfied:
            return True
        deadline = self._first_cycle_barrier_deadline
        if deadline is not None and self._time_fn() >= deadline:
            self._first_cycle_satisfied = True
            return True
        return False

    def can_start_restock(self, *, estimated_duration: float) -> bool:
        """Public inspection helper: check whether a restock of *estimated_duration* would finish before the earliest pending jump deadline; production scheduling still happens inside ``_select_next_locked``."""
        with self._condition:
            deadline = self._earliest_jump_deadline_locked()
            if deadline is None:
                return True
            return self._time_fn() + estimated_duration < deadline

    def _submit(
        self,
        *,
        kind: Literal["jump_plot", "restock"],
        slot_id: str,
        run: Callable[[], T],
        deadline: float | None,
        not_before: float | None,
        estimated_duration: float,
        cancel_event: threading.Event | None,
    ) -> SubmissionHandle[T]:
        handle = SubmissionHandle[T](
            slot_id=slot_id,
            deadline=deadline,
            not_before=not_before,
            estimated_duration=estimated_duration,
            cancel_event=cancel_event or threading.Event(),
        )
        if handle.cancel_event.is_set():
            handle.cancel()
            return handle

        with self._condition:
            if self._shutdown:
                raise RuntimeError("SequenceQueue is shut down.")
            if kind == "jump_plot":
                _ = self._registered_jump_deadlines.pop(slot_id, None)
                if not self._first_cycle_satisfied:
                    self._first_cycle_arrived += 1
                    if self._first_cycle_arrived >= self._first_cycle_expected:
                        self._first_cycle_satisfied = True
            block = _PendingBlock(
                kind=kind,
                sequence=self._next_sequence,
                handle=handle,
                run=run,
            )
            self._next_sequence += 1
            self._pending.append(cast(_PendingBlock[object], block))
            self._condition.notify_all()
        return handle

    def _worker_main(self) -> None:
        while True:
            block = self._next_block()
            if block is None:
                return

            handle = block.handle
            try:
                if handle.cancel_event.is_set():
                    handle.cancel()
                    continue
                result = block.run()
            except BaseException as exc:
                handle.set_exception(exc, exc.__traceback__)
            else:
                handle.set_result(result)
            finally:
                with self._condition:
                    self._active = None
                    self._condition.notify_all()

    def _next_block(self) -> _PendingBlock[object] | None:
        with self._condition:
            while True:
                self._prune_cancelled_pending_locked()
                if self._shutdown and self._active is None and not self._pending:
                    return None

                block = self._select_next_locked()
                if block is not None:
                    self._pending.remove(block)
                    self._active = block
                    return block

                _ = self._condition.wait(timeout=1.0)

    def _prune_cancelled_pending_locked(self) -> None:
        survivors: list[_PendingBlock[object]] = []
        for block in self._pending:
            if block.handle.cancel_event.is_set():
                block.handle.cancel()
            else:
                survivors.append(block)
        self._pending = survivors

    def _cancel_all_pending_locked(self) -> None:
        for block in self._pending:
            block.handle.cancel()
        self._pending.clear()

    def _select_next_locked(self) -> _PendingBlock[object] | None:
        if not self._pending:
            return None

        if not self._first_cycle_barrier_open_locked():
            return None

        jumps = sorted(
            (block for block in self._pending if block.kind == "jump_plot"),
            key=lambda block: (block.handle.deadline, block.sequence),
        )
        restocks = sorted(
            (block for block in self._pending if block.kind == "restock"),
            key=lambda block: block.sequence,
        )

        if jumps:
            earliest_jump = jumps[0]
            deadline = earliest_jump.handle.deadline
            if deadline is not None and deadline <= self._time_fn():
                return earliest_jump

            oldest_jump_sequence = min(jump.sequence for jump in jumps)
            older_restocks = [
                restock
                for restock in restocks
                if restock.sequence < oldest_jump_sequence
            ]
            next_restock = self._first_feasible_restock_locked(
                older_restocks,
                self._earliest_jump_deadline_locked(),
            )
            if next_restock is not None:
                return next_restock
            return earliest_jump

        return self._first_feasible_restock_locked(
            restocks,
            self._earliest_jump_deadline_locked(),
        )

    def _earliest_jump_deadline_locked(self) -> float | None:
        self._prune_stale_registered_deadlines_locked()
        now = self._time_fn()
        deadlines = [
            block.handle.deadline
            for block in self._pending
            if block.kind == "jump_plot"
            and block.handle.deadline is not None
            and block.handle.deadline > now
        ]
        deadlines.extend(self._registered_jump_deadlines.values())
        if not deadlines:
            return None
        return min(deadlines)

    def _first_feasible_restock_locked(
        self,
        restocks: list[_PendingBlock[object]],
        jump_deadline: float | None,
    ) -> _PendingBlock[object] | None:
        for restock in restocks:
            if self._restock_is_feasible(restock, jump_deadline):
                return restock
        return None

    def _prune_stale_registered_deadlines_locked(self) -> None:
        now = self._time_fn()
        stale_slot_ids = [
            slot_id
            for slot_id, deadline in self._registered_jump_deadlines.items()
            if deadline <= now
        ]
        for slot_id in stale_slot_ids:
            _ = self._registered_jump_deadlines.pop(slot_id, None)

    def _restock_is_feasible(
        self,
        restock: _PendingBlock[object],
        jump_deadline: float | None,
    ) -> bool:
        now = self._time_fn()
        if restock.handle.not_before is not None and now < restock.handle.not_before:
            return False
        if jump_deadline is None:
            return True
        return now + restock.handle.estimated_duration < jump_deadline
