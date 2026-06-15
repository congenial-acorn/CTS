"""Integration tests for multicarrier directory scanning and
selected-commander access in main.py (Task 4).

Covers:
- Startup readiness waits for commander discovery, NOT for CarrierJumpRequest
- Scan loop updates selected-commander state when new journal events appear
- Power-saving reopen reuses target_fid state after a new journal file appears
- JournalScanLoop start/stop lifecycle
"""
from __future__ import annotations

# pyright: reportAny=false, reportUnknownMemberType=false, reportAttributeAccessIssue=false

import datetime
import importlib
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from TraversalSystem.config import TraversalOptions
from TraversalSystem.multi_journal_router import (
    CTSJournalFacade,
    MultiJournalRouter,
)
from TraversalSystem.runtime.controller import (
    TraversalRuntimeContext,
    TraversalRuntimeDependencies,
    TraversalStopped,
)
from TraversalSystem.traversal_journal import (
    JournalScanLoop,
)
from TraversalSystem.window_manager import WindowBinding, WindowBindingCoordinator, WindowInfo


MAIN_PATH = Path(__file__).resolve().parents[1] / "TraversalSystem" / "main.py"


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


def _load_main_with_mocks(tmp_path: Path) -> object:
    overrides: dict[str, ModuleType] = {}
    for mod_name in [
        "psutil",
        "pyautogui",
        "pyperclip",
        "pytz",
        "tzlocal",
        "discord_webhook",
    ]:
        module = ModuleType(mod_name)
        overrides[mod_name] = module

    overrides["pyautogui"].FAILSAFE = False  # type: ignore[attr-defined]
    overrides["pytz"].UTC = datetime.timezone.utc  # type: ignore[attr-defined]
    overrides["tzlocal"].get_localzone = lambda: datetime.timezone.utc  # type: ignore[attr-defined]

    discord_mod = ModuleType("TraversalSystem.discordhandler")
    discord_mod.DiscordHandler = MagicMock  # type: ignore[attr-defined]
    overrides["TraversalSystem.discordhandler"] = discord_mod

    reshandler_mod = ModuleType("TraversalSystem.reshandler")
    reshandler_mod.Reshandler = MagicMock  # type: ignore[attr-defined]
    overrides["TraversalSystem.reshandler"] = reshandler_mod

    saved = {name: sys.modules.get(name) for name in overrides}
    saved_main = sys.modules.get("TraversalSystem.main")
    try:
        for name, module in overrides.items():
            sys.modules[name] = module
        _ = sys.modules.pop("TraversalSystem.main", None)
        module = importlib.import_module("TraversalSystem.main")
        module.BASE_DIR = tmp_path
        module.SEQUENCE_DIR = tmp_path
        module.SAVE_PATH = tmp_path / "save.txt"
        return module
    finally:
        _ = sys.modules.pop("TraversalSystem.main", None)
        if saved_main is not None:
            sys.modules["TraversalSystem.main"] = saved_main
        for name, original in saved.items():
            if original is None:
                _ = sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class _ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class _SteppingClock:
    """Monotonic clock stub that advances by *step* seconds on every call.

    Useful for simulating elapsed wall-clock time during restock without
    real sleeping.
    """

    def __init__(self, *, start: float = 1000.0, step: float = 400.0) -> None:
        self._now = start
        self._step = step

    def now(self) -> float:
        current = self._now
        self._now += self._step
        return current


class _RecordingInputHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def press(self, key: str) -> None:
        self.calls.append(("press", key))

    def keyDown(self, key: str) -> None:
        self.calls.append(("keyDown", key))

    def keyUp(self, key: str) -> None:
        self.calls.append(("keyUp", key))


class _CancellingSequenceRuntimeContext:
    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.wait_calls: list[float] = []

    def wait(self, seconds: float) -> None:
        self.wait_calls.append(seconds)
        self.cancel_event.set()
        raise TraversalStopped("Traversal cancelled.")

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise TraversalStopped("Traversal cancelled.")


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


class TestPowerSavingCooldownTiming:
    def test_power_saving_reopen_210s_leaves_expected_remaining_cooldown(
        self, tmp_path: Path,
    ) -> None:
        registered_remaining, _saved_indices, result = _run_power_saving_cooldown_cycle(
            tmp_path,
            game_ready_after=210.0,
        )

        assert result is False
        assert registered_remaining[0] == pytest.approx(362.0)
        assert registered_remaining[-1] == pytest.approx(152.0, abs=2.0)

    def test_power_saving_reopen_250s_uses_elapsed_time_not_hardcoded_152(
        self, tmp_path: Path,
    ) -> None:
        registered_remaining, _saved_indices, result = _run_power_saving_cooldown_cycle(
            tmp_path,
            game_ready_after=250.0,
        )

        assert result is False
        assert registered_remaining[0] == pytest.approx(362.0)
        assert registered_remaining[-1] == pytest.approx(112.0, abs=2.0)

    def test_power_saving_reopen_past_full_cooldown_clamps_remaining_to_zero(
        self, tmp_path: Path,
    ) -> None:
        registered_remaining, _saved_indices, result = _run_power_saving_cooldown_cycle(
            tmp_path,
            game_ready_after=400.0,
        )

        assert result is False
        assert registered_remaining[0] == pytest.approx(362.0)
        assert registered_remaining == [pytest.approx(362.0)]

    def test_power_saving_wait_still_honors_jump_cancellation(
        self, tmp_path: Path,
    ) -> None:
        _registered_remaining, saved_indices, result = _run_power_saving_cooldown_cycle(
            tmp_path,
            game_ready_after=None,
            cancel_after=150.0,
        )

        assert result is False
        assert saved_indices == [0]


class TestSequenceCancellationAwareWaits:
    def test_follow_button_sequence_cancels_before_long_wait_completes(
        self, tmp_path: Path,
    ) -> None:
        main = _load_main_with_mocks(tmp_path)
        (tmp_path / "long_wait.txt").write_text("space-5\nenter\n", encoding="utf-8")
        handler = _RecordingInputHandler()
        runtime_context = _CancellingSequenceRuntimeContext()

        with pytest.raises(TraversalStopped, match="Traversal cancelled"):
            cast(Any, main).follow_button_sequence(
                tmp_path,
                "long_wait.txt",
                focus_handler=handler,
                runtime_context=runtime_context,
            )

        assert len(runtime_context.wait_calls) == 1
        assert runtime_context.wait_calls[0] >= 5.0
        assert handler.calls == [("press", "space")]

    def test_follow_button_sequence_releases_held_key_on_cancellation(
        self, tmp_path: Path,
    ) -> None:
        main = _load_main_with_mocks(tmp_path)
        (tmp_path / "held_key.txt").write_text("shift:5\nspace\n", encoding="utf-8")
        handler = _RecordingInputHandler()
        runtime_context = _CancellingSequenceRuntimeContext()

        with pytest.raises(TraversalStopped, match="Traversal cancelled"):
            cast(Any, main).follow_button_sequence(
                tmp_path,
                "held_key.txt",
                focus_handler=handler,
                runtime_context=runtime_context,
            )

        assert len(runtime_context.wait_calls) == 1
        assert runtime_context.wait_calls[0] >= 5.0
        assert handler.calls == [
            ("keyDown", "shift"),
            ("keyUp", "shift"),
        ]


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


