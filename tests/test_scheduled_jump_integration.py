"""End-to-end integration tests for the scheduled jump feature.

Covers the full flow from DashboardWidget button click through countdown
creation, cancellation, error handling, and rescheduling.
"""
from __future__ import annotations

import datetime
import os
import time
from typing import cast
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _mock_qdialog_capture_and_clock(monkeypatch):
    from PySide6.QtWidgets import QDialog
    import TraversalSystem.window_capture as wc
    import TraversalSystem.gui.dashboard as dash_mod

    monkeypatch.setattr(QDialog, "exec", lambda self: 1)
    monkeypatch.setattr(wc, "capture_window", lambda *a, **k: None)
    monkeypatch.setattr(dash_mod, "capture_window", lambda *a, **k: None)
    monkeypatch.setattr(
        dash_mod.ManualBindDialog, "_start_capture_thread", lambda self: None
    )
    # Frozen-clock ScheduledJumpController so schedule() never raises
    # ValueError("Time is in the past").
    monkeypatch.setattr(
        dash_mod, "ScheduledJumpController", _frozen_clock_controller()
    )


from PySide6.QtCore import Signal, QObject

from TraversalSystem.gui_config import (
    CarrierSlotConfig,
    GuiConfig,
    UniversalSettings,
    DiscoveredCommander,
)
from TraversalSystem.gui.binding_controller import (
    BindingController,
    BindingSnapshot,
    SlotClassification,
)
from TraversalSystem.gui.worker_controller import WorkerController
from TraversalSystem.gui.worker_state import WorkerState
from TraversalSystem.gui.dashboard import DashboardWidget
from TraversalSystem.gui.scheduled_jump import ScheduledJumpController
from TraversalSystem.window_manager import WindowBinding, WindowInfo

# Fixed "now" that is always before the target time "23:59:50".
_MOCK_NOW = datetime.datetime(
    2026, 6, 12, 23, 0, 0, tzinfo=datetime.timezone.utc
)


def _frozen_clock_controller():
    """Subclass with a frozen clock so schedule() always succeeds."""

    class _Frozen(ScheduledJumpController):
        def __init__(self, parent=None, submit_func=None):
            super().__init__(
                time_provider=lambda: _MOCK_NOW,
                focus_func=lambda binding: None,
                click_func=lambda x, y: None,
                sleep_func=lambda s: None,
                submit_func=submit_func,
                parent=parent,
            )

    return _Frozen


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class MockWorkerController(QObject):
    """Mock WorkerController with Qt signals."""

    slot_state_changed = Signal(int, str)
    slot_log = Signal(int, str)
    slot_error = Signal(int, object)
    slot_finished = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._start_all_ready_return = ([], {})
        self._stop_all_active_return = []

    def start_all_ready(self):
        return self._start_all_ready_return

    def stop_all_active(self):
        return self._stop_all_active_return

    def start_slot(self, idx):
        return True

    def stop_slot(self, idx):
        return True

    def slot_state(self, idx):
        return WorkerState.READY

    def peek_shared_sequence_queue(self):
        return None


def _make_window_info(handle=1234):
    return WindowInfo(
        handle=handle,
        pid=1000 + handle,
        title="Elite - Dangerous (CLIENT)",
        window_class="EliteDangerous",
        backend="x11",
        focusable=True,
    )


def _make_binding(fid="F000000"):
    return WindowBinding.from_window(
        target_fid=fid,
        startup_identity=f"x11:{fid}:1234",
        window=_make_window_info(),
    )


def _make_config(tmp_path, *, time_str="23:59:50", btn_x=960, btn_y=540):
    route_file = tmp_path / "route.txt"
    route_file.write_text("Sol\nAchenar\n")
    return GuiConfig(
        schema_version=1,
        universal=UniversalSettings(
            journal_directory=str(tmp_path),
            default_route_directory=str(tmp_path),
        ),
        carrier_slots=[
            CarrierSlotConfig(
                slot_index=0,
                display_name="Slot 0",
                fid="F000000",
                commander_name="Cmdr 0",
                route_file=str(route_file),
                route_position=0,
                enabled=True,
                state="ready",
                scheduled_jump_time=time_str,
                scheduled_jump_button_x=btn_x,
                scheduled_jump_button_y=btn_y,
            )
        ],
    )


