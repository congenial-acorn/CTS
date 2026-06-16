"""Tests for TraversalSystem.runtime.controller helpers."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the package is importable when tests run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from TraversalSystem.config import TraversalOptions  # noqa: E402
from TraversalSystem.runtime.controller import (  # noqa: E402
    coerce_traversal_options,
)


def _full_mapping() -> dict[str, object]:
    """Return a mapping covering ALL 15 TraversalOptions fields."""
    return {
        # standard fields
        "webhook_url": "https://discord.example/hook",
        "journal_directory": "/tmp/cts-journals",
        "route_file": "/tmp/cts-route.txt",
        "route_position": 3,
        "tritium_slot": 2,
        "auto_plot_jumps": False,
        "disable_refuel": True,
        "refuel_mode": 1,
        "single_discord_message": True,
        "shutdown_on_complete": False,
        # multicarrier fields
        "multi_commander_enabled": True,
        "target_fid": "F12345",
        "auto_detect_window": False,
        "focus_timeout_seconds": 10,
        "ambiguous_window_policy": "manual",
    }


def test_coerce_preserves_multicarrier_fields() -> None:
    mapping = _full_mapping()
    opts = coerce_traversal_options(mapping)

    assert isinstance(opts, TraversalOptions)

    # --- multicarrier fields must survive the round-trip ---
    assert opts.multi_commander_enabled is True
    assert opts.target_fid == "F12345"
    assert opts.auto_detect_window is False
    assert opts.focus_timeout_seconds == 10
    assert opts.ambiguous_window_policy == "manual"

    # --- standard fields still work ---
    assert opts.webhook_url == "https://discord.example/hook"
    assert opts.journal_directory == Path("/tmp/cts-journals")
    assert opts.route_file == Path("/tmp/cts-route.txt")
    assert opts.route_position == 3
    assert opts.tritium_slot == 2
    assert opts.auto_plot_jumps is False
    assert opts.disable_refuel is True
    assert opts.refuel_mode == 1
    assert opts.single_discord_message is True
    assert opts.shutdown_on_complete is False


def test_coerce_uses_defaults_when_multicarrier_fields_absent() -> None:
    """A mapping without multicarrier fields must fall back to defaults."""
    mapping = _full_mapping()
    for field in (
        "multi_commander_enabled",
        "target_fid",
        "auto_detect_window",
        "focus_timeout_seconds",
        "ambiguous_window_policy",
    ):
        mapping.pop(field)

    opts = coerce_traversal_options(mapping)

    assert opts.multi_commander_enabled is False
    assert opts.target_fid == ""
    assert opts.auto_detect_window is True
    assert opts.focus_timeout_seconds == 5
    assert opts.ambiguous_window_policy == "abort"