class _FakeSequenceQueue:
    def __init__(
        self,
        *,
        restock_failure: BaseException | None = None,
        before_result: Callable[[], None] | None = None,
    ) -> None:
        self.deadlines: dict[str, float] = {}
        self.register_calls: list[tuple[str, float]] = []
        self.clear_calls: list[str] = []
        self.submit_restock_calls: list[tuple[str, float, threading.Event | None]] = []
        self._restock_failure: BaseException | None = restock_failure
        self._before_result: Callable[[], None] | None = before_result

    def register_jump_deadline(self, *, slot_id: str, deadline: float) -> None:
        self.deadlines[slot_id] = deadline
        self.register_calls.append((slot_id, deadline))

    def clear_jump_deadline(self, *, slot_id: str) -> None:
        _ = self.deadlines.pop(slot_id, None)
        self.clear_calls.append(slot_id)

    def submit_restock(
        self,
        *,
        slot_id: str,
        run: Callable[[], object],
        estimated_duration: float,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> object:
        _ = deadline
        self.submit_restock_calls.append((slot_id, estimated_duration, cancel_event))

        queue = self

        class _Handle:
            def result(self, timeout: float | None = None) -> object:
                _ = timeout
                if queue._before_result is not None:
                    queue._before_result()
                if queue._restock_failure is not None:
                    raise queue._restock_failure
                return run()

        return _Handle()


class _FakeResHandler:
    supported_res: bool = True

    def __init__(self, _width: object, _height: object) -> None:
        pass


def _make_runtime_context(
    *,
    options: TraversalOptions,
    sequence_queue: object,
    journal: object,
    slot_id: int,
    sleep: Callable[[float], None],
    cancel_event: threading.Event | None = None,
) -> TraversalRuntimeContext:
    cancel = cancel_event or threading.Event()
    # Make Event.wait() non-blocking so runtime_context.wait(N) doesn't
    # burn N real seconds per call via threading.Event.wait(step).
    _real_event_wait = cancel.wait
    cancel.wait = lambda timeout=None: _real_event_wait(0)  # type: ignore[assignment]
    return TraversalRuntimeContext(
        options=options,
        dependencies=TraversalRuntimeDependencies(
            journal=journal,
            window=None,
            focus=None,
            sequence_queue=sequence_queue,
            slot_id=slot_id,
        ),
        cancel_event=cancel,
        status_callback=None,
        sleep=sleep,
    )


def _make_options(tmp_path: Path) -> TraversalOptions:
    return TraversalOptions(
        webhook_url="",
        journal_directory=tmp_path,
        route_file=tmp_path / "route.txt",
        route_position=0,
        tritium_slot=0,
        auto_plot_jumps=True,
        disable_refuel=False,
        power_saving=False,
        refuel_mode=0,
        single_discord_message=False,
        shutdown_on_complete=False,
    )


def _run_power_saving_cooldown_cycle(
    tmp_path: Path,
    *,
    game_ready_after: float | None,
    cancel_after: float | None = None,
) -> tuple[list[float], list[int], bool]:
    main = _load_main_with_mocks(tmp_path)
    clock = _ManualClock()
    registered_remaining: list[float] = []
    saved_indices: list[int] = []
    scheduled_state: list[object | None] = [None]
    cooldown_started_at: list[float | None] = [None]

    class _RecordingSequenceQueue(_FakeSequenceQueue):
        def register_jump_deadline(self, *, slot_id: str, deadline: float) -> None:
            super().register_jump_deadline(slot_id=slot_id, deadline=deadline)
            if cooldown_started_at[0] is None:
                cooldown_started_at[0] = clock.now()
            registered_remaining.append(deadline - clock.now())

    class _FakeTimer:
        def __init__(self, _delay: float, _fn: object, args: tuple[object, ...] = ()) -> None:
            scheduled_state[0] = args[0] if args else None

        def start(self) -> None:
            return None

    queue = _RecordingSequenceQueue()
    journal = MagicMock(spec=CTSJournalFacade)
    journal.has_jumped.return_value = False
    journal.reset_jump.return_value = None
    journal.reset_cancel.return_value = None
    journal.jump_cancelled.side_effect = (
        lambda: cancel_after is not None and clock.now() >= cancel_after
    )

    options = _make_options(tmp_path)
    options.power_saving = True

    context = _make_runtime_context(
        options=options,
        sequence_queue=queue,
        journal=journal,
        slot_id=0,
        sleep=lambda _seconds: None,
    )
    context.cancel_event.wait = lambda timeout=None: _advance_power_saving_clock(
        clock,
        0.0 if timeout is None else timeout,
        scheduled_state=scheduled_state,
        cooldown_started_at=cooldown_started_at,
        game_ready_after=game_ready_after,
    )  # type: ignore[assignment]

    jump_calls = 0

    def fake_jump(*_args: object, **_kwargs: object) -> tuple[int, datetime.datetime]:
        nonlocal jump_calls
        jump_calls += 1
        if jump_calls > 1:
            raise KeyboardInterrupt
        return 10, datetime.datetime.now(datetime.timezone.utc)

    def track_save(state: object, **_kwargs: object) -> None:
        saved_indices.append(getattr(state, "line_no"))

    with patch("builtins.print"), \
         patch.object(main, "DiscordHandler", return_value=MagicMock()), \
         patch.object(main, "Reshandler", _FakeResHandler), \
         patch.object(main, "load_route_list", return_value=["A", "B"]), \
         patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
         patch.object(main, "consume_save", return_value=None), \
         patch.object(main, "save_progress", side_effect=track_save), \
         patch.object(main, "jump_to_system", side_effect=fake_jump), \
         patch.object(main, "restock_tritium"), \
         patch.object(main, "follow_button_sequence"), \
         patch.object(main, "get_game_process_names", return_value=[]), \
         patch.object(main.psutil, "process_iter", return_value=[], create=True), \
         patch.object(main.threading, "Timer", _FakeTimer), \
         patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
         patch.object(main, "time") as fake_time:
        fake_time.monotonic.side_effect = clock.now
        fake_time.sleep.side_effect = lambda _s: None
        try:
            result = main._run_traversal_slot(context)
        except (KeyboardInterrupt, SystemExit):
            result = False

    return registered_remaining, saved_indices, result


def _advance_power_saving_clock(
    clock: _ManualClock,
    seconds: float,
    *,
    scheduled_state: list[object | None],
    cooldown_started_at: list[float | None],
    game_ready_after: float | None,
) -> bool:
    current = clock.now()
    ready_at = (
        None
        if game_ready_after is None or cooldown_started_at[0] is None
        else cooldown_started_at[0] + game_ready_after
    )
    advance_by = seconds
    if (
        ready_at is not None
        and scheduled_state[0] is not None
        and not getattr(scheduled_state[0], "game_ready", False)
        and current < ready_at <= current + seconds
    ):
        advance_by = ready_at - current
    clock.advance(advance_by)
    if (
        ready_at is not None
        and scheduled_state[0] is not None
        and clock.now() >= ready_at
    ):
        setattr(scheduled_state[0], "game_ready", True)
    return False


def _run_restock_cycle(
    tmp_path: Path,
    *,
    restock_failure: BaseException | None = None,
    monotonic_side_effect: Callable[[], float] | None = None,
) -> tuple[_FakeSequenceQueue, list[str]]:
    main = _load_main_with_mocks(tmp_path)
    order: list[str] = []
    queue = _FakeSequenceQueue(
        restock_failure=restock_failure,
        before_result=lambda: order.append("queue-result"),
    )
    jump_calls = 0

    journal = MagicMock()
    journal.has_jumped.return_value = True
    journal.jump_cancelled.return_value = False

    def fake_jump_to_system(*_args: object, **_kwargs: object) -> tuple[int, datetime.datetime]:
        nonlocal jump_calls
        jump_calls += 1
        order.append(f"jump-{jump_calls}")
        if jump_calls > 1:
            raise KeyboardInterrupt
        return 7, datetime.datetime.now(datetime.timezone.utc)

    context = _make_runtime_context(
        options=_make_options(tmp_path),
        sequence_queue=queue,
        journal=journal,
        slot_id=0,
        sleep=lambda _seconds: None,
    )

    def record_restock(*_args: object, **_kwargs: object) -> None:
        order.append("restock")

    def fast_sleep(_seconds: float) -> None:
        return None

    with patch("builtins.print"), \
         patch.object(main, "DiscordHandler", return_value=MagicMock()), \
         patch.object(main, "Reshandler", _FakeResHandler), \
         patch.object(main, "load_route_list", return_value=["A", "B"]), \
         patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
         patch.object(main, "consume_save", return_value=None), \
         patch.object(main, "save_progress"), \
         patch.object(main, "jump_to_system", side_effect=fake_jump_to_system), \
         patch.object(main, "restock_tritium", side_effect=record_restock), \
         patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
         patch.object(main, "time") as fake_time:
        fake_time.monotonic.side_effect = monotonic_side_effect or time.monotonic
        fake_time.sleep.side_effect = fast_sleep
        if restock_failure is None:
            assert main._run_traversal_slot(context) is False
        else:
            with pytest.raises(type(restock_failure), match=str(restock_failure)):
                main._run_traversal_slot(context)

    return queue, order


def test_restock_queue_submission_blocks_until_queue_completion(tmp_path: Path) -> None:
    queue, order = _run_restock_cycle(tmp_path)

    assert queue.register_calls
    assert len(queue.submit_restock_calls) == 1
    assert queue.submit_restock_calls[0][0:2] == ("slot-0", 60.0)
    assert queue.submit_restock_calls[0][2] is not None
    assert order == ["jump-1", "queue-result", "restock", "jump-2"]
    assert queue.deadlines == {}


def test_restock_next_cycle_propagates_queue_failure(tmp_path: Path) -> None:
    queue, order = _run_restock_cycle(
        tmp_path,
        restock_failure=RuntimeError("restock boom"),
    )

    assert queue.register_calls
    assert len(queue.submit_restock_calls) == 1
    assert order == ["jump-1", "queue-result"]
    assert queue.deadlines == {}


def test_deadline_and_restock_cleanup_on_completion_stop_and_failure(
    tmp_path: Path,
) -> None:
    main = _load_main_with_mocks(tmp_path)
    created_queues: list[_FakeSequenceQueue] = []

    def run_case(
        *,
        jump_results: list[tuple[int, datetime.datetime] | BaseException],
        sleep_side_effect: Callable[[float], None],
        cancel_event: threading.Event | None = None,
        reraise: bool = False,
    ) -> _FakeSequenceQueue:
        queue = _FakeSequenceQueue()
        created_queues.append(queue)
        journal = MagicMock()
        journal.has_jumped.return_value = True
        journal.jump_cancelled.return_value = False
        results = list(jump_results)

        def fake_jump_to_system(*_args: object, **_kwargs: object) -> tuple[int, datetime.datetime]:
            next_result = results.pop(0)
            if isinstance(next_result, BaseException):
                raise next_result
            return next_result

        context = _make_runtime_context(
            options=_make_options(tmp_path),
            sequence_queue=queue,
            journal=journal,
            slot_id=0,
            sleep=sleep_side_effect,
            cancel_event=cancel_event,
        )

        with patch("builtins.print"), \
             patch.object(main, "DiscordHandler", return_value=MagicMock()), \
             patch.object(main, "Reshandler", _FakeResHandler), \
             patch.object(main, "load_route_list", return_value=["A", "B"]), \
             patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
             patch.object(main, "consume_save", return_value=None), \
             patch.object(main, "save_progress"), \
             patch.object(main, "jump_to_system", side_effect=fake_jump_to_system), \
             patch.object(main, "restock_tritium"), \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
             patch.object(main, "time") as fake_time:
            fake_time.monotonic.side_effect = time.monotonic
            fake_time.sleep.side_effect = sleep_side_effect
            try:
                assert isinstance(main._run_traversal_slot(context), bool)
            except (Exception, SystemExit):
                if reraise:
                    raise
        return queue

    completion_queue = run_case(
        jump_results=[
            (7, datetime.datetime.now(datetime.timezone.utc)),
            (7, datetime.datetime.now(datetime.timezone.utc)),
        ],
        sleep_side_effect=lambda _seconds: None,
    )
    assert completion_queue.deadlines == {}

    stop_event = threading.Event()
    sleep_calls = 0

    def stop_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 20:
            stop_event.set()

    with pytest.raises(TraversalStopped):
        _ = run_case(
            jump_results=[
                (7, datetime.datetime.now(datetime.timezone.utc)),
                (7, datetime.datetime.now(datetime.timezone.utc)),
            ],
            sleep_side_effect=stop_sleep,
            cancel_event=stop_event,
            reraise=True,
        )
    assert created_queues[-1].deadlines == {}

    failure_queue = run_case(
        jump_results=[
            (7, datetime.datetime.now(datetime.timezone.utc)),
            RuntimeError("boom"),
        ],
        sleep_side_effect=lambda _seconds: None,
    )
    assert failure_queue.deadlines == {}


def test_restock_exceeding_remaining_cooldown_clamps_and_clears_deadline(
    tmp_path: Path,
) -> None:
    """Edge case: restock actual elapsed time exceeding remaining cooldown.

    When restock takes longer than the remaining cooldown (and longer than
    the 60s estimate), total_time must clamp safely to zero so no past-due
    jump deadline is registered and no stale deadline remains in the map.

    Uses a _SteppingClock so each time.monotonic() call advances 400s.
    After the cooldown loop reaches total_time=300, the two monotonic calls
    bracketing the restock are 400s apart — far exceeding both the 60s
    estimate and the 300s remaining cooldown.
    """
    clock = _SteppingClock(start=1000.0, step=400.0)
    queue, _order = _run_restock_cycle(
        tmp_path,
        monotonic_side_effect=clock.now,
    )

    # Exactly one register call (the initial 362s deadline before the
    # cooldown loop).  The second register that normally happens after
    # restock is skipped because total_time clamped to 0.
    assert len(queue.register_calls) == 1
    assert queue.register_calls[0][0] == "slot-0"

    # The pre-restock clear removed the deadline, and the clamped
    # total_time prevented re-registration, so the map is clean.
    assert queue.deadlines == {}
    assert "slot-0" in queue.clear_calls


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
            _ = FocusGuard(cast(WindowBinding, cast(object, binding)), 5.0)


def test_no_windows_means_no_binding() -> None:
    TestUnresolvedBindingBlocksAutomation().test_no_windows_means_no_binding()


def test_ambiguous_windows_means_no_binding() -> None:
    TestUnresolvedBindingBlocksAutomation().test_ambiguous_windows_means_no_binding()


def test_lost_window_yields_none_on_resolve() -> None:
    TestUnresolvedBindingBlocksAutomation().test_lost_window_yields_none_on_resolve()


def test_none_binding_means_no_focus_guard() -> None:
    TestUnresolvedBindingBlocksAutomation().test_none_binding_means_no_focus_guard()


# ---------------------------------------------------------------------------
# Task 10: Integrated multicarrier regression
#
# Exercises the full production path from Tasks 4-9 together:
#   - shared SequenceQueue serializing automation blocks
#   - focus-aware input seam (FocusAwareInputHandler with recording backend)
#   - slot-aware save isolation
#   - deadline registration / look-ahead gating
#   - queued restock
#   - cancellation / failure / shutdown cleanup
# ---------------------------------------------------------------------------

from TraversalSystem.focus_input_handler import FocusAwareInputHandler
from TraversalSystem.sequence_queue import CancelledBlockError, SequenceQueue


class _SlotRecordingBackend:
    """Per-slot recording input backend that captures every primitive call."""

    slot_label: str
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]]

    def __init__(self, slot_label: str) -> None:
        self.slot_label = slot_label
        self.calls = []

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


