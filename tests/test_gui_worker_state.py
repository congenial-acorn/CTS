"""Tests for the worker state-machine contract.

Covers: valid transitions, invalid transitions, retry from error,
one-slot failure isolation, global dependency failure, stop request
behaviour, and the WorkerStateMachine helper.
"""

from __future__ import annotations

import pytest

from TraversalSystem.gui.worker_state import (
    FailureKind,
    InvalidTransition,
    SlotFailure,
    WorkerState,
    WorkerStateMachine,
    can_retry_from_error,
    classify_failure,
    is_valid_transition,
    validate_transition,
)


# =====================================================================
# State enum coverage
# =====================================================================

class TestWorkerStateEnum:
    """All 10 plan states are defined."""

    def test_all_states_defined(self) -> None:
        expected = {
            "unbound",
            "needs_manual_binding",
            "ready",
            "starting",
            "running",
            "waiting",
            "error",
            "stopping",
            "stopped",
            "complete",
        }
        actual = {s.value for s in WorkerState}
        assert actual == expected


# =====================================================================
# Valid transitions
# =====================================================================

class TestValidTransitions:
    """Happy-path transition sequences from the plan."""

    @pytest.mark.parametrize(
        "from_state, to_state",
        [
            (WorkerState.UNBOUND, WorkerState.NEEDS_MANUAL_BINDING),
            (WorkerState.UNBOUND, WorkerState.READY),
            (WorkerState.NEEDS_MANUAL_BINDING, WorkerState.UNBOUND),
            (WorkerState.NEEDS_MANUAL_BINDING, WorkerState.READY),
            (WorkerState.READY, WorkerState.STARTING),
            (WorkerState.STARTING, WorkerState.RUNNING),
            (WorkerState.RUNNING, WorkerState.WAITING),
            (WorkerState.WAITING, WorkerState.RUNNING),
            (WorkerState.RUNNING, WorkerState.COMPLETE),
            (WorkerState.RUNNING, WorkerState.STOPPING),
            (WorkerState.STOPPING, WorkerState.STOPPED),
            (WorkerState.ERROR, WorkerState.READY),
            (WorkerState.STOPPED, WorkerState.READY),
            (WorkerState.COMPLETE, WorkerState.READY),
        ],
    )
    def test_single_valid_transition(
        self, from_state: WorkerState, to_state: WorkerState
    ) -> None:
        assert is_valid_transition(from_state, to_state) is True

    def test_full_happy_path(self) -> None:
        """ready -> starting -> running -> waiting -> running -> complete."""
        sm = WorkerStateMachine("slot-1", WorkerState.READY)
        sm.transition_to(WorkerState.STARTING)
        sm.transition_to(WorkerState.RUNNING)
        sm.transition_to(WorkerState.WAITING)
        sm.transition_to(WorkerState.RUNNING)
        sm.transition_to(WorkerState.COMPLETE)
        assert sm.state is WorkerState.COMPLETE

    def test_error_to_ready_retry(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.ERROR)
        sm.transition_to(WorkerState.READY)
        assert sm.state is WorkerState.READY

    def test_stop_from_running(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.RUNNING)
        sm.transition_to(WorkerState.STOPPING)
        sm.transition_to(WorkerState.STOPPED)
        assert sm.state is WorkerState.STOPPED

    def test_stop_from_waiting(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.WAITING)
        sm.transition_to(WorkerState.STOPPING)
        assert sm.state is WorkerState.STOPPING

    def test_stop_from_starting(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.STARTING)
        sm.transition_to(WorkerState.STOPPING)
        assert sm.state is WorkerState.STOPPING


# =====================================================================
# Invalid transitions
# =====================================================================

