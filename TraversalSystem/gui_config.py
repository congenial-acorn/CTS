"""Authoritative JSON GUI configuration model for multi-carrier traversal.

This module defines the data model and loader for the GUI-first workflow.
JSON is the single source of truth; the legacy ``settings.ini`` is NOT
authoritative and will be imported/exported separately in later tasks.

Schema version 1 — fields and validation rules are pinned here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

# Allowed values for the ambiguous-window policy.
_AMBIGUOUS_POLICIES = frozenset({"abort", "manual"})

# Elite Dangerous FID pattern: uppercase alphanumeric, typically "F<digits>"
# but journals can emit variations.  We accept non-empty strings that look
# like an FID token (at least one non-whitespace character).
_FID_MIN_LENGTH = 1

# Route file extension whitelist.
_ROUTE_EXTENSIONS = frozenset({".txt", ".csv"})

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GuiConfigError(Exception):
    """Controlled error raised when GUI config is invalid or cannot be loaded."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DiscoveredCommander:
    """A commander identity discovered from journal scanning.

    This is *read-only* data populated by the FID-discovery layer, not
    something the user edits directly.
    """

    #: Frontier commander display name.
    name: str
    #: Frontier ID (FID) — the primary key for binding.
    fid: str
    #: Carrier ID (e.g. "P-C12..."), empty string if unknown.
    carrier_id: str = ""
    #: Carrier callsign/name, empty string if unknown.
    carrier_name: str = ""
    #: ISO 8601 timestamp when this commander was last seen in journals.
    discovered_at: str = ""
    #: Discovery confidence: ``"confirmed"`` (journal-verified) or
    #: ``"tentative"`` (partial match / stale journal data).
    discovery_status: str = "confirmed"
    is_squadron_carrier: bool = False


@dataclass(slots=True)
class CarrierSlotConfig:
    """Per-carrier slot configuration within the GUI.

    A slot represents one carrier binding in the multi-carrier layout.
    Slots start ``unbound`` and transition to ``ready`` when a known FID
    is resolved from journal discovery.  Manually entered unknown FIDs
    MUST remain ``unbound`` — they cannot be promoted to ``ready`` until
    the FID is actually discovered in a journal event.
    """

    #: Slot index in the GUI layout (0-based).
    slot_index: int
    #: User-assigned display/slot name for identification in the UI.
    display_name: str = ""
    #: Frontier ID this slot is bound to.  Empty string = unbound.
    fid: str = ""
    #: Commander display name (may be empty until discovered).
    commander_name: str = ""
    #: Carrier ID string (e.g. "P-C12345...").
    carrier_id: str = ""
    #: Path to the route file (absolute after expansion).
    route_file: str = ""
    #: Route position offset (0 = start of route).
    route_position: int = 0
    #: Tritium cargo slot index for refuel navigation.
    tritium_slot: int = 0
    #: Refuel mode: 0 = personal (first 8), 1 = personal (after 8), 2 = squadron.
    refuel_mode: int = 0
    #: Whether auto-plot-jumps is enabled for this slot.
    auto_plot_jumps: bool = True
    #: Whether refueling is disabled for this slot.
    disable_refuel: bool = False
    #: Whether this slot participates in mass-start (Start All).
    enabled: bool = True
    #: Per-slot power-saving override (close/reopen game between jumps).
    power_saving: bool = False
    #: Per-slot single Discord message override (edit one message vs. many posts).
    single_discord_message: bool = False
    #: Per-slot shutdown-on-complete override.
    shutdown_on_complete: bool = True
    #: Slot binding state: ``"unbound"`` or ``"ready"``.
    state: str = "unbound"
    is_squadron_carrier: bool = False
    #: UTC time for scheduled jump (HH:MM:SS). Empty string = disabled.
    scheduled_jump_time: str = ""
    #: X-coordinate on screen for the jump button click.
    scheduled_jump_button_x: int = 0
    #: Y-coordinate on screen for the jump button click.
    scheduled_jump_button_y: int = 0

    def is_bound(self) -> bool:
        """Return True when the slot has a resolved, known FID."""
        return self.state == "ready" and bool(self.fid)