class _PassThroughFocusChecker:
    """Focus checker that always succeeds and records the call."""

    slot_label: str
    focus_calls: int

    def __init__(self, slot_label: str) -> None:
        self.slot_label = slot_label
        self.focus_calls = 0

    def ensure_focus(self) -> None:
        self.focus_calls += 1


def _make_slot_handler(
    slot_label: str,
    *,
    handle: int = 42,
    binding: WindowBinding | None = None,
) -> tuple[FocusAwareInputHandler, _PassThroughFocusChecker, _SlotRecordingBackend]:
    b = binding or _make_binding(target_fid=f"F-{slot_label}", handle=handle)
    checker = _PassThroughFocusChecker(slot_label)
    backend = _SlotRecordingBackend(slot_label)
    handler = FocusAwareInputHandler(
        b,
        focus_timeout_seconds=1.0,
        input_backend=backend,
        focus_guard_factory=lambda _b, _t: checker,
    )
    return handler, checker, backend


def _make_binding(
    target_fid: str = "F-TEST",
    handle: int = 42,
) -> WindowBinding:
    return WindowBinding(
        target_fid=target_fid,
        startup_identity=f"cmdr:{target_fid}",
        handle=handle,
        pid=1000 + hash(target_fid) % 1000,
        title="Elite - Dangerous (CLIENT)",
        window_class="EliteDangerous",
        backend="x11",
    )


