
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
    QListView, QStatusBar, QSplitter, QTabWidget
)
from PySide6.QtCore import Qt, QModelIndex, QTimer
from .theme import STYLESHEET
from .slot_model import SlotListModel
from .slot_editor import SlotEditorWidget
from TraversalSystem.gui_config import GuiConfig, CarrierSlotConfig, save_gui_config
from TraversalSystem.gui.binding_controller import BindingController
from TraversalSystem.gui.worker_controller import WorkerController
from TraversalSystem.gui.dashboard import DashboardWidget
from TraversalSystem.gui.universal_settings import UniversalSettingsWidget
from TraversalSystem.multi_journal_router import MultiJournalRouter
from TraversalSystem.window_manager import enumerate_elite_windows

class CTSMainWindow(QMainWindow):
    universalSettingsPanel: QWidget
    tab_widget: QTabWidget
    router: MultiJournalRouter
    carrierList: QListView
    slotEditor: SlotEditorWidget
    startAllButton: QPushButton
    stopAllButton: QPushButton
    addSlotButton: QPushButton
    removeSlotButton: QPushButton
    config: GuiConfig
    slotModel: SlotListModel
    dashboard: DashboardWidget
    binding_controller: BindingController
    worker_controller: WorkerController

    def __init__(self, config: GuiConfig | None = None, config_path: str = "gui_config.json", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CTS Multi-Carrier Automation")
        self.resize(1024, 768)
        self.setStyleSheet(STYLESHEET)
        
        self.config = config or GuiConfig()
        self.config_path = config_path
        
        # Initialize controllers
        self._init_controllers()
        
        # Initialize background refresh timer
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._on_background_refresh)
        self.refresh_timer.start(5000)  # 5 seconds
        
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        
        # Tab widget for Configuration and Dashboard
        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)
        
        # Configuration Tab
        config_tab = QWidget()
        config_layout = QVBoxLayout(config_tab)
        
        splitter = QSplitter(Qt.Orientation.Horizontal)
        config_layout.addWidget(splitter)
        
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        
        self.carrierList = QListView()
        self.carrierList.setObjectName("carrierList")
        self.slotModel = SlotListModel(self.config)
        self.carrierList.setModel(self.slotModel)
        left_layout.addWidget(self.carrierList)
        
        btn_layout = QHBoxLayout()
        self.addSlotButton = QPushButton("Add Slot")
        self.addSlotButton.setObjectName("addSlotButton")
        self.removeSlotButton = QPushButton("Remove Slot")
        self.removeSlotButton.setObjectName("removeSlotButton")
        btn_layout.addWidget(self.addSlotButton)
        btn_layout.addWidget(self.removeSlotButton)
        left_layout.addLayout(btn_layout)
        
        splitter.addWidget(left_widget)
        
        self.slotEditor = SlotEditorWidget()
        splitter.addWidget(self.slotEditor)
        
        self.universalSettingsPanel = UniversalSettingsWidget(self.config)
        self.universalSettingsPanel.setObjectName("universalSettingsPanel")
        config_layout.addWidget(self.universalSettingsPanel)
        
        _ = self.tab_widget.addTab(config_tab, "Configuration")
        
        # Dashboard tab
        self.dashboard = DashboardWidget(
            self.config, self.binding_controller, self.worker_controller
        )
        _ = self.tab_widget.addTab(self.dashboard, "Dashboard")
        _ = self.dashboard.config_changed.connect(self._save_config_to_disk)
        
        self.startAllButton = self.dashboard.start_all_button
        self.stopAllButton = self.dashboard.stop_all_button
        
        # Status Bar
        status = QStatusBar()
        status.setObjectName("statusBar")
        self.setStatusBar(status)
        status.showMessage("System Ready")

        # Connections
        _ = self.addSlotButton.clicked.connect(self._on_add_slot)
        _ = self.removeSlotButton.clicked.connect(self._on_remove_slot)
        _ = self.carrierList.selectionModel().currentChanged.connect(self._on_slot_selected)
        _ = self.slotEditor.slot_saved.connect(self._on_slot_saved)
        _ = self.universalSettingsPanel.settings_changed.connect(self._on_universal_settings_saved)
        
        # Sync dashboard when switching to dashboard tab
        _ = self.tab_widget.currentChanged.connect(self._on_tab_changed)

    def _init_controllers(self) -> None:
        """Initialize binding and worker controllers."""
        # Initialize multi-journal router for FID discovery
        self.router = MultiJournalRouter()
        
        # Initialize binding controller
        self.binding_controller = BindingController(
            router=self.router,
            discover_windows=enumerate_elite_windows,
        )
        
        # Initialize worker controller
        self.worker_controller = WorkerController()
        
        # Sync slots with worker controller
        # We call classify_all first to get binding snapshots and populate discovered_commanders
        snapshots = self.binding_controller.classify_all(self.config)
        self.worker_controller.sync_slots(self.config, snapshots)

    def _on_background_refresh(self) -> None:
        """Periodically refresh discovery and bindings."""
        if hasattr(self, 'dashboard') and self.dashboard:
            self.dashboard.refresh_bindings()
            snapshots = self.binding_controller.classify_all(self.config)
            self.worker_controller.sync_slots(self.config, snapshots)

    def _on_tab_changed(self, index: int) -> None:
        """Handle tab change - refresh dashboard when switching to dashboard tab."""
        if index == 1:  # Dashboard tab (index 1)
            # Refresh bindings and sync with worker controller
            self.dashboard.refresh_bindings()
            
            # Sync slots with worker controller
            snapshots = self.binding_controller.classify_all(self.config)
            self.worker_controller.sync_slots(self.config, snapshots)
            
            self.statusBar().showMessage("Dashboard refreshed")

    def _on_add_slot(self) -> None:
        _ = self.slotModel.add_slot()

    def _on_remove_slot(self) -> None:
        idx = self.carrierList.currentIndex()
        if idx.isValid():
            self.slotModel.remove_slot(idx.row())
            self.slotEditor.set_slot(None)

    def _on_slot_selected(self, current: QModelIndex, previous: QModelIndex) -> None:
        _ = previous
        if current.isValid():
            # Use type: ignore for Qt data method returning Any
            slot = self.slotModel.data(current, int(Qt.ItemDataRole.UserRole))  # pyright: ignore[reportAny]
            if isinstance(slot, CarrierSlotConfig):
                self.slotEditor.set_slot(slot)
            else:
                self.slotEditor.set_slot(None)
        else:
            self.slotEditor.set_slot(None)

    def _on_slot_saved(self, slot_data: CarrierSlotConfig) -> None:
        idx = self.carrierList.currentIndex()
        if idx.isValid():
            self.slotModel.update_slot(idx.row(), slot_data)
            self._save_config_to_disk()

    def _on_universal_settings_saved(self) -> None:
        self._save_config_to_disk()

    def _save_config_to_disk(self) -> None:
        try:
            save_gui_config(self.config, self.config_path)
            self.statusBar().showMessage(f"Configuration saved to {self.config_path}")
        except Exception as e:
            self.statusBar().showMessage(f"Failed to save configuration: {e}")
