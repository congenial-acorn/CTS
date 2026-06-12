from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from PySide6.QtWidgets import QApplication

from TraversalSystem.config import TraversalOptions
from TraversalSystem.gui.worker_controller import JournalRuntime, WorkerController
from TraversalSystem.gui.worker_state import WorkerState
from TraversalSystem.multi_journal_router import CTSJournalFacade, MultiJournalRouter
from TraversalSystem.traversal_journal import JournalScanLoop

TraversalController = importlib.import_module(
    "TraversalSystem.runtime.controller"
).TraversalController


def _options_dict(tmp_path: Path) -> TraversalOptions:
    route_file = tmp_path / "route.txt"
    route_file.write_text("Sol\n", encoding="utf-8")
    return TraversalOptions(
        webhook_url="",
        journal_directory=tmp_path,
        route_file=route_file,
        route_position=0,
        tritium_slot=0,
        auto_plot_jumps=True,
        disable_refuel=False,
        power_saving=False,
        refuel_mode=0,
        single_discord_message=False,
        shutdown_on_complete=False,
        target_fid="F-TEST-CLI",
        multi_commander_enabled=True,
    )


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

    router_module = types.ModuleType("multi_journal_router")
    _router_cls = type("MultiJournalRouter", (), {"scan_once": lambda self, d: None, "commanders": {}})
    _facade_cls = type("CTSJournalFacade", (), {"__init__": lambda self, r, f: None, "state": lambda self: True, "target_fid": "F-TEST-CLI"})
    setattr(router_module, "CTSJournalFacade", _facade_cls)
    setattr(router_module, "MultiJournalRouter", _router_cls)

    journal_module = types.ModuleType("traversal_journal")
    _scan_loop_cls = type("JournalScanLoop", (), {
        "__init__": lambda self, r, d: None,
        "start": lambda self: None,
        "stop": lambda self: None,
    })
    setattr(journal_module, "JournalScanLoop", _scan_loop_cls)

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
        "multi_journal_router": router_module,
        "traversal_journal": journal_module,
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
        setattr(module, "run_traversal", lambda _options, **_kwargs: traversal_result)
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


# ---------------------------------------------------------------------------
# JournalRuntime lifecycle tests
# ---------------------------------------------------------------------------


class TestJournalRuntime:
    """Unit tests for ``JournalRuntime`` independently of WorkerController."""

    def test_facade_for_returns_facade_with_correct_target_fid(
        self, tmp_path: Path
    ) -> None:
        runtime = JournalRuntime(tmp_path)
        facade = runtime.facade_for("F12345")
        assert isinstance(facade, CTSJournalFacade)
        assert facade.target_fid == "F12345"

    def test_facade_for_returns_distinct_objects_per_fid(
        self, tmp_path: Path
    ) -> None:
        runtime = JournalRuntime(tmp_path)
        f0 = runtime.facade_for("F-A")
        f1 = runtime.facade_for("F-B")
        assert f0 is not f1
        assert f0.target_fid == "F-A"
        assert f1.target_fid == "F-B"

    def test_facade_for_shares_router_across_fids(
        self, tmp_path: Path
    ) -> None:
        runtime = JournalRuntime(tmp_path)
        f0 = runtime.facade_for("F-X")
        f1 = runtime.facade_for("F-Y")
        assert f0.router is f1.router
        assert f0.router is runtime.router

    def test_start_is_idempotent(self, tmp_path: Path) -> None:
        runtime = JournalRuntime(tmp_path)
        runtime.start()
        runtime.start()  # must not raise or start a second thread
        loop = runtime.scan_loop
        assert isinstance(loop, JournalScanLoop)
        runtime.stop()

    def test_stop_signals_scan_loop(self, tmp_path: Path) -> None:
        runtime = JournalRuntime(tmp_path)
        runtime.start()
        loop = runtime.scan_loop
        assert isinstance(loop, JournalScanLoop)
        runtime.stop()
        # The internal stop event should be set
        stop_event = getattr(loop, "_stop")
        assert stop_event.is_set()


# ---------------------------------------------------------------------------
# WorkerController journal lifecycle tests
# ---------------------------------------------------------------------------


def _make_controller_config(tmp_path: Path):
    """Build minimal GuiConfig + bindings for lifecycle tests."""
    from TraversalSystem.gui.binding_controller import BindingSnapshot, SlotClassification
    from TraversalSystem.gui_config import CarrierSlotConfig, GuiConfig, UniversalSettings
    from TraversalSystem.window_manager import WindowBinding, WindowInfo

    route_file = tmp_path / "route.txt"
    route_file.write_text("Sol\n", encoding="utf-8")

    def _window_info(handle: int) -> WindowInfo:
        return WindowInfo(
            handle=handle, pid=handle,
            title=f"Elite Dangerous {handle}",
            window_class="elite", backend="x11", focusable=True,
        )

    slots = [
        CarrierSlotConfig(
            slot_index=i, fid=f"FID-{i}", commander_name=f"Cmdr {i}",
            route_file=str(route_file), state="ready",
        )
        for i in range(2)
    ]
    config = GuiConfig(
        universal=UniversalSettings(
            webhook_url="", journal_directory=str(tmp_path / "journals"),
            multi_commander_enabled=True, focus_timeout_seconds=5,
        ),
        carrier_slots=slots,
    )
    bindings: dict[int, BindingSnapshot] = {}
    for slot in slots:
        wi = _window_info(slot.slot_index + 100)
        bindings[slot.slot_index] = BindingSnapshot(
            classification=SlotClassification.READY,
            fid=slot.fid,
            commander_name=slot.commander_name,
            window_binding=WindowBinding.from_window(
                target_fid=slot.fid,
                startup_identity=f"slot:{slot.slot_index}",
                window=wi,
            ),
            discovered_commander=None,
            candidate_windows=[wi],
        )
    return config, bindings