@dataclass(slots=True)
class UniversalSettings:
    """Global settings shared across all carrier slots."""

    #: Discord webhook URL (empty = disabled).
    webhook_url: str = ""
    #: Journal directory path (absolute after expansion).
    journal_directory: str = "~"
    #: Default directory for route file lookups (absolute after expansion).
    default_route_directory: str = ""
    #: Whether the multi-carrier mode is active.
    multi_commander_enabled: bool = False
    #: Whether to auto-detect the Elite Dangerous window on startup.
    auto_detect_window: bool = True
    #: Seconds to wait for window focus before raising FocusError.
    focus_timeout_seconds: int = 5
    #: Policy when multiple Elite windows are found: ``"abort"`` or ``"manual"``.
    ambiguous_window_policy: str = "abort"
    #: Whether to use a single Discord message (edit) vs. multiple posts.
    single_discord_message: bool = False
    #: Whether to shut down the system when the route completes.
    shutdown_on_complete: bool = True
    #: Power-saving mode (close/reopen game between jumps).
    power_saving: bool = False


@dataclass(slots=True)
class GuiConfig:
    """Top-level authoritative GUI configuration.

    This is the root object loaded from / saved to ``gui_config.json``.
    """

    #: Schema version for forward-compatible migration.
    schema_version: int = SCHEMA_VERSION
    #: Global settings shared by all slots.
    universal: UniversalSettings = field(default_factory=UniversalSettings)
    #: Ordered list of carrier slot configurations.
    carrier_slots: List[CarrierSlotConfig] = field(default_factory=list)
    #: Commanders discovered from journal scanning (populated at runtime,
    #: not persisted in JSON).
    discovered_commanders: List[DiscoveredCommander] = field(default_factory=list)

    # -- convenience ---------------------------------------------------------

    def slot_for_fid(self, fid: str) -> CarrierSlotConfig | None:
        """Return the slot bound to *fid*, or ``None``."""
        for slot in self.carrier_slots:
            if slot.fid == fid:
                return slot
        return None

    def active_slot(self) -> CarrierSlotConfig | None:
        """Return the first ``ready`` slot, or ``None``."""
        for slot in self.carrier_slots:
            if slot.is_bound():
                return slot
        return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_commander_name(name: str) -> None:
    """Raise ``GuiConfigError`` if *name* is empty or whitespace-only."""
    if not name or not name.strip():
        raise GuiConfigError(
            "Invalid GUI config: commander name must be a non-empty string"
        )


def _validate_fid(fid: str, *, context: str = "FID") -> None:
    """Raise ``GuiConfigError`` if *fid* is empty or whitespace-only."""
    if not fid or not fid.strip():
        raise GuiConfigError(
            f"Invalid GUI config: {context} must be a non-empty string"
        )


def _validate_carrier_id(carrier_id: str) -> None:
    """Raise ``GuiConfigError`` if *carrier_id* is non-empty but malformed.

    A carrier ID typically looks like ``P-C12...``, ``D-XYZ...``, etc.
    We accept any non-empty string that is not whitespace-only.
    An empty carrier_id is allowed (unknown carrier).
    """
    if carrier_id and not carrier_id.strip():
        raise GuiConfigError(
            "Invalid GUI config: carrier_id must be non-empty or omitted"
        )


def _validate_route_file(path_str: str) -> None:
    """Raise ``GuiConfigError`` if *path_str* has an unsupported extension."""
    if not path_str:
        return  # empty route_file is allowed (not configured yet)
    suffix = Path(path_str).suffix.lower()
    if suffix and suffix not in _ROUTE_EXTENSIONS:
        raise GuiConfigError(
            f"Invalid GUI config: route file must be .txt or .csv, got '{suffix}'"
        )


def _validate_focus_timeout(seconds: int) -> None:
    """Raise ``GuiConfigError`` if *seconds* is not positive."""
    if seconds < 1:
        raise GuiConfigError(
            "Invalid GUI config: focus_timeout_seconds must be > 0"
        )


def _validate_ambiguous_policy(policy: str) -> None:
    """Raise ``GuiConfigError`` if *policy* is not ``abort`` or ``manual``."""
    if policy not in _AMBIGUOUS_POLICIES:
        raise GuiConfigError(
            f"Invalid GUI config: ambiguous_window_policy must be "
            f"one of {sorted(_AMBIGUOUS_POLICIES)}, got '{policy}'"
        )


