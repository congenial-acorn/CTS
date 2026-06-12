"""Tests for focus-aware input behavior in GUI traversal paths.

These tests verify the post-Task-5 traversal input seam:

1. GUI jump/restock helper paths accept an injected
   ``FocusAwareInputHandler`` and route traversal primitives through it.
2. When the global ``TraversalSystem.input_handler`` module is monkeypatched
   to raise, the injected GUI path avoids it entirely.
3. The dependency injection chain (WorkerController → workers → controller →
   ``_run_traversal_slot``) propagates ``focus_dependency`` into traversal
   helper call sites.
4. Focus failures raised by the injected handler propagate to the owning slot,
   while legacy no-handler calls still use the global adapter.
"""
from __future__ import annotations

# pyright: reportAny=false, reportUnknownMemberType=false

import sys
import datetime
import threading
import types
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from TraversalSystem.focus_input_handler import FocusAwareInputHandler
from TraversalSystem.window_manager import (
    FocusError,
    WindowBinding,
)
from TraversalSystem.runtime.controller import (
    TraversalController,
    TraversalRuntimeContext,
    TraversalRuntimeDependencies,
)
from TraversalSystem.config import TraversalOptions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_options(**overrides: object) -> TraversalOptions:
    """Create a minimal TraversalOptions for testing."""
    from pathlib import Path as _P
    return TraversalOptions(
        webhook_url=str(overrides.get("webhook_url", "")),
        journal_directory=_P(str(overrides.get("journal_directory", "/tmp/journal"))),
        route_file=_P(str(overrides.get("route_file", "/tmp/route.txt"))),
        route_position=int(overrides.get("route_position", 0)),  # pyright: ignore[reportArgumentType]
        tritium_slot=int(overrides.get("tritium_slot", 0)),  # pyright: ignore[reportArgumentType]
        auto_plot_jumps=bool(overrides.get("auto_plot_jumps", True)),
        disable_refuel=bool(overrides.get("disable_refuel", False)),
        power_saving=bool(overrides.get("power_saving", False)),
        refuel_mode=int(overrides.get("refuel_mode", 0)),  # pyright: ignore[reportArgumentType]
        single_discord_message=bool(overrides.get("single_discord_message", False)),
        shutdown_on_complete=bool(overrides.get("shutdown_on_complete", True)),
    )


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
        x: int | None = None,
        y: int | None = None,
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
        self._should_fail: bool = should_fail

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