def test_journal_runtime_starts_on_first_slot(
    tmp_path: Path, qapp: QApplication,
) -> None:
    """The JournalRuntime scan loop starts lazily when the first slot is
    started, not during ``sync_slots``.
    """
    _ = qapp
    config, bindings = _make_controller_config(tmp_path)
    scan_errors: list[Exception] = []
    finished: list[tuple[int, bool]] = []

    def traversal_runner(options, **kw):
        cb = kw.get("status_callback")
        if cb:
            cb("running")
            cb("complete")
        return True

    created_runtimes: list[JournalRuntime] = []

    def runtime_factory(universal):
        rt = JournalRuntime(
            Path(universal.journal_directory).expanduser(),
            error_callback=scan_errors.append,
        )
        created_runtimes.append(rt)
        return rt

    controller = WorkerController(
        traversal_runner=traversal_runner,
        journal_runtime_factory=runtime_factory,
    )
    controller.sync_slots(config, bindings)

    # Before start: no runtime created yet.
    assert len(created_runtimes) == 0

    def on_finished(idx, success):
        finished.append((idx, success))

    _ = controller.slot_finished.connect(on_finished)

    assert controller.start_slot(0) is True

    # Runtime was created and scan loop started.
    assert len(created_runtimes) == 1
    rt = created_runtimes[0]
    assert isinstance(rt.scan_loop, JournalScanLoop)

    # Wait for the worker to finish.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if finished:
            break
        time.sleep(0.01)
    qapp.processEvents()

    assert controller.slot_state(0) is WorkerState.COMPLETE

    # Shutdown stops the runtime.
    controller.shutdown(wait=True)
    stop_event = getattr(rt.scan_loop, "_stop")
    assert stop_event.is_set()


def test_shutdown_stops_journal_runtime_even_with_no_workers(
    tmp_path: Path,
) -> None:
    """Calling shutdown when no workers have been started should still be safe
    (no journal runtime to stop).
    """
    config, bindings = _make_controller_config(tmp_path)

    controller = WorkerController()
    controller.sync_slots(config, bindings)
    controller.shutdown(wait=True)  # must not raise


def test_all_workers_finished_stops_journal_runtime(
    tmp_path: Path, qapp: QApplication,
) -> None:
    """When all active workers finish, the journal runtime should be stopped
    automatically.
    """
    _ = qapp
    config, bindings = _make_controller_config(tmp_path)
    finished: list[tuple[int, bool]] = []

    def traversal_runner(options, **kw):
        cb = kw.get("status_callback")
        if cb:
            cb("running")
            cb("complete")
        return True

    created_runtimes: list[JournalRuntime] = []

    def runtime_factory(universal):
        rt = JournalRuntime(Path(universal.journal_directory).expanduser())
        created_runtimes.append(rt)
        return rt

    controller = WorkerController(
        traversal_runner=traversal_runner,
        journal_runtime_factory=runtime_factory,
    )
    controller.sync_slots(config, bindings)

    def on_finished(idx, success):
        finished.append((idx, success))

    _ = controller.slot_finished.connect(on_finished)

    assert controller.start_all_ready()[0] == [0, 1]

    # Wait for both workers to finish.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if len(finished) >= 2:
            break
        time.sleep(0.01)
    qapp.processEvents()

    assert len(created_runtimes) == 1
    rt = created_runtimes[0]
    stop_event = getattr(rt.scan_loop, "_stop")
    # After all workers finish the runtime should be stopped.
    assert stop_event.is_set()


def test_sync_slots_resets_journal_runtime(
    tmp_path: Path, qapp: QApplication,
) -> None:
    """Calling sync_slots again should stop any existing journal runtime."""
    _ = qapp
    config, bindings = _make_controller_config(tmp_path)
    finished: list[tuple[int, bool]] = []

    def traversal_runner(options, **kw):
        cb = kw.get("status_callback")
        if cb:
            cb("running")
            cb("complete")
        return True

    created_runtimes: list[JournalRuntime] = []

    def runtime_factory(universal):
        rt = JournalRuntime(Path(universal.journal_directory).expanduser())
        created_runtimes.append(rt)
        return rt

    controller = WorkerController(
        traversal_runner=traversal_runner,
        journal_runtime_factory=runtime_factory,
    )
    controller.sync_slots(config, bindings)

    # Start a slot to trigger runtime creation.
    def on_finished(idx, success):
        finished.append((idx, success))

    _ = controller.slot_finished.connect(on_finished)
    assert controller.start_slot(0) is True

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if finished:
            break
        time.sleep(0.01)
    qapp.processEvents()

    assert len(created_runtimes) == 1
    first_rt = created_runtimes[0]

    # sync_slots should stop the first runtime.
    controller.sync_slots(config, bindings)
    first_stop = getattr(first_rt.scan_loop, "_stop")
    assert first_stop.is_set()

    # Starting a new slot creates a new runtime.
    finished.clear()
    assert controller.start_slot(0) is True
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if finished:
            break
        time.sleep(0.01)
    qapp.processEvents()
    assert len(created_runtimes) == 2

    controller.shutdown(wait=True)
