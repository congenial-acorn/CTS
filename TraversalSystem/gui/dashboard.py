"""Dashboard widget for multi-carrier traversal automation.

Provides per-slot controls and global start/stop functionality.
Blocked slots remain visible with exact skip reasons for machine-testable logs.
"""
from __future__ import annotations

import datetime
from typing import cast
from collections.abc import Callable

from PySide6.QtGui import QImage, QPixmap, QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QScrollArea, QFrame, QTextEdit, QDialog, QListWidget, QListWidgetItem,
    QDialogButtonBox,
    QMessageBox, QGroupBox,
)
from PySide6.QtCore import Qt, Signal, QObject, QThread, QSize, Slot

from TraversalSystem.gui_config import GuiConfig, CarrierSlotConfig, DiscoveredCommander
from TraversalSystem.gui.binding_controller import BindingController, BindingSnapshot, SlotClassification
from TraversalSystem.gui.worker_controller import WorkerController
from TraversalSystem.gui.worker_state import WorkerState
from TraversalSystem.window_capture import capture_window, create_placeholder
from TraversalSystem.window_manager import WindowInfo
from TraversalSystem.gui.theme import ED_DARK_BG, ED_PANEL_BG, ED_ORANGE, ED_TEXT, ED_BORDER
from TraversalSystem.gui.scheduled_jump import ScheduledJumpController


# ---------------------------------------------------------------------------
# Manual Bind Dialog
# ---------------------------------------------------------------------------

class _CaptureWorker(QObject):
    capture_result = Signal(int, object)
    finished = Signal()

    def __init__(
        self,
        candidates: list[WindowInfo],
        max_size: tuple[int, int] = (320, 240),
    ) -> None:
        super().__init__()
        self._candidates = candidates
        self._max_size = max_size

    @Slot()
    def run(self) -> None:
        try:
            for i, window in enumerate(self._candidates):
                try:
                    img = capture_window(window.handle, window.backend, self._max_size)
                    if img is not None:
                        img = img.convert("RGBA")
                        data = img.tobytes("raw", "RGBA")
                        qimg = QImage(
                            data,
                            img.width,
                            img.height,
                            img.width * 4,
                            QImage.Format.Format_RGBA8888,
                        ).copy()
                        self.capture_result.emit(i, qimg)
                    else:
                        self.capture_result.emit(i, None)
                except Exception:
                    self.capture_result.emit(i, None)
        finally:
            self.finished.emit()

