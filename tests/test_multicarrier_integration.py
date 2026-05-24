"""Integration tests for multicarrier directory scanning and
selected-commander access in main.py (Task 4).

Covers:
- Startup readiness waits for commander discovery, NOT for CarrierJumpRequest
- Scan loop updates selected-commander state when new journal events appear
- Power-saving reopen reuses target_fid state after a new journal file appears
- LegacyJournalFacade correctly wraps JournalWatcher
- JournalScanLoop start/stop lifecycle
"""
from __future__ import annotations

# pyright: reportAny=false, reportUnknownMemberType=false, reportAttributeAccessIssue=false

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from TraversalSystem.multi_journal_router import (
    CTSJournalFacade,
    MultiJournalRouter,
)
from TraversalSystem.traversal_journal import (
    JournalScanLoop,
    LegacyJournalFacade,
)
from TraversalSystem.window_manager import WindowBinding, WindowBindingCoordinator, WindowInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_lines(path: Path, events: list[dict[str, object]]) -> None:
    """Append JSON-line events to *path*."""
    with path.open("a", encoding="utf-8") as fh:
        for evt in events:
            _ = fh.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _evt(event: str, **kw: object) -> dict[str, object]:
    _ = kw.setdefault("timestamp", "2026-04-25T12:00:00Z")
    return {"event": event, **kw}


# ---------------------------------------------------------------------------
# test_scan_loop_updates_selected_commander_state
# ---------------------------------------------------------------------------

class TestStartupReadinessWithoutJumpRequest:
    """Startup should proceed once the selected commander is discovered,
    even when no CarrierJumpRequest has been issued yet.

    This is a regression test: the original implementation waited on
    ``facade.last_carrier_request() is None``, which would deadlock until
    a jump was requested.  The correct readiness gate is
    ``facade.state() is not None``.
    """

    def test_startup_proceeds_without_carrier_jump_request(
        self, tmp_path: Path,
    ) -> None:
        journal = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(journal, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-READY", Name="ReadyCmdr"),
        ])

        router = MultiJournalRouter()
        facade = CTSJournalFacade(router, "F-READY")
        scan_loop = JournalScanLoop(router, tmp_path)

        scan_loop.start()
        try:
            deadline = time.monotonic() + 5
            while facade.state() is None:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)

            st = facade.state()
            assert st is not None
            assert st.commander_name == "ReadyCmdr"
            assert facade.last_carrier_request() is None
        finally:
            scan_loop.stop()


class TestScanLoopUpdatesSelectedCommanderState:
    """The scan loop should update the selected commander's state when new
    journal events appear in the directory.
    """

    def test_scan_loop_updates_selected_commander_state(
        self, tmp_path: Path,
    ) -> None:
        # Create an initial journal with a Commander event but no carrier request.
        journal = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(journal, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-TARGET", Name="TargetCmdr"),
        ])

        router = MultiJournalRouter()
        facade = CTSJournalFacade(router, "F-TARGET")
        scan_loop = JournalScanLoop(router, tmp_path)

        # Start the scan loop — it polls every 1 second.
        scan_loop.start()
        try:
            # Give the loop time to process initial content.
            time.sleep(1.5)
            assert facade.last_carrier_request() is None

            # Append a CarrierJumpRequest for the target commander.
            _write_lines(journal, [
                _evt(
                    "CarrierJumpRequest",
                    SystemName="Sol",
                    DepartureTime="2026-04-25T12:15:00Z",
                ),
            ])

            # Wait for the scan loop to pick up the new event.
            deadline = time.monotonic() + 5
            while facade.last_carrier_request() is None:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)

            assert facade.last_carrier_request() == "Sol"
            assert facade.departure_time() == "2026-04-25T12:15:00Z"
        finally:
            scan_loop.stop()


# ---------------------------------------------------------------------------
# test_power_saving_reopen_reuses_target_fid_after_new_journal_appears
# ---------------------------------------------------------------------------