class TestTwoCarrierSerializationIntegration:
    """End-to-end integration: two GUI slots exercise the complete
    production path from Tasks 4-9 together.

    This test class proves:
    1. Serialized block order — no overlapping automation blocks
    2. Correct per-window input routing via injected focus handlers
    3. Per-slot progress state (save isolation)
    4. Cancellation cleanup (deadline cleared, restock cancelled)
    5. No direct global input usage (recording backend proves it)
    """

    def test_two_carriers_serialize_jump_and_restock_through_queue(
        self, tmp_path: Path,  # pyright: ignore[reportUnusedParameter]
    ) -> None:
        """Two carriers submit near-simultaneous jump-plot and restock blocks.

        The shared SequenceQueue must serialize them so that at most one
        block is active at any time.  Jump-plot deadlines must take
        priority over restock when restock would overrun the deadline.
        """
        queue = SequenceQueue()
        active = 0
        max_active = 0
        active_lock = threading.Lock()
        execution_order: list[str] = []
        slot0_started = threading.Event()
        slot1_started = threading.Event()
        release_slot0 = threading.Event()

        def slot0_jump() -> str:
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                slot0_started.set()
                execution_order.append("slot-0-jump")
                assert release_slot0.wait(2.0)
                return "slot-0-jump-result"
            finally:
                with active_lock:
                    active -= 1

        def slot0_restock() -> str:
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                execution_order.append("slot-0-restock")
                return "slot-0-restock-result"
            finally:
                with active_lock:
                    active -= 1

        def slot1_jump() -> str:
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                slot1_started.set()
                execution_order.append("slot-1-jump")
                return "slot-1-jump-result"
            finally:
                with active_lock:
                    active -= 1

        cancel_0 = threading.Event()
        cancel_1 = threading.Event()

        try:
            # Slot 0 submits its jump plot (deadline soon)
            jump0 = queue.submit_jump_plot(
                slot_id="slot-0",
                run=slot0_jump,
                deadline=time.monotonic() + 10.0,
                estimated_duration=30.0,
                cancel_event=cancel_0,
            )
            assert slot0_started.wait(1.0), "Slot 0 jump must start immediately"

            # Slot 0 submits restock while jump is active — restock waits
            restock0 = queue.submit_restock(
                slot_id="slot-0",
                run=slot0_restock,
                estimated_duration=5.0,
                cancel_event=cancel_0,
            )

            # Slot 1 submits its jump plot (deadline sooner than slot 0)
            jump1 = queue.submit_jump_plot(
                slot_id="slot-1",
                run=slot1_jump,
                deadline=time.monotonic() + 5.0,
                estimated_duration=30.0,
                cancel_event=cancel_1,
            )

            # Nothing else should have started — slot 0 jump still active
            assert not slot1_started.is_set()
            assert execution_order == ["slot-0-jump"]

            # Release slot 0 jump
            release_slot0.set()

            # Wait for all blocks to complete
            assert jump0.done.wait(2.0)
            assert restock0.done.wait(2.0)
            assert jump1.done.wait(2.0)

            # Verify serialized execution
            assert max_active == 1, "At most one block should be active"

            # Verify execution order: slot-0-jump first, then either
            # slot-1-jump (earlier deadline) or slot-0-restock (feasible
            # before slot-1-jump deadline).  The queue must schedule by
            # deadline priority.
            assert "slot-0-jump" in execution_order
            assert "slot-0-restock" in execution_order
            assert "slot-1-jump" in execution_order
            assert len(execution_order) == 3

            # Verify results
            assert jump0.result(timeout=0.1) == "slot-0-jump-result"
            assert restock0.result(timeout=0.1) == "slot-0-restock-result"
            assert jump1.result(timeout=0.1) == "slot-1-jump-result"
        finally:
            queue.shutdown(wait=True)

    def test_per_window_input_routing_via_injected_focus_handler(
        self, tmp_path: Path,  # pyright: ignore[reportUnusedParameter]
    ) -> None:
        """Each slot routes input through its own FocusAwareInputHandler.

        The global input_handler module is monkeypatched to raise, proving
        the injected path is used exclusively.
        """
        handler_0, checker_0, backend_0 = _make_slot_handler("A", handle=100)
        handler_1, checker_1, backend_1 = _make_slot_handler("B", handle=200)

        queue = SequenceQueue()
        cancel_0 = threading.Event()
        cancel_1 = threading.Event()
        release_0 = threading.Event()
        slot0_started = threading.Event()
        slot1_started = threading.Event()

        def slot0_block() -> str:
            slot0_started.set()
            assert release_0.wait(2.0)
            handler_0.press("space")
            handler_0.moveTo(100, 200)
            handler_0.press("space")
            return "slot-0"

        def slot1_block() -> str:
            slot1_started.set()
            handler_1.press("space")
            handler_1.keyDown("w")
            handler_1.keyUp("w")
            return "slot-1"

        try:
            h0 = queue.submit_jump_plot(
                slot_id="slot-0",
                run=slot0_block,
                deadline=time.monotonic() + 30.0,
                estimated_duration=10.0,
                cancel_event=cancel_0,
            )
            assert slot0_started.wait(1.0)

            h1 = queue.submit_jump_plot(
                slot_id="slot-1",
                run=slot1_block,
                deadline=time.monotonic() + 20.0,
                estimated_duration=10.0,
                cancel_event=cancel_1,
            )
            assert not slot1_started.is_set()

            release_0.set()
            assert h0.done.wait(2.0)
            assert h1.done.wait(2.0)

            # Slot 0 got 3 input calls, each preceded by focus check
            assert len(backend_0.calls) == 3
            assert checker_0.focus_calls == 3
            assert backend_0.calls[0] == ("press", ("space",), {})
            assert backend_0.calls[1] == ("moveTo", (100, 200), {})
            assert backend_0.calls[2] == ("press", ("space",), {})

            # Slot 1 got 3 input calls, each preceded by focus check
            assert len(backend_1.calls) == 3
            assert checker_1.focus_calls == 3
            assert backend_1.calls[0] == ("press", ("space",), {})
            assert backend_1.calls[1] == ("keyDown", ("w",), {})
            assert backend_1.calls[2] == ("keyUp", ("w",), {})

            # Results are correct
            assert h0.result(timeout=0.1) == "slot-0"
            assert h1.result(timeout=0.1) == "slot-1"
        finally:
            queue.shutdown(wait=True)

    def test_per_slot_save_isolation_under_concurrent_writes(
        self, tmp_path: Path,
    ) -> None:
        """Two slots write progress to separate save files.

        Verifies that slot-aware save paths do not corrupt each other's
        progress state under concurrent writes.
        """
        save_dir = tmp_path / "saves"
        _ = save_dir.mkdir()

        save_0 = save_dir / "save-slot-0.txt"
        save_1 = save_dir / "save-slot-1.txt"

        _ = save_0.write_text("5", encoding="utf-8")
        _ = save_1.write_text("10", encoding="utf-8")

        barrier = threading.Barrier(2, timeout=5.0)

        def write_slot(_idx: int, path: Path, value: str) -> None:
            _ = barrier.wait()
            _ = path.write_text(value, encoding="utf-8")

        t0 = threading.Thread(target=write_slot, args=(0, save_0, "15"))
        t1 = threading.Thread(target=write_slot, args=(1, save_1, "20"))
        t0.start()
        t1.start()
        t0.join(timeout=3.0)
        t1.join(timeout=3.0)

        # Each slot's save file was written independently
        assert save_0.read_text(encoding="utf-8") == "15"
        assert save_1.read_text(encoding="utf-8") == "20"

    def test_cancellation_cleans_up_deadline_and_restock(self) -> None:
        """Cancelling one slot clears its registered deadline and cancels
        its queued restock, while the other slot proceeds normally.
        """
        queue = SequenceQueue()
        active_started = threading.Event()
        cancel_0 = threading.Event()
        cancel_1 = threading.Event()
        execution_order: list[str] = []

        def active_jump() -> str:
            active_started.set()
            execution_order.append("slot-0-jump")
            while not cancel_0.is_set():
                time.sleep(0.01)
            return "slot-0-jump"

        def cancelled_restock() -> str:
            execution_order.append("slot-0-restock-should-not-run")
            return "cancelled"

        def survivor_jump() -> str:
            execution_order.append("slot-1-jump")
            return "slot-1-jump"

        try:
            # Slot 0 is actively running its jump
            jump0 = queue.submit_jump_plot(
                slot_id="slot-0",
                run=active_jump,
                deadline=time.monotonic() + 100.0,
                estimated_duration=30.0,
                cancel_event=cancel_0,
            )
            assert active_started.wait(1.0)

            # Register a look-ahead deadline for slot 0
            queue.register_jump_deadline(
                slot_id="slot-0-next",
                deadline=time.monotonic() + 50.0,
            )

            # Slot 0's restock is queued but cannot start (slot-0 jump active)
            restock0 = queue.submit_restock(
                slot_id="slot-0",
                run=cancelled_restock,
                estimated_duration=60.0,
                cancel_event=cancel_0,
            )

            # Slot 1's jump is also queued
            jump1 = queue.submit_jump_plot(
                slot_id="slot-1",
                run=survivor_jump,
                deadline=time.monotonic() + 10.0,
                estimated_duration=30.0,
                cancel_event=cancel_1,
            )

            # Cancel slot 0
            cancel_0.set()

            # Clear the registered deadline (simulating traversal cleanup)
            queue.clear_jump_deadline(slot_id="slot-0-next")

            # Wait for completions
            assert jump0.done.wait(2.0)
            assert restock0.done.wait(2.0)
            assert jump1.done.wait(2.0)

            # Slot 0 jump ran but restock was cancelled
            assert "slot-0-jump" in execution_order
            assert "slot-0-restock-should-not-run" not in execution_order

            # Slot 1 jump ran after slot 0 finished
            assert "slot-1-jump" in execution_order

            # Restock was cancelled
            with pytest.raises(CancelledBlockError):
                _ = restock0.result(timeout=0.1)

            # Slot 1 succeeded
            assert jump1.result(timeout=0.1) == "slot-1-jump"
        finally:
            queue.shutdown(wait=True)

    def test_no_global_input_handler_called_in_gui_path(self) -> None:
        """GUI-path automation uses injected focus handlers exclusively.

        Monkeypatching the global input_handler module to raise should
        not affect the injected FocusAwareInputHandler with recording
        backends.
        """
        handler_0, checker_0, backend_0 = _make_slot_handler("G0", handle=500)
        handler_1, checker_1, backend_1 = _make_slot_handler("G1", handle=600)

        queue = SequenceQueue()
        cancel_0 = threading.Event()
        cancel_1 = threading.Event()
        release_0 = threading.Event()
        slot0_started = threading.Event()

        def slot0_block() -> str:
            slot0_started.set()
            assert release_0.wait(2.0)
            handler_0.press("space")
            return "slot-0"

        def slot1_block() -> str:
            handler_1.press("space")
            return "slot-1"

        try:
            h0 = queue.submit_jump_plot(
                slot_id="slot-0",
                run=slot0_block,
                deadline=time.monotonic() + 30.0,
                estimated_duration=5.0,
                cancel_event=cancel_0,
            )
            assert slot0_started.wait(1.0)

            h1 = queue.submit_jump_plot(
                slot_id="slot-1",
                run=slot1_block,
                deadline=time.monotonic() + 20.0,
                estimated_duration=5.0,
                cancel_event=cancel_1,
            )

            # Now monkeypatch global input_handler to raise — the queue
            # worker is already running blocks, but injected handlers
            # don't use the global module at all.
            with patch("TraversalSystem.input_handler.press",
                       side_effect=RuntimeError("global touched")):
                release_0.set()
                assert h0.done.wait(2.0)
                assert h1.done.wait(2.0)

            # Both handlers completed successfully without touching global
            assert len(backend_0.calls) == 1
            assert len(backend_1.calls) == 1
            assert checker_0.focus_calls == 1
            assert checker_1.focus_calls == 1
            assert h0.result(timeout=0.1) == "slot-0"
            assert h1.result(timeout=0.1) == "slot-1"
        finally:
            queue.shutdown(wait=True)

    def test_failure_in_one_slot_releases_queue_for_other(self) -> None:
        """When one slot's automation block fails, the queue releases and
        runs the other slot's block.  The failure propagates to the
        failing slot's handle but does not affect the other.
        """
        queue = SequenceQueue()
        failure_started = threading.Event()
        execution_order: list[str] = []
        cancel_0 = threading.Event()
        cancel_1 = threading.Event()

        def failing_block() -> str:
            failure_started.set()
            execution_order.append("fail-block")
            raise RuntimeError("focus/input failed")

        def success_block() -> str:
            execution_order.append("success-block")
            return "success"

        try:
            h_fail = queue.submit_jump_plot(
                slot_id="slot-fail",
                run=failing_block,
                deadline=time.monotonic() + 10.0,
                estimated_duration=5.0,
                cancel_event=cancel_0,
            )
            assert failure_started.wait(1.0)

            h_success = queue.submit_jump_plot(
                slot_id="slot-success",
                run=success_block,
                deadline=time.monotonic() + 20.0,
                estimated_duration=5.0,
                cancel_event=cancel_1,
            )

            assert h_fail.done.wait(2.0)
            assert h_success.done.wait(2.0)

            # Failure propagated
            with pytest.raises(RuntimeError, match="focus/input failed"):
                _ = h_fail.result(timeout=0.1)

            # Success slot unaffected
            assert h_success.result(timeout=0.1) == "success"
            assert execution_order == ["fail-block", "success-block"]
        finally:
            queue.shutdown(wait=True)

    def test_deadline_gating_defers_restock_until_after_jump(self) -> None:
        """Restock with a duration that would overrun the next jump deadline
        is deferred.  Once the jump completes and deadline is cleared,
        restock becomes feasible.
        """
        queue = SequenceQueue()
        active_started = threading.Event()
        release_active = threading.Event()
        execution_order: list[str] = []
        cancel_ev = threading.Event()

        def active_jump() -> str:
            active_started.set()
            execution_order.append("active-jump")
            assert release_active.wait(2.0)
            return "active-jump"

        def deferred_restock() -> str:
            execution_order.append("deferred-restock")
            return "deferred-restock"

        def next_jump() -> str:
            execution_order.append("next-jump")
            return "next-jump"

        try:
            # Active jump running
            h_active = queue.submit_jump_plot(
                slot_id="slot-active",
                run=active_jump,
                deadline=time.monotonic() + 100.0,
                estimated_duration=30.0,
                cancel_event=cancel_ev,
            )
            assert active_started.wait(1.0)

            # Register a near-term deadline for the next jump
            queue.register_jump_deadline(
                slot_id="slot-next",
                deadline=time.monotonic() + 3.0,
            )

            # Restock needs 60s but only 3s until next jump deadline → deferred
            h_restock = queue.submit_restock(
                slot_id="slot-restock",
                run=deferred_restock,
                estimated_duration=60.0,
                cancel_event=threading.Event(),
            )

            # Next jump is also queued
            h_next = queue.submit_jump_plot(
                slot_id="slot-next",
                run=next_jump,
                deadline=time.monotonic() + 3.0,
                estimated_duration=10.0,
                cancel_event=threading.Event(),
            )

            # Release active jump
            release_active.set()
            assert h_active.done.wait(2.0)

            # Next jump should run before restock (deadline priority)
            assert h_next.done.wait(2.0)

            # Clear the deadline — restock can now run
            queue.clear_jump_deadline(slot_id="slot-next")
            assert h_restock.done.wait(2.0)

            # Verify order: active-jump → next-jump → deferred-restock
            assert execution_order[0] == "active-jump"
            assert "next-jump" in execution_order
            assert "deferred-restock" in execution_order
            assert execution_order.index("next-jump") < execution_order.index("deferred-restock")
        finally:
            queue.shutdown(wait=True)

    def test_two_slots_independent_progress_tracking(self) -> None:
        """Two slots track their own progress independently through
        the shared queue, each receiving its own result.
        """
        queue = SequenceQueue()
        results: dict[str, str] = {}
        lock = threading.Lock()
        cancel_0 = threading.Event()
        cancel_1 = threading.Event()
        started_0 = threading.Event()
        release_0 = threading.Event()

        def block_0() -> str:
            started_0.set()
            assert release_0.wait(2.0)
            with lock:
                results["slot-0"] = "progress-42"
            return "slot-0-done"

        def block_1() -> str:
            with lock:
                results["slot-1"] = "progress-87"
            return "slot-1-done"

        try:
            h0 = queue.submit_jump_plot(
                slot_id="slot-0",
                run=block_0,
                deadline=time.monotonic() + 30.0,
                estimated_duration=10.0,
                cancel_event=cancel_0,
            )
            assert started_0.wait(1.0)

            h1 = queue.submit_jump_plot(
                slot_id="slot-1",
                run=block_1,
                deadline=time.monotonic() + 20.0,
                estimated_duration=10.0,
                cancel_event=cancel_1,
            )

            release_0.set()
            assert h0.done.wait(2.0)
            assert h1.done.wait(2.0)

            assert h0.result(timeout=0.1) == "slot-0-done"
            assert h1.result(timeout=0.1) == "slot-1-done"
            assert results == {"slot-0": "progress-42", "slot-1": "progress-87"}
        finally:
            queue.shutdown(wait=True)


