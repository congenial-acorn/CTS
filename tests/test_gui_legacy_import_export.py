"""Tests for legacy settings.ini import/export (Task 11).

Covers:
- Import: flat INI → GuiConfig (universal + one slot)
- Export: selected slot from GuiConfig → flat INI
- Round-trip fidelity
- Error conditions (missing file, invalid slot index)
- JSON authority: import is one-way, not continuous sync
"""
from pathlib import Path

import pytest

from TraversalSystem.gui_config import GuiConfig, CarrierSlotConfig, UniversalSettings
from TraversalSystem.gui_legacy_import_export import (  # pyright: ignore[reportMissingImports]
    export_legacy_settings,
    import_legacy_settings,
)

FIXTURES = Path(__file__).parent / "fixtures" / "settings"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_ini(tmp_path: Path, content: str) -> Path:
    """Write an INI string to a temp file and return its path."""
    ini_path = tmp_path / "test.ini"
    ini_path.write_text(content, encoding="utf-8")
    return ini_path


def _make_config_with_slot(
    *,
    fid: str = "FID-TEST",
    commander_name: str = "TestCmdr",
    carrier_id: str = "P-C12345",
    route_file: str = "/tmp/route.txt",
    route_position: int = 3,
    tritium_slot: int = 2,
    refuel_mode: int = 1,
    auto_plot_jumps: bool = False,
    disable_refuel: bool = True,
    state: str = "ready",
) -> GuiConfig:
    """Create a minimal GuiConfig with one carrier slot."""
    slot = CarrierSlotConfig(
        slot_index=0,
        fid=fid,
        commander_name=commander_name,
        carrier_id=carrier_id,
        route_file=route_file,
        route_position=route_position,
        tritium_slot=tritium_slot,
        refuel_mode=refuel_mode,
        auto_plot_jumps=auto_plot_jumps,
        disable_refuel=disable_refuel,
        state=state,
    )
    return GuiConfig(
        universal=UniversalSettings(
            webhook_url="https://discord.example.com/webhook",
            journal_directory="/home/test/journals",
            multi_commander_enabled=True,
            auto_detect_window=False,
            focus_timeout_seconds=10,
            ambiguous_window_policy="manual",
            single_discord_message=True,
            shutdown_on_complete=False,
            power_saving=True,
        ),
        carrier_slots=[slot],
    )


# ===========================================================================
# IMPORT TESTS
# ===========================================================================


class TestImportBasic:
    """Basic import from a fully populated legacy INI."""

    def test_import_populates_universal(self):
        cfg = import_legacy_settings(FIXTURES / "multicarrier_enabled.ini")
        uni = cfg.universal
        assert uni.webhook_url == ""
        assert uni.journal_directory == "~"
        assert uni.multi_commander_enabled is True
        assert uni.auto_detect_window is True
        assert uni.focus_timeout_seconds == 5
        assert uni.ambiguous_window_policy == "abort"
        assert uni.single_discord_message is False
        assert uni.shutdown_on_complete is False
        assert uni.power_saving is False

    def test_import_creates_one_slot(self):
        cfg = import_legacy_settings(FIXTURES / "multicarrier_enabled.ini")
        assert len(cfg.carrier_slots) == 1
        slot = cfg.carrier_slots[0]
        assert slot.slot_index == 0
        assert slot.fid == "FID-PRIMARY"
        # Fail-closed: imported FIDs are always unbound until discovery
        assert slot.state == "unbound"

    def test_import_slot_fields(self):
        cfg = import_legacy_settings(FIXTURES / "multicarrier_enabled.ini")
        slot = cfg.carrier_slots[0]
        assert slot.route_position == 0
        assert slot.tritium_slot == 0  # fixture has no tritium_slot key → default
        assert slot.auto_plot_jumps is True
        assert slot.disable_refuel is False
        assert slot.refuel_mode == 0

    def test_import_commander_name_empty(self):
        """Legacy INI does not carry commander_name."""
        cfg = import_legacy_settings(FIXTURES / "multicarrier_enabled.ini")
        assert cfg.carrier_slots[0].commander_name == ""

    def test_import_carrier_id_empty(self):
        """Legacy INI does not carry carrier_id."""
        cfg = import_legacy_settings(FIXTURES / "multicarrier_enabled.ini")
        assert cfg.carrier_slots[0].carrier_id == ""

    def test_import_schema_version(self):
        cfg = import_legacy_settings(FIXTURES / "multicarrier_enabled.ini")
        assert cfg.schema_version == 1


