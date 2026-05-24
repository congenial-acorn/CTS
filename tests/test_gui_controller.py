from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, cast

import pytest

from TraversalSystem.config import TraversalOptions

TraversalController = importlib.import_module(
    "TraversalSystem.runtime.controller"
).TraversalController


def _options_dict(tmp_path: Path) -> dict[str, object]:
    route_file = tmp_path / "route.txt"
    route_file.write_text("Sol\n", encoding="utf-8")
    return {
        "webhook_url": "",
        "journal_directory": tmp_path,
        "route_file": route_file,
        "route_position": 0,
        "tritium_slot": 0,
        "auto_plot_jumps": True,
        "disable_refuel": False,
        "power_saving": False,
        "refuel_mode": 0,
        "single_discord_message": False,
        "shutdown_on_complete": False,
    }


def test_controller_reports_status_sequence_and_injected_dependencies(tmp_path: Path) -> None:
    statuses: list[str] = []
    journal = object()
    window = object()
    focus = object()

    def traversal(runtime_context: Any) -> bool:
        assert isinstance(runtime_context.options, TraversalOptions)
        assert runtime_context.dependencies.journal is journal
        assert runtime_context.dependencies.window is window
        assert runtime_context.dependencies.focus is focus
        runtime_context.transition("waiting")
        runtime_context.transition("running")
        return True

    result = TraversalController().run(
        traversal,
        _options_dict(tmp_path),
        journal=journal,
        window=window,
        focus=focus,
        status_callback=statuses.append,
    )

    assert result is True
    assert statuses == ["starting", "running", "waiting", "running", "complete"]


def test_controller_reports_error_status_when_callable_raises(tmp_path: Path) -> None:
    statuses: list[str] = []

    def traversal(_runtime_context: Any) -> bool:
        raise RuntimeError("boom")

    result = TraversalController().run(
        traversal,
        _options_dict(tmp_path),
        status_callback=statuses.append,
    )

    assert result is False
    assert statuses == ["starting", "running", "error"]


def test_controller_stops_before_input_when_cancelled(tmp_path: Path) -> None:
    statuses: list[str] = []
    cancel_event = threading.Event()
    cancel_event.set()
    called = False

    def traversal(_runtime_context: Any) -> bool:
        nonlocal called
        called = True
        return True

    result = TraversalController().run(
        traversal,
        _options_dict(tmp_path),
        cancel_event=cancel_event,
        status_callback=statuses.append,
    )

    assert result is False
    assert called is False
    assert statuses == ["starting", "stopped"]


@dataclass
class _ExitCalled(Exception):
    code: int


def _load_main_module(tmp_path: Path):
    config_module = types.ModuleType("config")
    setattr(config_module, "BASE_DIR", tmp_path)
    setattr(config_module, "TraversalOptions", TraversalOptions)
    setattr(config_module, "load_settings", lambda: _options_dict(tmp_path))

    discord_module = types.ModuleType("discordhandler")
    setattr(discord_module, "DiscordHandler", type("DiscordHandler", (), {}))

    watcher_module = types.ModuleType("journalwatcher")
    setattr(watcher_module, "JournalWatcher", type("JournalWatcher", (), {}))

    reshandler_module = types.ModuleType("reshandler")
    setattr(
        reshandler_module,
        "Reshandler",
        type(
            "Reshandler",
            (),
            {"__init__": lambda self, *_args, **_kwargs: setattr(self, "supported_res", True)},
        ),
    )

    platform_utils_module = types.ModuleType("platform_utils")
    setattr(platform_utils_module, "get_screen_resolution", lambda: (1920, 1080))
    setattr(platform_utils_module, "open_steam_game", lambda *_args, **_kwargs: None)
    setattr(platform_utils_module, "system_shutdown", lambda *_args, **_kwargs: None)
    setattr(platform_utils_module, "get_game_process_names", lambda: [])
    setattr(platform_utils_module, "IS_WINDOWS", False)

    runtime_controller_module = importlib.import_module(
        "TraversalSystem.runtime.controller"
    )
    runtime_package = types.ModuleType("runtime")
    setattr(runtime_package, "controller", runtime_controller_module)

    module_overrides: dict[str, object] = {
        "config": config_module,
        "discordhandler": discord_module,
        "journalwatcher": watcher_module,
        "reshandler": reshandler_module,
        "platform_utils": platform_utils_module,
        "input_handler": types.ModuleType("input_handler"),
        "pyautogui": types.SimpleNamespace(FAILSAFE=False),
        "pyperclip": types.ModuleType("pyperclip"),
        "psutil": types.SimpleNamespace(process_iter=lambda: []),
        "pytz": types.SimpleNamespace(UTC=None),
        "tzlocal": types.SimpleNamespace(get_localzone=lambda: None),
        "runtime": runtime_package,
        "runtime.controller": runtime_controller_module,
    }

    saved = {name: sys.modules.get(name) for name in module_overrides}
    for name, module in module_overrides.items():
        sys.modules[name] = cast(types.ModuleType, module)

    try:
        main_path = Path(__file__).resolve().parents[1] / "TraversalSystem" / "main.py"
        spec = importlib.util.spec_from_file_location("cts_main_for_test", main_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module, saved
    except Exception:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
        raise


@pytest.mark.parametrize(
    ("traversal_result", "expected_exit_code"),
    [(True, 0), (False, 1)],
)
def test_main_preserves_cli_exit_behavior(
    tmp_path: Path,
    traversal_result: bool,
    expected_exit_code: int,
) -> None:
    module, saved = _load_main_module(tmp_path)

    try:
        setattr(module, "warn_if_outdated", lambda: None)
        setattr(module, "load_settings", lambda: _options_dict(tmp_path))
        setattr(module, "run_traversal", lambda _options: traversal_result)
        module.os._exit = lambda code: (_ for _ in ()).throw(_ExitCalled(code))

        with pytest.raises(_ExitCalled) as exc_info:
            module.main()

        assert exc_info.value.code == expected_exit_code
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
