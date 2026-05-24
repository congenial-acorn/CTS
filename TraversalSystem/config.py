from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict


def _detect_base_dir() -> Path:
    # When running under PyInstaller onefile, resources should be loaded from
    # the directory containing the executable, not the temporary _MEIPASS dir.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _detect_base_dir()
DEFAULT_SETTINGS_PATH = BASE_DIR / "settings.ini"


def _parse_key_values(path: Path) -> Dict[str, str]:
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


_VALID_AMBIGUOUS_POLICIES = frozenset({"abort", "manual"})
_DEFAULT_FOCUS_TIMEOUT = 5


def _parse_focus_timeout(value: str | None) -> int:
    raw = _as_int(value, default=-1)
    if raw == -1:
        return _DEFAULT_FOCUS_TIMEOUT
    if raw < 1:
        return 1
    return raw


def _parse_ambiguous_policy(value: str | None) -> str:
    if not value or not value.strip():
        return "abort"
    policy = value.strip().lower()
    if policy not in _VALID_AMBIGUOUS_POLICIES:
        raise ValueError(
            f"ambiguous-window-policy must be one of "
            f"{sorted(_VALID_AMBIGUOUS_POLICIES)}, got '{policy}'"
        )
    return policy


@dataclass(slots=True)
class TraversalOptions:
    webhook_url: str
    journal_directory: Path
    route_file: Path
    route_position: int = 0
    tritium_slot: int = 0
    auto_plot_jumps: bool = True
    disable_refuel: bool = False
    power_saving: bool = False
    refuel_mode: int = 0
    single_discord_message: bool = False
    shutdown_on_complete: bool = True
    multi_commander_enabled: bool = False
    target_fid: str = ""
    auto_detect_window: bool = True
    focus_timeout_seconds: int = 5
    ambiguous_window_policy: str = "abort"


def load_settings(
    settings_path: Path | str | None = None,
) -> TraversalOptions:
    """Load traversal settings from a single settings.ini file."""
    settings_file = Path(settings_path or DEFAULT_SETTINGS_PATH).expanduser()

    # Fallback to legacy settings.txt if settings.ini is missing
    if not settings_file.exists() and settings_file.name == "settings.ini":
        legacy = BASE_DIR / "settings.txt"
        if legacy.exists():
            settings_file = legacy

    settings_values = _parse_key_values(settings_file)

    journal_directory = Path(settings_values.get("journal_directory", "~")).expanduser()
    route_file = Path(settings_values.get("route_file", "route.txt"))
    if not route_file.is_absolute():
        route_file = settings_file.parent / route_file

    opts = TraversalOptions(
        webhook_url=settings_values.get("webhook_url", ""),
        journal_directory=journal_directory,
        route_file=route_file,
        route_position=max(
            0, _as_int(settings_values.get("route_position"), default=0)
        ),
        tritium_slot=_as_int(settings_values.get("tritium_slot"), default=0),
        auto_plot_jumps=_as_bool(settings_values.get("auto-plot-jumps"), default=True),
        disable_refuel=_as_bool(settings_values.get("disable-refuel"), default=False),
        power_saving=_as_bool(settings_values.get("power-saving"), default=False),
        refuel_mode=_as_int(settings_values.get("refuel-mode"), default=0),
        single_discord_message=_as_bool(
            settings_values.get("single-discord-message"), default=False
        ),
        shutdown_on_complete=_as_bool(
            settings_values.get("shutdown-on-complete"), default=True
        ),
        multi_commander_enabled=_as_bool(
            settings_values.get("multi-commander-enabled"), default=False
        ),
        target_fid=settings_values.get("target-fid", ""),
        auto_detect_window=_as_bool(
            settings_values.get("auto-detect-window"), default=True
        ),
        focus_timeout_seconds=_parse_focus_timeout(
            settings_values.get("focus-timeout-seconds")
        ),
        ambiguous_window_policy=_parse_ambiguous_policy(
            settings_values.get("ambiguous-window-policy")
        ),
    )

    if opts.multi_commander_enabled and not opts.target_fid.strip():
        raise ValueError(
            "target-fid must be non-empty when multi-commander-enabled is true"
        )

    return opts
