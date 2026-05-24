from typing import Any
from PySide6.QtCore import QAbstractListModel, Qt, QModelIndex, QPersistentModelIndex, QObject
from TraversalSystem.gui_config import GuiConfig, CarrierSlotConfig

class SlotListModel(QAbstractListModel):
    _config: GuiConfig

    def __init__(self, config: GuiConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int: # pyright: ignore[reportImplicitOverride]
        if parent is not None and parent.isValid():
            return 0
        return len(self._config.carrier_slots)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any: # pyright: ignore[reportImplicitOverride]
        if not index.isValid():
            return None
        
        row = index.row()
        if not (0 <= row < len(self._config.carrier_slots)):
            return None
            
        slot = self._config.carrier_slots[row]
        
        if role == int(Qt.ItemDataRole.DisplayRole):
            name = slot.commander_name or "Unknown Commander"
            fid = slot.fid or "No FID"
            state = slot.state
            return f"{name} ({fid}) - {state}"
            
        if role == int(Qt.ItemDataRole.UserRole):
            return slot
            
        return None

    def add_slot(self) -> CarrierSlotConfig:
        self.beginInsertRows(QModelIndex(), self.rowCount(), self.rowCount())
        new_slot = CarrierSlotConfig(slot_index=self.rowCount())
        self._config.carrier_slots.append(new_slot)
        self.endInsertRows()
        return new_slot

    def remove_slot(self, row: int) -> None:
        if 0 <= row < len(self._config.carrier_slots):
            self.beginRemoveRows(QModelIndex(), row, row)
            _ = self._config.carrier_slots.pop(row)
            # Reindex
            for i, slot in enumerate(self._config.carrier_slots):
                slot.slot_index = i
            self.endRemoveRows()

    def update_slot(self, row: int, slot_data: CarrierSlotConfig) -> None:
        if 0 <= row < len(self._config.carrier_slots):
            # Check validation rules for 'ready' state
            if not slot_data.fid.strip() or not slot_data.commander_name.strip():
                slot_data.state = "unbound"
            
            self._config.carrier_slots[row] = slot_data
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [int(Qt.ItemDataRole.DisplayRole), int(Qt.ItemDataRole.UserRole)])
