"""Tests for multicarrier configuration extension (Task 1)."""

from pathlib import Path

import pytest

from TraversalSystem.config import load_settings

FIXTURES = Path(__file__).parent / "fixtures" / "settings"


def _write_ini(tmp_path: Path, content: str) -> Path:
    """Write an INI string to a temp file and return its path."""
    ini_path = tmp_path / "test.ini"
    _ = ini_path.write_text(content, encoding="utf-8")
    return ini_path


class TestMulticarrierEnabled:
    """Loading settings with multi-commander mode enabled."""

    def test_enabled_mode_parses_all_fields(self):
        opts = load_settings(FIXTURES / "multicarrier_enabled.ini")
        assert opts.multi_commander_enabled is True
        assert opts.target_fid == "FID-PRIMARY"
        assert opts.auto_detect_window is True
        assert opts.focus_timeout_seconds == 5
        assert opts.ambiguous_window_policy == "abort"

    def test_legacy_fields_unchanged(self):
        opts = load_settings(FIXTURES / "multicarrier_enabled.ini")
        assert opts.auto_plot_jumps is True
        assert opts.disable_refuel is False
        assert opts.refuel_mode == 0


class TestMulticarrierDisabled:
    """Legacy single-commander behaviour when multicarrier is off."""

    def test_defaults_when_disabled(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=route.txt\n"
            + "multi-commander-enabled=false\ntarget-fid=\n",
        )
        opts = load_settings(ini)
        assert opts.multi_commander_enabled is False
        assert opts.target_fid == ""
        assert opts.auto_detect_window is True
        assert opts.ambiguous_window_policy == "abort"
        assert opts.focus_timeout_seconds == 5

    def test_missing_multicarrier_keys_defaults_to_disabled(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=route.txt\n",
        )
        opts = load_settings(ini)
        assert opts.multi_commander_enabled is False
        assert opts.target_fid == ""
        assert opts.auto_detect_window is True
        assert opts.ambiguous_window_policy == "abort"
        assert opts.focus_timeout_seconds == 5


class TestMissingTargetFid:
    """Validation gate: enabled mode requires non-empty target_fid."""

    def test_enabled_without_target_fid_raises_value_error(self):
        with pytest.raises(ValueError, match="target-fid"):
            _ = load_settings(FIXTURES / "missing_target_fid.ini")

    def test_enabled_with_whitespace_only_fid_raises(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=route.txt\n"
            + "multi-commander-enabled=true\ntarget-fid=   \n",
        )
        with pytest.raises(ValueError, match="target-fid"):
            _ = load_settings(ini)


class TestDefaultPolicyValues:
    """Verify deterministic defaults for focus_timeout_seconds and ambiguous_window_policy."""

    def test_focus_timeout_clamped_to_minimum_one(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=route.txt\n"
            + "multi-commander-enabled=true\ntarget-fid=FID-X\n"
            + "focus-timeout-seconds=0\n",
        )
        opts = load_settings(ini)
        assert opts.focus_timeout_seconds == 1

    def test_focus_timeout_invalid_value_gets_default(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=route.txt\n"
            + "multi-commander-enabled=true\ntarget-fid=FID-X\n"
            + "focus-timeout-seconds=abc\n",
        )
        opts = load_settings(ini)
        assert opts.focus_timeout_seconds == 5

    def test_ambiguous_policy_empty_falls_back_to_abort(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=route.txt\n"
            + "multi-commander-enabled=true\ntarget-fid=FID-X\n"
            + "ambiguous-window-policy=\n",
        )
        opts = load_settings(ini)
        assert opts.ambiguous_window_policy == "abort"

    def test_unsupported_ambiguous_policy_raises(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=route.txt\n"
            + "multi-commander-enabled=true\ntarget-fid=FID-X\n"
            + "ambiguous-window-policy=prompt\n",
        )
        with pytest.raises(ValueError, match="ambiguous-window-policy"):
            _ = load_settings(ini)

    def test_warn_policy_also_raises(self, tmp_path: Path):
        ini = _write_ini(
            tmp_path,
            "webhook_url=\njournal_directory=~\nroute_file=route.txt\n"
            + "multi-commander-enabled=true\ntarget-fid=FID-X\n"
            + "ambiguous-window-policy=warn\n",
        )
        with pytest.raises(ValueError, match="ambiguous-window-policy"):
            _ = load_settings(ini)
