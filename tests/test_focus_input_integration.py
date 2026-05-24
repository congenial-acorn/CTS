"""Tests for focus-aware input dispatch integration (Task 9).

Verifies:
- Every input primitive calls FocusGuard.ensure_focus() before dispatching
- FocusError prevents input dispatch and propagates to caller
- Only the affected slot's input path is stopped (isolation)
- The underlying input_handler remains testable via injected behavior
- execute_sequence helper guards each step with focus
- Focus guard is called exactly once per input primitive
"""
from __future__ import annotations

# pyright: reportAny=false, reportUnknownMemberType=false

from typing import Optional
from unittest.mock import MagicMock, call

import pytest

from TraversalSystem.focus_input_handler import (  # pyright: ignore[reportMissingImports]
    FocusAwareInputHandler,
    InputBackend,
)
from TraversalSystem.window_manager import (
    FocusError,
    FocusGuard,
    WindowBinding,
)
from TraversalSystem.gui.worker_state import (
    FailureKind,
    SlotFailure,
    WorkerState,
    WorkerStateMachine,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_binding(
    target_fid: str = "F-TEST",
    handle: int = 42,
) -> WindowBinding:
    return WindowBinding(
        target_fid=target_fid,
        startup_identity="cmdr:j1",
        handle=handle,
        pid=1000,
        title="Elite - Dangerous (CLIENT)",
        window_class="EliteDangerous",
        backend="x11",
    )


class RecordingInputBackend:
    """Records every input call for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def press(self, key: str) -> None:
        self.calls.append(("press", (key,), {}))

    def keyDown(self, key: str) -> None:
        self.calls.append(("keyDown", (key,), {}))

    def keyUp(self, key: str) -> None:
        self.calls.append(("keyUp", (key,), {}))

    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
    ) -> None:
        self.calls.append(("click", (x, y), {"button": button}))

    def moveTo(self, x: int, y: int) -> None:
        self.calls.append(("moveTo", (x, y), {}))

    def typewrite(self, text: str, interval: float = 0.0) -> None:
        self.calls.append(("typewrite", (text,), {"interval": interval}))


class StubFocusChecker:
    """Focus checker that records calls and optionally fails."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.call_count: int = 0
        self._should_fail = should_fail

    def ensure_focus(self) -> None:
        self.call_count += 1
        if self._should_fail:
            raise FocusError("Simulated focus failure")


def _make_handler(
    *,
    focus_fail: bool = False,
) -> tuple[FocusAwareInputHandler, StubFocusChecker, RecordingInputBackend]:
    checker = StubFocusChecker(should_fail=focus_fail)
    backend = RecordingInputBackend()
    handler = FocusAwareInputHandler(
        _make_binding(),
        focus_timeout_seconds=1.0,
        input_backend=backend,
        focus_guard_factory=lambda _b, _t: checker,
    )
    return handler, checker, backend


# ===========================================================================
# TEST GROUP 1: Focus-before-input verification
# ===========================================================================


class TestFocusBeforeInput:
    """Every automated input primitive must call ensure_focus first."""

    def test_press_calls_ensure_focus_before_dispatch(self) -> None:
        handler, checker, backend = _make_handler()
        handler.press("space")
        assert checker.call_count == 1
        assert backend.calls == [("press", ("space",), {})]

    def test_keyDown_calls_ensure_focus_before_dispatch(self) -> None:
        handler, checker, backend = _make_handler()
        handler.keyDown("ctrl")
        assert checker.call_count == 1
        assert backend.calls == [("keyDown", ("ctrl",), {})]

    def test_keyUp_calls_ensure_focus_before_dispatch(self) -> None:
        handler, checker, backend = _make_handler()
        handler.keyUp("ctrl")
        assert checker.call_count == 1
        assert backend.calls == [("keyUp", ("ctrl",), {})]

    def test_click_calls_ensure_focus_before_dispatch(self) -> None:
        handler, checker, backend = _make_handler()
        handler.click(100, 200, button="right")
        assert checker.call_count == 1
        assert backend.calls == [("click", (100, 200), {"button": "right"})]

    def test_moveTo_calls_ensure_focus_before_dispatch(self) -> None:
        handler, checker, backend = _make_handler()
        handler.moveTo(500, 300)
        assert checker.call_count == 1
        assert backend.calls == [("moveTo", (500, 300), {})]

    def test_typewrite_calls_ensure_focus_before_dispatch(self) -> None:
        handler, checker, backend = _make_handler()
        handler.typewrite("Sol", interval=0.05)
        assert checker.call_count == 1
        assert backend.calls == [("typewrite", ("Sol",), {"interval": 0.05})]

    def test_click_without_coordinates(self) -> None:
        handler, checker, backend = _make_handler()
        handler.click()
        assert checker.call_count == 1
        assert backend.calls == [("click", (None, None), {"button": "left"})]

    def test_multiple_inputs_each_check_focus(self) -> None:
        handler, checker, backend = _make_handler()
        handler.press("space")
        handler.moveTo(100, 200)
        handler.click(100, 200)
        assert checker.call_count == 3
        assert len(backend.calls) == 3


# ===========================================================================
# TEST GROUP 2: FocusError blocks input dispatch
# ===========================================================================


class TestFocusErrorBlocksInput:
    """FocusError prevents input dispatch and propagates to caller."""

    def test_focus_error_prevents_press(self) -> None:
        handler, checker, backend = _make_handler(focus_fail=True)
        with pytest.raises(FocusError, match="Simulated focus failure"):
            handler.press("space")
        assert backend.calls == []

    def test_focus_error_prevents_click(self) -> None:
        handler, checker, backend = _make_handler(focus_fail=True)
        with pytest.raises(FocusError, match="Simulated focus failure"):
            handler.click(100, 200)
        assert backend.calls == []

    def test_focus_error_prevents_moveTo(self) -> None:
        handler, checker, backend = _make_handler(focus_fail=True)
        with pytest.raises(FocusError, match="Simulated focus failure"):
            handler.moveTo(500, 300)
        assert backend.calls == []

    def test_focus_error_prevents_typewrite(self) -> None:
        handler, checker, backend = _make_handler(focus_fail=True)
        with pytest.raises(FocusError, match="Simulated focus failure"):
            handler.typewrite("Sol")
        assert backend.calls == []

    def test_focus_error_prevents_keyDown(self) -> None:
        handler, checker, backend = _make_handler(focus_fail=True)
        with pytest.raises(FocusError, match="Simulated focus failure"):
            handler.keyDown("ctrl")
        assert backend.calls == []

    def test_focus_error_prevents_keyUp(self) -> None:
        handler, checker, backend = _make_handler(focus_fail=True)
        with pytest.raises(FocusError, match="Simulated focus failure"):
            handler.keyUp("ctrl")
        assert backend.calls == []


# ===========================================================================
# TEST GROUP 3: Slot isolation — FocusError stops only affected slot
# ===========================================================================


class TestFocusErrorSlotIsolation:
    """FocusError from one slot must not affect another slot's handler."""

    def test_independent_handlers_isolate_failures(self) -> None:
        """Two handlers for different slots: one fails, other still works."""
        checker_a = StubFocusChecker(should_fail=True)
        checker_b = StubFocusChecker(should_fail=False)
        backend_a = RecordingInputBackend()
        backend_b = RecordingInputBackend()

        binding_a = _make_binding(target_fid="F-SLOT-A", handle=100)
        binding_b = _make_binding(target_fid="F-SLOT-B", handle=200)

        handler_a = FocusAwareInputHandler(
            binding_a,
            input_backend=backend_a,
            focus_guard_factory=lambda _b, _t: checker_a,
        )
        handler_b = FocusAwareInputHandler(
            binding_b,
            input_backend=backend_b,
            focus_guard_factory=lambda _b, _t: checker_b,
        )

        # Slot A fails
        with pytest.raises(FocusError):
            handler_a.press("space")
        assert backend_a.calls == []

        # Slot B still works fine
        handler_b.press("space")
        assert backend_b.calls == [("press", ("space",), {})]

    def test_focus_error_triggers_slot_error_state(self) -> None:
        """Simulates the contract: worker catches FocusError -> error state."""
        handler, _, _ = _make_handler(focus_fail=True)
        sm = WorkerStateMachine("slot-0", WorkerState.RUNNING)

        with pytest.raises(FocusError):
            handler.press("space")

        # Worker contract: transition to error on FocusError
        sm.transition_to(WorkerState.ERROR)
        assert sm.state == WorkerState.ERROR

    def test_focus_error_classified_as_slot_local(self) -> None:
        """FocusError is a slot-local failure, not a global dependency."""
        failure = SlotFailure(
            slot_id="slot-0",
            kind=FailureKind.SLOT_LOCAL,
            message="FocusError: window 42 not focusable",
        )
        from TraversalSystem.gui.worker_state import classify_failure
        result = classify_failure([failure])
        assert result == FailureKind.SLOT_LOCAL

    def test_other_slot_continues_after_peer_focus_failure(self) -> None:
        """When slot A hits FocusError, slot B's state machine is unaffected."""
        sm_a = WorkerStateMachine("slot-a", WorkerState.RUNNING)
        sm_b = WorkerStateMachine("slot-b", WorkerState.RUNNING)

        # Simulate focus failure on slot A
        try:
            raise FocusError("Window 42 lost focus")
        except FocusError:
            sm_a.transition_to(WorkerState.ERROR)

        # Slot B is still running
        assert sm_a.state == WorkerState.ERROR
        assert sm_b.state == WorkerState.RUNNING


# ===========================================================================
# TEST GROUP 4: Input handler remains testable (injection)
# ===========================================================================


class TestInputHandlerTestability:
    """The input layer remains testable via injected behavior."""

    def test_injected_backend_receives_all_calls(self) -> None:
        backend = RecordingInputBackend()
        checker = StubFocusChecker()
        handler = FocusAwareInputHandler(
            _make_binding(),
            input_backend=backend,
            focus_guard_factory=lambda _b, _t: checker,
        )

        handler.press("a")
        handler.keyDown("shift")
        handler.keyUp("shift")
        handler.moveTo(10, 20)
        handler.click(10, 20)
        handler.typewrite("hello", interval=0.01)

        assert len(backend.calls) == 6
        assert backend.calls[0] == ("press", ("a",), {})
        assert backend.calls[1] == ("keyDown", ("shift",), {})
        assert backend.calls[2] == ("keyUp", ("shift",), {})
        assert backend.calls[3] == ("moveTo", (10, 20), {})
        assert backend.calls[4] == ("click", (10, 20), {"button": "left"})
        assert backend.calls[5] == ("typewrite", ("hello",), {"interval": 0.01})

    def test_mock_backend_verifies_no_unwanted_input(self) -> None:
        """When focus fails, mock backend confirms zero dispatches."""
        backend = RecordingInputBackend()
        checker = StubFocusChecker(should_fail=True)
        handler = FocusAwareInputHandler(
            _make_binding(),
            input_backend=backend,
            focus_guard_factory=lambda _b, _t: checker,
        )

        with pytest.raises(FocusError):
            handler.press("space")

        assert len(backend.calls) == 0

    def test_injected_focus_guard_controls_behavior(self) -> None:
        """Injected focus guard controls whether input proceeds."""
        call_log: list[str] = []

        class SelectiveFocusChecker:
            def __init__(self) -> None:
                self._count = 0

            def ensure_focus(self) -> None:
                self._count += 1
                # Fail on third call
                if self._count == 3:
                    raise FocusError("Third time unlucky")

        checker = SelectiveFocusChecker()
        backend = RecordingInputBackend()
        handler = FocusAwareInputHandler(
            _make_binding(),
            input_backend=backend,
            focus_guard_factory=lambda _b, _t: checker,
        )

        handler.press("a")  # succeeds
        handler.press("b")  # succeeds
        with pytest.raises(FocusError):
            handler.press("c")  # fails

        # First two dispatched, third blocked
        assert len(backend.calls) == 2
        assert backend.calls[0] == ("press", ("a",), {})
        assert backend.calls[1] == ("press", ("b",), {})


# ===========================================================================
# TEST GROUP 5: execute_sequence helper
# ===========================================================================


class TestExecuteSequence:
    """execute_sequence guards each step with focus."""

    def test_sequence_executes_all_steps(self) -> None:
        handler, checker, backend = _make_handler()
        handler.execute_sequence([
            ("press", "space"),
            ("moveTo", (100, 200)),
            ("click", (100, 200)),
        ])
        assert checker.call_count == 3
        assert len(backend.calls) == 3

    def test_sequence_stops_on_focus_error(self) -> None:
        """When focus fails mid-sequence, remaining steps are skipped."""
        call_log: list[str] = []

        class TwoThenFailChecker:
            def __init__(self) -> None:
                self._count = 0

            def ensure_focus(self) -> None:
                self._count += 1
                if self._count > 2:
                    raise FocusError("Focus lost")

        backend = RecordingInputBackend()
        handler = FocusAwareInputHandler(
            _make_binding(),
            input_backend=backend,
            focus_guard_factory=lambda _b, _t: TwoThenFailChecker(),
        )

        with pytest.raises(FocusError):
            handler.execute_sequence([
                ("press", "a"),       # step 1: succeeds
                ("press", "b"),       # step 2: succeeds
                ("press", "c"),       # step 3: focus fails
                ("press", "d"),       # step 4: never reached
            ])

        # Only first 2 dispatched
        assert len(backend.calls) == 2

    def test_sequence_with_dict_args(self) -> None:
        handler, checker, backend = _make_handler()
        handler.execute_sequence([
            ("click", {"x": 100, "y": 200, "button": "right"}),
        ])
        assert checker.call_count == 1
        assert backend.calls == [("click", (100, 200), {"button": "right"})]

    def test_empty_sequence_is_noop(self) -> None:
        handler, checker, backend = _make_handler()
        handler.execute_sequence([])
        assert checker.call_count == 0
        assert backend.calls == []


# ===========================================================================
# TEST GROUP 6: Integration with FocusGuard and WindowBinding
# ===========================================================================


class TestFocusGuardIntegration:
    """Verify the handler correctly uses FocusGuard with binding."""

    def test_default_guard_factory_creates_focus_guard(self) -> None:
        """Without injection, handler creates a real FocusGuard."""
        binding = _make_binding()
        handler = FocusAwareInputHandler(binding, focus_timeout_seconds=2.5)
        # The internal focus_checker should be a FocusGuard instance
        assert isinstance(handler._focus_checker, FocusGuard)

    def test_handler_uses_binding_handle(self) -> None:
        """The binding's handle is what FocusGuard targets."""
        binding = _make_binding(handle=12345)
        handler = FocusAwareInputHandler(binding)
        guard = handler._focus_checker
        assert isinstance(guard, FocusGuard)
        # FocusGuard stores binding internally
        assert guard._binding.handle == 12345

    def test_focus_error_on_missing_window(self) -> None:
        """FocusGuard raises FocusError when window cannot be found."""
        from unittest.mock import patch

        binding = _make_binding(handle=99999)
        handler = FocusAwareInputHandler(
            binding,
            focus_timeout_seconds=0.05,
        )

        mock_backend = RecordingInputBackend()
        handler._backend = mock_backend

        with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
             patch("TraversalSystem.window_manager.IS_LINUX", True), \
             patch("subprocess.check_output", side_effect=FileNotFoundError("xdotool")):
            with pytest.raises(FocusError):
                handler.press("space")

        assert len(mock_backend.calls) == 0


# ===========================================================================
# TEST GROUP 7: Real FocusError with subprocess mock
# ===========================================================================


class TestFocusErrorWithSubprocessMock:
    """End-to-end: FocusError from real FocusGuard blocks input."""

    def test_real_focus_guard_failure_blocks_all_input(self) -> None:
        from unittest.mock import patch

        binding = _make_binding(handle=42)
        backend = RecordingInputBackend()

        def mock_check_output(cmd: list[str], **_kwargs: object) -> str:
            if cmd[0] == "xdotool" and cmd[1] == "getactivewindow":
                return "99999\n"  # wrong window
            if cmd[0] == "xdotool" and cmd[1] == "windowactivate":
                return ""
            raise FileNotFoundError(cmd[0])

        with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
             patch("TraversalSystem.window_manager.IS_LINUX", True), \
             patch("subprocess.check_output", side_effect=mock_check_output):
            handler = FocusAwareInputHandler(
                binding,
                focus_timeout_seconds=0.05,
                input_backend=backend,
            )

            with pytest.raises(FocusError, match="Failed to acquire"):
                handler.press("space")

            with pytest.raises(FocusError, match="Failed to acquire"):
                handler.click(100, 200)

        assert len(backend.calls) == 0

    def test_real_focus_guard_success_allows_input(self) -> None:
        from unittest.mock import patch

        binding = _make_binding(handle=42)
        backend = RecordingInputBackend()

        def mock_check_output(cmd: list[str], **_kwargs: object) -> str:
            if cmd[0] == "xdotool" and cmd[1] == "getactivewindow":
                return "42\n"  # matches binding handle
            if cmd[0] == "xdotool" and cmd[1] == "windowactivate":
                return ""
            raise FileNotFoundError(cmd[0])

        with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
             patch("TraversalSystem.window_manager.IS_LINUX", True), \
             patch("subprocess.check_output", side_effect=mock_check_output):
            handler = FocusAwareInputHandler(
                binding,
                focus_timeout_seconds=0.5,
                input_backend=backend,
            )

            handler.press("space")
            handler.click(100, 200)

        assert len(backend.calls) == 2
        assert backend.calls[0] == ("press", ("space",), {})
        assert backend.calls[1] == ("click", (100, 200), {"button": "left"})