class TestPowerSavingReopenReusesTargetFid:
    """When a new journal file appears (simulating power-saving reopen),
    the router should continue tracking the same target FID's state
    through the new file.
    """

    def test_power_saving_reopen_reuses_target_fid_after_new_journal_appears(
        self, tmp_path: Path,
    ) -> None:
        # Initial journal file with the target commander.
        journal1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(journal1, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-REOPEN", Name="ReopenCmdr"),
            _evt(
                "CarrierJumpRequest",
                SystemName="Sol",
                DepartureTime="2026-04-25T12:15:00Z",
            ),
        ])

        router = MultiJournalRouter()
        facade = CTSJournalFacade(router, "F-REOPEN")
        scan_loop = JournalScanLoop(router, tmp_path)

        # Simulate: scan loop was running, now stopped (power-saving close).
        scan_loop.start()
        time.sleep(1.5)
        scan_loop.stop()

        # Verify initial state.
        assert facade.last_carrier_request() == "Sol"

        # Simulate game reopen: a NEW journal file appears.
        journal2 = tmp_path / "Journal.2026-04-25T130000.01.log"
        _write_lines(journal2, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("LoadGame", FID="F-REOPEN", Commander="ReopenCmdr"),
        ])

        # Restart scan loop (open_game does scan_loop.start()).
        scan_loop.start()
        try:
            time.sleep(1.5)

            # The commander should still be tracked.
            state = facade.state()
            assert state is not None
            assert state.commander_name == "ReopenCmdr"

            # Write a new carrier request in the new file.
            _write_lines(journal2, [
                _evt(
                    "CarrierJumpRequest",
                    SystemName="Deciat",
                    DepartureTime="2026-04-25T13:20:00Z",
                ),
            ])

            deadline = time.monotonic() + 5
            while facade.last_carrier_request() != "Deciat":
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)

            assert facade.last_carrier_request() == "Deciat"
            assert facade.departure_time() == "2026-04-25T13:20:00Z"
        finally:
            scan_loop.stop()


class TestLostWindowInvalidatesBinding:
    def test_lost_window_invalidates_binding_and_blocks_automation(
        self, tmp_path: Path,
    ) -> None:
        journal = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(journal, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-BIND", Name="BindCmdr"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        facade = CTSJournalFacade(router, "F-BIND")
        state = facade.state()
        assert state is not None
        startup_identity = f"{state.commander_name}:{'|'.join(sorted(state.active_files))}"

        live_windows = [
            WindowInfo(
                handle=900,
                pid=9000,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="x11",
                focusable=True,
            ),
        ]
        coordinator = WindowBindingCoordinator(lambda: live_windows)

        binding = coordinator.resolve_binding(
            target_fid="F-BIND",
            startup_identity=startup_identity,
        )
        assert binding is not None
        assert binding.handle == 900

        live_windows.clear()

        assert coordinator.get_live_binding(
            target_fid="F-BIND",
            startup_identity=startup_identity,
        ) is None
        assert coordinator.resolve_binding(
            target_fid="F-BIND",
            startup_identity=startup_identity,
            ambiguous_window_policy="abort",
        ) is None


# ---------------------------------------------------------------------------
# LegacyJournalFacade unit tests
# ---------------------------------------------------------------------------

class TestLegacyJournalFacade:
    """LegacyJournalFacade wraps JournalWatcher with the same interface as
    CTSJournalFacade.
    """

    def test_delegates_last_carrier_request(self) -> None:
        from TraversalSystem.journalwatcher import JournalWatcher

        watcher = JournalWatcher()
        facade = LegacyJournalFacade(watcher)

        # JournalWatcher defaults to empty string; facade returns None.
        assert facade.last_carrier_request() is None

    def test_delegates_departure_time(self) -> None:
        from TraversalSystem.journalwatcher import JournalWatcher

        watcher = JournalWatcher()
        facade = LegacyJournalFacade(watcher)

        # Default departureTime is empty string; facade returns None.
        assert facade.departure_time() is None

    def test_delegates_has_jumped(self) -> None:
        from TraversalSystem.journalwatcher import JournalWatcher

        watcher = JournalWatcher()
        facade = LegacyJournalFacade(watcher)

        assert facade.has_jumped() is False

    def test_delegates_reset_jump(self) -> None:
        from TraversalSystem.journalwatcher import JournalWatcher

        watcher = JournalWatcher()
        facade = LegacyJournalFacade(watcher)
        facade.reset_jump()  # should not raise


# ---------------------------------------------------------------------------
# JournalScanLoop lifecycle
# ---------------------------------------------------------------------------

class TestJournalScanLoopLifecycle:
    """JournalScanLoop start/stop behaves correctly."""

    def test_start_and_stop(self, tmp_path: Path) -> None:
        router = MultiJournalRouter()
        loop = JournalScanLoop(router, tmp_path)

        loop.start()
        thread = getattr(loop, "_thread")
        assert thread is not None
        assert thread.is_alive()

        loop.stop()
        thread.join(timeout=3)
        assert not thread.is_alive()

    def test_stop_idempotent(self, tmp_path: Path) -> None:
        router = MultiJournalRouter()
        loop = JournalScanLoop(router, tmp_path)

        loop.start()
        loop.stop()
        loop.stop()  # second stop should not raise

    def test_scan_discovers_commander_in_background(
        self, tmp_path: Path,
    ) -> None:
        journal = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(journal, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-BG", Name="BgCmdr"),
        ])

        router = MultiJournalRouter()
        loop = JournalScanLoop(router, tmp_path)

        loop.start()
        try:
            deadline = time.monotonic() + 5
            while "F-BG" not in router.commanders:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)

            assert "F-BG" in router.commanders
        finally:
            loop.stop()


