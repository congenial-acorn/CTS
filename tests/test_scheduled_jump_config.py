"""Tests for scheduled jump fields on CarrierSlotConfig."""
import json

import pytest

from TraversalSystem.gui_config import (
    CarrierSlotConfig,
    GuiConfig,
    UniversalSettings,
    gui_config_to_dict,
    load_gui_config,
    save_gui_config,
)


class TestScheduledJumpConfig:
    """Verify scheduled_jump_time, scheduled_jump_button_x/y defaults, persistence, and compat."""

    def test_default_values(self):
        slot = CarrierSlotConfig(slot_index=0)
        assert slot.scheduled_jump_time == ""
        assert slot.scheduled_jump_button_x == 0
        assert slot.scheduled_jump_button_y == 0

    def test_json_round_trip(self, tmp_path):
        slot = CarrierSlotConfig(
            slot_index=0,
            fid="F12345",
            commander_name="TestCmdr",
            state="ready",
            scheduled_jump_time="14:30:00",
            scheduled_jump_button_x=960,
            scheduled_jump_button_y=540,
        )
        config = GuiConfig(
            universal=UniversalSettings(
                journal_directory="/tmp",
                default_route_directory="/tmp",
            ),
            carrier_slots=[slot],
        )
        path = tmp_path / "config.json"
        save_gui_config(config, path)
        loaded = load_gui_config(path)
        s = loaded.carrier_slots[0]
        assert s.scheduled_jump_time == "14:30:00"
        assert s.scheduled_jump_button_x == 960
        assert s.scheduled_jump_button_y == 540

    def test_backward_compat_missing_fields(self, tmp_path):
        old_json = {
            "schema_version": 1,
            "universal": {
                "journal_directory": "/tmp",
                "default_route_directory": "/tmp",
            },
            "carrier_slots": [{"slot_index": 0, "state": "unbound"}],
        }
        path = tmp_path / "old_config.json"
        path.write_text(json.dumps(old_json), encoding="utf-8")
        loaded = load_gui_config(path)
        s = loaded.carrier_slots[0]
        assert s.scheduled_jump_time == ""
        assert s.scheduled_jump_button_x == 0
        assert s.scheduled_jump_button_y == 0

    def test_scheduled_jump_fields_in_serialized_output(self):
        slot = CarrierSlotConfig(
            slot_index=0,
            scheduled_jump_time="14:30:00",
            scheduled_jump_button_x=960,
            scheduled_jump_button_y=540,
        )
        config = GuiConfig(
            universal=UniversalSettings(
                journal_directory="/tmp",
                default_route_directory="/tmp",
            ),
            carrier_slots=[slot],
        )
        d = gui_config_to_dict(config)
        slot_dict = d["carrier_slots"][0]
        assert slot_dict["scheduled_jump_time"] == "14:30:00"
        assert slot_dict["scheduled_jump_button_x"] == 960
        assert slot_dict["scheduled_jump_button_y"] == 540

    def test_round_trip_with_existing_fixture(self, tmp_path):
        """Build a config inline (no fixture file), set scheduled jump, save/reload, verify."""
        slot = CarrierSlotConfig(
            slot_index=0,
            fid="F99999",
            commander_name="FixtureCmdr",
            state="ready",
            route_file="",
        )
        config = GuiConfig(
            universal=UniversalSettings(
                journal_directory="/tmp",
                default_route_directory="/tmp",
            ),
            carrier_slots=[slot],
        )
        config.carrier_slots[0].scheduled_jump_time = "23:59:00"
        path = tmp_path / "fixture_config.json"
        save_gui_config(config, path)
        loaded = load_gui_config(path)
        assert loaded.carrier_slots[0].scheduled_jump_time == "23:59:00"