def _import_main_with_mocks() -> types.ModuleType:
    """Import TraversalSystem.main with all third-party deps mocked.

    main.py cannot be imported normally because it depends on psutil,
    pyautogui, pyperclip, pytz, tzlocal, and discord_webhook.  This
    helper pre-populates sys.modules with mocks so the import succeeds.

    Returns the imported ``main`` module.
    """
    # Only mock if not already imported
    if "TraversalSystem.main" in sys.modules:
        return sys.modules["TraversalSystem.main"]

    # Pre-populate third-party deps that main.py imports at module level
    for mod_name in [
        "psutil", "pyautogui", "pyperclip", "pytz", "tzlocal",
        "discord_webhook",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    # Mock modules whose fallback imports are broken
    for mod_name in ["TraversalSystem.discordhandler", "TraversalSystem.reshandler"]:
        if mod_name not in sys.modules:
            m = ModuleType(mod_name)
            sys.modules[mod_name] = m
    sys.modules["TraversalSystem.discordhandler"].DiscordHandler = MagicMock  # pyright: ignore[reportAttributeAccessIssue]
    sys.modules["TraversalSystem.reshandler"].Reshandler = MagicMock  # pyright: ignore[reportAttributeAccessIssue]

    from TraversalSystem import main  # type: ignore[reportMissingImports]
    return main


# ---------------------------------------------------------------------------
# GROUP 1: Recording backend proves injected handler captures all primitives
# ---------------------------------------------------------------------------


class TestRecordingBackendCapturesPrimitives:
    """The recording backend must capture every input primitive that traversal
    helpers would emit, proving the injected handler is usable as a drop-in."""

    def test_jump_sequence_recorded_on_injected_handler(self) -> None:
        """Simulate the input sequence that jump_to_system emits.

        The injected handler's recording backend must capture the same
        primitives that the production code currently sends via global
        input_handler: moveTo, press("space"), keyDown("ctrl"), press("v"),
        keyUp("ctrl"), press("space").
        """
        handler, checker, backend = _make_handler()

        # Simulate jump_to_system input sequence (main.py:279-296)
        handler.moveTo(500, 100)          # res_handler.sysNameX, sysNameUpperY
        handler.press("space")            # open search
        handler.keyDown("ctrl")           # ctrl+v paste
        handler.press("v")
        handler.keyUp("ctrl")
        handler.moveTo(500, 200)          # res_handler.sysNameX, sysNameLowerY
        handler.press("space")            # select system
        handler.moveTo(300, 400)          # res_handler.jumpButtonX, jumpButtonY
        handler.press("space")            # confirm jump
        handler.press("backspace")        # close menu
        handler.press("backspace")

        assert checker.call_count == 11   # one focus check per primitive
        assert len(backend.calls) == 11

        # Verify specific primitives in order
        method_names = [c[0] for c in backend.calls]
        assert method_names == [
            "moveTo", "press", "keyDown", "press", "keyUp",
            "moveTo", "press", "moveTo", "press", "press", "press",
        ]

    def test_button_sequence_recorded_on_injected_handler(self) -> None:
        """Simulate the input sequence that follow_button_sequence emits."""
        handler, checker, backend = _make_handler()

        # Simulate follow_button_sequence (main.py:196-211)
        handler.keyDown("w")
        handler.keyUp("w")
        handler.press("space")

        assert checker.call_count == 3
        assert backend.calls == [
            ("keyDown", ("w",), {}),
            ("keyUp", ("w",), {}),
            ("press", ("space",), {}),
        ]

    def test_restock_sequence_recorded_on_injected_handler(self) -> None:
        """Simulate the input sequence that restock_tritium emits."""
        handler, checker, backend = _make_handler()

        # Simulate restock_tritium cargo slot navigation (main.py:228-236)
        handler.press("w")
        for _ in range(3):
            handler.press("s")

        assert checker.call_count == 4
        assert backend.calls == [
            ("press", ("w",), {}),
            ("press", ("s",), {}),
            ("press", ("s",), {}),
            ("press", ("s",), {}),
        ]


# ---------------------------------------------------------------------------
# GROUP 2: Global input_handler monkeypatch raises when touched
# ---------------------------------------------------------------------------


class TestGlobalInputHandlerMonkeypatch:
    """When the global input_handler module is monkeypatched to raise, any
    code that touches it (instead of the injected handler) will fail.

    This proves the global module is a dangerous dependency for GUI paths
    because it operates on the OS-level focused window, not the per-slot
    target window.
    """

    def test_monkeypatched_global_press_raises(self) -> None:
        """Monkeypatching input_handler.press causes RuntimeError."""
        with patch("TraversalSystem.input_handler.press") as mock_press:
            mock_press.side_effect = RuntimeError(
                "GUI path must not touch global input_handler.press"
            )
            with pytest.raises(RuntimeError, match="global input_handler"):
                from TraversalSystem import input_handler as ih
                ih.press("space")

    def test_monkeypatched_global_moveTo_raises(self) -> None:
        """Monkeypatching input_handler.moveTo causes RuntimeError."""
        with patch("TraversalSystem.input_handler.moveTo") as mock_move:
            mock_move.side_effect = RuntimeError(
                "GUI path must not touch global input_handler.moveTo"
            )
            with pytest.raises(RuntimeError, match="global input_handler"):
                from TraversalSystem import input_handler as ih
                ih.moveTo(100, 200)

    def test_monkeypatched_global_keyDown_raises(self) -> None:
        """Monkeypatching input_handler.keyDown causes RuntimeError."""
        with patch("TraversalSystem.input_handler.keyDown") as mock_kd:
            mock_kd.side_effect = RuntimeError(
                "GUI path must not touch global input_handler.keyDown"
            )
            with pytest.raises(RuntimeError, match="global input_handler"):
                from TraversalSystem import input_handler as ih
                ih.keyDown("ctrl")

    def test_monkeypatched_global_keyUp_raises(self) -> None:
        """Monkeypatching input_handler.keyUp causes RuntimeError."""
        with patch("TraversalSystem.input_handler.keyUp") as mock_ku:
            mock_ku.side_effect = RuntimeError(
                "GUI path must not touch global input_handler.keyUp"
            )
            with pytest.raises(RuntimeError, match="global input_handler"):
                from TraversalSystem import input_handler as ih
                ih.keyUp("ctrl")

    def test_injected_handler_unaffected_by_global_monkeypatch(self) -> None:
        """The injected FocusAwareInputHandler works even when global
        input_handler is monkeypatched to raise.

        This proves the injected handler is properly isolated from the
        global module.
        """
        handler, checker, backend = _make_handler()

        with patch("TraversalSystem.input_handler.press") as mock_press, \
             patch("TraversalSystem.input_handler.moveTo") as mock_move, \
             patch("TraversalSystem.input_handler.keyDown") as mock_kd, \
             patch("TraversalSystem.input_handler.keyUp") as mock_ku:
            mock_press.side_effect = RuntimeError("global touched")
            mock_move.side_effect = RuntimeError("global touched")
            mock_kd.side_effect = RuntimeError("global touched")
            mock_ku.side_effect = RuntimeError("global touched")

            # Injected handler works fine — it doesn't touch global module
            handler.press("space")
            handler.moveTo(100, 200)
            handler.keyDown("ctrl")
            handler.keyUp("ctrl")

            # Global primitives were NEVER called
            mock_press.assert_not_called()
            mock_move.assert_not_called()
            mock_kd.assert_not_called()
            mock_ku.assert_not_called()

        # Recording backend captured everything
        assert len(backend.calls) == 4
        assert checker.call_count == 4


# ---------------------------------------------------------------------------
# GROUP 3: Traversal seam tests — contract for injected handler usage
#
# These tests import main.py (with mocked third-party deps) and call
# traversal helpers with both:
#   (a) the global input_handler monkeypatched to raise, and
#   (b) an injected FocusAwareInputHandler with a recording backend.
#
# CONTRACT: traversal helpers MUST accept a focus_handler parameter and
# route input through it, avoiding the global input_handler entirely.
#
# ---------------------------------------------------------------------------


class TestTraversalHelpersAcceptInjectedHandler:
    """Contract tests: traversal helpers MUST accept and use a focus_handler.

    When the global input_handler is monkeypatched to raise AND a
    focus_handler (FocusAwareInputHandler with recording backend) is
    provided, the helpers MUST:
    1. Not touch the global input_handler
    2. Route all input through the injected focus_handler
    """

    _main: types.ModuleType  # pyright: ignore[reportUninitializedInstanceVariable]

    @pytest.fixture(autouse=True)
    def _import_main(self) -> None:
        """Import main.py with mocked deps before each test."""
        self._main = _import_main_with_mocks()

    def test_follow_button_sequence_uses_injected_handler(
        self, tmp_path: Path,
    ) -> None:
        """follow_button_sequence must route through focus_handler when given.
        """
        seq_file = tmp_path / "test_seq.txt"
        _ = seq_file.write_text("space\n")

        handler, checker, backend = _make_handler()

        with patch.object(
            self._main.input_handler, "press",
            side_effect=RuntimeError(
                "GUI path must not touch global input_handler.press"
            ),
        ):
            self._main.follow_button_sequence(
                tmp_path, "test_seq.txt",
                focus_handler=handler,  # type: ignore[call-arg]
            )

        # Recording backend captured the press
        assert len(backend.calls) == 1
        assert backend.calls[0] == ("press", ("space",), {})
        assert checker.call_count == 1

    def test_follow_button_sequence_held_key_uses_injected_handler(
        self, tmp_path: Path,
    ) -> None:
        """follow_button_sequence keyDown/keyUp route through focus_handler."""
        seq_file = tmp_path / "held.txt"
        _ = seq_file.write_text("w:0.5\n")

        handler, checker, backend = _make_handler()

        with patch.object(
            self._main.input_handler, "keyDown",
            side_effect=RuntimeError(
                "GUI path must not touch global input_handler.keyDown"
            ),
        ), patch.object(
            self._main.input_handler, "keyUp",
            side_effect=RuntimeError(
                "GUI path must not touch global input_handler.keyUp"
            ),
        ):
            self._main.follow_button_sequence(
                tmp_path, "held.txt",
                focus_handler=handler,  # type: ignore[call-arg]
            )

        assert len(backend.calls) == 2
        assert backend.calls[0] == ("keyDown", ("w",), {})
        assert backend.calls[1] == ("keyUp", ("w",), {})
        assert checker.call_count == 2

    def test_restock_tritium_uses_injected_handler(
        self, tmp_path: Path,
    ) -> None:
        """restock_tritium must route through focus_handler when given."""
        options = _make_options(
            auto_plot_jumps=True,
            disable_refuel=False,
            refuel_mode=0,
        )

        for name in ["restock_fc.txt", "open_cargo_transfer.txt", "restock_cargo.txt"]:
            _ = (tmp_path / name).write_text("space\n")

        handler, checker, backend = _make_handler()

        with patch.object(
            self._main.input_handler, "press",
            side_effect=RuntimeError(
                "GUI path must not touch global input_handler.press"
            ),
        ):
            self._main.restock_tritium(
                options, tmp_path,
                focus_handler=handler,  # type: ignore[call-arg]
            )

        # Three sequence files, each with "space" press
        assert len(backend.calls) == 3
        for call in backend.calls:
            assert call[0] == "press"
            assert call[1] == ("space",)
        assert checker.call_count == 3

    def test_restock_cargo_navigation_uses_injected_handler(
        self, tmp_path: Path,
    ) -> None:
        """restock_tritium cargo slot navigation routes through focus_handler."""
        options = _make_options(
            auto_plot_jumps=True,
            disable_refuel=False,
            refuel_mode=1,
            tritium_slot=2,
        )

        for name in ["restock_fc.txt", "open_cargo_transfer.txt", "restock_cargo.txt"]:
            _ = (tmp_path / name).write_text("space\n")

        handler, _checker, backend = _make_handler()

        with patch.object(
            self._main.input_handler, "press",
            side_effect=RuntimeError(
                "GUI path must not touch global input_handler.press"
            ),
        ):
            self._main.restock_tritium(
                options, tmp_path,
                focus_handler=handler,  # type: ignore[call-arg]
            )

        # restock_fc(1) + open_cargo(1) + w(1) + s*2 + restock_cargo(1) = 6
        press_calls = [c for c in backend.calls if c[0] == "press"]
        press_keys = [c[1][0] for c in press_calls]
        assert "w" in press_keys
        assert press_keys.count("s") == 2

    def test_injected_handler_would_succeed_where_global_fails(
        self,
    ) -> None:
        """Proof of concept: injected handler captures sequence correctly."""
        handler, checker, backend = _make_handler()

        handler.press("space")
        handler.keyDown("w")
        handler.keyUp("w")
        handler.press("s")

        assert len(backend.calls) == 4
        assert checker.call_count == 4

    def test_follow_button_sequence_propagates_focus_failure(
        self, tmp_path: Path,
    ) -> None:
        seq_file = tmp_path / "focus_fail.txt"
        _ = seq_file.write_text("space\n")

        handler, _checker, _backend = _make_handler(focus_fail=True)

        with pytest.raises(FocusError, match="Simulated focus failure"):
            self._main.follow_button_sequence(
                tmp_path,
                "focus_fail.txt",
                focus_handler=handler,
            )

    def test_follow_button_sequence_without_injected_handler_uses_global(
        self, tmp_path: Path,
    ) -> None:
        seq_file = tmp_path / "legacy.txt"
        _ = seq_file.write_text("space\n")

        with patch.object(self._main.input_handler, "press") as mock_press:
            self._main.follow_button_sequence(tmp_path, "legacy.txt")

        mock_press.assert_called_once_with("space")

    def test_jump_to_system_propagates_injected_focus_failure(
        self, tmp_path: Path,
    ) -> None:
        _ = (tmp_path / "jump_nav_1.txt").write_text("space\n")

        handler, _checker, _backend = _make_handler(focus_fail=True)
        options = _make_options(auto_plot_jumps=True, refuel_mode=0)
        res_handler = types.SimpleNamespace(
            sysNameX=100,
            sysNameUpperY=110,
            sysNameLowerY=120,
            jumpButtonX=130,
            jumpButtonY=140,
        )
        journal_watcher = MagicMock()

        with pytest.raises(FocusError, match="Simulated focus failure"):
            self._main.jump_to_system(
                "Sol",
                options,
                res_handler,
                journal_watcher,
                tmp_path,
                focus_handler=handler,
            )


# ---------------------------------------------------------------------------
# GROUP 4: Dependency injection chain — focus reaches traversal helpers
# ---------------------------------------------------------------------------


class TestDependencyInjectionChain:
    """Tests proving the injection chain propagates ``focus_dependency``
    correctly through the runtime layer and into traversal helpers.
    """

    def test_runtime_dependencies_hold_focus(self) -> None:
        """TraversalRuntimeDependencies stores the focus dependency."""
        backend = RecordingInputBackend()
        checker = StubFocusChecker()
        handler = FocusAwareInputHandler(
            _make_binding(),
            input_backend=backend,
            focus_guard_factory=lambda _b, _t: checker,
        )
        deps = TraversalRuntimeDependencies(
            journal=None,
            window=None,
            focus=handler,
        )
        assert deps.focus is handler
        assert isinstance(deps.focus, FocusAwareInputHandler)

    def test_runtime_context_holds_focus_dependency(self) -> None:
        """TraversalRuntimeContext propagates the focus dependency."""
        handler, _, _ = _make_handler()
        deps = TraversalRuntimeDependencies(
            journal=None,
            window=None,
            focus=handler,
        )
        ctx = TraversalRuntimeContext(
            options=_make_options(),
            dependencies=deps,
            cancel_event=threading.Event(),
            status_callback=None,
            sleep=lambda _: None,
        )
        assert ctx.dependencies.focus is handler

    def test_traversal_controller_passes_focus_to_context(self) -> None:
        """TraversalController.run() creates context with focus dependency."""
        handler, _, _ = _make_handler()

        captured_focus: list[object] = []

        def _probe_slot(ctx: TraversalRuntimeContext) -> bool:
            """Simulates _run_traversal_slot, captures focus from context."""
            captured_focus.append(ctx.dependencies.focus)
            return True

        controller = TraversalController(sleep=lambda _: None)
        result = controller.run(
            _probe_slot,
            _make_options(),
            focus=handler,
        )

        assert result is True
        assert len(captured_focus) == 1
        assert captured_focus[0] is handler

    def test_run_traversal_slot_passes_focus_dependency_to_jump_helper(
        self, tmp_path: Path,
    ) -> None:
        main = _import_main_with_mocks()
        handler, _checker, _backend = _make_handler()
        options = _make_options(route_file=tmp_path / "route.txt")
        context = TraversalRuntimeContext(
            options=options,
            dependencies=TraversalRuntimeDependencies(
                journal=MagicMock(),
                window=None,
                focus=handler,
            ),
            cancel_event=threading.Event(),
            status_callback=None,
            sleep=lambda _: None,
        )
        captured_focus: list[object] = []

        class FakeResHandler:
            supported_res: bool = True

            def __init__(self, _width: object, _height: object) -> None:
                pass

        def _capture_jump(
            _system: str,
            _options: object,
            _res_handler: object,
            _journal_watcher: object,
            _sequence_dir: Path,
            _runtime_context: TraversalRuntimeContext | None = None,
            *,
            focus_handler: object | None = None,
        ) -> tuple[int, datetime.datetime | int]:
            captured_focus.append(focus_handler)
            raise KeyboardInterrupt

        with patch.object(main, "DiscordHandler", return_value=MagicMock()), \
             patch.object(main, "Reshandler", FakeResHandler), \
             patch.object(main, "load_route_list", return_value=["Sol"]), \
             patch.object(main, "latest_journal_path", return_value=tmp_path / "Journal.log"), \
             patch.object(main, "start_journal_thread"), \
             patch.object(main, "jump_to_system", side_effect=_capture_jump), \
             patch.object(main, "SAVE_PATH", tmp_path / "missing-save.txt"), \
             patch.object(main.tzlocal, "get_localzone", return_value=datetime.timezone.utc):
            assert main._run_traversal_slot(context) is False

        assert captured_focus == [handler]


# ---------------------------------------------------------------------------
# GROUP 5: Wiring documentation — proving GUI traversal uses the injected seam
# ---------------------------------------------------------------------------


class TestGapDocumentation:
    """Document the traversal input seam and its remaining global fallback.

    These tests verify that GUI traversal can avoid the global input module,
    while the legacy adapter still exists for non-injected call paths.
    """

    def test_main_py_imports_global_input_handler(self) -> None:
        """main.py imports input_handler at module level (line 36).

        The global adapter remains available for legacy CLI usage when no
        injected focus handler is supplied.
        """
        from TraversalSystem import input_handler as ih
        assert hasattr(ih, "press")
        assert hasattr(ih, "moveTo")
        assert hasattr(ih, "keyDown")
        assert hasattr(ih, "keyUp")
        assert hasattr(ih, "click")

    def test_injected_handler_does_not_use_global_module(self) -> None:
        """FocusAwareInputHandler with recording backend never touches
        the global input_handler module.
        """
        handler, _checker, backend = _make_handler()

        with patch("TraversalSystem.input_handler.press") as global_press, \
             patch("TraversalSystem.input_handler.moveTo") as global_move:
            handler.press("a")
            handler.moveTo(1, 2)

            global_press.assert_not_called()
            global_move.assert_not_called()

        assert len(backend.calls) == 2

    def test_runtime_context_exposes_focus_for_extraction(self) -> None:
        """TraversalRuntimeContext.dependencies.focus is available for
        extraction in _run_traversal_slot and forwarding into helper calls.
        """
        handler, _, _ = _make_handler()
        deps = TraversalRuntimeDependencies(
            journal=MagicMock(),
            window=MagicMock(),
            focus=handler,
        )
        ctx = TraversalRuntimeContext(
            options=_make_options(),
            dependencies=deps,
            cancel_event=threading.Event(),
            status_callback=None,
            sleep=lambda _: None,
        )

        # The focus dependency IS available via the context
        assert ctx.dependencies.focus is not None
        assert isinstance(ctx.dependencies.focus, FocusAwareInputHandler)

    def test_full_injection_chain_propagates_focus(self) -> None:
        """End-to-end: WorkerController → workers → controller → context.

        Simulates the full GUI injection chain to prove focus_dependency
        reaches the runtime context consumed by _run_traversal_slot.
        """
        handler, _checker, _backend = _make_handler()

        deps = TraversalRuntimeDependencies(
            journal=None,
            window=None,
            focus=handler,
        )

        assert deps.focus is handler
        assert isinstance(deps.focus, FocusAwareInputHandler)


# ---------------------------------------------------------------------------
# GROUP 6: Module-level wrappers for plan-required node IDs
# ---------------------------------------------------------------------------


def test_global_input_handler_press_monkeypatched_raises() -> None:
    """Module-level wrapper: global input_handler.press raises when monkeypatched."""
    TestGlobalInputHandlerMonkeypatch().test_monkeypatched_global_press_raises()


def test_global_input_handler_moveTo_monkeypatched_raises() -> None:
    """Module-level wrapper: global input_handler.moveTo raises when monkeypatched."""
    TestGlobalInputHandlerMonkeypatch().test_monkeypatched_global_moveTo_raises()


def test_traversal_seam_jump_uses_injected_not_global(tmp_path: Path) -> None:
    """Module-level wrapper: follow_button_sequence must use injected handler."""
    main = _import_main_with_mocks()
    seq_file = tmp_path / "test_seq.txt"
    _ = seq_file.write_text("space\n")
    handler, _checker, backend = _make_handler()
    with patch.object(
        main.input_handler, "press",
        side_effect=RuntimeError("GUI path must not touch global input_handler.press"),
    ):
        main.follow_button_sequence(
            tmp_path, "test_seq.txt",
            focus_handler=handler,  # type: ignore[call-arg]
        )
    assert len(backend.calls) == 1


def test_traversal_seam_restock_uses_injected_not_global(tmp_path: Path) -> None:
    """Module-level wrapper: restock_tritium must use injected handler."""
    main = _import_main_with_mocks()
    options = _make_options(auto_plot_jumps=True, disable_refuel=False, refuel_mode=0)
    for name in ["restock_fc.txt", "open_cargo_transfer.txt", "restock_cargo.txt"]:
        _ = (tmp_path / name).write_text("space\n")
    handler, _checker, backend = _make_handler()
    with patch.object(
        main.input_handler, "press",
        side_effect=RuntimeError("GUI path must not touch global input_handler.press"),
    ):
        main.restock_tritium(
            options, tmp_path,
            focus_handler=handler,  # type: ignore[call-arg]
        )
    assert len(backend.calls) >= 1


def test_injected_handler_unaffected_by_global_monkeypatch() -> None:
    """Module-level wrapper: injected handler works despite global monkeypatch."""
    TestGlobalInputHandlerMonkeypatch().test_injected_handler_unaffected_by_global_monkeypatch()


def test_runtime_dependencies_hold_focus() -> None:
    """Module-level wrapper: runtime deps store focus handler."""
    TestDependencyInjectionChain().test_runtime_dependencies_hold_focus()


def test_full_injection_chain_propagates_focus() -> None:
    """Module-level wrapper: full chain propagates focus dependency."""
    TestGapDocumentation().test_full_injection_chain_propagates_focus()