def _validate_slot_state(state: str) -> None:
    """Raise ``GuiConfigError`` if *state* is not a recognised binding state."""
    if state not in ("unbound", "ready"):
        raise GuiConfigError(
            f"Invalid GUI config: slot state must be 'unbound' or 'ready', "
            f"got '{state}'"
        )


# ---------------------------------------------------------------------------
# Path expansion
# ---------------------------------------------------------------------------


def _expand_path(path_str: str, base_dir: Path | None = None) -> str:
    """Expand ``~`` and resolve relative paths against *base_dir*.

    Returns the expanded absolute path as a string.
    """
    if not path_str:
        return path_str
    p = Path(path_str).expanduser()
    if not p.is_absolute() and base_dir is not None:
        p = base_dir / p
    return str(p)


# ---------------------------------------------------------------------------
# JSON → dataclass loading
# ---------------------------------------------------------------------------


def _parse_universal(data: Dict[str, Any]) -> UniversalSettings:
    """Build ``UniversalSettings`` from a raw dict with safe defaults."""
    return UniversalSettings(
        webhook_url=str(data.get("webhook_url", "")),
        journal_directory=str(data.get("journal_directory", "~")),
        default_route_directory=str(data.get("default_route_directory", "")),
        multi_commander_enabled=bool(data.get("multi_commander_enabled", False)),
        auto_detect_window=bool(data.get("auto_detect_window", True)),
        focus_timeout_seconds=int(data.get("focus_timeout_seconds", 5)),
        ambiguous_window_policy=str(data.get("ambiguous_window_policy", "abort")),
        single_discord_message=bool(data.get("single_discord_message", False)),
        shutdown_on_complete=bool(data.get("shutdown_on_complete", True)),
        power_saving=bool(data.get("power_saving", False)),
    )


def _parse_slot(index: int, data: Dict[str, Any]) -> CarrierSlotConfig:
    """Build ``CarrierSlotConfig`` from a raw dict with safe defaults."""
    return CarrierSlotConfig(
        slot_index=index,
        display_name=str(data.get("display_name", "")),
        fid=str(data.get("fid", "")),
        commander_name=str(data.get("commander_name", "")),
        carrier_id=str(data.get("carrier_id", "")),
        route_file=str(data.get("route_file", "")),
        route_position=int(data.get("route_position", 0)),
        tritium_slot=int(data.get("tritium_slot", 0)),
        refuel_mode=int(data.get("refuel_mode", 0)),
        auto_plot_jumps=bool(data.get("auto_plot_jumps", True)),
        disable_refuel=bool(data.get("disable_refuel", False)),
        enabled=bool(data.get("enabled", True)),
        power_saving=bool(data.get("power_saving", False)),
        single_discord_message=bool(data.get("single_discord_message", False)),
        shutdown_on_complete=bool(data.get("shutdown_on_complete", True)),
        state=str(data.get("state", "unbound")),
        is_squadron_carrier=bool(data.get("is_squadron_carrier", False)),
        scheduled_jump_time=str(data.get("scheduled_jump_time", "")),
        scheduled_jump_button_x=int(data.get("scheduled_jump_button_x", 0)),
        scheduled_jump_button_y=int(data.get("scheduled_jump_button_y", 0)),
    )


def _parse_discovered_commander(
    data: Dict[str, Any],
) -> DiscoveredCommander:
    """Build ``DiscoveredCommander`` from a raw dict."""
    return DiscoveredCommander(
        name=str(data.get("name", "")),
        fid=str(data.get("fid", "")),
        carrier_id=str(data.get("carrier_id", "")),
        carrier_name=str(data.get("carrier_name", "")),
        discovered_at=str(data.get("discovered_at", "")),
        discovery_status=str(data.get("discovery_status", "confirmed")),
        is_squadron_carrier=bool(data.get("is_squadron_carrier", False)),
    )