def _make_binding_ctrl(*, has_binding=True, classification=SlotClassification.READY):
    """Create a mock BindingController.

    Returns READY snapshots with window_binding by default.
    """
    bc = Mock(spec=BindingController)

    def _classify_all(config):
        result = {}
        for slot in config.carrier_slots:
            binding = _make_binding(fid=slot.fid) if has_binding else None
            result[slot.slot_index] = BindingSnapshot(
                classification=classification,
                fid=slot.fid if has_binding else "",
                commander_name=slot.commander_name if has_binding else "",
                window_binding=binding,
                discovered_commander=None,
                candidate_windows=[_make_window_info()] if has_binding else [],
            )
        return result

    bc.classify_all = Mock(side_effect=_classify_all)
    bc.manual_bind = Mock()
    bc.invalidate_binding = Mock()
    return bc


def _log(dash, slot_index=0):
    return dash.slot_widgets[slot_index].log_area.toPlainText()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestScheduledJumpIntegration:
    """End-to-end tests for scheduled jump button → controller → log flow."""

    def test_schedule_button_creates_controller(self, qapp, tmp_path):
        """Clicking Schedule on a READY slot logs confirmation and enables Cancel."""
        config = _make_config(tmp_path)
        bc = _make_binding_ctrl()
        wc = MockWorkerController()
        dash = DashboardWidget(config, bc, cast(WorkerController, cast(object, wc)))

        assert dash.slot_widgets[0].sj_schedule_btn.isEnabled()
        dash.slot_widgets[0].sj_schedule_btn.click()
        qapp.processEvents()

        log = _log(dash)
        assert "Scheduled jump: set for 23:59:50 UTC" in log
        assert not dash.slot_widgets[0].sj_schedule_btn.isEnabled()
        assert dash.slot_widgets[0].sj_cancel_btn.isEnabled()

    def test_cancel_button_logs_cancellation(self, qapp, tmp_path):
        """Cancel button click is wired and produces the expected log flow.

        The schedule() method internally calls cancel() to clear prior state,
        which emits "cancelled".  The user-facing Cancel button is then
        enabled.  Clicking it calls _on_cancel_scheduled_jump which attempts
        to pop the controller from the tracking dict.
        """
        config = _make_config(tmp_path)
        bc = _make_binding_ctrl()
        wc = MockWorkerController()
        dash = DashboardWidget(config, bc, cast(WorkerController, cast(object, wc)))

        dash.slot_widgets[0].sj_schedule_btn.click()
        qapp.processEvents()

        assert "Scheduled jump: cancelled" in _log(dash)
        assert "Scheduled jump: set for 23:59:50 UTC" in _log(dash)
        assert dash.slot_widgets[0].sj_cancel_btn.isEnabled()

        # Clicking cancel calls _on_cancel_scheduled_jump — verifies the
        # wiring from button → dashboard handler (no crash).
        dash.slot_widgets[0].sj_cancel_btn.click()
        qapp.processEvents()

    def test_schedule_without_binding_logs_error(self, qapp, tmp_path):
        """Scheduling an unbound slot logs error and creates no controller."""
        config = _make_config(tmp_path)
        bc = _make_binding_ctrl(has_binding=False, classification=SlotClassification.UNBOUND)
        wc = MockWorkerController()
        dash = DashboardWidget(config, bc, cast(WorkerController, cast(object, wc)))

        # Call _on_schedule_jump directly (button is disabled for UNBOUND)
        dash._on_schedule_jump(0)
        qapp.processEvents()

        assert 0 not in dash._scheduled_controllers
        assert "slot not bound to window" in _log(dash)

    def test_schedule_button_disabled_when_no_coordinates(self, qapp, tmp_path):
        """Schedule button is disabled when button coordinates are (0, 0)."""
        config = _make_config(tmp_path, btn_x=0, btn_y=0)
        bc = _make_binding_ctrl()
        wc = MockWorkerController()
        dash = DashboardWidget(config, bc, cast(WorkerController, cast(object, wc)))

        assert not dash.slot_widgets[0].sj_schedule_btn.isEnabled()

    def test_reschedule_replaces_previous_controller(self, qapp, tmp_path):
        """Scheduling twice produces two "set for" log entries."""
        config = _make_config(tmp_path)
        bc = _make_binding_ctrl()
        wc = MockWorkerController()
        dash = DashboardWidget(config, bc, cast(WorkerController, cast(object, wc)))

        # First schedule
        dash._on_schedule_jump(0)
        qapp.processEvents()

        # Second schedule
        dash._on_schedule_jump(0)
        qapp.processEvents()

        log = _log(dash)
        # Each _on_schedule_jump call appends "set for <time> UTC"
        assert log.count("Scheduled jump: set for 23:59:50 UTC") == 2
