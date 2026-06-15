from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, 
    QLineEdit, QCheckBox, QSpinBox, QComboBox, QPushButton, QLabel,
    QTimeEdit, QGroupBox
)
from PySide6.QtCore import Signal, QTime
from TraversalSystem.gui_config import CarrierSlotConfig

class SlotEditorWidget(QWidget):
    slot_saved = Signal(CarrierSlotConfig)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_slot = None

        layout = QVBoxLayout(self)

        self.form_layout = QFormLayout()
        
        self.display_name_input = QLineEdit()
        self.form_layout.addRow("Slot Display Name:", self.display_name_input)
        
        self.cmd_name_input = QLineEdit()
        self.form_layout.addRow("Commander Name:", self.cmd_name_input)
        
        self.fid_input = QLineEdit()
        self.form_layout.addRow("Frontier ID (FID):", self.fid_input)
        
        self.carrier_id_input = QLineEdit()
        self.form_layout.addRow("Carrier ID:", self.carrier_id_input)
        
        self.route_file_input = QLineEdit()
        self.form_layout.addRow("Route File:", self.route_file_input)
        
        self.route_position_spin = QSpinBox()
        self.route_position_spin.setRange(0, 9999)
        self.form_layout.addRow("Route Position:", self.route_position_spin)
        
        self.tritium_spin = QSpinBox()
        self.tritium_spin.setRange(0, 9999)
        self.form_layout.addRow("Tritium Slot:", self.tritium_spin)
        
        self.refuel_combo = QComboBox()
        self.refuel_combo.addItems(["Personal (first 8)", "Personal (after 8)", "Squadron"])
        self.form_layout.addRow("Refuel Mode:", self.refuel_combo)
        
        self.enabled_check = QCheckBox("Slot Enabled")
        self.form_layout.addRow("", self.enabled_check)
        
        self.auto_plot_check = QCheckBox("Auto-plot Jumps")
        self.form_layout.addRow("", self.auto_plot_check)
        
        self.disable_refuel_check = QCheckBox("Disable Refuel")
        self.form_layout.addRow("", self.disable_refuel_check)
        
        self.single_discord_check = QCheckBox("Single Discord Message")
        self.form_layout.addRow("", self.single_discord_check)

        # Scheduled Jump group
        scheduled_group = QGroupBox("Scheduled Jump")
        sj_layout = QFormLayout(scheduled_group)

        self.scheduled_jump_time_input = QTimeEdit()
        self.scheduled_jump_time_input.setDisplayFormat("HH:mm:ss")
        self.scheduled_jump_time_input.setTime(QTime(0, 0, 0))
        sj_layout.addRow("Jump Time (UTC):", self.scheduled_jump_time_input)

        self.scheduled_jump_x_spin = QSpinBox()
        self.scheduled_jump_x_spin.setRange(0, 3840)
        sj_layout.addRow("Button X:", self.scheduled_jump_x_spin)

        self.scheduled_jump_y_spin = QSpinBox()
        self.scheduled_jump_y_spin.setRange(0, 2160)
        sj_layout.addRow("Button Y:", self.scheduled_jump_y_spin)

        self.form_layout.addRow(scheduled_group)

        layout.addLayout(self.form_layout)

        self.status_label = QLabel()
        layout.addWidget(self.status_label)

        btn_layout = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(self.save_btn)
        btn_layout.addStretch()
        
        layout.addLayout(btn_layout)
        self.setEnabled(False)

        self.cmd_name_input.textChanged.connect(self._update_validation)
        self.fid_input.textChanged.connect(self._update_validation)

    def set_slot(self, slot: CarrierSlotConfig | None):
        self._current_slot = slot
        if not slot:
            self.setEnabled(False)
            self._clear_fields()
            return
            
        self.setEnabled(True)
        self.display_name_input.setText(slot.display_name)
        self.cmd_name_input.setText(slot.commander_name)
        self.fid_input.setText(slot.fid)
        self.carrier_id_input.setText(slot.carrier_id)
        self.route_file_input.setText(slot.route_file)
        self.route_position_spin.setValue(slot.route_position)
        self.tritium_spin.setValue(slot.tritium_slot)
        self.refuel_combo.setCurrentIndex(slot.refuel_mode)
        self.enabled_check.setChecked(slot.enabled)
        self.auto_plot_check.setChecked(slot.auto_plot_jumps)
        self.disable_refuel_check.setChecked(slot.disable_refuel)
        self.single_discord_check.setChecked(slot.single_discord_message)
        
        if slot.scheduled_jump_time:
            t = QTime.fromString(slot.scheduled_jump_time, "HH:mm:ss")
            if t.isValid():
                self.scheduled_jump_time_input.setTime(t)
        self.scheduled_jump_x_spin.setValue(slot.scheduled_jump_button_x)
        self.scheduled_jump_y_spin.setValue(slot.scheduled_jump_button_y)
        
        self._update_validation()

    def _clear_fields(self):
        self.display_name_input.clear()
        self.cmd_name_input.clear()
        self.fid_input.clear()
        self.carrier_id_input.clear()
        self.route_file_input.clear()
        self.route_position_spin.setValue(0)
        self.tritium_spin.setValue(0)
        self.refuel_combo.setCurrentIndex(0)
        self.enabled_check.setChecked(True)
        self.auto_plot_check.setChecked(True)
        self.disable_refuel_check.setChecked(False)
        self.single_discord_check.setChecked(False)
        self.scheduled_jump_time_input.setTime(QTime(0, 0, 0))
        self.scheduled_jump_x_spin.setValue(0)
        self.scheduled_jump_y_spin.setValue(0)
        self.status_label.setText("")

    def _update_validation(self):
        cmd = self.cmd_name_input.text().strip()
        fid = self.fid_input.text().strip()
        
        if cmd and fid:
            self.status_label.setText("Status: unbound (Configured — pending discovery)")
            self.status_label.setStyleSheet("color: #ccaa00;")
        else:
            self.status_label.setText("Status: unbound (Requires Name and FID)")
            self.status_label.setStyleSheet("color: orange;")

    def _on_save(self):
        if not self._current_slot:
            return
            
        self._current_slot.display_name = self.display_name_input.text().strip()
        self._current_slot.commander_name = self.cmd_name_input.text().strip()
        self._current_slot.fid = self.fid_input.text().strip()
        self._current_slot.carrier_id = self.carrier_id_input.text().strip()
        self._current_slot.route_file = self.route_file_input.text().strip()
        self._current_slot.route_position = self.route_position_spin.value()
        self._current_slot.tritium_slot = self.tritium_spin.value()
        self._current_slot.refuel_mode = self.refuel_combo.currentIndex()
        self._current_slot.enabled = self.enabled_check.isChecked()
        self._current_slot.auto_plot_jumps = self.auto_plot_check.isChecked()
        self._current_slot.disable_refuel = self.disable_refuel_check.isChecked()
        self._current_slot.single_discord_message = self.single_discord_check.isChecked()
        
        self._current_slot.scheduled_jump_time = self.scheduled_jump_time_input.time().toString("HH:mm:ss")
        self._current_slot.scheduled_jump_button_x = self.scheduled_jump_x_spin.value()
        self._current_slot.scheduled_jump_button_y = self.scheduled_jump_y_spin.value()
        
        # Fail-closed: editor saves always land in unbound. Only the
        # binding controller (journal discovery) can promote to ready.
        self._current_slot.state = "unbound"
            
        self.slot_saved.emit(self._current_slot)