class ManualBindDialog(QDialog):
    """Dialog for manually selecting a window for slot binding.

    Shows thumbnail previews of candidate windows captured in a
    background thread. Placeholders are shown until capture completes.
    """

    window_selected = Signal(object)

    _THUMB_W = 160
    _THUMB_H = 120
    _GRID_W = 180
    _GRID_H = 170

    def __init__(
        self,
        slot_index: int,
        fid: str,
        candidate_windows: list[WindowInfo],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.slot_index = slot_index
        self.fid = fid
        self.candidate_windows = candidate_windows
        self.selected_window: WindowInfo | None = None

        self.setWindowTitle(f"Manual Bind - Slot {slot_index}")
        self.setMinimumWidth(560)

        self.setStyleSheet(f"""
            QDialog {{ background-color: {ED_DARK_BG}; }}
            QListWidget {{
                background-color: {ED_PANEL_BG};
                border: 1px solid {ED_BORDER};
                color: {ED_TEXT};
            }}
            QListWidget::item:selected {{
                background-color: {ED_ORANGE};
                color: {ED_DARK_BG};
            }}
            QLabel {{ color: {ED_TEXT}; }}
            QDialogButtonBox QPushButton {{
                background-color: {ED_PANEL_BG};
                color: {ED_ORANGE};
                border: 1px solid {ED_ORANGE};
                padding: 5px 15px;
            }}
            QDialogButtonBox QPushButton:hover {{
                background-color: {ED_ORANGE};
                color: {ED_DARK_BG};
            }}
        """)

        layout = QVBoxLayout(self)

        # Instructions
        info_label = QLabel(f"Select a window for FID: {fid}")
        info_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(info_label)

        self.window_list = QListWidget()
        self.window_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.window_list.setIconSize(QSize(self._THUMB_W, self._THUMB_H))
        self.window_list.setGridSize(QSize(self._GRID_W, self._GRID_H))
        self.window_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.window_list.setSpacing(10)
        self.window_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

        placeholder_img = create_placeholder(self._THUMB_W, self._THUMB_H)
        ph_data = placeholder_img.convert("RGBA").tobytes("raw", "RGBA")
        ph_qimg = QImage(
            ph_data,
            self._THUMB_W,
            self._THUMB_H,
            self._THUMB_W * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
        self._placeholder_icon = QIcon(QPixmap.fromImage(ph_qimg))

        for i, window in enumerate(candidate_windows):
            item = QListWidgetItem()
            item.setIcon(self._placeholder_icon)
            item.setText(f"{window.title}\nPID: {window.pid}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.window_list.addItem(item)

        layout.addWidget(self.window_list)

        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self._capture_thread: QThread | None = None
        self._capture_worker: _CaptureWorker | None = None
        self._start_capture_thread()

    def _start_capture_thread(self) -> None:
        if not self.candidate_windows:
            return

        self._capture_thread = QThread(self)
        self._capture_worker = _CaptureWorker(
            self.candidate_windows,
            max_size=(self._THUMB_W * 2, self._THUMB_H * 2),
        )
        self._capture_worker.moveToThread(self._capture_thread)

        self._capture_thread.started.connect(self._capture_worker.run)
        self._capture_worker.capture_result.connect(self._on_thumbnail_ready)
        self._capture_worker.finished.connect(self._capture_thread.quit)
        self._capture_thread.finished.connect(self._cleanup_capture_thread)

        self._capture_thread.start()

    def _on_thumbnail_ready(self, index: int, qimage: QImage | None) -> None:
        if qimage is None:
            return

        pixmap = QPixmap.fromImage(qimage).scaled(
            self._THUMB_W,
            self._THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        item = self.window_list.item(index)
        if item is not None:
            item.setIcon(QIcon(pixmap))

    def _cleanup_capture_thread(self) -> None:
        if self._capture_worker is not None:
            self._capture_worker.deleteLater()
            self._capture_worker = None
        if self._capture_thread is not None:
            self._capture_thread.deleteLater()
            self._capture_thread = None

    def _on_accept(self) -> None:
        row = self.window_list.currentRow()
        if row >= 0 and row < len(self.candidate_windows):
            self.selected_window = self.candidate_windows[row]
            self.window_selected.emit(self.selected_window)
            self.accept()
        else:
            QMessageBox.warning(
                self,
                "No Selection",
                "Please select a window from the list.",
            )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._capture_thread is not None and self._capture_thread.isRunning():
            self._capture_thread.quit()
            self._capture_thread.wait(2000)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Use Discovered Commander Dialog
# ---------------------------------------------------------------------------

class UseDiscoveredCommanderDialog(QDialog):
    """Dialog for selecting a discovered commander for slot binding."""
    
    commander_selected = Signal(str)  # emits fid
    
    def __init__(
        self,
        slot_index: int,
        discovered_commanders: list[DiscoveredCommander],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.slot_index = slot_index
        self.discovered_commanders = discovered_commanders
        self.selected_fid: str = ""
        
        self.setWindowTitle(f"Use Discovered Commander - Slot {slot_index}")
        self.setMinimumWidth(400)
        
        layout = QVBoxLayout(self)
        
        # Instructions
        info_label = QLabel(f"Select a discovered commander for Slot {slot_index}:")
        info_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(info_label)
        
        # Commander list
        self.cmdr_list = QListWidget()
        for cmdr in discovered_commanders:
            metadata = []
            if cmdr.discovered_at:
                metadata.append(f"Last Seen: {cmdr.discovered_at}")
            if cmdr.discovery_status:
                metadata.append(f"Status: {cmdr.discovery_status}")
            meta_str = f" [{', '.join(metadata)}]" if metadata else ""
            self.cmdr_list.addItem(f"{cmdr.name} (FID: {cmdr.fid}){meta_str}")
        layout.addWidget(self.cmdr_list)
        
        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
    
    def _on_accept(self) -> None:
        row = self.cmdr_list.currentRow()
        if row >= 0 and row < len(self.discovered_commanders):
            cmdr = self.discovered_commanders[row]
            self.selected_fid = cmdr.fid
            self.commander_selected.emit(cmdr.fid)
            self.accept()
        else:
            QMessageBox.warning(self, "No Selection", "Please select a commander from the list.")


# ---------------------------------------------------------------------------
# Dashboard Slot Widget
# ---------------------------------------------------------------------------

class DashboardSlotWidget(QFrame):
    """Widget representing a single carrier slot in the dashboard."""
    
    # Track slot enabled state for user toggle
    enabled_changed = Signal(int, bool)
    
    def __init__(self, slot_index: int, slot_config: CarrierSlotConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.slot_index = slot_index
        self.slot_config = slot_config
        self.current_state: WorkerState = WorkerState.UNBOUND
        self.blocked_reason: str = ""
        self.is_enabled: bool = slot_config.enabled  # Bound to configuration
        
        self.setFrameStyle(int(QFrame.Shape.StyledPanel) | int(QFrame.Shadow.Raised))
        
        layout = QVBoxLayout(self)
        
        header_layout = QHBoxLayout()
        self.title_label = QLabel(f"Slot {slot_index}: {slot_config.commander_name or 'Unnamed'}")
        self.title_label.setStyleSheet("font-weight: bold;")
        self.status_label = QLabel("Status: Unknown")
        header_layout.addWidget(self.title_label)
        header_layout.addWidget(self.status_label)
        header_layout.addStretch()
        
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.retry_btn = QPushButton("Retry")
        self.manual_bind_btn = QPushButton("Manual Bind")
        self.use_discovered_btn = QPushButton("Use Discovered")
        self.enable_disable_btn = QPushButton("Disable" if self.is_enabled else "Enable")
        
        header_layout.addWidget(self.start_btn)
        header_layout.addWidget(self.stop_btn)
        header_layout.addWidget(self.retry_btn)
        header_layout.addWidget(self.manual_bind_btn)
        header_layout.addWidget(self.use_discovered_btn)
        header_layout.addWidget(self.enable_disable_btn)
        
        layout.addLayout(header_layout)
        
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(100)
        layout.addWidget(self.log_area)
        
        # Scheduled Jump section
        self.scheduled_jump_group = QGroupBox("Scheduled Jump")
        sj_layout = QHBoxLayout(self.scheduled_jump_group)
        
        self.sj_time_label = QLabel(
            slot_config.scheduled_jump_time or "Not configured"
        )
        sj_layout.addWidget(QLabel("Time:"))
        sj_layout.addWidget(self.sj_time_label)
        
        self.sj_countdown_label = QLabel("--:--:--")
        sj_layout.addWidget(self.sj_countdown_label)
        
        self.sj_schedule_btn = QPushButton("Schedule")
        self.sj_cancel_btn = QPushButton("Cancel")
        self.sj_cancel_btn.setEnabled(False)
        sj_layout.addWidget(self.sj_schedule_btn)
        sj_layout.addWidget(self.sj_cancel_btn)
        sj_layout.addStretch()
        
        layout.addWidget(self.scheduled_jump_group)
        
        # Connect enable/disable button
        self.enable_disable_btn.clicked.connect(self._on_enable_disable_toggle)
        
        # Initialize button states
        self._update_button_states()
        
    def _on_enable_disable_toggle(self) -> None:
        """Toggle the slot enabled state."""
        self.is_enabled = not self.is_enabled
        self.enable_disable_btn.setText("Disable" if self.is_enabled else "Enable")
        self.enabled_changed.emit(self.slot_index, self.is_enabled)
        self.set_state(self.current_state, self.blocked_reason)
    
    def _update_button_states(self) -> None:
        """Update button enabled states based on current worker state."""
        # Start: enabled only when READY and slot is enabled
        self.start_btn.setEnabled(
            self.is_enabled and 
            self.current_state is WorkerState.READY
        )
        
        # Stop: enabled when STARTING, RUNNING, or WAITING
        self.stop_btn.setEnabled(
            self.current_state in (
                WorkerState.STARTING,
                WorkerState.RUNNING,
                WorkerState.WAITING,
            )
        )
        
        # Retry: enabled only in ERROR state
        self.retry_btn.setEnabled(
            self.is_enabled and 
            self.current_state is WorkerState.ERROR
        )
        
        # Manual Bind: enabled when NEEDS_MANUAL_BINDING
        self.manual_bind_btn.setEnabled(
            self.is_enabled and 
            self.current_state is WorkerState.NEEDS_MANUAL_BINDING
        )
        
        # Use Discovered: enabled when UNBOUND (to pick a commander)
        self.use_discovered_btn.setEnabled(
            self.is_enabled and 
            self.current_state is WorkerState.UNBOUND
        )
        
        # Scheduled jump button state
        self.sj_schedule_btn.setEnabled(
            bool(self.slot_config.scheduled_jump_time)
            and self.slot_config.scheduled_jump_button_x > 0
            and self.slot_config.scheduled_jump_button_y > 0
            and self.is_enabled
            and self.current_state is WorkerState.READY
        )
        self.sj_cancel_btn.setEnabled(False)
    
    def set_state(self, state: WorkerState, blocked_reason: str = "") -> None:
        """Update the widget state and UI."""
        self.current_state = state
        self.blocked_reason = blocked_reason
        
        # Update status label
        status_text = f"Status: {state.value}"
        if not self.is_enabled:
            status_text += " (disabled)"
        elif blocked_reason:
            status_text += f" ({blocked_reason})"
        self.status_label.setText(status_text)
        
        # Update button states
        self._update_button_states()
    
    def append_log(self, message: str) -> None:
        """Append a log message to the slot's log area.
        
        Logs are machine-testable - no timestamps, just plain messages.
        """
        self.log_area.append(message)
    
    def set_scheduled_state(self, status: str) -> None:
        """Update scheduled jump button states and display."""
        if status == "scheduled":
            self.sj_schedule_btn.setEnabled(False)
            self.sj_cancel_btn.setEnabled(True)
        elif status in ("completed", "cancelled", "failed", "idle"):
            self.sj_schedule_btn.setEnabled(
                bool(self.slot_config.scheduled_jump_time)
                and self.slot_config.scheduled_jump_button_x > 0
                and self.slot_config.scheduled_jump_button_y > 0
                and self.is_enabled
                and self.current_state is WorkerState.READY
            )
            self.sj_cancel_btn.setEnabled(False)
            self.sj_countdown_label.setText("--:--:--")
        elif status == "focusing":
            self.sj_countdown_label.setText("FOCUSING...")

    def update_countdown(self, text: str) -> None:
        """Update the countdown display."""
        self.sj_countdown_label.setText(text)
    
    def set_status(self, status: str, blocked_reason: str = "") -> None:
        """Legacy method - use set_state instead."""
        # Try to map status string to WorkerState
        try:
            state = WorkerState(status)
            self.set_state(state, blocked_reason)
        except ValueError:
            # If not a valid WorkerState, just display as text
            msg = f"Status: {status}"
            if blocked_reason:
                msg += f" ({blocked_reason})"
            self.status_label.setText(msg)


# ---------------------------------------------------------------------------
# Dashboard Widget
# ---------------------------------------------------------------------------

class DashboardWidget(QWidget):
    """Main dashboard widget showing all carrier slots and controls."""
    
    config_changed = Signal()
    
    def __init__(
        self,
        config: GuiConfig,
        binding_controller: BindingController,
        worker_controller: WorkerController,
        parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.binding_controller = binding_controller
        self.worker_controller = worker_controller
        self.slot_widgets: dict[int, DashboardSlotWidget] = {}
        self.binding_snapshots: dict[int, BindingSnapshot] = {}
        self._scheduled_controllers: dict[int, ScheduledJumpController] = {}
        
        layout = QVBoxLayout(self)
        
        # Global controls
        controls_layout = QHBoxLayout()
        self.start_all_button = QPushButton("Start All")
        self.start_all_button.setObjectName("startAllButton")
        self.stop_all_button = QPushButton("Stop All")
        self.stop_all_button.setObjectName("stopAllButton")
        controls_layout.addWidget(self.start_all_button)
        controls_layout.addWidget(self.stop_all_button)
        controls_layout.addStretch()
        layout.addLayout(controls_layout)
        
        # Scrollable slot area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.slots_layout = QVBoxLayout(self.scroll_content)
        scroll.setWidget(self.scroll_content)
        layout.addWidget(scroll)
        
        # Connect global controls
        self.start_all_button.clicked.connect(self._on_start_all)
        self.stop_all_button.clicked.connect(self._on_stop_all)
        
        # Connect worker controller signals
        self.worker_controller.slot_state_changed.connect(self._on_slot_state_changed)
        self.worker_controller.slot_log.connect(self._on_slot_log)
        
        # Build slot widgets
        self._build_slots()
        
        # Initial classification
        self._update_classification()
    
    def _build_slots(self) -> None:
        """Build slot widgets from config."""
        for idx, slot_config in enumerate(self.config.carrier_slots):
            widget = DashboardSlotWidget(idx, slot_config)
            self.slot_widgets[idx] = widget
            self.slots_layout.addWidget(widget)
            
            # Connect per-slot controls
            widget.start_btn.clicked.connect(lambda _, i=idx: self._on_start_slot(i))
            widget.stop_btn.clicked.connect(lambda _, i=idx: self._on_stop_slot(i))
            widget.retry_btn.clicked.connect(lambda _, i=idx: self._on_retry_slot(i))
            widget.manual_bind_btn.clicked.connect(lambda _, i=idx: self._on_manual_bind(i))
            widget.use_discovered_btn.clicked.connect(lambda _, i=idx: self._on_use_discovered(i))
            widget.enabled_changed.connect(self._on_slot_enabled_changed)
            widget.sj_schedule_btn.clicked.connect(lambda _, i=idx: self._on_schedule_jump(i))
            widget.sj_cancel_btn.clicked.connect(lambda _, i=idx: self._on_cancel_scheduled_jump(i))
    
    def _update_classification(self) -> None:
        """Update slot classifications from binding controller."""
        snapshots = self.binding_controller.classify_all(self.config)
        self.binding_snapshots = snapshots
        
        for idx, snapshot in snapshots.items():
            if idx in self.slot_widgets:
                state = self._classification_to_state(snapshot)
                blocked_reason = self._get_blocked_reason(snapshot)
                self.slot_widgets[idx].set_state(state, blocked_reason)
    
    def _classification_to_state(self, snapshot: BindingSnapshot) -> WorkerState:
        """Convert binding classification to worker state."""
        from TraversalSystem.gui.binding_controller import classification_to_worker_state
        return classification_to_worker_state(snapshot.classification)
    
    def _get_blocked_reason(self, snapshot: BindingSnapshot) -> str:
        """Get the exact blocked reason for a slot based on classification."""
        reasons = {
            SlotClassification.UNBOUND: "No FID configured",
            SlotClassification.NEEDS_MANUAL_BINDING: "Manual window selection required",
            SlotClassification.STALE: "Previously bound window no longer available",
            SlotClassification.UNAVAILABLE: "No Elite Dangerous windows detected",
            SlotClassification.AMBIGUOUS: "Multiple indistinguishable windows detected",
        }
        return reasons.get(snapshot.classification, "")
    
    def _on_start_all(self) -> None:
        """Handle Start All button click - starts only READY slots."""
        started, skip_reasons = self.worker_controller.start_all_ready()
        
        for idx in started:
            if idx in self.slot_widgets:
                self.slot_widgets[idx].append_log(f"Slot {idx} started via Start All")
                
        for idx, reason in skip_reasons.items():
            if idx in self.slot_widgets:
                self.slot_widgets[idx].append_log(f"Start All skipped: {reason}")
                
        if not started:
            self._log_global("No ready carriers")

    def _log_global(self, message: str) -> None:
        """Log a message to all slot widgets to indicate global action."""
        for widget in self.slot_widgets.values():
            widget.append_log(message)

    def _on_stop_all(self) -> None:
        """Handle Stop All button click - stops all running workers."""
        stopped = self.worker_controller.stop_all_active()
        for idx in stopped:
            if idx in self.slot_widgets:
                self.slot_widgets[idx].append_log(f"Slot {idx} stopped via Stop All")

    def _on_schedule_jump(self, slot_index: int) -> None:
        """Create and start a ScheduledJumpController for the given slot."""
        slot = self.config.carrier_slots[slot_index]
        if not slot.scheduled_jump_time:
            self.slot_widgets[slot_index].append_log("Scheduled jump: no time configured")
            return

        snapshot = self.binding_snapshots.get(slot_index)
        if not snapshot or not snapshot.window_binding:
            self.slot_widgets[slot_index].append_log("Scheduled jump: slot not bound to window")
            self.slot_widgets[slot_index].set_scheduled_state("failed")
            return

        if slot.scheduled_jump_button_x == 0 or slot.scheduled_jump_button_y == 0:
            self.slot_widgets[slot_index].append_log("Scheduled jump: button coordinates not set")
            return

        parts = slot.scheduled_jump_time.split(":")
        if len(parts) != 3:
            self.slot_widgets[slot_index].append_log(f"Scheduled jump: invalid time format '{slot.scheduled_jump_time}'")
            return
        try:
            target_time = datetime.time(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            self.slot_widgets[slot_index].append_log(f"Scheduled jump: invalid time '{slot.scheduled_jump_time}'")
            return

        # Cancel existing controller for this slot if any
        old = self._scheduled_controllers.pop(slot_index, None)
        if old:
            old.cancel()

        controller = ScheduledJumpController(parent=self)

        widget = self.slot_widgets[slot_index]
        controller.countdown_updated.connect(widget.update_countdown)
        controller.status_changed.connect(widget.set_scheduled_state)

        def on_status(status: str) -> None:
            widget.append_log(f"Scheduled jump: {status}")
            if status in ("completed", "cancelled", "failed"):
                self._scheduled_controllers.pop(slot_index, None)
        controller.status_changed.connect(on_status)

        try:
            controller.schedule(
                target_utc=target_time,
                button_x=slot.scheduled_jump_button_x,
                button_y=slot.scheduled_jump_button_y,
                binding=snapshot.window_binding,
            )
            self._scheduled_controllers[slot_index] = controller
            widget.append_log(f"Scheduled jump: set for {slot.scheduled_jump_time} UTC")
        except ValueError as exc:
            widget.append_log(f"Scheduled jump: {exc}")
            widget.set_scheduled_state("failed")

    def _on_cancel_scheduled_jump(self, slot_index: int) -> None:
        """Cancel a running scheduled jump for the given slot."""
        controller = self._scheduled_controllers.pop(slot_index, None)
        if controller:
            controller.cancel()
            self.slot_widgets[slot_index].append_log("Scheduled jump: cancelled")

    def _on_start_slot(self, slot_index: int) -> None:
        """Handle per-slot Start button click."""
        if self.worker_controller.start_slot(slot_index):
            self.slot_widgets[slot_index].append_log(f"Slot {slot_index} start requested")
    
    def _on_stop_slot(self, slot_index: int) -> None:
        """Handle per-slot Stop button click."""
        if self.worker_controller.stop_slot(slot_index):
            self.slot_widgets[slot_index].append_log(f"Slot {slot_index} stop requested")
    
    def _on_retry_slot(self, slot_index: int) -> None:
        """Handle per-slot Retry button click.
        
        Retry attempts to transition from ERROR back to READY if preconditions are met.
        """
        # Check if we can retry (binding and config must be valid)
        snapshot = self.binding_snapshots.get(slot_index)
        if snapshot is None:
            return
        
        # For retry, we need the slot to be in a bindable state
        state = self._classification_to_state(snapshot)
        if state is WorkerState.READY:
            # If ready, try starting
            if self.worker_controller.start_slot(slot_index):
                self.slot_widgets[slot_index].append_log(f"Slot {slot_index} retry: started")
        else:
            # Show why retry is not available
            reason = self._get_blocked_reason(snapshot)
            self.slot_widgets[slot_index].append_log(f"Slot {slot_index} retry blocked: {reason}")
    
    def _on_manual_bind(self, slot_index: int) -> None:
        """Handle per-slot Manual Bind button click."""
        snapshot = self.binding_snapshots.get(slot_index)
        if snapshot is None:
            return
        
        # If there are candidate windows, show selection dialog
        if snapshot.candidate_windows:
            dialog = ManualBindDialog(
                slot_index=slot_index,
                fid=snapshot.fid,
                candidate_windows=snapshot.candidate_windows,
                parent=self,
            )
            
            def on_window_selected(window: WindowInfo) -> None:
                # Call binding controller to perform manual bind
                new_snapshot = self.binding_controller.manual_bind(
                    slot_index, self.config, window
                )
                self.binding_snapshots[slot_index] = new_snapshot
                
                # Update UI
                state = self._classification_to_state(new_snapshot)
                blocked_reason = self._get_blocked_reason(new_snapshot)
                self.slot_widgets[slot_index].set_state(state, blocked_reason)
                self.slot_widgets[slot_index].append_log(
                    f"Slot {slot_index} manually bound to window {window.handle}"
                )
                self.worker_controller.sync_slots(self.config, self.binding_snapshots)
            
            dialog.window_selected.connect(on_window_selected)
            dialog.exec()
        else:
            self.slot_widgets[slot_index].append_log(
                f"Slot {slot_index} manual bind: no candidate windows available"
            )
    
    def _on_use_discovered(self, slot_index: int) -> None:
        """Handle per-slot Use Discovered Commander button click."""
        if not self.config.discovered_commanders:
            self.slot_widgets[slot_index].append_log(
                f"Slot {slot_index}: no discovered commanders available"
            )
            return
        
        dialog = UseDiscoveredCommanderDialog(
            slot_index=slot_index,
            discovered_commanders=self.config.discovered_commanders,
            parent=self,
        )
        
        def on_commander_selected(fid: str) -> None:
            # Update the slot's FID to use the discovered commander
            slot = self.config.carrier_slots[slot_index]
            slot.fid = fid
            
            # Find the commander name
            for cmdr in self.config.discovered_commanders:
                if cmdr.fid == fid:
                    slot.commander_name = cmdr.name
                    break
            
            # Update widget title
            self.slot_widgets[slot_index].title_label.setText(
                f"Slot {slot_index}: {slot.commander_name or 'Unnamed'}"
            )
            
            # Re-classify the slot
            self._update_classification()
            
            self.slot_widgets[slot_index].append_log(
                f"Slot {slot_index} bound to discovered commander {fid}"
            )
            self.worker_controller.sync_slots(self.config, self.binding_snapshots)
        
        dialog.commander_selected.connect(on_commander_selected)
        dialog.exec()
    
    def _on_slot_enabled_changed(self, slot_index: int, enabled: bool) -> None:
        """Handle slot enable/disable toggle."""
        for slot in self.config.carrier_slots:
            if slot.slot_index == slot_index:
                slot.enabled = enabled
                self.config_changed.emit()
                break
                
        self.slot_widgets[slot_index]._update_button_states()
        
        action = "enabled" if enabled else "disabled"
        self.slot_widgets[slot_index].append_log(f"Slot {slot_index} {action} by user")
    
    def _on_slot_state_changed(self, slot_index: int, state_val: str) -> None:
        """Handle slot state change signal from worker controller."""
        if slot_index in self.slot_widgets:
            try:
                state = WorkerState(state_val)
                self.slot_widgets[slot_index].set_state(state)
            except ValueError:
                # Invalid state string, just display as-is
                self.slot_widgets[slot_index].set_status(state_val)
    
    def _on_slot_log(self, slot_index: int, message: str) -> None:
        """Handle slot log signal from worker controller."""
        if slot_index in self.slot_widgets:
            self.slot_widgets[slot_index].append_log(message)
    
    def refresh_bindings(self) -> None:
        """Refresh slot bindings (call after config changes or window discovery)."""
        self._update_classification()
