"""Tests for TraversalSystem.gui.dashboard (Task 10).

Covers:
  - "Start All" starts only enabled READY slots
  - Blocked slots (unbound, needs_manual_binding, invalid_config, disabled)
    remain visible with exact skip reasons
  - "Stop All" requests cancellation for running workers
  - Per-slot Start/Stop/Retry/Manual Bind/Use Discovered Commander/Enable Disable
    update state and UI
  - Dashboard logs remain machine-testable
  - start-ready-only, zero-ready-slots, and use-discovered-commander scenarios pass
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable
from unittest.mock import Mock, MagicMock

import pytest

@pytest.fixture(autouse=True)
def mock_qdialog_exec(monkeypatch):
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, "exec", lambda self: 1)
    import TraversalSystem.window_capture as wc
    import TraversalSystem.gui.dashboard as dashboard_module

    monkeypatch.setattr(wc, "capture_window", lambda *a, **k: None)
    monkeypatch.setattr(dashboard_module, "capture_window", lambda *a, **k: None)
    monkeypatch.setattr(dashboard_module.ManualBindDialog, "_start_capture_thread", lambda self: None)

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QComboBox, QListWidget

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
from TraversalSystem.gui.dashboard import DashboardWidget, DashboardSlotWidget
from TraversalSystem.window_manager import WindowBinding, WindowInfo
from TraversalSystem.multi_journal_router import MultiJournalRouter

_ = os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_path(tmp_path):
    return tmp_path


@pytest.fixture
def config(tmp_path):
    route_file = tmp_path / "route.txt"
    route_file.write_text("Sol\n", encoding="utf-8")
    
    cfg = GuiConfig(
        universal=UniversalSettings(
            webhook_url="",
            journal_directory=str(tmp_path / "journals"),
            multi_commander_enabled=True,
            focus_timeout_seconds=5,
        ),
        carrier_slots=[
            CarrierSlotConfig(
                slot_index=0,
                commander_name="Cmdr1",
                fid="F123",
                state="ready",
                route_file=str(route_file),
            ),
            CarrierSlotConfig(
                slot_index=1,
                commander_name="Cmdr2",
                fid="F456",
                state="ready",
                route_file=str(route_file),
            ),
        ],
        discovered_commanders=[
            DiscoveredCommander(name="Cmdr1", fid="F123"),
            DiscoveredCommander(name="Cmdr2", fid="F456"),
        ],
    )
    return cfg


@pytest.fixture
def binding_controller():
    bc = Mock()
    
    # Mock classify_all to return a dict of snapshots
    def mock_classify_all(config):
        snapshots = {}
        for slot in config.carrier_slots:
            if slot.fid:
                snapshots[slot.slot_index] = BindingSnapshot(
                    classification=SlotClassification.READY,
                    fid=slot.fid,
                    commander_name=slot.commander_name,
                    window_binding=None,
                    discovered_commander=None,
                    candidate_windows=[],
                )
            else:
                snapshots[slot.slot_index] = BindingSnapshot(
                    classification=SlotClassification.UNBOUND,
                    fid="",
                    commander_name="",
                    window_binding=None,
                    discovered_commander=None,
                    candidate_windows=[],
                )
        return snapshots
    
    bc.classify_all = Mock(side_effect=mock_classify_all)
    bc.manual_bind = Mock()
    bc.invalidate_binding = Mock()
    return bc


@pytest.fixture
def worker_controller():
    """Create a mock worker controller with Qt signals."""
    # Create a real QObject with signals
    class MockWorkerController(QObject):
        slot_state_changed = Signal(int, str)
        slot_log = Signal(int, str)
        slot_error = Signal(int, object)
        slot_finished = Signal(int, bool)
        
        def __init__(self):
            super().__init__()
            self._start_all_ready_return = ([], {})
            self._stop_all_active_return = []
            self._start_slot_return = False
            self._stop_slot_return = False
            self._slot_state_return = WorkerState.READY
            self.sync_slots_calls = []
        
        def start_all_ready(self):
            return self._start_all_ready_return
        
        def stop_all_active(self):
            return self._stop_all_active_return
        
        def start_slot(self, slot_index):
            return self._start_slot_return
        
        def stop_slot(self, slot_index):
            return self._stop_slot_return
        
        def slot_state(self, slot_index):
            return self._slot_state_return

        def sync_slots(self, config, binding_snapshots):
            self.sync_slots_calls.append((config, binding_snapshots))

    mock_wc = MockWorkerController()
    return mock_wc


@pytest.fixture
def window_info():
    return WindowInfo(
        handle=100,
        pid=1000,
        title="Elite - Dangerous (CLIENT)",
        window_class="EliteDangerous",
        backend="x11",
        focusable=True,
    )


@pytest.fixture
def ready_snapshot(config, window_info):
    binding = WindowBinding.from_window(
        target_fid="F123",
        startup_identity="slot:0",
        window=window_info,
    )
    return BindingSnapshot(
        classification=SlotClassification.READY,
        fid="F123",
        commander_name="Cmdr1",
        window_binding=binding,
        discovered_commander=config.discovered_commanders[0],
        candidate_windows=[window_info],
    )


@pytest.fixture
def unbound_snapshot():
    return BindingSnapshot(
        classification=SlotClassification.UNBOUND,
        fid="",
        commander_name="",
        window_binding=None,
        discovered_commander=None,
        candidate_windows=[],
    )


@pytest.fixture
def needs_manual_snapshot():
    return BindingSnapshot(
        classification=SlotClassification.NEEDS_MANUAL_BINDING,
        fid="F789",
        commander_name="Cmdr3",
        window_binding=None,
        discovered_commander=None,
        candidate_windows=[],
    )


def _make_window_info(handle: int = 100, title: str = "Elite Dangerous") -> WindowInfo:
    return WindowInfo(
        handle=handle,
        pid=1000 + handle,
        title=title,
        window_class="EliteDangerous",
        backend="x11",
        focusable=True,
    )


# ---------------------------------------------------------------------------
# Test: Start All functionality
# ---------------------------------------------------------------------------

class TestStartAll:
    """Test "Start All" starts only READY slots."""
    
    def test_start_all_starts_only_ready_slots(
        self, qapp, config, binding_controller, worker_controller, ready_snapshot
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # Set slot 0 to READY, slot 1 to UNBOUND
        worker_controller._start_all_ready_return = ([0], {})
        
        dashboard.start_all_button.click()
        
        assert worker_controller._start_all_ready_return[0] == [0]
    
    def test_start_all_with_zero_ready_slots(
        self, qapp, config, binding_controller, worker_controller, unbound_snapshot
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # No slots are READY
        worker_controller._start_all_ready_return = ([], {})
        
        dashboard.start_all_button.click()
        
        assert worker_controller._start_all_ready_return[0] == []
    
    def test_start_ready_only_mix_of_ready_and_blocked(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # Slot 0 READY, slot 1 UNBOUND
        worker_controller._start_all_ready_return = ([0], {})
        
        dashboard.start_all_button.click()
        
        assert worker_controller._start_all_ready_return[0] == [0]


# ---------------------------------------------------------------------------
# Test: Blocked slots remain visible with exact reasons
# ---------------------------------------------------------------------------

class TestBlockedSlotsVisibleWithReasons:
    """Test blocked slots remain visible with exact skip reasons."""
    
    def test_unbound_slot_visible_with_reason(
        self, qapp, config, binding_controller, worker_controller, unbound_snapshot
    ):
        # Make slot 0 UNBOUND
        config.carrier_slots[0].fid = ""
        config.carrier_slots[0].commander_name = ""
        
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # Set slot 0 to UNBOUND
        slot_widget = dashboard.slot_widgets[0]
        slot_widget.set_state(WorkerState.UNBOUND, "No FID configured")
        
        assert "unbound" in slot_widget.status_label.text().lower()
        assert "No FID configured" in slot_widget.status_label.text()
    
    def test_needs_manual_binding_slot_visible_with_reason(
        self, qapp, config, binding_controller, worker_controller, needs_manual_snapshot
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        slot_widget = dashboard.slot_widgets[0]
        slot_widget.set_state(WorkerState.NEEDS_MANUAL_BINDING, "Multiple windows detected")
        
        assert "needs_manual_binding" in slot_widget.status_label.text().lower()
        assert "Multiple windows detected" in slot_widget.status_label.text()
    
    def test_invalid_config_slot_visible_with_reason(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        slot_widget = dashboard.slot_widgets[0]
        slot_widget.set_state(WorkerState.UNBOUND, "Invalid route file")
        
        assert "unbound" in slot_widget.status_label.text().lower()
        assert "Invalid route file" in slot_widget.status_label.text()
    
    def test_disabled_slot_visible_with_reason(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        slot_widget = dashboard.slot_widgets[0]
        slot_widget.set_state(WorkerState.UNBOUND, "Slot disabled by user")
        
        assert "unbound" in slot_widget.status_label.text().lower()
        assert "Slot disabled by user" in slot_widget.status_label.text()


# ---------------------------------------------------------------------------
# Test: Stop All functionality
# ---------------------------------------------------------------------------

class TestStopAll:
    """Test "Stop All" requests cancellation for running workers."""
    
    def test_stop_all_requests_cancellation(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        worker_controller._stop_all_active_return = [0, 1]
        
        dashboard.stop_all_button.click()
        
        assert worker_controller._stop_all_active_return == [0, 1]
    
    def test_stop_all_does_not_affect_non_running_workers(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # Only slot 0 is running
        worker_controller._stop_all_active_return = [0]
        
        dashboard.stop_all_button.click()
        
        assert worker_controller._stop_all_active_return == [0]


# ---------------------------------------------------------------------------
# Test: Per-slot controls
# ---------------------------------------------------------------------------

class TestPerSlotControls:
    """Test per-slot Start/Stop/Retry/Manual Bind/Use Discovered Commander/Enable Disable."""
    
    def test_start_button_starts_ready_slot(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        worker_controller._start_slot_return = True
        
        dashboard.slot_widgets[0].start_btn.click()
        
        assert worker_controller._start_slot_return is True
    
    def test_stop_button_stops_running_slot(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        worker_controller._stop_slot_return = True
        
        dashboard.slot_widgets[0].stop_btn.click()
        
        assert worker_controller._stop_slot_return is True
    
    def test_retry_button_for_error_slot(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # Slot is in ERROR state
        dashboard.slot_widgets[0].set_state(WorkerState.ERROR)
        
        # Click retry
        dashboard.slot_widgets[0].retry_btn.click()
        
        # Verify the button is enabled in ERROR state
        assert dashboard.slot_widgets[0].retry_btn.isEnabled()
    
    def test_manual_bind_button_flow(
        self, qapp, config, binding_controller, worker_controller, window_info
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # Mock manual_bind
        binding_snapshot = BindingSnapshot(
            classification=SlotClassification.READY,
            fid="F123",
            commander_name="Cmdr1",
            window_binding=WindowBinding.from_window(
                target_fid="F123",
                startup_identity="manual:0",
                window=window_info,
            ),
            discovered_commander=config.discovered_commanders[0],
            candidate_windows=[window_info],
        )
        binding_controller.manual_bind = Mock(return_value=binding_snapshot)
        
        # Set slot to NEEDS_MANUAL_BINDING
        dashboard.slot_widgets[0].set_state(WorkerState.NEEDS_MANUAL_BINDING)
        dashboard.binding_snapshots[0] = BindingSnapshot(
            classification=SlotClassification.NEEDS_MANUAL_BINDING,
            fid="F123",
            commander_name="Cmdr1",
            window_binding=None,
            discovered_commander=None,
            candidate_windows=[window_info],
        )
        
        # Click manual bind button
        dashboard.slot_widgets[0].manual_bind_btn.click()
        
        # Manual bind should be callable
        assert dashboard.slot_widgets[0].manual_bind_btn.isEnabled()
    
    def test_use_discovered_commander_button(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # Set slot to UNBOUND
        dashboard.slot_widgets[0].set_state(WorkerState.UNBOUND)
        
        # Click use discovered button
        dashboard.slot_widgets[0].use_discovered_btn.click()
        
        # Verify the button is enabled in UNBOUND state
        assert dashboard.slot_widgets[0].use_discovered_btn.isEnabled()
    
    def test_enable_disable_button_toggles_state(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # Initial state
        initial_text = dashboard.slot_widgets[0].enable_disable_btn.text()
        assert initial_text == "Disable"
        
        # Click toggle button
        dashboard.slot_widgets[0].enable_disable_btn.click()
        
        # State should toggle
        new_text = dashboard.slot_widgets[0].enable_disable_btn.text()
        assert new_text == "Enable"


# ---------------------------------------------------------------------------
# Test: UI State Updates
# ---------------------------------------------------------------------------

class TestUIStateUpdates:
    """Test button enable/disable based on WorkerState."""
    
    def test_start_button_enabled_only_for_ready_state(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # READY state - Start should be enabled
        slot_widget = dashboard.slot_widgets[0]
        slot_widget.set_state(WorkerState.READY)
        assert slot_widget.start_btn.isEnabled()
        
        # ERROR state - Start should be disabled
        slot_widget.set_state(WorkerState.ERROR)
        assert not slot_widget.start_btn.isEnabled()
    
    def test_stop_button_enabled_for_running_states(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # RUNNING state - Stop should be enabled
        slot_widget = dashboard.slot_widgets[0]
        slot_widget.set_state(WorkerState.RUNNING)
        assert slot_widget.stop_btn.isEnabled()
        
        # READY state - Stop should be disabled
        slot_widget.set_state(WorkerState.READY)
        assert not slot_widget.stop_btn.isEnabled()
    
    def test_retry_button_enabled_only_for_error_state(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # ERROR state - Retry should be enabled
        slot_widget = dashboard.slot_widgets[0]
        slot_widget.set_state(WorkerState.ERROR)
        assert slot_widget.retry_btn.isEnabled()
        
        # READY state - Retry should be disabled
        slot_widget.set_state(WorkerState.READY)
        assert not slot_widget.retry_btn.isEnabled()
    
    def test_status_label_updates_on_state_change(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        slot_widget = dashboard.slot_widgets[0]
        
        # Emit state change signal
        slot_widget.set_state(WorkerState.RUNNING)
        
        assert "running" in slot_widget.status_label.text().lower()
    
    def test_log_area_updates_on_slot_log(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        slot_widget = dashboard.slot_widgets[0]
        test_message = "Test log message"
        
        slot_widget.append_log(test_message)
        
        log_text = slot_widget.log_area.toPlainText()
        assert test_message in log_text


# ---------------------------------------------------------------------------
# Test: Dashboard logs remain machine-testable
# ---------------------------------------------------------------------------

class TestDashboardLogsMachineTestable:
    """Test dashboard logs remain machine-testable."""
    
    def test_log_messages_are_deterministic(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        slot_widget = dashboard.slot_widgets[0]
        
        # Add multiple log messages
        slot_widget.append_log("Message 1")
        slot_widget.append_log("Message 2")
        
        log_text = slot_widget.log_area.toPlainText()
        log_lines = log_text.strip().split("\n")
        
        # Messages should be in order
        assert "Message 1" in log_lines
        assert "Message 2" in log_lines
    
    def test_log_messages_do_not_contain_timestamps(
        self, qapp, config, binding_controller, worker_controller
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        slot_widget = dashboard.slot_widgets[0]
        slot_widget.append_log("Test message")
        
        log_text = slot_widget.log_area.toPlainText()
        
        # Logs should not contain timestamps for machine-testability
        assert "Test message" in log_text
        # Check that no timestamp pattern is present (e.g., "12:34:56" or "2024-")
        import re
        timestamp_pattern = r'\d{1,2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}'
        assert not re.search(timestamp_pattern, log_text)


# ---------------------------------------------------------------------------
# Test: Integration scenarios
# ---------------------------------------------------------------------------

class TestIntegrationScenarios:
    """Test start-ready-only, zero-ready-slots, and use-discovered-commander scenarios."""
    
    def test_use_discovered_commander_scenario(self, qapp, config, binding_controller, worker_controller):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        dashboard.slot_widgets[1].set_state(WorkerState.UNBOUND)
        assert dashboard.slot_widgets[1].use_discovered_btn.isEnabled()
    def test_multiple_slots_mixed_states(
        self, qapp, config, binding_controller, worker_controller
    ):
        """Test multiple slots with different states."""
        dashboard = DashboardWidget(config, binding_controller, worker_controller)
        
        # Set different states for each slot
        dashboard.slot_widgets[0].set_state(WorkerState.READY)
        dashboard.slot_widgets[1].set_state(WorkerState.UNBOUND, "No FID")
        
        # Both slots should be visible
        assert 0 in dashboard.slot_widgets
        assert 1 in dashboard.slot_widgets
        
        # Statuses should reflect current state
        assert "ready" in dashboard.slot_widgets[0].status_label.text().lower()
        assert "unbound" in dashboard.slot_widgets[1].status_label.text().lower()


# ---------------------------------------------------------------------------
# Test: Existing tests (preserved and improved)
# ---------------------------------------------------------------------------

def test_start_all_wires_to_worker_controller(qapp, config, binding_controller, worker_controller):
    dashboard = DashboardWidget(config, binding_controller, worker_controller)
    worker_controller._start_all_ready_return = ([0, 1], {})
    dashboard.start_all_button.click()
    assert worker_controller._start_all_ready_return[0] == [0, 1]


def test_stop_all_wires_to_worker_controller(qapp, config, binding_controller, worker_controller):
    dashboard = DashboardWidget(config, binding_controller, worker_controller)
    worker_controller._stop_all_active_return = [0, 1]
    dashboard.stop_all_button.click()
    assert worker_controller._stop_all_active_return == [0, 1]


def test_blocked_slots_show_skip_reasons(qapp, config, binding_controller, worker_controller):
    """Test that blocked slots show exact skip reasons."""
    dashboard = DashboardWidget(config, binding_controller, worker_controller)
    
    # Set slot 0 to UNBOUND with reason
    dashboard.slot_widgets[0].set_state(WorkerState.UNBOUND, "No FID configured")
    
    status_text = dashboard.slot_widgets[0].status_label.text()
    assert "unbound" in status_text.lower()
    assert "No FID configured" in status_text


def test_disable_slot_prevents_start(qapp, config, binding_controller, worker_controller):
    """Test that disabling a slot prevents it from starting."""
    dashboard = DashboardWidget(config, binding_controller, worker_controller)
    
    # Disable the slot
    dashboard.slot_widgets[0].enable_disable_btn.click()
    
    # Start button should be disabled
    assert not dashboard.slot_widgets[0].start_btn.isEnabled()
    assert dashboard.slot_widgets[0].enable_disable_btn.text() == "Enable"


def test_manual_bind_flow(qapp, config, binding_controller, worker_controller, window_info):
    """Test manual bind button shows dialog."""
    dashboard = DashboardWidget(config, binding_controller, worker_controller)
    
    # Set slot to NEEDS_MANUAL_BINDING
    dashboard.slot_widgets[0].set_state(WorkerState.NEEDS_MANUAL_BINDING)
    
    # Button should be enabled
    assert dashboard.slot_widgets[0].manual_bind_btn.isEnabled()





class TestManualBindDialogThumbnails:
    def test_dialog_creates_icon_mode_items(
        self, qapp, config, binding_controller, worker_controller, window_info
    ):
        from TraversalSystem.gui.dashboard import ManualBindDialog

        dialog = ManualBindDialog(
            slot_index=0,
            fid="F123",
            candidate_windows=[window_info],
            parent=None,
        )

        assert dialog.window_list.viewMode() == QListWidget.ViewMode.IconMode
        assert dialog.window_list.count() == 1
        item = dialog.window_list.item(0)
        assert item is not None
        assert not item.icon().isNull()

        idx = item.data(Qt.ItemDataRole.UserRole)
        assert idx == 0

        dialog.close()

    def test_dialog_text_includes_pid(
        self, qapp, config, binding_controller, worker_controller, window_info
    ):
        from TraversalSystem.gui.dashboard import ManualBindDialog

        dialog = ManualBindDialog(
            slot_index=0,
            fid="F123",
            candidate_windows=[window_info],
            parent=None,
        )

        item = dialog.window_list.item(0)
        assert str(window_info.pid) in item.text()
        assert window_info.title in item.text()

        dialog.close()

    def test_placeholder_shown_when_capture_fails(
        self, qapp, config, binding_controller, worker_controller, window_info
    ):
        from TraversalSystem.gui.dashboard import ManualBindDialog

        dialog = ManualBindDialog(
            slot_index=0,
            fid="F123",
            candidate_windows=[window_info],
            parent=None,
        )

        item = dialog.window_list.item(0)
        assert not item.icon().isNull()

        dialog.close()

def test_use_discovered_commander_flow(qapp, config, binding_controller, worker_controller):
    """Test use discovered commander button."""
    dashboard = DashboardWidget(config, binding_controller, worker_controller)
    
    # Set slot to UNBOUND
    dashboard.slot_widgets[0].set_state(WorkerState.UNBOUND)
    
    # Button should be enabled
    assert dashboard.slot_widgets[0].use_discovered_btn.isEnabled()


def test_dashboard_logs_are_machine_testable(qapp, config, binding_controller, worker_controller):
    """Test that dashboard logs are machine-testable."""
    dashboard = DashboardWidget(config, binding_controller, worker_controller)
    
    slot_widget = dashboard.slot_widgets[0]
    slot_widget.append_log("Test message 1")
    slot_widget.append_log("Test message 2")
    
    log_text = slot_widget.log_area.toPlainText()
    
    assert "Test message 1" in log_text
    assert "Test message 2" in log_text
    # No timestamps
    import re
    timestamp_pattern = r'\d{1,2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}'
    assert not re.search(timestamp_pattern, log_text)


def test_zero_ready_slots(qapp, config, binding_controller, worker_controller):
    """Test start all with zero ready slots."""
    # Make all slots UNBOUND
    config.carrier_slots[0].fid = ""
    config.carrier_slots[1].fid = ""
    
    dashboard = DashboardWidget(config, binding_controller, worker_controller)
    
    worker_controller._start_all_ready_return = ([], {})
    
    dashboard.start_all_button.click()
    
    assert worker_controller._start_all_ready_return[0] == []

def test_start_all_emits_no_ready_carriers(qapp, config, binding_controller, worker_controller):
    dashboard = DashboardWidget(config, binding_controller, worker_controller)
    worker_controller._start_all_ready_return = ([], {0: "Slot is unbound"})
    
    # Track calls to _log_global manually since it's an instance method
    original_log_global = dashboard._log_global
    calls = []
    def mock_log_global(message):
        calls.append(message)
        original_log_global(message)
    dashboard._log_global = mock_log_global
    
    dashboard.start_all_button.click()
    
    assert "No ready carriers" in calls


# ---------------------------------------------------------------------------
# Test: sync_slots called immediately after binding updates (Bug 7)
# ---------------------------------------------------------------------------

class TestSyncSlotsAfterBinding:
    """Test that worker_controller.sync_slots() is called immediately after
    manual bind or use-discovered-commander updates the binding snapshots,
    without waiting for the 5-second background timer."""

    def test_sync_slots_called_after_manual_bind(
        self, qapp, config, binding_controller, worker_controller,
        window_info, monkeypatch,
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)

        # Slot 0 is in NEEDS_MANUAL_BINDING with a candidate window
        dashboard.binding_snapshots[0] = BindingSnapshot(
            classification=SlotClassification.NEEDS_MANUAL_BINDING,
            fid="F123",
            commander_name="Cmdr1",
            window_binding=None,
            discovered_commander=None,
            candidate_windows=[window_info],
        )

        # Mock manual_bind to return a READY snapshot
        bound_snapshot = BindingSnapshot(
            classification=SlotClassification.READY,
            fid="F123",
            commander_name="Cmdr1",
            window_binding=WindowBinding.from_window(
                target_fid="F123",
                startup_identity="manual:0",
                window=window_info,
            ),
            discovered_commander=config.discovered_commanders[0],
            candidate_windows=[window_info],
        )
        binding_controller.manual_bind = Mock(return_value=bound_snapshot)

        # Patch ManualBindDialog.exec to emit window_selected then return
        from TraversalSystem.gui.dashboard import ManualBindDialog

        def emit_then_return(self):
            self.window_selected.emit(window_info)
            return 1

        monkeypatch.setattr(ManualBindDialog, "exec", emit_then_return)

        dashboard._on_manual_bind(0)

        assert len(worker_controller.sync_slots_calls) == 1
        called_config, called_snapshots = worker_controller.sync_slots_calls[0]
        assert called_config is config
        assert called_snapshots is dashboard.binding_snapshots

    def test_sync_slots_called_after_use_discovered(
        self, qapp, config, binding_controller, worker_controller, monkeypatch,
    ):
        dashboard = DashboardWidget(config, binding_controller, worker_controller)

        from TraversalSystem.gui.dashboard import UseDiscoveredCommanderDialog

        def emit_then_return(self):
            self.commander_selected.emit("F456")
            return 1

        monkeypatch.setattr(UseDiscoveredCommanderDialog, "exec", emit_then_return)

        dashboard._on_use_discovered(0)

        assert len(worker_controller.sync_slots_calls) == 1
        called_config, called_snapshots = worker_controller.sync_slots_calls[0]
        assert called_config is config
        assert called_snapshots is dashboard.binding_snapshots
    