def test_scan_loop_updates_selected_commander_state(tmp_path: Path) -> None:
    TestScanLoopUpdatesSelectedCommanderState().test_scan_loop_updates_selected_commander_state(tmp_path)


def test_power_saving_reopen_reuses_target_fid_after_new_journal_appears(
    tmp_path: Path,
) -> None:
    TestPowerSavingReopenReusesTargetFid().test_power_saving_reopen_reuses_target_fid_after_new_journal_appears(tmp_path)


def test_lost_window_invalidates_binding_and_blocks_automation(
    tmp_path: Path,
) -> None:
    TestLostWindowInvalidatesBinding().test_lost_window_invalidates_binding_and_blocks_automation(tmp_path)


# ---------------------------------------------------------------------------
# Task 5: traversal-level facade integration
# ---------------------------------------------------------------------------

class TestJumpFlowUsesSelectedCommanderOnly:

    def test_jump_flow_uses_selected_commander_only(self, tmp_path: Path) -> None:
        target_journal = tmp_path / "Journal.2026-04-25T120000.01.log"
        other_journal = tmp_path / "Journal.2026-04-25T120001.01.log"

        _write_lines(target_journal, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-TARGET", Name="TargetCmdr"),
        ])
        _write_lines(other_journal, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-OTHER", Name="OtherCmdr"),
        ])

        router = MultiJournalRouter()
        target_facade = CTSJournalFacade(router, "F-TARGET")
        other_facade = CTSJournalFacade(router, "F-OTHER")
        scan_loop = JournalScanLoop(router, tmp_path)

        scan_loop.start()
        try:
            deadline = time.monotonic() + 5
            while target_facade.state() is None or other_facade.state() is None:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)

            assert target_facade.state() is not None
            assert other_facade.state() is not None

            _write_lines(other_journal, [
                _evt(
                    "CarrierJumpRequest",
                    SystemName="Deciat",
                    DepartureTime="2026-04-25T12:20:00Z",
                ),
            ])

            deadline = time.monotonic() + 5
            while other_facade.last_carrier_request() != "Deciat":
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)

            assert other_facade.last_carrier_request() == "Deciat"
            assert target_facade.last_carrier_request() is None
            assert target_facade.departure_time() is None

            _write_lines(target_journal, [
                _evt(
                    "CarrierJumpRequest",
                    SystemName="Sol",
                    DepartureTime="2026-04-25T12:30:00Z",
                ),
            ])

            deadline = time.monotonic() + 5
            while target_facade.last_carrier_request() != "Sol":
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)

            assert target_facade.last_carrier_request() == "Sol"
            assert target_facade.departure_time() == "2026-04-25T12:30:00Z"
            assert other_facade.last_carrier_request() == "Deciat"

            _write_lines(other_journal, [
                _evt("CarrierJump"),
            ])
            time.sleep(1.5)

            assert other_facade.has_jumped() is True
            assert target_facade.has_jumped() is False
        finally:
            scan_loop.stop()


