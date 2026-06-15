"""Legacy settings.ini import/export for the GUI multi-carrier config.

This module provides **explicit, one-way** conversion between the flat
``settings.ini`` format and the authoritative ``GuiConfig`` JSON model.

- **Import** reads a legacy ``settings.ini`` and returns a ``GuiConfig``
  with universal settings populated and one carrier slot (slot 0) filled
  from the legacy flat fields.  JSON remains authoritative; the import is
  a one-time migration action, not continuous sync.

- **Export** writes a legacy-compatible flat ``settings.ini`` from one
  *selected* carrier slot of a ``GuiConfig``, combining universal settings
  with the per-slot fields of the chosen slot.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from TraversalSystem.gui_config import (
    CarrierSlotConfig,
    GuiConfig,
    UniversalSettings,
    SCHEMA_VERSION,
)

# ---------------------------------------------------------------------------
# Legacy INI key names (flat file, no sections)
# ---------------------------------------------------------------------------

# Keys that map to UniversalSettings.
_UNIVERSAL_KEYS: Dict[str, str] = {
    "webhook_url": "webhook_url",
    "journal_directory": "journal_directory",
    "single-discord-message": "single_discord_message",
    "shutdown-on-complete": "shutdown_on_complete",
    "multi-commander-enabled": "multi_commander_enabled",
    "auto-detect-window": "auto_detect_window",
    "focus-timeout-seconds": "focus_timeout_seconds",
    "ambiguous-window-policy": "ambiguous_window_policy",
}

# Keys that map to CarrierSlotConfig fields.
_SLOT_KEYS: Dict[str, str] = {
    "target-fid": "fid",
    "route_file": "route_file",
    "route_position": "route_position",
    "tritium_slot": "tritium_slot",
    "auto-plot-jumps": "auto_plot_jumps",
    "disable-refuel": "disable_refuel",
    "refuel-mode": "refuel_mode",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_key_values(path: Path) -> Dict[str, str]:
    """Parse a flat key=value file (comments and blank lines ignored)."""
    values: Dict[str, str] = {}
    if not path.exists():
        raise FileNotFoundError(f"Missing settings file: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _as_int(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: str | None, default: str = "") -> str:
    return value if value is not None else default


# ---------------------------------------------------------------------------
# Import: legacy INI → GuiConfig
# ---------------------------------------------------------------------------


def import_legacy_settings(path: Path | str) -> GuiConfig:
    """Import a legacy ``settings.ini`` into a new ``GuiConfig``.

    Creates a ``GuiConfig`` with:
    - Universal settings populated from INI keys.
    - One carrier slot (index 0) populated from flat slot keys.

    The *commander_name* and *carrier_id* fields are left empty because
    the legacy format does not carry them — they are populated later via
    journal discovery.

    JSON remains authoritative; this is an explicit one-time import, not
    continuous sync.

    Parameters
    ----------
    path:
        Path to the legacy ``settings.ini`` file.

    Returns
    -------
    GuiConfig
        A new configuration with imported values.

    Raises
    ------
    FileNotFoundError
        If the INI file does not exist.
    """
    ini_path = Path(path).expanduser()
    raw = _parse_key_values(ini_path)

    # -- Universal settings --
    universal = UniversalSettings(
        webhook_url=_str(raw.get("webhook_url")),
        journal_directory=_str(raw.get("journal_directory"), default="~"),
        multi_commander_enabled=_as_bool(
            raw.get("multi-commander-enabled"), default=False
        ),
        auto_detect_window=_as_bool(
            raw.get("auto-detect-window"), default=True
        ),
        focus_timeout_seconds=max(
            1, _as_int(raw.get("focus-timeout-seconds"), default=5)
        ),
        ambiguous_window_policy=_str(
            raw.get("ambiguous-window-policy"), default="abort"
        ) or "abort",
        single_discord_message=_as_bool(
            raw.get("single-discord-message"), default=False
        ),
        shutdown_on_complete=_as_bool(
            raw.get("shutdown-on-complete"), default=True
        ),
    )

    # -- Resolve relative route_file against INI location --
    route_file_raw = _str(raw.get("route_file"), default="")
    if route_file_raw:
        route_p = Path(route_file_raw)
        if not route_p.is_absolute():
            route_p = ini_path.resolve().parent / route_p
        route_file_resolved = str(route_p)
    else:
        route_file_resolved = ""

    # -- Single carrier slot from flat fields --
    fid = _str(raw.get("target-fid"), default="")
    slot = CarrierSlotConfig(
        slot_index=0,
        fid=fid,
        commander_name="",
        carrier_id="",
        route_file=route_file_resolved,
        route_position=max(0, _as_int(raw.get("route_position"), default=0)),
        tritium_slot=_as_int(raw.get("tritium_slot"), default=0),
        refuel_mode=_as_int(raw.get("refuel-mode"), default=0),
        auto_plot_jumps=_as_bool(
            raw.get("auto-plot-jumps"), default=True
        ),
        disable_refuel=_as_bool(
            raw.get("disable-refuel"), default=False
        ),
        # Fail-closed: imported FIDs are always unbound until journal
        # discovery confirms them.  The binding controller is the sole
        # authority for promoting to ready.
        state="unbound",
    )

    return GuiConfig(
        schema_version=SCHEMA_VERSION,
        universal=universal,
        carrier_slots=[slot],
    )


# ---------------------------------------------------------------------------
# Export: selected slot from GuiConfig → legacy INI
# ---------------------------------------------------------------------------


def export_legacy_settings(
    config: GuiConfig,
    slot_index: int,
    path: Path | str,
) -> None:
    """Export one selected carrier slot from *config* as a legacy ``settings.ini``.

    The exported file is a flat key=value file using the current key names
    from the legacy format.  Only the selected slot's fields are written;
    universal settings come from ``config.universal``.

    This is an explicit export action — JSON remains authoritative.  There
    is no continuous sync between JSON and INI.

    Parameters
    ----------
    config:
        Authoritative GUI configuration.
    slot_index:
        Index of the carrier slot to export.
    path:
        Destination path for the written INI file.

    Raises
    ------
    ValueError
        If *slot_index* is out of range.
    """
    if slot_index < 0 or slot_index >= len(config.carrier_slots):
        raise ValueError(
            f"slot_index {slot_index} out of range "
            f"(0..{len(config.carrier_slots) - 1})"
        )

    slot = config.carrier_slots[slot_index]
    uni = config.universal
    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "# Exported from gui_config.json — JSON remains authoritative",
        f"webhook_url={uni.webhook_url}",
        f"journal_directory={uni.journal_directory}",
        f"tritium_slot={slot.tritium_slot}",
        f"route_file={slot.route_file}",
        f"route_position={slot.route_position}",
        f"auto-plot-jumps={'true' if slot.auto_plot_jumps else 'false'}",
        f"disable-refuel={'true' if slot.disable_refuel else 'false'}",
        f"refuel-mode={slot.refuel_mode}",
        f"single-discord-message={'true' if uni.single_discord_message else 'false'}",
        f"shutdown-on-complete={'true' if uni.shutdown_on_complete else 'false'}",
        f"multi-commander-enabled={'true' if uni.multi_commander_enabled else 'false'}",
        f"target-fid={slot.fid}",
        f"auto-detect-window={'true' if uni.auto_detect_window else 'false'}",
        f"focus-timeout-seconds={uni.focus_timeout_seconds}",
        f"ambiguous-window-policy={uni.ambiguous_window_policy}",
    ]

    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