def _validate_config(config: GuiConfig, *, base_dir: Path | None = None) -> None:
    """Run all validation checks on a loaded ``GuiConfig``.

    Raises ``GuiConfigError`` with a message containing ``Invalid GUI config``
    for any validation failure.
    """
    # Schema version gate
    if config.schema_version != SCHEMA_VERSION:
        raise GuiConfigError(
            f"Invalid GUI config: unsupported schema_version "
            f"{config.schema_version}, expected {SCHEMA_VERSION}"
        )

    uni = config.universal

    # Focus timeout
    _validate_focus_timeout(uni.focus_timeout_seconds)

    # Ambiguous policy
    _validate_ambiguous_policy(uni.ambiguous_window_policy)

    # Expand journal directory
    config.universal.journal_directory = _expand_path(
        uni.journal_directory, base_dir
    )

    # Expand default route directory
    if uni.default_route_directory:
        config.universal.default_route_directory = _expand_path(
            uni.default_route_directory, base_dir
        )

    for slot in config.carrier_slots:
        # Validate slot state
        _validate_slot_state(slot.state)

        # Validate route file extension
        _validate_route_file(slot.route_file)

        # Expand route file path
        if slot.route_file:
            slot.route_file = _expand_path(slot.route_file, base_dir)

        # Validate carrier_id if present
        if slot.carrier_id:
            _validate_carrier_id(slot.carrier_id)

        # A slot in "ready" state must have a non-empty FID
        if slot.state == "ready":
            _validate_fid(slot.fid, context="slot FID in ready state")

        # A slot with commander_name populated must be valid
        if slot.commander_name:
            _validate_commander_name(slot.commander_name)

    # Validate discovered commanders
    for cmdr in config.discovered_commanders:
        _validate_commander_name(cmdr.name)
        _validate_fid(cmdr.fid, context="discovered commander FID")
        if cmdr.carrier_id:
            _validate_carrier_id(cmdr.carrier_id)


def load_gui_config(
    path: Path | str,
    *,
    extra_discovered: Sequence[DiscoveredCommander] = (),
) -> GuiConfig:
    """Load and validate the authoritative GUI config from a JSON file.

    Parameters
    ----------
    path:
        Path to the ``gui_config.json`` file.
    extra_discovered:
        Additional discovered commanders to merge (e.g. from live journal
        scanning).  These are appended to the ``discovered_commanders`` list
        and validated.

    Returns
    -------
    GuiConfig
        Fully validated and path-expanded configuration.

    Raises
    ------
    GuiConfigError
        If the file cannot be parsed, or any validation rule fails.  The
        message will contain ``Invalid GUI config`` so callers can fail
        closed without exposing raw tracebacks.
    FileNotFoundError
        If *path* does not exist (distinct from malformed JSON).
    """
    config_path = Path(path).expanduser()

    if not config_path.exists():
        raise FileNotFoundError(f"GUI config file not found: {config_path}")

    base_dir = config_path.resolve().parent

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuiConfigError(
            f"Invalid GUI config: cannot read file: {exc}"
        ) from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise GuiConfigError(
            f"Invalid GUI config: malformed JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise GuiConfigError(
            "Invalid GUI config: root element must be a JSON object"
        )

    schema_version = int(data.get("schema_version", SCHEMA_VERSION))

    universal_data = data.get("universal", {})
    if not isinstance(universal_data, dict):
        raise GuiConfigError(
            "Invalid GUI config: 'universal' must be an object"
        )
    universal = _parse_universal(universal_data)

    slots_data = data.get("carrier_slots", [])
    if not isinstance(slots_data, list):
        raise GuiConfigError(
            "Invalid GUI config: 'carrier_slots' must be an array"
        )
    carrier_slots = [
        _parse_slot(i, slot_data)
        for i, slot_data in enumerate(slots_data)
        if isinstance(slot_data, dict)
    ]

    discovered_data = data.get("discovered_commanders", [])
    if not isinstance(discovered_data, list):
        raise GuiConfigError(
            "Invalid GUI config: 'discovered_commanders' must be an array"
        )
    discovered = [
        _parse_discovered_commander(d)
        for d in discovered_data
        if isinstance(d, dict)
    ]
    discovered.extend(extra_discovered)

    config = GuiConfig(
        schema_version=schema_version,
        universal=universal,
        carrier_slots=carrier_slots,
        discovered_commanders=discovered,
    )

    _validate_config(config, base_dir=base_dir)

    return config


# ---------------------------------------------------------------------------
# Dataclass → JSON serialisation
# ---------------------------------------------------------------------------