class TestJumpCancelledClearsOnlyTargetCommanderPendingState:

    def test_jump_cancelled_clears_only_target_commander_pending_state(
        self, tmp_path: Path,
    ) -> None:
        journal_a = tmp_path / "Journal.2026-04-25T120000.01.log"
        journal_b = tmp_path / "Journal.2026-04-25T120001.01.log"

        _write_lines(journal_a, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-A", Name="CmdrA"),
        ])
        _write_lines(journal_b, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-B", Name="CmdrB"),
        ])

        router = MultiJournalRouter()
        facade_a = CTSJournalFacade(router, "F-A")
        facade_b = CTSJournalFacade(router, "F-B")

        router.scan_once(tmp_path)
        assert facade_a.state() is not None
        assert facade_b.state() is not None

        _write_lines(journal_a, [
            _evt(
                "CarrierJumpRequest",
                SystemName="Sol",
                DepartureTime="2026-04-25T12:15:00Z",
            ),
        ])
        _write_lines(journal_b, [
            _evt(
                "CarrierJumpRequest",
                SystemName="Deciat",
                DepartureTime="2026-04-25T12:20:00Z",
            ),
        ])

        router.scan_once(tmp_path)

        assert facade_a.last_carrier_request() == "Sol"
        assert facade_a.departure_time() == "2026-04-25T12:15:00Z"
        assert facade_b.last_carrier_request() == "Deciat"
        assert facade_b.departure_time() == "2026-04-25T12:20:00Z"

        _write_lines(journal_b, [
            _evt("CarrierJumpCancelled"),
        ])

        router.scan_once(tmp_path)

        assert facade_b.jump_cancelled() is True
        assert facade_b.last_carrier_request() is None
        assert facade_b.departure_time() is None

        assert facade_a.jump_cancelled() is False
        assert facade_a.last_carrier_request() == "Sol"
        assert facade_a.departure_time() == "2026-04-25T12:15:00Z"

        facade_b.reset_cancel()
        assert facade_b.jump_cancelled() is False

        assert facade_a.has_jumped() is False
        assert facade_b.has_jumped() is False


def test_jump_flow_uses_selected_commander_only(tmp_path: Path) -> None:
    TestJumpFlowUsesSelectedCommanderOnly().test_jump_flow_uses_selected_commander_only(tmp_path)


def test_jump_cancelled_clears_only_target_commander_pending_state(
    tmp_path: Path,
) -> None:
    TestJumpCancelledClearsOnlyTargetCommanderPendingState().test_jump_cancelled_clears_only_target_commander_pending_state(tmp_path)


# ---------------------------------------------------------------------------
# Task 8: focus-failure blocks input dispatch
# ---------------------------------------------------------------------------

class TestFocusFailureBlocksInputDispatch:
    """When focus verification fails, no input should be dispatched.

    Verifies that FocusGuard.ensure_focus() raises FocusError when the
    target window cannot be focused, and that the error prevents any
    subsequent automation from running.
    """

    def test_focus_failure_blocks_input_dispatch(self) -> None:
        from TraversalSystem.window_manager import FocusError, FocusGuard

        binding = WindowBinding(
            target_fid="F-BLOCK",
            startup_identity="cmdr:j1",
            handle=12345,
            pid=999,
            title="Elite - Dangerous (CLIENT)",
            window_class="",
            backend="x11",
        )

        def mock_check_output(cmd: list[str], **_kwargs: object) -> str:
            if cmd[0] == "xdotool" and cmd[1] == "getactivewindow":
                return "99999\n"
            if cmd[0] == "xdotool" and cmd[1] == "windowactivate":
                return ""
            raise FileNotFoundError(cmd[0])

        with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
             patch("TraversalSystem.window_manager.IS_LINUX", True), \
             patch("subprocess.check_output", side_effect=mock_check_output):
            guard = FocusGuard(binding, focus_timeout_seconds=0.1)
            with pytest.raises(FocusError, match="Failed to acquire"):
                guard.ensure_focus()

        input_dispatched = False

        def simulate_automation() -> None:
            nonlocal input_dispatched
            guard2 = FocusGuard(binding, focus_timeout_seconds=0.1)
            guard2.ensure_focus()
            input_dispatched = True

        with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
             patch("TraversalSystem.window_manager.IS_LINUX", True), \
             patch("subprocess.check_output", side_effect=mock_check_output):
            with pytest.raises(FocusError):
                simulate_automation()

        assert input_dispatched is False


