"""Worker state-machine contract for per-carrier automation slots.

Defines the formal state enum, allowed transitions, and validation logic
consumed by Qt workers (task 8) and the dashboard controller (task 10).

States (from plan / Metis review):
    unbound               – slot has no validated commander/FID binding
    needs_manual_binding  – FID known but window is ambiguous or missing
    ready                 – binding valid, config valid, slot can start
    starting              – worker thread initializing
    running               – automation actively executing
    waiting               – waiting for game event (jump cooldown, etc.)
    error                 – slot encountered a failure
    stopping              – graceful stop requested, winding down
    stopped               – worker has stopped (cancelled by user)
    complete              – route traversal finished successfully
"""

from __future__ import annotations

import enum
from typing import FrozenSet, Mapping, Sequence


class WorkerState(enum.Enum):
    """Formal state values for a carrier automation slot."""

    UNBOUND = "unbound"
    NEEDS_MANUAL_BINDING = "needs_manual_binding"
    READY = "ready"
    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    ERROR = "error"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETE = "complete"


class InvalidTransition(RuntimeError):
    """Raised when a state transition is not allowed."""

    def __init__(self, from_state: WorkerState, to_state: WorkerState) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid transition: {from_state.value} -> {to_state.value}"
        )


# ---------------------------------------------------------------------------
# Allowed transitions table
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: Mapping[WorkerState, FrozenSet[WorkerState]] = {
    WorkerState.UNBOUND: frozenset({
        WorkerState.NEEDS_MANUAL_BINDING,
        WorkerState.READY,          # when binding/config become valid
    }),
    WorkerState.NEEDS_MANUAL_BINDING: frozenset({
        WorkerState.UNBOUND,        # binding lost
        WorkerState.READY,          # manual bind succeeds
    }),
    WorkerState.READY: frozenset({
        WorkerState.STARTING,       # user hits start
        WorkerState.UNBOUND,        # binding lost while idle
        WorkerState.NEEDS_MANUAL_BINDING,  # window becomes ambiguous
        WorkerState.ERROR,          # config validation fails on retry
    }),
    WorkerState.STARTING: frozenset({
        WorkerState.RUNNING,        # worker initialized ok
        WorkerState.ERROR,          # startup failed
        WorkerState.STOPPING,       # cancel during init
    }),
    WorkerState.RUNNING: frozenset({
        WorkerState.WAITING,        # entering wait phase
        WorkerState.COMPLETE,       # route done
        WorkerState.ERROR,          # runtime failure
        WorkerState.STOPPING,       # user requested stop
    }),
    WorkerState.WAITING: frozenset({
        WorkerState.RUNNING,        # wait finished, resume
        WorkerState.ERROR,          # failure during wait
        WorkerState.STOPPING,       # user requested stop while waiting
    }),
    WorkerState.ERROR: frozenset({
        WorkerState.READY,          # retry after error (requires preconditions)
        WorkerState.STOPPED,        # user gives up / slot abandoned
        WorkerState.UNBOUND,        # binding lost after error
    }),
    WorkerState.STOPPING: frozenset({
        WorkerState.STOPPED,        # graceful stop completed
        WorkerState.ERROR,          # error during stop sequence
    }),
    WorkerState.STOPPED: frozenset({
        WorkerState.READY,          # restart after stop
        WorkerState.UNBOUND,        # binding lost while stopped
    }),
    WorkerState.COMPLETE: frozenset({
        WorkerState.READY,          # reset for new route
        WorkerState.UNBOUND,        # binding lost after completion
    }),
}


def is_valid_transition(from_state: WorkerState, to_state: WorkerState) -> bool:
    """Return True if *from_state -> to_state* is an allowed transition."""
    return to_state in _ALLOWED_TRANSITIONS.get(from_state, frozenset())


def validate_transition(from_state: WorkerState, to_state: WorkerState) -> None:
    """Raise :class:`InvalidTransition` if the transition is not allowed."""
    if not is_valid_transition(from_state, to_state):
        raise InvalidTransition(from_state, to_state)


