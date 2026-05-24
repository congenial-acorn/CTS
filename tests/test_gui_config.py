"""Tests for TraversalSystem.gui_config — authoritative JSON GUI configuration model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from TraversalSystem.gui_config import (
    SCHEMA_VERSION,
    CarrierSlotConfig,
    DiscoveredCommander,
    GuiConfig,
    GuiConfigError,
    UniversalSettings,
    bind_slot_to_fid,
    gui_config_to_dict,
    load_gui_config,
    save_gui_config,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gui"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(tmp_path: Path, data: object, name: str = "gui_config.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


# ===================================================================
# Loading valid configs
# ===================================================================


class TestLoadValidSingleSlot:
    def test_parses_single_slot(self):
        cfg = load_gui_config(FIXTURES / "valid_single_slot.json")
        assert cfg.schema_version == 1
        assert len(cfg.carrier_slots) == 1
        slot = cfg.carrier_slots[0]
        assert slot.fid == "F12345"
        assert slot.commander_name == "TestCommander"
        assert slot.carrier_id == "P-C12-TEST"
        assert slot.state == "ready"
        assert slot.is_bound()

    def test_universal_settings(self):
        cfg = load_gui_config(FIXTURES / "valid_single_slot.json")
        uni = cfg.universal
        assert uni.webhook_url == "https://discord.example.com/webhook/test"
        assert uni.multi_commander_enabled is True
        assert uni.focus_timeout_seconds == 5
        assert uni.ambiguous_window_policy == "abort"


class TestLoadValidMultiSlot:
    def test_parses_three_slots(self):
        cfg = load_gui_config(FIXTURES / "valid_multi_slot.json")
        assert len(cfg.carrier_slots) == 3

    def test_first_two_ready_third_unbound(self):
        cfg = load_gui_config(FIXTURES / "valid_multi_slot.json")
        assert cfg.carrier_slots[0].state == "ready"
        assert cfg.carrier_slots[1].state == "ready"
        assert cfg.carrier_slots[2].state == "unbound"
        assert not cfg.carrier_slots[2].is_bound()

    def test_ambiguous_policy_manual(self):
        cfg = load_gui_config(FIXTURES / "valid_multi_slot.json")
        assert cfg.universal.ambiguous_window_policy == "manual"


class TestPathExpansion:
    def test_tilde_expanded_in_journal_directory(self):
        cfg = load_gui_config(FIXTURES / "valid_single_slot.json")
        journal = cfg.universal.journal_directory
        # Must be absolute and not start with raw "~"
        assert not journal.startswith("~")
        assert Path(journal).is_absolute()

    def test_relative_route_file_resolved(self):
        cfg = load_gui_config(FIXTURES / "valid_single_slot.json")
        route = cfg.carrier_slots[0].route_file
        # route_file "route.txt" should be resolved relative to the fixture dir
        assert Path(route).is_absolute()


class TestDefaults:
    def test_missing_keys_use_defaults(self, tmp_path: Path):
        """Minimal JSON with only schema_version still loads."""
        data = {"schema_version": 1, "universal": {}, "carrier_slots": []}
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.universal.focus_timeout_seconds == 5
        assert cfg.universal.ambiguous_window_policy == "abort"
        assert cfg.universal.auto_detect_window is True
        assert cfg.universal.webhook_url == ""
        assert cfg.carrier_slots == []

    def test_default_schema_version_constant(self):
        assert SCHEMA_VERSION == 1


# ===================================================================
# Round-trip (load → serialise → load)
# ===================================================================


class TestRoundTrip:
    def test_round_trip_preserves_data(self, tmp_path: Path):
        cfg = load_gui_config(FIXTURES / "valid_multi_slot.json")
        out_path = tmp_path / "round_trip.json"
        save_gui_config(cfg, out_path)

        cfg2 = load_gui_config(out_path)
        assert cfg2.schema_version == cfg.schema_version
        assert cfg2.universal.ambiguous_window_policy == "manual"
        assert len(cfg2.carrier_slots) == 3
        assert cfg2.carrier_slots[0].fid == "F10001"
        assert cfg2.carrier_slots[1].route_file.endswith("route_beta.csv")

    def test_to_dict_excludes_discovered_commanders(self):
        cfg = load_gui_config(FIXTURES / "valid_single_slot.json")
        cfg.discovered_commanders.append(
            DiscoveredCommander(name="Foo", fid="F00001")
        )
        d = gui_config_to_dict(cfg)
        assert "discovered_commanders" not in d


# ===================================================================
# Malformed JSON
# ===================================================================


class TestMalformedJSON:
    def test_malformed_json_raises_config_error(self):
        with pytest.raises(GuiConfigError, match="Invalid GUI config"):
            load_gui_config(FIXTURES / "malformed_json.json")

    def test_error_message_mentions_malformed(self):
        with pytest.raises(GuiConfigError, match="malformed JSON"):
            load_gui_config(FIXTURES / "malformed_json.json")

    def test_non_object_root(self, tmp_path: Path):
        p = _write_json(tmp_path, [1, 2, 3])
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*object"):
            load_gui_config(p)

    def test_missing_file_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_gui_config(tmp_path / "nonexistent.json")


# ===================================================================
# Validation: schema_version
# ===================================================================


class TestSchemaVersionValidation:
    def test_unsupported_version_raises(self):
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*schema_version"):
            load_gui_config(FIXTURES / "invalid_schema_version.json")


# ===================================================================
# Validation: focus_timeout_seconds
# ===================================================================


class TestFocusTimeoutValidation:
    def test_zero_timeout_raises(self):
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*focus_timeout"):
            load_gui_config(FIXTURES / "invalid_focus_timeout.json")

    def test_negative_timeout_raises(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {"focus_timeout_seconds": -1},
            "carrier_slots": [],
        }
        p = _write_json(tmp_path, data)
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*focus_timeout"):
            load_gui_config(p)


# ===================================================================
# Validation: ambiguous_window_policy
# ===================================================================


class TestAmbiguousPolicyValidation:
    def test_unsupported_policy_raises(self):
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*ambiguous"):
            load_gui_config(FIXTURES / "invalid_ambiguous_policy.json")

    @pytest.mark.parametrize("policy", ["abort", "manual"])
    def test_valid_policies_accepted(self, tmp_path: Path, policy: str):
        data = {
            "schema_version": 1,
            "universal": {"ambiguous_window_policy": policy},
            "carrier_slots": [],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.universal.ambiguous_window_policy == policy


# ===================================================================
# Validation: route file extension
# ===================================================================


class TestRouteFileValidation:
    def test_bad_extension_raises(self):
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*route file"):
            load_gui_config(FIXTURES / "invalid_route_extension.json")

    @pytest.mark.parametrize("ext", ["txt", "csv"])
    def test_valid_extensions_pass(self, tmp_path: Path, ext: str):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"route_file": f"route.{ext}", "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.carrier_slots[0].route_file.endswith(f".{ext}")


# ===================================================================
# Validation: ready slot must have FID
# ===================================================================


class TestReadySlotFidValidation:
    def test_ready_slot_empty_fid_raises(self):
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*slot FID"):
            load_gui_config(FIXTURES / "ready_slot_empty_fid.json")


# ===================================================================
# Unknown FID → unbound (NOT ready)
# ===================================================================


class TestUnknownFidStaysUnbound:
    def test_fixture_unknown_fid_is_unbound(self):
        cfg = load_gui_config(FIXTURES / "unknown_fid_unbound.json")
        slot = cfg.carrier_slots[0]
        assert slot.fid == "F99999"
        assert slot.state == "unbound"
        assert not slot.is_bound()

    def test_bind_unknown_fid_stays_unbound(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "fid": "", "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)

        # Bind to an FID not in discovered list
        slot = bind_slot_to_fid(cfg, 0, "F_UNDISCOVERED", discovered_fids=[])
        assert slot.fid == "F_UNDISCOVERED"
        assert slot.state == "unbound"
        assert not slot.is_bound()

    def test_bind_known_fid_transitions_to_ready(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "fid": "", "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)

        slot = bind_slot_to_fid(
            cfg, 0, "F_KNOWN", discovered_fids=["F_KNOWN", "F_OTHER"]
        )
        assert slot.fid == "F_KNOWN"
        assert slot.state == "ready"
        assert slot.is_bound()


# ===================================================================
# Slot binding edge cases
# ===================================================================


class TestBindSlotEdgeCases:
    def test_empty_fid_raises(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [{"slot_index": 0, "state": "unbound"}],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*empty FID"):
            bind_slot_to_fid(cfg, 0, "")

    def test_whitespace_fid_raises(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [{"slot_index": 0, "state": "unbound"}],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*empty FID"):
            bind_slot_to_fid(cfg, 0, "   ")

    def test_out_of_range_slot_raises(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [{"slot_index": 0, "state": "unbound"}],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*slot_index"):
            bind_slot_to_fid(cfg, 5, "F123")


# ===================================================================
# DiscoveredCommander
# ===================================================================


class TestDiscoveredCommanderValidation:
    def test_extra_discovered_merged(self, tmp_path: Path):
        data = {"schema_version": 1, "universal": {}, "carrier_slots": []}
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(
            p,
            extra_discovered=[
                DiscoveredCommander(name="Ryzen", fid="F42", carrier_id="P-C1"),
            ],
        )
        assert len(cfg.discovered_commanders) == 1
        assert cfg.discovered_commanders[0].name == "Ryzen"

    def test_invalid_commander_name_in_discovered(self, tmp_path: Path):
        data = {"schema_version": 1, "universal": {}, "carrier_slots": []}
        p = _write_json(tmp_path, data)
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*commander name"):
            load_gui_config(
                p,
                extra_discovered=[DiscoveredCommander(name="", fid="F42")],
            )

    def test_invalid_fid_in_discovered(self, tmp_path: Path):
        data = {"schema_version": 1, "universal": {}, "carrier_slots": []}
        p = _write_json(tmp_path, data)
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*FID"):
            load_gui_config(
                p,
                extra_discovered=[DiscoveredCommander(name="Alice", fid="")],
            )


# ===================================================================
# GuiConfig convenience lookups
# ===================================================================


class TestGuiConfigLookups:
    def test_slot_for_fid_returns_match(self):
        cfg = load_gui_config(FIXTURES / "valid_multi_slot.json")
        slot = cfg.slot_for_fid("F10002")
        assert slot is not None
        assert slot.commander_name == "BetaCmdr"

    def test_slot_for_fid_returns_none_on_miss(self):
        cfg = load_gui_config(FIXTURES / "valid_single_slot.json")
        assert cfg.slot_for_fid("NONEXISTENT") is None

    def test_active_slot_returns_first_ready(self):
        cfg = load_gui_config(FIXTURES / "valid_multi_slot.json")
        active = cfg.active_slot()
        assert active is not None
        assert active.fid == "F10001"

    def test_active_slot_none_when_all_unbound(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "state": "unbound"},
                {"slot_index": 1, "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.active_slot() is None


# ===================================================================
# Slot state validation
# ===================================================================


class TestSlotStateValidation:
    def test_invalid_state_raises(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [{"slot_index": 0, "state": "pending"}],
        }
        p = _write_json(tmp_path, data)
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*slot state"):
            load_gui_config(p)


# ===================================================================
# Commander name / carrier_id validation in slot
# ===================================================================


class TestSlotFieldValidation:
    def test_whitespace_commander_name_raises(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "commander_name": "   ", "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*commander name"):
            load_gui_config(p)

    def test_whitespace_carrier_id_raises(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "carrier_id": "  ", "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        with pytest.raises(GuiConfigError, match="Invalid GUI config.*carrier_id"):
            load_gui_config(p)

    def test_empty_carrier_id_is_allowed(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "carrier_id": "", "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.carrier_slots[0].carrier_id == ""


# ===================================================================
# Save creates parent dirs
# ===================================================================


class TestSaveCreatesDirs:
    def test_creates_nested_dirs(self, tmp_path: Path):
        target = tmp_path / "nested" / "dir" / "config.json"
        cfg = GuiConfig()
        save_gui_config(cfg, target)
        assert target.exists()
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded["schema_version"] == 1


# ===================================================================
# New universal field: default_route_directory
# ===================================================================


class TestDefaultRouteDirectory:
    def test_default_route_directory_loaded(self, tmp_path: Path):
        route_dir = str(tmp_path / "routes")
        data = {
            "schema_version": 1,
            "universal": {"default_route_directory": route_dir},
            "carrier_slots": [],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.universal.default_route_directory == route_dir

    def test_default_route_directory_empty_when_absent(self, tmp_path: Path):
        data = {"schema_version": 1, "universal": {}, "carrier_slots": []}
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.universal.default_route_directory == ""

    def test_default_route_directory_tilde_expanded(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {"default_route_directory": "~/routes"},
            "carrier_slots": [],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert not cfg.universal.default_route_directory.startswith("~")

    def test_default_route_directory_serialized(self, tmp_path: Path):
        cfg = GuiConfig(
            universal=UniversalSettings(default_route_directory="/tmp/out")
        )
        out_path = tmp_path / "out.json"
        save_gui_config(cfg, out_path)
        raw = json.loads(out_path.read_text(encoding="utf-8"))
        assert raw["universal"]["default_route_directory"] == "/tmp/out"

    def test_round_trip_preserves_default_route_directory(self, tmp_path: Path):
        cfg = load_gui_config(FIXTURES / "valid_single_slot.json")
        route_dir = str(tmp_path / "custom" / "routes")
        cfg.universal.default_route_directory = route_dir
        out_path = tmp_path / "rt.json"
        save_gui_config(cfg, out_path)
        cfg2 = load_gui_config(out_path)
        assert cfg2.universal.default_route_directory == route_dir


# ===================================================================
# New per-slot fields
# ===================================================================


class TestPerSlotNewFields:
    def test_display_name_loaded(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "display_name": "Alpha Carrier", "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.carrier_slots[0].display_name == "Alpha Carrier"

    def test_enabled_loaded(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "enabled": False, "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.carrier_slots[0].enabled is False

    def test_per_slot_power_saving(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "power_saving": True, "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.carrier_slots[0].power_saving is True

    def test_per_slot_single_discord_message(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "single_discord_message": True, "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.carrier_slots[0].single_discord_message is True

    def test_per_slot_shutdown_on_complete(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "shutdown_on_complete": False, "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.carrier_slots[0].shutdown_on_complete is False

    def test_per_slot_defaults_when_absent(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [{"slot_index": 0, "state": "unbound"}],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        slot = cfg.carrier_slots[0]
        assert slot.display_name == ""
        assert slot.enabled is True
        assert slot.power_saving is False
        assert slot.single_discord_message is False
        assert slot.shutdown_on_complete is True

    def test_per_slot_fields_round_trip(self, tmp_path: Path):
        cfg = GuiConfig(
            carrier_slots=[
                CarrierSlotConfig(
                    slot_index=0,
                    display_name="Test Slot",
                    enabled=False,
                    power_saving=True,
                    single_discord_message=True,
                    shutdown_on_complete=False,
                    state="unbound",
                ),
            ],
        )
        out_path = tmp_path / "slot_rt.json"
        save_gui_config(cfg, out_path)
        cfg2 = load_gui_config(out_path)
        slot = cfg2.carrier_slots[0]
        assert slot.display_name == "Test Slot"
        assert slot.enabled is False
        assert slot.power_saving is True
        assert slot.single_discord_message is True
        assert slot.shutdown_on_complete is False

    def test_route_position_loaded(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [
                {"slot_index": 0, "route_position": 7, "state": "unbound"},
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.carrier_slots[0].route_position == 7

    def test_per_slot_fields_in_serialized_output(self, tmp_path: Path):
        cfg = GuiConfig(
            carrier_slots=[
                CarrierSlotConfig(
                    slot_index=0,
                    display_name="Slot A",
                    enabled=True,
                    power_saving=False,
                    single_discord_message=True,
                    shutdown_on_complete=True,
                    state="unbound",
                ),
            ],
        )
        d = gui_config_to_dict(cfg)
        slot_d = d["carrier_slots"][0]
        assert slot_d["display_name"] == "Slot A"
        assert slot_d["enabled"] is True
        assert slot_d["power_saving"] is False
        assert slot_d["single_discord_message"] is True
        assert slot_d["shutdown_on_complete"] is True


# ===================================================================
# DiscoveredCommander metadata
# ===================================================================


class TestDiscoveredCommanderMetadata:
    def test_discovered_at_default_empty(self):
        cmdr = DiscoveredCommander(name="Test", fid="F001")
        assert cmdr.discovered_at == ""

    def test_discovery_status_default_confirmed(self):
        cmdr = DiscoveredCommander(name="Test", fid="F001")
        assert cmdr.discovery_status == "confirmed"

    def test_discovered_at_loaded_from_dict(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [],
            "discovered_commanders": [
                {
                    "name": "Alice",
                    "fid": "F100",
                    "discovered_at": "2026-05-23T12:00:00Z",
                    "discovery_status": "confirmed",
                },
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.discovered_commanders[0].discovered_at == "2026-05-23T12:00:00Z"
        assert cfg.discovered_commanders[0].discovery_status == "confirmed"

    def test_tentative_status_loaded(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "universal": {},
            "carrier_slots": [],
            "discovered_commanders": [
                {
                    "name": "Bob",
                    "fid": "F200",
                    "discovery_status": "tentative",
                },
            ],
        }
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(p)
        assert cfg.discovered_commanders[0].discovery_status == "tentative"

    def test_metadata_round_trip_via_save(self, tmp_path: Path):
        cfg = GuiConfig()
        cfg.discovered_commanders.append(
            DiscoveredCommander(
                name="Carol",
                fid="F300",
                discovered_at="2026-05-23T15:30:00Z",
                discovery_status="confirmed",
            )
        )
        out_path = tmp_path / "disc_rt.json"
        save_gui_config(cfg, out_path)

        raw = json.loads(out_path.read_text(encoding="utf-8"))
        assert "discovered_commanders" not in raw

    def test_extra_discovered_with_metadata(self, tmp_path: Path):
        data = {"schema_version": 1, "universal": {}, "carrier_slots": []}
        p = _write_json(tmp_path, data)
        cfg = load_gui_config(
            p,
            extra_discovered=[
                DiscoveredCommander(
                    name="Dave",
                    fid="F400",
                    discovered_at="2026-05-24T08:00:00Z",
                    discovery_status="tentative",
                ),
            ],
        )
        assert cfg.discovered_commanders[0].discovered_at == "2026-05-24T08:00:00Z"
        assert cfg.discovered_commanders[0].discovery_status == "tentative"