def test_focus_failure_blocks_input_dispatch() -> None:
    TestFocusFailureBlocksInputDispatch().test_focus_failure_blocks_input_dispatch()


# ---------------------------------------------------------------------------
# Fail-closed regression: unresolved binding blocks automation
# ---------------------------------------------------------------------------

class TestUnresolvedBindingBlocksAutomation:
    """When binding resolution fails in multicarrier mode, automation must
    not proceed with blind input.

    This is the fail-closed regression test: resolve_binding returning None
    for a multicarrier target means no FocusGuard can be created, and the
    caller must treat this as a hard stop.
    """

    def test_no_windows_means_no_binding(self) -> None:
        """resolve_binding returns None when no Elite windows exist."""
        coordinator = WindowBindingCoordinator(lambda: [])

        binding = coordinator.resolve_binding(
            target_fid="F-NOWIN",
            startup_identity="cmdr:j1",
            ambiguous_window_policy="abort",
        )

        assert binding is None

    def test_ambiguous_windows_means_no_binding(self) -> None:
        """resolve_binding returns None when multiple indistinguishable
        Elite windows are found and abort policy is active.
        """
        windows = [
            WindowInfo(
                handle=100, pid=1000,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="x11", focusable=True,
            ),
            WindowInfo(
                handle=200, pid=2000,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="x11", focusable=True,
            ),
        ]
        coordinator = WindowBindingCoordinator(lambda: list(windows))

        binding = coordinator.resolve_binding(
            target_fid="F-AMBIG",
            startup_identity="cmdr:j1",
            ambiguous_window_policy="abort",
        )

        assert binding is None

    def test_lost_window_yields_none_on_resolve(self) -> None:
        """After a window disappears, resolve_binding returns None."""
        live_windows: list[WindowInfo] = [
            WindowInfo(
                handle=900, pid=9000,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="x11", focusable=True,
            ),
        ]
        coordinator = WindowBindingCoordinator(lambda: live_windows)

        binding = coordinator.resolve_binding(
            target_fid="F-LOST",
            startup_identity="cmdr:j1",
        )
        assert binding is not None

        live_windows.clear()

        rechecked = coordinator.resolve_binding(
            target_fid="F-LOST",
            startup_identity="cmdr:j1",
            ambiguous_window_policy="abort",
        )
        assert rechecked is None

    def test_none_binding_means_no_focus_guard(self) -> None:
        """A None binding cannot produce a FocusGuard — the constructor
        requires a WindowBinding. This is the fail-closed contract.
        """
        from TraversalSystem.window_manager import FocusGuard

        coordinator = WindowBindingCoordinator(lambda: [])
        binding = coordinator.resolve_binding(
            target_fid="F-GUARD",
            startup_identity="cmdr:j1",
            ambiguous_window_policy="abort",
        )
        assert binding is None

        # FocusGuard(binding, ...) would raise TypeError if binding is None,
        # which prevents blind input from being dispatched.
        with pytest.raises(TypeError):
            _ = FocusGuard(binding, 5.0)  # type: ignore[arg-type]  # intentional: verifying None raises TypeError


def test_no_windows_means_no_binding() -> None:
    TestUnresolvedBindingBlocksAutomation().test_no_windows_means_no_binding()


def test_ambiguous_windows_means_no_binding() -> None:
    TestUnresolvedBindingBlocksAutomation().test_ambiguous_windows_means_no_binding()


def test_lost_window_yields_none_on_resolve() -> None:
    TestUnresolvedBindingBlocksAutomation().test_lost_window_yields_none_on_resolve()


def test_none_binding_means_no_focus_guard() -> None:
    TestUnresolvedBindingBlocksAutomation().test_none_binding_means_no_focus_guard()