# ---------------------------------------------------------------------------
# Retry precondition checks
# ---------------------------------------------------------------------------

def can_retry_from_error(
    *,
    binding_valid: bool,
    config_valid: bool,
) -> bool:
    """Return True only when both binding and config preconditions pass.

    Transitioning from ``error`` to ``ready`` requires that the underlying
    issue (binding loss, config problem) has been resolved.  This function
    encodes the preconditions so callers can gate the retry.
    """
    return binding_valid and config_valid


# ---------------------------------------------------------------------------
# Slot failure classification
# ---------------------------------------------------------------------------

class FailureKind(enum.Enum):
    """Classification of why a slot or global failure occurred."""

    SLOT_LOCAL = "slot_local"           # failure in one slot only
    GLOBAL_DEPENDENCY = "global_dependency"  # shared dependency broken


class SlotFailure:
    """Structured record of a slot failure for dashboard consumption."""

    __slots__ = ("slot_id", "kind", "message")

    def __init__(
        self, slot_id: str, kind: FailureKind, message: str
    ) -> None:
        self.slot_id = slot_id
        self.kind = kind
        self.message = message

    def __repr__(self) -> str:
        return (
            f"SlotFailure(slot_id={self.slot_id!r}, "
            f"kind={self.kind.value!r}, message={self.message!r})"
        )


def classify_failure(
    failures: Sequence[SlotFailure],
) -> FailureKind:
    """Classify whether failures are local or a global dependency issue.

    Returns :attr:`FailureKind.GLOBAL_DEPENDENCY` if any failure has that
    kind, otherwise :attr:`FailureKind.SLOT_LOCAL`.
    """
    for f in failures:
        if f.kind is FailureKind.GLOBAL_DEPENDENCY:
            return FailureKind.GLOBAL_DEPENDENCY
    return FailureKind.SLOT_LOCAL


# ---------------------------------------------------------------------------
# State machine tracker (lightweight, no Qt dependency)
# ---------------------------------------------------------------------------

class WorkerStateMachine:
    """Tracks the current state of a single carrier slot and enforces
    transition validity.  No threading or Qt coupling — pure logic that
    workers and controllers can import.
    """

    __slots__ = ("_state", "_slot_id")

    def __init__(self, slot_id: str, initial: WorkerState = WorkerState.UNBOUND) -> None:
        self._slot_id = slot_id
        self._state = initial

    @property
    def slot_id(self) -> str:
        return self._slot_id

    @property
    def state(self) -> WorkerState:
        return self._state

    def transition_to(self, target: WorkerState) -> None:
        """Attempt to transition to *target*.  Raises on invalid."""
        validate_transition(self._state, target)
        self._state = target

    def try_transition(self, target: WorkerState) -> bool:
        """Attempt transition; return True on success, False if invalid."""
        if is_valid_transition(self._state, target):
            self._state = target
            return True
        return False

    def retry_from_error(
        self,
        *,
        binding_valid: bool,
        config_valid: bool,
    ) -> bool:
        """Attempt error -> ready transition if preconditions met.

        Returns True if the retry succeeded (state is now ``ready``).
        Returns False if preconditions fail or current state isn't ``error``.
        """
        if self._state is not WorkerState.ERROR:
            return False
        if not can_retry_from_error(
            binding_valid=binding_valid, config_valid=config_valid
        ):
            return False
        self._state = WorkerState.READY
        return True

    def request_stop(self) -> bool:
        """Request stop from a running-like state.

        Valid from: ``starting``, ``running``, ``waiting``.
        Returns True if stop was accepted (state -> ``stopping``).
        """
        if self._state in (
            WorkerState.STARTING,
            WorkerState.RUNNING,
            WorkerState.WAITING,
        ):
            self._state = WorkerState.STOPPING
            return True
        return False