def _slot_to_dict(slot: CarrierSlotConfig) -> Dict[str, Any]:
    return {
        "slot_index": slot.slot_index,
        "display_name": slot.display_name,
        "fid": slot.fid,
        "commander_name": slot.commander_name,
        "carrier_id": slot.carrier_id,
        "route_file": slot.route_file,
        "route_position": slot.route_position,
        "tritium_slot": slot.tritium_slot,
        "refuel_mode": slot.refuel_mode,
        "auto_plot_jumps": slot.auto_plot_jumps,
        "disable_refuel": slot.disable_refuel,
        "enabled": slot.enabled,
        "power_saving": slot.power_saving,
        "single_discord_message": slot.single_discord_message,
        "shutdown_on_complete": slot.shutdown_on_complete,
        "state": slot.state,
        "is_squadron_carrier": slot.is_squadron_carrier,
        "scheduled_jump_time": slot.scheduled_jump_time,
        "scheduled_jump_button_x": slot.scheduled_jump_button_x,
        "scheduled_jump_button_y": slot.scheduled_jump_button_y,
    }


def _universal_to_dict(uni: UniversalSettings) -> Dict[str, Any]:
    return {
        "webhook_url": uni.webhook_url,
        "journal_directory": uni.journal_directory,
        "default_route_directory": uni.default_route_directory,
        "multi_commander_enabled": uni.multi_commander_enabled,
        "auto_detect_window": uni.auto_detect_window,
        "focus_timeout_seconds": uni.focus_timeout_seconds,
        "ambiguous_window_policy": uni.ambiguous_window_policy,
        "single_discord_message": uni.single_discord_message,
        "shutdown_on_complete": uni.shutdown_on_complete,
        "power_saving": uni.power_saving,
    }


def _discovered_to_dict(cmdr: DiscoveredCommander) -> Dict[str, Any]:
    return {
        "name": cmdr.name,
        "fid": cmdr.fid,
        "carrier_id": cmdr.carrier_id,
        "carrier_name": cmdr.carrier_name,
        "discovered_at": cmdr.discovered_at,
        "discovery_status": cmdr.discovery_status,
        "is_squadron_carrier": cmdr.is_squadron_carrier,
    }


def gui_config_to_dict(config: GuiConfig) -> Dict[str, Any]:
    """Serialise a ``GuiConfig`` to a JSON-compatible dict.

    The ``discovered_commanders`` list is intentionally excluded from
    persistence — it is a runtime-only dataset populated by journal
    scanning.
    """
    return {
        "schema_version": config.schema_version,
        "universal": _universal_to_dict(config.universal),
        "carrier_slots": [_slot_to_dict(s) for s in config.carrier_slots],
    }


def save_gui_config(config: GuiConfig, path: Path | str) -> None:
    """Write a validated ``GuiConfig`` to *path* as indented JSON."""
    config_path = Path(path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    blob = gui_config_to_dict(config)
    config_path.write_text(
        json.dumps(blob, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Slot binding helper
# ---------------------------------------------------------------------------


def bind_slot_to_fid(
    config: GuiConfig,
    slot_index: int,
    fid: str,
    *,
    discovered_fids: Sequence[str] = (),
) -> CarrierSlotConfig:
    """Bind a carrier slot to the given FID.

    If *fid* is found in *discovered_fids* (i.e. it was actually seen in a
    journal), the slot transitions to ``ready``.  Otherwise the slot is set
    to ``unbound`` — manually entered unknown FIDs are never promoted to
    ``ready``.

    Returns the updated slot.

    Raises
    ------
    GuiConfigError
        If *slot_index* is out of range or *fid* is empty.
    """
    if not fid or not fid.strip():
        raise GuiConfigError(
            "Invalid GUI config: cannot bind slot to empty FID"
        )

    if slot_index < 0 or slot_index >= len(config.carrier_slots):
        raise GuiConfigError(
            f"Invalid GUI config: slot_index {slot_index} out of range "
            f"(0..{len(config.carrier_slots) - 1})"
        )

    slot = config.carrier_slots[slot_index]
    slot.fid = fid.strip()

    known = frozenset(discovered_fids)
    if fid in known:
        slot.state = "ready"
    else:
        slot.state = "unbound"

    return slot