class TestInvalidTransitions:
    """Transitions that must be rejected."""

    def test_unbound_to_running_rejected(self) -> None:
        """Plan acceptance criterion: unbound -> running is invalid."""
        with pytest.raises(InvalidTransition) as exc_info:
            validate_transition(WorkerState.UNBOUND, WorkerState.RUNNING)
        assert exc_info.value.from_state is WorkerState.UNBOUND
        assert exc_info.value.to_state is WorkerState.RUNNING

    def test_unbound_to_starting_rejected(self) -> None:
        assert is_valid_transition(WorkerState.UNBOUND, WorkerState.STARTING) is False

    def test_stopped_to_running_rejected(self) -> None:
        assert is_valid_transition(WorkerState.STOPPED, WorkerState.RUNNING) is False

    def test_complete_to_running_rejected(self) -> None:
        assert is_valid_transition(WorkerState.COMPLETE, WorkerState.RUNNING) is False

    def test_error_to_running_rejected(self) -> None:
        assert is_valid_transition(WorkerState.ERROR, WorkerState.RUNNING) is False

    def test_ready_to_complete_rejected(self) -> None:
        assert is_valid_transition(WorkerState.READY, WorkerState.COMPLETE) is False

    @pytest.mark.parametrize(
        "from_state, to_state",
        [
            (WorkerState.RUNNING, WorkerState.READY),
            (WorkerState.WAITING, WorkerState.STARTING),
            (WorkerState.STOPPING, WorkerState.RUNNING),
            (WorkerState.COMPLETE, WorkerState.STARTING),
            (WorkerState.STOPPED, WorkerState.STARTING),
        ],
    )
    def test_various_invalid(
        self, from_state: WorkerState, to_state: WorkerState
    ) -> None:
        assert is_valid_transition(from_state, to_state) is False

    def test_state_machine_raises_on_invalid(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.UNBOUND)
        with pytest.raises(InvalidTransition):
            sm.transition_to(WorkerState.RUNNING)
        # State must not change
        assert sm.state is WorkerState.UNBOUND


# =====================================================================
# Retry from error
# =====================================================================

class TestRetryFromError:
    """Retry from error -> ready only when preconditions satisfied."""

    def test_retry_succeeds_when_preconditions_met(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.ERROR)
        result = sm.retry_from_error(binding_valid=True, config_valid=True)
        assert result is True
        assert sm.state is WorkerState.READY

    def test_retry_fails_when_binding_invalid(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.ERROR)
        result = sm.retry_from_error(binding_valid=False, config_valid=True)
        assert result is False
        assert sm.state is WorkerState.ERROR

    def test_retry_fails_when_config_invalid(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.ERROR)
        result = sm.retry_from_error(binding_valid=True, config_valid=False)
        assert result is False
        assert sm.state is WorkerState.ERROR

    def test_retry_fails_when_both_invalid(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.ERROR)
        result = sm.retry_from_error(binding_valid=False, config_valid=False)
        assert result is False
        assert sm.state is WorkerState.ERROR

    def test_retry_from_non_error_state_is_noop(self) -> None:
        sm = WorkerStateMachine("slot-1", WorkerState.READY)
        result = sm.retry_from_error(binding_valid=True, config_valid=True)
        assert result is False
        assert sm.state is WorkerState.READY

    def test_can_retry_from_error_standalone(self) -> None:
        assert can_retry_from_error(binding_valid=True, config_valid=True) is True
        assert can_retry_from_error(binding_valid=False, config_valid=True) is False
        assert can_retry_from_error(binding_valid=True, config_valid=False) is False


# =====================================================================
# One-slot failure isolation
# =====================================================================

class TestOneSlotFailure:
    """One slot failure does not affect other slots' state machines."""

    def test_one_slot_error_others_unaffected(self) -> None:
        sm1 = WorkerStateMachine("carrier-1", WorkerState.RUNNING)
        sm2 = WorkerStateMachine("carrier-2", WorkerState.RUNNING)

        # Simulate failure on slot 1
        sm1.transition_to(WorkerState.ERROR)
        assert sm1.state is WorkerState.ERROR
        assert sm2.state is WorkerState.RUNNING

        # Slot 2 continues to completion
        sm2.transition_to(WorkerState.COMPLETE)
        assert sm2.state is WorkerState.COMPLETE

    def test_two_independent_lifecycles(self) -> None:
        """Two slots run through full independent lifecycles."""
        sm1 = WorkerStateMachine("c-1", WorkerState.READY)
        sm2 = WorkerStateMachine("c-2", WorkerState.READY)

        sm1.transition_to(WorkerState.STARTING)
        sm2.transition_to(WorkerState.STARTING)

        sm1.transition_to(WorkerState.RUNNING)
        sm2.transition_to(WorkerState.RUNNING)

        sm1.transition_to(WorkerState.ERROR)
        sm2.transition_to(WorkerState.COMPLETE)

        assert sm1.state is WorkerState.ERROR
        assert sm2.state is WorkerState.COMPLETE


# =====================================================================
# Global dependency failure
# =====================================================================

