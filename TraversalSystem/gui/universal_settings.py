from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QCheckBox, QSpinBox, QComboBox, QPushButton, QFileDialog, QFormLayout, QGroupBox
)
from PySide6.QtCore import Signal
from TraversalSystem.gui_config import GuiConfig

class UniversalSettingsWidget(QWidget):
    settings_changed = Signal()

    def __init__(self, config: GuiConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        group_box = QGroupBox("Universal Settings")
        form_layout = QFormLayout(group_box)
        
        # Journal Directory
        journal_layout = QHBoxLayout()
        self.journal_dir_input = QLineEdit()
        self.journal_dir_btn = QPushButton("Browse...")
        journal_layout.addWidget(self.journal_dir_input)
        journal_layout.addWidget(self.journal_dir_btn)
        form_layout.addRow("Journal Directory:", journal_layout)
        
        # Default Route Directory
        route_dir_layout = QHBoxLayout()
        self.default_route_dir_input = QLineEdit()
        self.default_route_dir_btn = QPushButton("Browse...")
        route_dir_layout.addWidget(self.default_route_dir_input)
        route_dir_layout.addWidget(self.default_route_dir_btn)
        form_layout.addRow("Default Route Directory:", route_dir_layout)
        
        # Webhook URL
        self.webhook_input = QLineEdit()
        form_layout.addRow("Discord Webhook URL:", self.webhook_input)
        
        # Focus Timeout
        self.focus_timeout_spin = QSpinBox()
        self.focus_timeout_spin.setMinimum(1)
        self.focus_timeout_spin.setMaximum(60)
        form_layout.addRow("Focus Timeout (s):", self.focus_timeout_spin)
        
        # Ambiguous Window Policy
        self.ambiguous_policy_combo = QComboBox()
        self.ambiguous_policy_combo.addItems(["abort", "manual"])
        form_layout.addRow("Ambiguous Window Policy:", self.ambiguous_policy_combo)
        
        # Toggles
        self.auto_detect_check = QCheckBox("Auto-Detect Elite Window")
        form_layout.addRow("", self.auto_detect_check)
        
        self.single_discord_check = QCheckBox("Edit Single Discord Message")
        form_layout.addRow("", self.single_discord_check)
        
        self.shutdown_check = QCheckBox("Shutdown on Complete")
        form_layout.addRow("", self.shutdown_check)
        
        main_layout.addWidget(group_box)
        main_layout.addStretch()
        
        self._populate()
        self._connect_signals()

    def _populate(self) -> None:
        uni = self.config.universal
        self.journal_dir_input.setText(uni.journal_directory)
        self.default_route_dir_input.setText(uni.default_route_directory)
        self.webhook_input.setText(uni.webhook_url)
        self.focus_timeout_spin.setValue(uni.focus_timeout_seconds)
        self.ambiguous_policy_combo.setCurrentText(uni.ambiguous_window_policy)
        
        self.auto_detect_check.setChecked(uni.auto_detect_window)
        self.single_discord_check.setChecked(uni.single_discord_message)
        self.shutdown_check.setChecked(uni.shutdown_on_complete)

    def _connect_signals(self) -> None:
        self.journal_dir_btn.clicked.connect(self._browse_journal_dir)
        self.default_route_dir_btn.clicked.connect(self._browse_default_route_dir)
        self.journal_dir_input.textChanged.connect(self._on_change)
        self.default_route_dir_input.textChanged.connect(self._on_change)
        self.webhook_input.textChanged.connect(self._on_change)
        self.focus_timeout_spin.valueChanged.connect(self._on_change)
        self.ambiguous_policy_combo.currentTextChanged.connect(self._on_change)
        self.auto_detect_check.toggled.connect(self._on_change)
        self.single_discord_check.toggled.connect(self._on_change)
        self.shutdown_check.toggled.connect(self._on_change)

    def _browse_journal_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Journal Directory", self.journal_dir_input.text())
        if path:
            self.journal_dir_input.setText(path)

    def _browse_default_route_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Default Route Directory", self.default_route_dir_input.text())
        if path:
            self.default_route_dir_input.setText(path)

    def _on_change(self, *args) -> None:
        uni = self.config.universal
        uni.journal_directory = self.journal_dir_input.text()
        uni.default_route_directory = self.default_route_dir_input.text()
        uni.webhook_url = self.webhook_input.text()
        uni.focus_timeout_seconds = self.focus_timeout_spin.value()
        uni.ambiguous_window_policy = self.ambiguous_policy_combo.currentText()
        uni.auto_detect_window = self.auto_detect_check.isChecked()
        uni.single_discord_message = self.single_discord_check.isChecked()
        uni.shutdown_on_complete = self.shutdown_check.isChecked()
        self.settings_changed.emit()