class TestImportMinimal:
    """Import from a minimal INI (missing optional keys)."""

    def test_defaults_with_minimal_ini(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=route.txt\n",
        )
        cfg = import_legacy_settings(ini)
        assert cfg.universal.multi_commander_enabled is False
        assert cfg.universal.auto_detect_window is True
        assert cfg.universal.focus_timeout_seconds == 5
        assert cfg.universal.ambiguous_window_policy == "abort"
        assert cfg.universal.shutdown_on_complete is True
        slot = cfg.carrier_slots[0]
        assert slot.fid == ""
        assert slot.state == "unbound"
        assert slot.route_position == 0
        assert slot.tritium_slot == 0
        assert slot.auto_plot_jumps is True
        assert slot.disable_refuel is False
        assert slot.refuel_mode == 0


class TestImportRouteFileResolution:
    """Route file relative path resolution."""

    def test_relative_route_resolved_against_ini_dir(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=my_route.txt\n",
        )
        cfg = import_legacy_settings(ini)
        route = cfg.carrier_slots[0].route_file
        assert Path(route).is_absolute()
        assert route.endswith("my_route.txt")

    def test_absolute_route_preserved(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=/absolute/route.txt\n",
        )
        cfg = import_legacy_settings(ini)
        assert Path(cfg.carrier_slots[0].route_file) == Path("/absolute/route.txt")


class TestImportMissingFile:
    """Import raises FileNotFoundError for missing INI."""

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="Missing settings file"):
            import_legacy_settings(tmp_path / "nonexistent.ini")


class TestImportFocusTimeoutClamping:
    """Focus timeout clamped to minimum 1."""

    def test_zero_clamped_to_one(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=r.txt\n"
            "focus-timeout-seconds=0\n",
        )
        cfg = import_legacy_settings(ini)
        assert cfg.universal.focus_timeout_seconds == 1

    def test_negative_clamped_to_one(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=r.txt\n"
            "focus-timeout-seconds=-5\n",
        )
        cfg = import_legacy_settings(ini)
        assert cfg.universal.focus_timeout_seconds == 1


class TestImportAmbiguousPolicyFallback:
    """Empty ambiguous-window-policy defaults to 'abort'."""

    def test_empty_policy_gets_abort(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=r.txt\n"
            "ambiguous-window-policy=\n",
        )
        cfg = import_legacy_settings(ini)
        assert cfg.universal.ambiguous_window_policy == "abort"


class TestImportUnboundWhenNoFid:
    """Slot starts unbound when target-fid is empty."""

    def test_empty_fid_means_unbound(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=r.txt\n"
            "target-fid=\n",
        )
        cfg = import_legacy_settings(ini)
        assert cfg.carrier_slots[0].state == "unbound"
        assert cfg.carrier_slots[0].fid == ""

    def test_whitespace_fid_means_unbound(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=r.txt\n"
            "target-fid=   \n",
        )
        cfg = import_legacy_settings(ini)
        assert cfg.carrier_slots[0].state == "unbound"


# ===========================================================================
# EXPORT TESTS
# ===========================================================================


class TestExportBasic:
    """Basic export of a selected slot to legacy INI."""

    def test_export_writes_file(self, tmp_path: Path):
        cfg = _make_config_with_slot()
        out = tmp_path / "exported.ini"
        export_legacy_settings(cfg, 0, out)
        assert out.exists()

    def test_export_contains_all_keys(self, tmp_path: Path):
        cfg = _make_config_with_slot()
        out = tmp_path / "exported.ini"
        export_legacy_settings(cfg, 0, out)
        text = out.read_text(encoding="utf-8")
        expected_keys = [
            "webhook_url=",
            "journal_directory=",
            "tritium_slot=",
            "route_file=",
            "route_position=",
            "auto-plot-jumps=",
            "disable-refuel=",
            "power-saving=",
            "refuel-mode=",
            "single-discord-message=",
            "shutdown-on-complete=",
            "multi-commander-enabled=",
            "target-fid=",
            "auto-detect-window=",
            "focus-timeout-seconds=",
            "ambiguous-window-policy=",
        ]
        for key in expected_keys:
            assert key in text, f"Missing key '{key}' in exported INI"

    def test_export_universal_values(self, tmp_path: Path):
        cfg = _make_config_with_slot()
        out = tmp_path / "exported.ini"
        export_legacy_settings(cfg, 0, out)
        text = out.read_text(encoding="utf-8")
        assert "webhook_url=https://discord.example.com/webhook" in text
        assert "journal_directory=/home/test/journals" in text
        assert "multi-commander-enabled=true" in text
        assert "auto-detect-window=false" in text
        assert "focus-timeout-seconds=10" in text
        assert "ambiguous-window-policy=manual" in text
        assert "single-discord-message=true" in text
        assert "shutdown-on-complete=false" in text
        assert "power-saving=true" in text

    def test_export_slot_values(self, tmp_path: Path):
        cfg = _make_config_with_slot()
        out = tmp_path / "exported.ini"
        export_legacy_settings(cfg, 0, out)
        text = out.read_text(encoding="utf-8")
        assert "target-fid=FID-TEST" in text
        assert "route_file=/tmp/route.txt" in text
        assert "route_position=3" in text
        assert "tritium_slot=2" in text
        assert "refuel-mode=1" in text
        assert "auto-plot-jumps=false" in text
        assert "disable-refuel=true" in text

    def test_export_has_authority_comment(self, tmp_path: Path):
        cfg = _make_config_with_slot()
        out = tmp_path / "exported.ini"
        export_legacy_settings(cfg, 0, out)
        text = out.read_text(encoding="utf-8")
        assert "authoritative" in text.lower()