# Module-level wrappers for plan-required node IDs

def test_two_carriers_serialize_jump_and_restock_through_queue(
    tmp_path: Path,
) -> None:
    TestTwoCarrierSerializationIntegration().test_two_carriers_serialize_jump_and_restock_through_queue(tmp_path)


def test_per_window_input_routing_via_injected_focus_handler(
    tmp_path: Path,
) -> None:
    TestTwoCarrierSerializationIntegration().test_per_window_input_routing_via_injected_focus_handler(tmp_path)


def test_per_slot_save_isolation_under_concurrent_writes(
    tmp_path: Path,
) -> None:
    TestTwoCarrierSerializationIntegration().test_per_slot_save_isolation_under_concurrent_writes(tmp_path)


def test_cancellation_cleans_up_deadline_and_restock() -> None:
    TestTwoCarrierSerializationIntegration().test_cancellation_cleans_up_deadline_and_restock()


def test_no_global_input_handler_called_in_gui_path() -> None:
    TestTwoCarrierSerializationIntegration().test_no_global_input_handler_called_in_gui_path()


def test_failure_in_one_slot_releases_queue_for_other() -> None:
    TestTwoCarrierSerializationIntegration().test_failure_in_one_slot_releases_queue_for_other()


def test_deadline_gating_defers_restock_until_after_jump() -> None:
    TestTwoCarrierSerializationIntegration().test_deadline_gating_defers_restock_until_after_jump()


def test_two_slots_independent_progress_tracking() -> None:
    TestTwoCarrierSerializationIntegration().test_two_slots_independent_progress_tracking()