class TestGlobalDependencyFailure:
    """Classification of slot-local vs global dependency failures."""

    def test_single_local_failure(self) -> None:
        failures = [
            SlotFailure("c-1", FailureKind.SLOT_LOCAL, "route error"),
        ]
        assert classify_failure(failures) is FailureKind.SLOT_LOCAL

    def test_multiple_local_failures_still_local(self) -> None:
        failures = [
            SlotFailure("c-1", FailureKind.SLOT_LOCAL, "route error"),
            SlotFailure("c-2", FailureKind.SLOT_LOCAL, "jump timeout"),
        ]
        assert classify_failure(failures) is FailureKind.SLOT_LOCAL

    def test_global_dependency_escalates(self) -> None:
        failures = [
            SlotFailure("c-1", FailureKind.SLOT_LOCAL, "route error"),
            SlotFailure("_global", FailureKind.GLOBAL_DEPENDENCY, "journal dir vanished"),
        ]
        assert classify_failure(failures) is FailureKind.GLOBAL_DEPENDENCY

    def test_empty_failures_default_to_local(self) -> None:
        assert classify_failure([]) is FailureKind.SLOT_LOCAL

    def test_slot_failure_repr(self) -> None:
        sf = SlotFailure("c-1", FailureKind.SLOT_LOCAL, "oops")
        assert "c-1" in repr(sf)
        assert "slot_local" in repr(sf)


# =====================================================================
# Stop request behaviour
# =====================================================================

class TestStopRequest:
    """request_stop() from active states."""

    def test_stop_from_running(self) -> None:
        sm = WorkerStateMachine("s1", WorkerState.RUNNING)
        assert sm.request_stop() is True
        assert sm.state is WorkerState.STOPPING

    def test_stop_from_starting(self) -> None:
        sm = WorkerStateMachine("s1", WorkerState.STARTING)
        assert sm.request_stop() is True
        assert sm.state is WorkerState.STOPPING

    def test_stop_from_waiting(self) -> None:
        sm = WorkerStateMachine("s1", WorkerState.WAITING)
        assert sm.request_stop() is True
        assert sm.state is WorkerState.STOPPING

    def test_stop_from_ready_rejected(self) -> None:
        sm = WorkerStateMachine("s1", WorkerState.READY)
        assert sm.request_stop() is False
        assert sm.state is WorkerState.READY

    def test_stop_from_error_rejected(self) -> None:
        sm = WorkerStateMachine("s1", WorkerState.ERROR)
        assert sm.request_stop() is False
        assert sm.state is WorkerState.ERROR

    def test_stop_from_stopped_rejected(self) -> None:
        sm = WorkerStateMachine("s1", WorkerState.STOPPED)
        assert sm.request_stop() is False
        assert sm.state is WorkerState.STOPPED

    def test_stop_from_complete_rejected(self) -> None:
        sm = WorkerStateMachine("s1", WorkerState.COMPLETE)
        assert sm.request_stop() is False
        assert sm.state is WorkerState.COMPLETE


# =====================================================================
# try_transition (non-throwing variant)
# =====================================================================

class TestTryTransition:
    """Non-throwing transition helper."""

    def test_valid_try_succeeds(self) -> None:
        sm = WorkerStateMachine("s1", WorkerState.READY)
        assert sm.try_transition(WorkerState.STARTING) is True
        assert sm.state is WorkerState.STARTING

    def test_invalid_try_fails(self) -> None:
        sm = WorkerStateMachine("s1", WorkerState.UNBOUND)
        assert sm.try_transition(WorkerState.RUNNING) is False
        assert sm.state is WorkerState.UNBOUND


# =====================================================================
# WorkerStateMachine basics
# =====================================================================

class TestWorkerStateMachineBasics:
    """Construction and property access."""

    def test_default_initial_state_is_unbound(self) -> None:
        sm = WorkerStateMachine("slot-x")
        assert sm.state is WorkerState.UNBOUND
        assert sm.slot_id == "slot-x"

    def test_custom_initial_state(self) -> None:
        sm = WorkerStateMachine("slot-y", WorkerState.READY)
        assert sm.state is WorkerState.READY

    def test_invalid_transition_does_not_mutate(self) -> None:
        sm = WorkerStateMachine("slot-z", WorkerState.RUNNING)
        with pytest.raises(InvalidTransition):
            sm.transition_to(WorkerState.READY)
        assert sm.state is WorkerState.RUNNING
