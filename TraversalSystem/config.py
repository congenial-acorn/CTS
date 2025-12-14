from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SETTINGS_PATH = BASE_DIR / "settings.txt"
DEFAULT_OPTIONS_PATH = BASE_DIR / "settings.ini"


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


@dataclass(slots=True)
class TraversalOptions:
    webhook_url: str
    journal_directory: Path
    route_file: Path
    tritium_slot: int
    auto_plot_jumps: bool = True
    disable_refuel: bool = False
    power_saving: bool = False
    refuel_mode: int = 0
    single_discord_message: bool = False


def load_settings(
    settings_path: Path | str | None = None,
    options_path: Path | str | None = None,
) -> TraversalOptions:
    """Load traversal settings from the two legacy files.

    The previous layout scattered the files across the repo. To keep the
    traversal system self contained, both files now live under the
    TraversalSystem directory by default.
    """
    settings_file = Path(settings_path or DEFAULT_SETTINGS_PATH).expanduser()
    options_file = Path(options_path or DEFAULT_OPTIONS_PATH).expanduser()

    settings_values = _parse_key_values(settings_file)
    options_values = _parse_key_values(options_file) if options_file.exists() else {}

    journal_directory = Path(
        settings_values.get("journal_directory", "~")
    ).expanduser()
    route_file = Path(settings_values.get("route_file", "route.txt"))
    if not route_file.is_absolute():
        route_file = settings_file.parent / route_file

    return TraversalOptions(
        webhook_url=settings_values.get("webhook_url", ""),
        journal_directory=journal_directory,
        route_file=route_file,
        tritium_slot=_as_int(settings_values.get("tritium_slot"), default=0),
        auto_plot_jumps=_as_bool(options_values.get("auto-plot-jumps"), default=True),
        disable_refuel=_as_bool(options_values.get("disable-refuel"), default=False),
        power_saving=_as_bool(options_values.get("power-saving"), default=False),
        refuel_mode=_as_int(options_values.get("refuel-mode"), default=0),
        single_discord_message=_as_bool(
            options_values.get("single-discord-message"), default=False
        ),
    )
