import pytest # pyright: ignore[reportMissingImports]
from collections.abc import Generator
from PySide6.QtWidgets import QApplication # pyright: ignore[reportMissingImports]
from TraversalSystem.gui_config import GuiConfig, CarrierSlotConfig # pyright: ignore[reportMissingImports]
from TraversalSystem.gui.slot_model import SlotListModel # pyright: ignore[reportMissingImports]
from TraversalSystem.gui.slot_editor import SlotEditorWidget # pyright: ignore[reportMissingImports]

@pytest.fixture(scope="session")
def qapp() -> Generator[QApplication, None, None]:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    assert isinstance(app, QApplication)
    yield app

@pytest.fixture
def empty_config() -> GuiConfig:
    return GuiConfig()

def test_slot_model_row_count(empty_config: GuiConfig) -> None:
    model = SlotListModel(empty_config)
    assert model.rowCount() == 0
    empty_config.carrier_slots.append(CarrierSlotConfig(slot_index=0))
    assert model.rowCount() == 1

def test_slot_model_add_remove(empty_config: GuiConfig) -> None:
    model = SlotListModel(empty_config)
    slot = model.add_slot()
    assert model.rowCount() == 1
    assert slot.slot_index == 0
    assert len(empty_config.carrier_slots) == 1
    
    model.remove_slot(0)
    assert model.rowCount() == 0
    assert len(empty_config.carrier_slots) == 0

def test_slot_model_update_persistence(empty_config: GuiConfig) -> None:
    model = SlotListModel(empty_config)
    slot = model.add_slot()
    
    # Edit the slot
    slot.commander_name = "Cmdr Test"
    slot.fid = "F123"
    slot.state = "ready"
    
    model.update_slot(0, slot)
    
    # Check persistence back to config
    persisted = empty_config.carrier_slots[0]
    assert persisted.commander_name == "Cmdr Test"
    assert persisted.fid == "F123"
    assert persisted.state == "ready"

def test_slot_model_invalid_stays_unbound(empty_config: GuiConfig) -> None:
    model = SlotListModel(empty_config)
    slot = model.add_slot()
    
    # Missing FID should drop it to unbound
    slot.commander_name = "Cmdr Test"
    slot.fid = ""
    slot.state = "ready" # Attempt to bypass
    
    model.update_slot(0, slot)
    
    persisted = empty_config.carrier_slots[0]
    assert persisted.state == "unbound"

def test_slot_editor_validation(qapp: QApplication) -> None:
    _ = qapp
    editor = SlotEditorWidget()
    slot = CarrierSlotConfig(slot_index=0)
    editor.set_slot(slot)
    
    # Initially invalid (empty)
    editor._update_validation() # pyright: ignore[reportPrivateUsage]
    assert "unbound" in editor.status_label.text()
    
    editor.cmd_name_input.setText("Jameson")
    editor.fid_input.setText("F999")
    editor._update_validation() # pyright: ignore[reportPrivateUsage]
    
    # Fail-closed: editor shows pending-discovery, NOT ready
    assert "pending discovery" in editor.status_label.text().lower()
    assert "ready" not in editor.status_label.text().lower()

def test_slot_editor_save_signal(qapp: QApplication) -> None:
    _ = qapp
    editor = SlotEditorWidget()
    slot = CarrierSlotConfig(slot_index=0)
    editor.set_slot(slot)
    
    emitted: list[CarrierSlotConfig] = []
    
    def on_saved(s: CarrierSlotConfig) -> None:
        emitted.append(s)
        
    _ = editor.slot_saved.connect(on_saved)
    
    editor.cmd_name_input.setText("Jameson")
    editor.fid_input.setText("F999")
    editor._on_save() # pyright: ignore[reportPrivateUsage]
    
    assert len(emitted) == 1
    saved_slot = emitted[0]
    assert saved_slot.commander_name == "Jameson"
    assert saved_slot.fid == "F999"
    # Fail-closed: editor never promotes to ready; discovery only
    assert saved_slot.state == "unbound"