class TestExportInvalidSlot:
    """Export rejects invalid slot indices."""

    def test_negative_index_raises(self, tmp_path: Path):
        cfg = _make_config_with_slot()
        with pytest.raises(ValueError, match="slot_index.*out of range"):
            export_legacy_settings(cfg, -1, tmp_path / "out.ini")

    def test_too_large_index_raises(self, tmp_path: Path):
        cfg = _make_config_with_slot()
        with pytest.raises(ValueError, match="slot_index.*out of range"):
            export_legacy_settings(cfg, 5, tmp_path / "out.ini")

    def test_empty_slots_raises(self, tmp_path: Path):
        cfg = GuiConfig(carrier_slots=[])
        with pytest.raises(ValueError, match="slot_index.*out of range"):
            export_legacy_settings(cfg, 0, tmp_path / "out.ini")


class TestExportCreatesDirectory:
    """Export creates parent directories if needed."""

    def test_creates_missing_parent_dir(self, tmp_path: Path):
        cfg = _make_config_with_slot()
        out = tmp_path / "sub" / "dir" / "settings.ini"
        export_legacy_settings(cfg, 0, out)
        assert out.exists()


# ===========================================================================
# ROUND-TRIP TESTS
# ===========================================================================


class TestRoundTrip:
    """Import → Export round-trip fidelity."""

    def test_round_trip_preserves_values(self, tmp_path: Path):
        """Import a fixture, export slot 0, re-import, compare key fields."""
        cfg1 = import_legacy_settings(FIXTURES / "multicarrier_enabled.ini")
        out = tmp_path / "roundtrip.ini"
        export_legacy_settings(cfg1, 0, out)

        cfg2 = import_legacy_settings(out)

        # Universal
        assert cfg2.universal.webhook_url == cfg1.universal.webhook_url
        assert cfg2.universal.journal_directory == cfg1.universal.journal_directory
        assert cfg2.universal.multi_commander_enabled == cfg1.universal.multi_commander_enabled
        assert cfg2.universal.auto_detect_window == cfg1.universal.auto_detect_window
        assert cfg2.universal.focus_timeout_seconds == cfg1.universal.focus_timeout_seconds
        assert cfg2.universal.ambiguous_window_policy == cfg1.universal.ambiguous_window_policy
        assert cfg2.universal.single_discord_message == cfg1.universal.single_discord_message
        assert cfg2.universal.shutdown_on_complete == cfg1.universal.shutdown_on_complete
        assert cfg2.universal.power_saving == cfg1.universal.power_saving

        # Slot
        s1, s2 = cfg1.carrier_slots[0], cfg2.carrier_slots[0]
        assert s2.fid == s1.fid
        assert s2.route_position == s1.route_position
        assert s2.tritium_slot == s1.tritium_slot
        assert s2.auto_plot_jumps == s1.auto_plot_jumps
        assert s2.disable_refuel == s1.disable_refuel
        assert s2.refuel_mode == s1.refuel_mode
        assert s2.state == s1.state


class TestJsonAuthority:
    """JSON remains authoritative — import is one-way, not continuous."""

    def test_import_does_not_modify_original_ini(self, tmp_path: Path):
        """Importing an INI does not modify the source file."""
        ini_content = (
            "webhook_url=original\njournal_directory=~\nroute_file=r.txt\n"
            "target-fid=FID-ORIG\n"
        )
        ini = _write_ini(tmp_path, ini_content)
        original_text = ini.read_text(encoding="utf-8")

        import_legacy_settings(ini)

        assert ini.read_text(encoding="utf-8") == original_text

    def test_export_does_not_modify_config(self, tmp_path: Path):
        """Exporting a slot does not mutate the GuiConfig object."""
        cfg = _make_config_with_slot()
        fid_before = cfg.carrier_slots[0].fid
        webhook_before = cfg.universal.webhook_url

        export_legacy_settings(cfg, 0, tmp_path / "out.ini")

        assert cfg.carrier_slots[0].fid == fid_before
        assert cfg.universal.webhook_url == webhook_before