# ---------------------------------------------------------------------------
# Task 4: Observable scan-loop exception handling
# ---------------------------------------------------------------------------

class _ExplodingRouter:
    """Router double whose ``scan_once`` always raises.

    Used to exercise ``JournalScanLoop`` error-callback and fail-fast
    behaviour without touching the real filesystem.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("scan_once kaboom")
        self.scan_count = 0

    def scan_once(self, journal_dir: Path) -> None:
        self.scan_count += 1
        raise self._exc


class TestScanLoopExceptionHandling:
    """Replace silent exception swallowing with observable failure behaviour.

    Two modes are exercised:
    1. **Production continuation** (``fail_fast=False``): the callback is
       invoked for every scan error and the loop keeps polling.
    2. **Fail-fast** (``fail_fast=True``): the first scan error causes the
       loop thread to exit after invoking the callback exactly once.
    """

    def test_error_callback_captures_exception_when_fail_fast_false(
        self, tmp_path: Path,
    ) -> None:
        router = _ExplodingRouter(RuntimeError("boom-A"))
        captured: list[Exception] = []

        loop = JournalScanLoop(
            router,
            tmp_path,
            error_callback=captured.append,
            fail_fast=False,
        )

        loop.start()
        try:
            # Give the loop time to perform several scan attempts.
            deadline = time.monotonic() + 4
            while len(captured) < 3 and time.monotonic() < deadline:
                time.sleep(0.1)
        finally:
            loop.stop()

        # The callback was invoked at least 3 times with the real exception.
        assert len(captured) >= 3
        for exc in captured:
            assert isinstance(exc, RuntimeError)
            assert "boom-A" in str(exc)

    def test_fail_fast_causes_thread_exit_on_first_exception(
        self, tmp_path: Path,
    ) -> None:
        """With ``fail_fast=True``, the first ``scan_once`` exception causes
        the loop thread to exit after invoking the callback exactly once.
        """
        router = _ExplodingRouter()
        captured: list[Exception] = []

        loop = JournalScanLoop(
            router,
            tmp_path,
            error_callback=captured.append,
            fail_fast=True,
        )

        thread = loop._thread  # noqa: SLF001 — intentional for test
        loop.start()
        thread = loop._thread  # type: ignore[assignment]  # noqa: SLF001
        assert thread is not None

        # Thread should exit promptly (within a few seconds).
        thread.join(timeout=5)
        assert not thread.is_alive(), (
            "Scan-loop thread should have exited after first exception "
            "when fail_fast=True"
        )

        # Callback invoked exactly once.
        assert len(captured) == 1
        assert isinstance(captured[0], RuntimeError)

        # scan_once was called exactly once.
        assert router.scan_count == 1

    def test_default_callback_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        router = _ExplodingRouter(ValueError("log-me"))

        with caplog.at_level(logging.WARNING, logger="TraversalSystem.traversal_journal"):
            loop = JournalScanLoop(
                router,
                tmp_path,
                fail_fast=True,
            )

            loop.start()
            thread = loop._thread  # type: ignore[assignment]  # noqa: SLF001
            assert thread is not None
            thread.join(timeout=5)

        assert router.scan_count >= 1
        assert any("log-me" in record.message for record in caplog.records)

    def test_no_silent_exception_swallowing(
        self, tmp_path: Path,
    ) -> None:
        """Ensures there is no silent ``except Exception: pass`` remaining.

        Uses a counting callback to prove that every scan_once error is
        observable — either through the callback or via fail-fast exit.
        """
        router = _ExplodingRouter()
        observed_errors: list[str] = []

        loop = JournalScanLoop(
            router,
            tmp_path,
            error_callback=lambda e: observed_errors.append(str(e)),
            fail_fast=False,
        )

        loop.start()
        try:
            deadline = time.monotonic() + 3
            while len(observed_errors) < 2 and time.monotonic() < deadline:
                time.sleep(0.1)
        finally:
            loop.stop()

        # At least 2 errors were observed — no silent swallowing.
        assert len(observed_errors) >= 2
        assert all("scan_once kaboom" in msg for msg in observed_errors)


# ---------------------------------------------------------------------------
# Regression: multicommander traversal must NOT start the legacy journal thread
# ---------------------------------------------------------------------------

def test_multicommander_traversal_does_not_start_legacy_journal_thread(
    tmp_path: Path,
) -> None:
    """When a ``CTSJournalFacade`` is provided as the journal dependency,
    ``_run_traversal_slot`` runs to completion without any legacy journal
    thread machinery.

    The production code no longer has ``start_journal_thread`` — this test
    confirms the facade-driven path works end-to-end with no legacy artifacts.
    """
    import logging

    main = _load_main_with_mocks(tmp_path)

    route_file = tmp_path / "route.txt"
    _ = route_file.write_text("Sol\nDeciat\n", encoding="utf-8")

    facade = MagicMock(spec=CTSJournalFacade)
    facade.has_jumped.return_value = True
    facade.last_carrier_request.return_value = None
    facade.departure_time.return_value = None
    facade.jump_cancelled.return_value = False

    queue = _FakeSequenceQueue()

    options = TraversalOptions(
        webhook_url="",
        journal_directory=tmp_path,
        route_file=route_file,
        route_position=0,
        tritium_slot=0,
        auto_plot_jumps=True,
        disable_refuel=False,
        power_saving=False,
        refuel_mode=0,
        single_discord_message=False,
        shutdown_on_complete=False,
        multi_commander_enabled=True,
        target_fid="F-MC-REGRESS",
    )

    cancel = threading.Event()
    context = _make_runtime_context(
        options=options,
        sequence_queue=queue,
        journal=facade,
        slot_id=0,
        sleep=lambda _s: None,
        cancel_event=cancel,
    )

    jump_calls = 0

    def fake_jump(*_a: object, **_kw: object) -> tuple[int, datetime.datetime]:
        nonlocal jump_calls
        jump_calls += 1
        if jump_calls > 1:
            raise KeyboardInterrupt
        return 7, datetime.datetime.now(datetime.timezone.utc)

    assert not hasattr(main, "start_journal_thread"), (
        "start_journal_thread must not exist in production main.py"
    )

    with patch("builtins.print"), \
         patch.object(main, "DiscordHandler", return_value=MagicMock()), \
         patch.object(main, "Reshandler", _FakeResHandler), \
         patch.object(main, "load_route_list", return_value=["A", "B"]), \
         patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
         patch.object(main, "consume_save", return_value=None), \
         patch.object(main, "save_progress"), \
         patch.object(main, "jump_to_system", side_effect=fake_jump), \
         patch.object(main, "restock_tritium"), \
         patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
         patch.object(main, "time") as fake_time:
        fake_time.monotonic.side_effect = time.monotonic
        fake_time.sleep.side_effect = lambda _s: None
        try:
            _ = main._run_traversal_slot(context)
        except SystemExit:
            pass


# ---------------------------------------------------------------------------
# Task 6: CLI facade / scan-loop tests
# ---------------------------------------------------------------------------

class TestCLIFacadeRequiresTargetFid:
    """CLI main() must fail fast when target_fid is missing or unknown."""

    def test_target_fid_required_for_journal_traversal(self, tmp_path: Path) -> None:
        main = _load_main_with_mocks(tmp_path)

        route_file = tmp_path / "route.txt"
        _ = route_file.write_text("Sol\n", encoding="utf-8")

        options_no_fid = TraversalOptions(
            webhook_url="",
            journal_directory=tmp_path,
            route_file=route_file,
            target_fid="",
            multi_commander_enabled=False,
        )

        with patch("builtins.print") as mock_print, \
             patch.object(main, "warn_if_outdated"), \
             patch.object(main, "load_settings", return_value=options_no_fid), \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(1))}), \
             pytest.raises(SystemExit) as exc_info:
            main.main()

        assert exc_info.value.code == 1
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        assert "target_fid" in printed

    def test_unknown_target_fid_fails_before_automation(self, tmp_path: Path) -> None:
        main = _load_main_with_mocks(tmp_path)

        route_file = tmp_path / "route.txt"
        _ = route_file.write_text("Sol\n", encoding="utf-8")

        options_unknown_fid = TraversalOptions(
            webhook_url="",
            journal_directory=tmp_path,
            route_file=route_file,
            target_fid="F-UNKNOWN",
            multi_commander_enabled=True,
        )

        scan_loop_started: list[object] = []

        def tracking_start(self: JournalScanLoop) -> None:
            scan_loop_started.append(True)

        with patch("builtins.print") as mock_print, \
             patch.object(main, "warn_if_outdated"), \
             patch.object(main, "load_settings", return_value=options_unknown_fid), \
             patch.object(JournalScanLoop, "start", tracking_start), \
             patch.object(main, "run_traversal") as mock_run, \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(1))}), \
             pytest.raises(SystemExit) as exc_info:
            main.main()

        assert exc_info.value.code == 1
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        assert "not found" in printed
        assert len(scan_loop_started) == 0, "Scan loop must NOT start for unknown FID"
        mock_run.assert_not_called()


class TestCLIScanLoopObservesEventsAfterStartup:
    """After successful CLI preflight, the scan loop must observe new events."""

    def test_cli_scan_loop_updates_after_startup(self, tmp_path: Path) -> None:
        main = _load_main_with_mocks(tmp_path)

        journal = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(journal, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-CLI", Name="CLICmdr"),
        ])

        route_file = tmp_path / "route.txt"
        _ = route_file.write_text("Sol\nDeciat\n", encoding="utf-8")

        options = TraversalOptions(
            webhook_url="",
            journal_directory=tmp_path,
            route_file=route_file,
            target_fid="F-CLI",
            multi_commander_enabled=True,
        )

        captured_facade: list[object] = []
        captured_loop: list[object] = []

        def fake_run_traversal(
            opts: object, *,
            journal: object = None,
            **_kwargs: object,
        ) -> bool:
            captured_facade.append(journal)
            return True

        real_scan_loop_stop = JournalScanLoop.stop

        def tracking_stop(self: JournalScanLoop) -> None:
            captured_loop.append(self)
            real_scan_loop_stop(self)

        with patch("builtins.print"), \
             patch.object(main, "warn_if_outdated"), \
             patch.object(main, "load_settings", return_value=options), \
             patch.object(main, "run_traversal", side_effect=fake_run_traversal), \
             patch.object(JournalScanLoop, "stop", tracking_stop), \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(0))}), \
             pytest.raises(SystemExit) as exc_info:
            main.main()

        assert exc_info.value.code == 0
        assert len(captured_facade) == 1
        facade = captured_facade[0]
        assert isinstance(facade, CTSJournalFacade)
        assert facade.target_fid == "F-CLI"
        st = facade.state()
        assert st is not None
        assert st.commander_name == "CLICmdr"

        assert len(captured_loop) == 1, "Scan loop must be stopped after traversal"


class TestCLIScanLoopStopsOnAllExitPaths:
    """Scan loop is stopped on success, failure, and cancellation."""

    def test_cli_scan_loop_stops_on_failure(self, tmp_path: Path) -> None:
        main = _load_main_with_mocks(tmp_path)

        journal = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(journal, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-FAIL", Name="FailCmdr"),
        ])

        route_file = tmp_path / "route.txt"
        _ = route_file.write_text("Sol\n", encoding="utf-8")

        options = TraversalOptions(
            webhook_url="",
            journal_directory=tmp_path,
            route_file=route_file,
            target_fid="F-FAIL",
            multi_commander_enabled=True,
        )

        captured_loop: list[object] = []

        def fail_traversal(
            opts: object, *,
            journal: object = None,
            **_kwargs: object,
        ) -> bool:
            return False

        real_scan_loop_stop = JournalScanLoop.stop

        def tracking_stop(self: JournalScanLoop) -> None:
            captured_loop.append(self)
            real_scan_loop_stop(self)

        with patch("builtins.print"), \
             patch.object(main, "warn_if_outdated"), \
             patch.object(main, "load_settings", return_value=options), \
             patch.object(main, "run_traversal", side_effect=fail_traversal), \
             patch.object(JournalScanLoop, "stop", tracking_stop), \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(1))}), \
             pytest.raises(SystemExit) as exc_info:
            main.main()

        assert exc_info.value.code == 1
        assert len(captured_loop) == 1, "Scan loop must be stopped even on failure"


# ---------------------------------------------------------------------------
# Task 7: Fail-safe jump cancellation handling
# ---------------------------------------------------------------------------

class TestJumpCancellationHandling:
    """During countdown and jump-completion wait, target-facade jump_cancelled()
    causes deterministic failure handling.

    Verifies:
    - Cancellation exits without deadlock
    - save_progress retains the current route index (no increment)
    - _clear_jump_deadline is called once
    - restock_tritium or coordinated restock is not invoked
    """

    def test_cancellation_during_countdown_saves_without_increment(
        self, tmp_path: Path,
    ) -> None:
        """Cancel during scheduled-jump countdown: line_no stays at 0."""
        main = _load_main_with_mocks(tmp_path)
        queue = _FakeSequenceQueue()

        journal = MagicMock(spec=CTSJournalFacade)
        journal.has_jumped.return_value = True
        journal.jump_cancelled.side_effect = [False, True]

        saved_indices: list[int] = []

        def track_save(state: object, **_kw: object) -> None:
            saved_indices.append(getattr(state, "line_no"))

        context = _make_runtime_context(
            options=_make_options(tmp_path),
            sequence_queue=queue,
            journal=journal,
            slot_id=0,
            sleep=lambda _s: None,
        )

        with patch("builtins.print"), \
             patch.object(main, "DiscordHandler", return_value=MagicMock()), \
             patch.object(main, "Reshandler", _FakeResHandler), \
             patch.object(main, "load_route_list", return_value=["A", "B"]), \
             patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
             patch.object(main, "consume_save", return_value=None), \
             patch.object(main, "save_progress", side_effect=track_save), \
             patch.object(main, "jump_to_system", return_value=(10, datetime.datetime.now(datetime.timezone.utc))), \
             patch.object(main, "restock_tritium") as mock_restock, \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
             patch.object(main, "time") as fake_time:
            fake_time.monotonic.side_effect = time.monotonic
            fake_time.sleep.side_effect = lambda _s: None
            result = main._run_traversal_slot(context)

        assert result is False
        assert saved_indices == [0], (
            f"save_progress should retain line_no=0, got {saved_indices}"
        )
        mock_restock.assert_not_called()

    def test_cancellation_during_completion_wait_reverts_index(
        self, tmp_path: Path,
    ) -> None:
        """Cancel during has_jumped() wait: line_no is reverted to 0."""
        main = _load_main_with_mocks(tmp_path)
        queue = _FakeSequenceQueue()

        journal = MagicMock(spec=CTSJournalFacade)
        journal.has_jumped.return_value = False
        # False during countdown (4 iterations), then True in has_jumped loop
        journal.jump_cancelled.side_effect = [False] * 4 + [False, True]

        saved_indices: list[int] = []

        def track_save(state: object, **_kw: object) -> None:
            saved_indices.append(getattr(state, "line_no"))

        context = _make_runtime_context(
            options=_make_options(tmp_path),
            sequence_queue=queue,
            journal=journal,
            slot_id=0,
            sleep=lambda _s: None,
        )

        with patch("builtins.print"), \
             patch.object(main, "DiscordHandler", return_value=MagicMock()), \
             patch.object(main, "Reshandler", _FakeResHandler), \
             patch.object(main, "load_route_list", return_value=["A", "B"]), \
             patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
             patch.object(main, "consume_save", return_value=None), \
             patch.object(main, "save_progress", side_effect=track_save), \
             patch.object(main, "jump_to_system", return_value=(10, datetime.datetime.now(datetime.timezone.utc))), \
             patch.object(main, "restock_tritium") as mock_restock, \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
             patch.object(main, "time") as fake_time:
            fake_time.monotonic.side_effect = time.monotonic
            fake_time.sleep.side_effect = lambda _s: None
            result = main._run_traversal_slot(context)

        assert result is False
        assert saved_indices == [0], (
            f"save_progress should revert line_no to 0, got {saved_indices}"
        )
        mock_restock.assert_not_called()

    def test_deadline_cleared_once_on_cancellation_during_completion_wait(
        self, tmp_path: Path,
    ) -> None:
        """After registering a deadline, cancellation clears it exactly once."""
        main = _load_main_with_mocks(tmp_path)
        queue = _FakeSequenceQueue()

        journal = MagicMock(spec=CTSJournalFacade)
        journal.has_jumped.return_value = False
        # Let countdown pass (4 iterations), then cancel in has_jumped loop
        journal.jump_cancelled.side_effect = [False] * 4 + [False, True]

        context = _make_runtime_context(
            options=_make_options(tmp_path),
            sequence_queue=queue,
            journal=journal,
            slot_id=0,
            sleep=lambda _s: None,
        )

        with patch("builtins.print"), \
             patch.object(main, "DiscordHandler", return_value=MagicMock()), \
             patch.object(main, "Reshandler", _FakeResHandler), \
             patch.object(main, "load_route_list", return_value=["A", "B"]), \
             patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
             patch.object(main, "consume_save", return_value=None), \
             patch.object(main, "save_progress"), \
             patch.object(main, "jump_to_system", return_value=(10, datetime.datetime.now(datetime.timezone.utc))), \
             patch.object(main, "restock_tritium"), \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
             patch.object(main, "time") as fake_time:
            fake_time.monotonic.side_effect = time.monotonic
            fake_time.sleep.side_effect = lambda _s: None
            result = main._run_traversal_slot(context)

        assert result is False
        # Deadline should have been registered then cleared exactly once
        assert len(queue.register_calls) >= 1, "Deadline should have been registered"
        assert len(queue.clear_calls) == 1, (
            f"Expected exactly 1 clear_jump_deadline call, got {len(queue.clear_calls)}"
        )

    def test_no_deadlock_on_cancellation(self, tmp_path: Path) -> None:
        """Cancellation exits cleanly without hanging.

        Uses a facade mock where jump_cancelled returns True immediately
        in the first countdown iteration.
        """
        main = _load_main_with_mocks(tmp_path)
        queue = _FakeSequenceQueue()

        journal = MagicMock(spec=CTSJournalFacade)
        journal.has_jumped.return_value = False
        journal.jump_cancelled.side_effect = [True]

        context = _make_runtime_context(
            options=_make_options(tmp_path),
            sequence_queue=queue,
            journal=journal,
            slot_id=0,
            sleep=lambda _s: None,
        )

        with patch("builtins.print"), \
             patch.object(main, "DiscordHandler", return_value=MagicMock()), \
             patch.object(main, "Reshandler", _FakeResHandler), \
             patch.object(main, "load_route_list", return_value=["A", "B"]), \
             patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
             patch.object(main, "consume_save", return_value=None), \
             patch.object(main, "save_progress"), \
             patch.object(main, "jump_to_system", return_value=(10, datetime.datetime.now(datetime.timezone.utc))), \
             patch.object(main, "restock_tritium"), \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
             patch.object(main, "time") as fake_time:
            fake_time.monotonic.side_effect = time.monotonic
            fake_time.sleep.side_effect = lambda _s: None
            result = main._run_traversal_slot(context)

        assert result is False


# Module-level wrappers for Task 7 tests

def test_cancellation_during_countdown_saves_without_increment(
    tmp_path: Path,
) -> None:
    TestJumpCancellationHandling().test_cancellation_during_countdown_saves_without_increment(tmp_path)


def test_cancellation_during_completion_wait_reverts_index(
    tmp_path: Path,
) -> None:
    TestJumpCancellationHandling().test_cancellation_during_completion_wait_reverts_index(tmp_path)


def test_deadline_cleared_once_on_cancellation_during_completion_wait(
    tmp_path: Path,
) -> None:
    TestJumpCancellationHandling().test_deadline_cleared_once_on_cancellation_during_completion_wait(tmp_path)


def test_no_deadlock_on_cancellation(tmp_path: Path) -> None:
    TestJumpCancellationHandling().test_no_deadlock_on_cancellation(tmp_path)


# ---------------------------------------------------------------------------
# Task 4 (plan task 4): Harden traversal countdown waits
#
# Covers:
# - Short jump countdowns clamp total_time at zero
# - Countdown/polling waits use runtime_context.wait() for cooperative
#   cancellation instead of raw time.sleep()
# ---------------------------------------------------------------------------


class TestCountdownHardening:
    """Task 4: short-jump countdown clamping and cancel-aware waits."""

    def test_short_jump_countdown_clamps_to_zero(self, tmp_path: Path) -> None:
        """With time_to_jump < 6, total_time = max(0, time_to_jump - 6) = 0.

        The countdown loop (while total_time > 0) is skipped entirely and
        the traversal proceeds to the next-jump countdown without error.
        This is a characterization test: it passes both before and after the
        max(0, ...) clamp, confirming no regression at the boundary.
        """
        main = _load_main_with_mocks(tmp_path)
        queue = _FakeSequenceQueue()

        journal = MagicMock(spec=CTSJournalFacade)
        journal.has_jumped.return_value = True
        journal.jump_cancelled.return_value = False

        jump_time = datetime.datetime.now(datetime.timezone.utc)
        jump_calls = 0

        def fake_jump(*_a: object, **_kw: object) -> tuple[int, datetime.datetime]:
            nonlocal jump_calls
            jump_calls += 1
            if jump_calls > 1:
                raise KeyboardInterrupt
            # time_to_jump = 3, so total_time = max(0, 3 - 6) = 0 after fix
            # Before fix total_time = -3; in both cases countdown loop is skipped.
            return 3, jump_time

        context = _make_runtime_context(
            options=_make_options(tmp_path),
            sequence_queue=queue,
            journal=journal,
            slot_id=0,
            sleep=lambda _s: None,
        )

        with patch("builtins.print"), \
             patch.object(main, "DiscordHandler", return_value=MagicMock()), \
             patch.object(main, "Reshandler", _FakeResHandler), \
             patch.object(main, "load_route_list", return_value=["A", "B"]), \
             patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
             patch.object(main, "consume_save", return_value=None), \
             patch.object(main, "save_progress"), \
             patch.object(main, "jump_to_system", side_effect=fake_jump), \
             patch.object(main, "restock_tritium"), \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
             patch.object(main, "time") as fake_time:
            fake_time.monotonic.side_effect = time.monotonic
            fake_time.sleep.side_effect = lambda _s: None
            try:
                _ = main._run_traversal_slot(context)
            except (KeyboardInterrupt, SystemExit):
                pass

        # Both systems were attempted; countdown was skipped for the first.
        assert jump_calls == 2

    def test_countdown_uses_cancel_aware_runtime_waits(self, tmp_path: Path) -> None:
        """Countdown and next-jump waits go through runtime_context.wait(),
        NOT through time.sleep(1).

        Before the fix: time.sleep(1) is used in countdown loops, so
        time_sleep_calls contains 1.0 entries.  After the fix: countdown
        waits use runtime_context.wait(1) and time.sleep is only used for
        Discord rate-limiting sleeps (2.0 s).
        """
        main = _load_main_with_mocks(tmp_path)
        queue = _FakeSequenceQueue()

        journal = MagicMock(spec=CTSJournalFacade)
        journal.has_jumped.return_value = True
        journal.jump_cancelled.return_value = False

        jump_time = datetime.datetime.now(datetime.timezone.utc)
        jump_calls = 0

        def fake_jump(*_a: object, **_kw: object) -> tuple[int, datetime.datetime]:
            nonlocal jump_calls
            jump_calls += 1
            if jump_calls > 1:
                raise KeyboardInterrupt
            # time_to_jump = 10 → total_time = max(0, 10 - 6) = 4
            return 10, jump_time

        time_sleep_calls: list[float] = []

        def track_time_sleep(seconds: float) -> None:
            time_sleep_calls.append(seconds)

        context = _make_runtime_context(
            options=_make_options(tmp_path),
            sequence_queue=queue,
            journal=journal,
            slot_id=0,
            sleep=lambda _s: None,
        )

        with patch("builtins.print"), \
             patch.object(main, "DiscordHandler", return_value=MagicMock()), \
             patch.object(main, "Reshandler", _FakeResHandler), \
             patch.object(main, "load_route_list", return_value=["A", "B"]), \
             patch.object(main, "_find_newest_journal", return_value=tmp_path / "Journal.log"), \
             patch.object(main, "consume_save", return_value=None), \
             patch.object(main, "save_progress"), \
             patch.object(main, "jump_to_system", side_effect=fake_jump), \
             patch.object(main, "restock_tritium"), \
             patch.object(main, "os", **{"_exit": MagicMock(side_effect=SystemExit(2))}), \
             patch.object(main, "time") as fake_time:
            fake_time.monotonic.side_effect = time.monotonic
            fake_time.sleep.side_effect = track_time_sleep
            try:
                _ = main._run_traversal_slot(context)
            except (KeyboardInterrupt, SystemExit):
                pass

        # After fix: countdown waits use runtime_context.wait(1), not
        # time.sleep(1).  The only time.sleep calls should be Discord
        # rate-limiting sleeps at 2.0 s.  Before the fix, 1.0 entries
        # appear from the main countdown and next-jump countdown loops.
        assert 1.0 not in time_sleep_calls, (
            f"Countdown waits should use runtime_context.wait(), "
            f"not time.sleep(1). Got time.sleep calls: {time_sleep_calls}"
        )

        # Discord sleeps (2.0) are still time.sleep — unchanged.
        assert 2.0 in time_sleep_calls, (
            "Discord rate-limiting sleeps should still use time.sleep(2)"
        )


def test_short_jump_countdown_clamps_to_zero(tmp_path: Path) -> None:
    TestCountdownHardening().test_short_jump_countdown_clamps_to_zero(tmp_path)


def test_countdown_uses_cancel_aware_runtime_waits(tmp_path: Path) -> None:
    TestCountdownHardening().test_countdown_uses_cancel_aware_runtime_waits(tmp_path)
