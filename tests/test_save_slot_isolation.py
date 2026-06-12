"""Per-slot GUI save isolation tests.

These tests load the ACTUAL production save code from
``TraversalSystem/main.py`` (with heavy dependencies mocked) and prove
that two GUI slot contexts with different slot IDs cannot overwrite or
delete each other's progress.

All tests FAIL because production currently:
1. ``save_progress()`` does not accept ``slot_id``
2. No per-slot path resolver function exists
3. All save operations route through a single global ``SAVE_PATH``

Task 6 will add slot-aware behaviour — the implementation may live in
``main.py``, a dedicated module, or any file under ``TraversalSystem/``.
These tests are agnostic to the specific module location.

Legacy CLI compatibility is verified in ``test_save_legacy_compat.py``.
"""
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false, reportDeprecated=false
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

import pytest

MAIN_PATH = Path(__file__).resolve().parent.parent / "TraversalSystem" / "main.py"


# ---------------------------------------------------------------------------
# Load main.py with mocked heavy dependencies
# ---------------------------------------------------------------------------

def _build_module_overrides(base_dir: Path) -> dict[str, Any]:
    """Build mock modules for main.py's import dependencies.

    Mirrors the pattern in ``test_gui_controller.py::_load_main_module``.
    """
    from TraversalSystem.config import TraversalOptions

    config_mod = types.ModuleType("config")
    setattr(config_mod, "BASE_DIR", base_dir)
    setattr(config_mod, "TraversalOptions", TraversalOptions)
    setattr(config_mod, "load_settings", lambda: None)

    discord_mod = types.ModuleType("discordhandler")
    setattr(discord_mod, "DiscordHandler", type("DiscordHandler", (), {}))

    watcher_mod = types.ModuleType("journalwatcher")
    setattr(watcher_mod, "JournalWatcher", type("JournalWatcher", (), {}))

    res_mod = types.ModuleType("reshandler")
    setattr(
        res_mod, "Reshandler",
        type("Reshandler", (), {
            "__init__": lambda self, *_a, **_k: setattr(self, "supported_res", True),
        }),
    )

    platform_mod = types.ModuleType("platform_utils")
    setattr(platform_mod, "get_screen_resolution", lambda: (1920, 1080))
    setattr(platform_mod, "open_steam_game", lambda *_a, **_k: None)
    setattr(platform_mod, "system_shutdown", lambda *_a, **_k: None)
    setattr(platform_mod, "get_game_process_names", lambda: [])
    setattr(platform_mod, "IS_WINDOWS", False)

    runtime_ctrl = importlib.import_module("TraversalSystem.runtime.controller")
    runtime_pkg = types.ModuleType("runtime")
    setattr(runtime_pkg, "controller", runtime_ctrl)

    return {
        "config": config_mod,
        "discordhandler": discord_mod,
        "journalwatcher": watcher_mod,
        "reshandler": res_mod,
        "platform_utils": platform_mod,
        "input_handler": types.ModuleType("input_handler"),
        "pyautogui": types.SimpleNamespace(FAILSAFE=False),
        "pyperclip": types.ModuleType("pyperclip"),
        "psutil": types.SimpleNamespace(process_iter=lambda: []),
        "pytz": types.SimpleNamespace(UTC=None),
        "tzlocal": types.SimpleNamespace(get_localzone=lambda: None),
        "runtime": runtime_pkg,
        "runtime.controller": runtime_ctrl,
    }


@contextmanager
def _save_test_module(
    tmp_path: Path,
) -> Generator[Any, None, None]:
    """Load ``TraversalSystem/main.py`` with mocked deps for save testing.

    Sets ``BASE_DIR`` to *tmp_path* so ``SAVE_PATH`` resolves inside it.
    Restores ``sys.modules`` on exit.
    """
    overrides = _build_module_overrides(tmp_path)
    saved: dict[str, Any] = {name: sys.modules.get(name) for name in overrides}
    for name, mod in overrides.items():
        sys.modules[name] = mod

    try:
        spec = importlib.util.spec_from_file_location(
            "cts_main_save_test", MAIN_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        _ = spec.loader.exec_module(module)
        yield module
    finally:
        for name, orig in saved.items():
            if orig is None:
                _ = sys.modules.pop(name, None)
            else:
                sys.modules[name] = orig


# ---------------------------------------------------------------------------
# Slot-aware save API discovery (implementation-flexible)
# ---------------------------------------------------------------------------

def _find_slot_resolver(module: Any) -> Any:
    """Find a slot-aware save path resolver in *module*.

    Checks for common function names.  Returns the callable or ``None``.
    Does not mandate a specific function name or module location.
    """
    for name in ("resolve_save_path", "get_save_path", "_save_path_for_slot"):
        fn = getattr(module, name, None)
        if fn is not None and callable(fn):
            return fn
    return None


# ===================================================================
# Behavioral tests — FAIL until Task 6 implements slot-aware saves
# ===================================================================


class TestSlotSaveIsolation:
    """Run two GUI slot contexts and prove independent save state.

    Each test loads the actual production code and exercises the save
    functions with two different ``slot_id`` values.  Tests FAIL because
    the current production API does not support per-slot isolation.
    """

    # --- API contract ---

    def test_save_slot_isolation_save_progress_accepts_slot_id(self, tmp_path: Path) -> None:
        """``save_progress(state, slot_id=0)`` must not raise TypeError."""
        with _save_test_module(tmp_path) as mod:
            state = mod.TraversalState(line_no=5)
            try:
                mod.save_progress(state, slot_id=0)
            except TypeError as exc:
                pytest.fail(
                    "save_progress() must accept an optional slot_id "
                    + "parameter for per-slot GUI save isolation. "
                    + f"Got TypeError: {exc}"
                )

    def test_save_slot_isolation_resolver_function_exists(self, tmp_path: Path) -> None:
        """A per-slot save path resolver must be available in production."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            if resolver is None:
                pytest.fail(
                    "No slot-aware save path resolver found in production. "
                    + "Expected a function like resolve_save_path(base_dir, slot_id) "
                    + "that returns different paths for different slot IDs. "
                    + "This may be implemented in main.py or any TraversalSystem module."
                )

    # --- Path isolation ---

    def test_save_slot_isolation_two_slots_different_paths(self, tmp_path: Path) -> None:
        """Resolver must produce distinct paths for slot 0 and slot 1."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            if resolver is None:
                pytest.fail(
                    "No slot-aware save path resolver. "
                    + "Cannot verify path isolation without a resolver."
                )
            path_0: Path = resolver(tmp_path, slot_id=0)
            path_1: Path = resolver(tmp_path, slot_id=1)
            assert path_0 != path_1, (
                f"Slots 0 and 1 resolved to the same path: {path_0}"
            )

    # --- Write isolation ---

    def test_save_slot_isolation_write_slot_0_then_1(self, tmp_path: Path) -> None:
        """Writing slot 0 then slot 1 must leave both progress values intact."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            if resolver is None:
                pytest.fail("No slot-aware save path resolver.")

            path_0: Path = resolver(tmp_path, slot_id=0)
            path_1: Path = resolver(tmp_path, slot_id=1)

            mod.save_progress(mod.TraversalState(line_no=5), slot_id=0)
            mod.save_progress(mod.TraversalState(line_no=42), slot_id=1)

            assert int(path_0.read_text(encoding="utf-8")) == 5, (
                "Slot 0 progress was overwritten by slot 1 write"
            )
            assert int(path_1.read_text(encoding="utf-8")) == 42, (
                "Slot 1 progress was overwritten by slot 0 write"
            )

    def test_save_slot_isolation_write_slot_1_then_0(self, tmp_path: Path) -> None:
        """Writing slot 1 then slot 0 must also leave both intact."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            if resolver is None:
                pytest.fail("No slot-aware save path resolver.")

            path_0: Path = resolver(tmp_path, slot_id=0)
            path_1: Path = resolver(tmp_path, slot_id=1)

            mod.save_progress(mod.TraversalState(line_no=99), slot_id=1)
            mod.save_progress(mod.TraversalState(line_no=10), slot_id=0)

            assert int(path_1.read_text(encoding="utf-8")) == 99
            assert int(path_0.read_text(encoding="utf-8")) == 10

    # --- Read isolation ---

    def test_save_slot_isolation_read_returns_own_value(self, tmp_path: Path) -> None:
        """Each slot reads back its own line_no, not the other's."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            if resolver is None:
                pytest.fail("No slot-aware save path resolver.")

            path_0: Path = resolver(tmp_path, slot_id=0)
            path_1: Path = resolver(tmp_path, slot_id=1)

            mod.save_progress(mod.TraversalState(line_no=7), slot_id=0)
            mod.save_progress(mod.TraversalState(line_no=200), slot_id=1)

            assert int(path_0.read_text(encoding="utf-8")) == 7
            assert int(path_1.read_text(encoding="utf-8")) == 200

    def test_save_slot_isolation_read_without_other_save(self, tmp_path: Path) -> None:
        """Slot 1 can be read when slot 0 has no save file at all."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            if resolver is None:
                pytest.fail("No slot-aware save path resolver.")

            path_0: Path = resolver(tmp_path, slot_id=0)
            path_1: Path = resolver(tmp_path, slot_id=1)

            mod.save_progress(mod.TraversalState(line_no=50), slot_id=1)

            assert not path_0.exists(), "Slot 0 should have no save file"
            assert int(path_1.read_text(encoding="utf-8")) == 50

    # --- Delete isolation ---

    def test_save_slot_isolation_delete_slot_0_preserves_slot_1(self, tmp_path: Path) -> None:
        """Deleting slot 0's save (resume consumption) must not touch slot 1."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            if resolver is None:
                pytest.fail("No slot-aware save path resolver.")

            path_0: Path = resolver(tmp_path, slot_id=0)
            path_1: Path = resolver(tmp_path, slot_id=1)

            mod.save_progress(mod.TraversalState(line_no=3), slot_id=0)
            mod.save_progress(mod.TraversalState(line_no=77), slot_id=1)

            # Simulate resume-then-delete (production pattern at main.py:503-507)
            path_0.unlink(missing_ok=True)

            assert not path_0.exists(), "Slot 0 save should be deleted"
            assert path_1.exists(), "Slot 1 save must survive slot 0 deletion"
            assert int(path_1.read_text(encoding="utf-8")) == 77

    def test_save_slot_isolation_delete_slot_1_preserves_slot_0(self, tmp_path: Path) -> None:
        """Deleting slot 1's save must not touch slot 0."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            if resolver is None:
                pytest.fail("No slot-aware save path resolver.")

            path_0: Path = resolver(tmp_path, slot_id=0)
            path_1: Path = resolver(tmp_path, slot_id=1)

            mod.save_progress(mod.TraversalState(line_no=12), slot_id=0)
            mod.save_progress(mod.TraversalState(line_no=88), slot_id=1)

            path_1.unlink(missing_ok=True)

            assert not path_1.exists()
            assert path_0.exists()
            assert int(path_0.read_text(encoding="utf-8")) == 12


# ===================================================================
# consume_save() — slot-aware restore-read + delete
# ===================================================================


class TestConsumeSaveSlotIsolation:
    """Verify consume_save() reads and deletes the correct per-slot file."""

    def _find_consume_save(self, module: Any) -> Any:
        fn = getattr(module, "consume_save", None)
        if fn is not None and callable(fn):
            return fn
        return None

    def test_consume_save_exists(self, tmp_path: Path) -> None:
        """consume_save must be exported from production module."""
        with _save_test_module(tmp_path) as mod:
            fn = self._find_consume_save(mod)
            if fn is None:
                pytest.fail("consume_save() not found in production module.")

    def test_consume_save_reads_slot_not_global(self, tmp_path: Path) -> None:
        """consume_save(slot_id=0) reads slot-0 file, ignoring global save.txt."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            assert resolver is not None
            consume = self._find_consume_save(mod)
            assert consume is not None

            global_path: Path = tmp_path / "save.txt"
            slot_path: Path = resolver(tmp_path, slot_id=0)

            _ = global_path.write_text("99", encoding="utf-8")
            _ = slot_path.write_text("7", encoding="utf-8")

            result: int | None = consume(tmp_path, slot_id=0)
            assert result == 7, (
                f"consume_save(slot_id=0) returned {result}, expected 7 from slot file"
            )

    def test_consume_save_deletes_slot_not_global(self, tmp_path: Path) -> None:
        """consume_save(slot_id=0) deletes only slot file, not global."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            assert resolver is not None
            consume = self._find_consume_save(mod)
            assert consume is not None

            global_path: Path = tmp_path / "save.txt"
            slot_path: Path = resolver(tmp_path, slot_id=0)

            _ = global_path.write_text("99", encoding="utf-8")
            _ = slot_path.write_text("7", encoding="utf-8")

            _ = consume(tmp_path, slot_id=0)

            assert not slot_path.exists(), "Slot file should be deleted"
            assert global_path.exists(), "Global save.txt must survive slot consume"
            assert int(global_path.read_text(encoding="utf-8")) == 99

    def test_consume_save_returns_none_when_no_slot_file(self, tmp_path: Path) -> None:
        """consume_save(slot_id=5) returns None when no slot file exists."""
        with _save_test_module(tmp_path) as mod:
            consume = self._find_consume_save(mod)
            assert consume is not None
            result: int | None = consume(tmp_path, slot_id=5)
            assert result is None, "Should return None when no save file exists"

    def test_consume_save_cross_slot_isolation(self, tmp_path: Path) -> None:
        """Consuming slot 0 does not affect slot 1's file."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            assert resolver is not None
            consume = self._find_consume_save(mod)
            assert consume is not None

            path_0: Path = resolver(tmp_path, slot_id=0)
            path_1: Path = resolver(tmp_path, slot_id=1)

            _ = path_0.write_text("10", encoding="utf-8")
            _ = path_1.write_text("20", encoding="utf-8")

            result = consume(tmp_path, slot_id=0)
            assert result == 10
            assert not path_0.exists()
            assert path_1.exists()
            assert int(path_1.read_text(encoding="utf-8")) == 20

    def test_consume_save_legacy_reads_global(self, tmp_path: Path) -> None:
        """consume_save(slot_id=None) reads and deletes global save.txt."""
        with _save_test_module(tmp_path) as mod:
            consume = self._find_consume_save(mod)
            assert consume is not None

            global_path: Path = tmp_path / "save.txt"
            _ = global_path.write_text("42", encoding="utf-8")

            result: int | None = consume(tmp_path, slot_id=None)
            assert result == 42
            assert not global_path.exists()


# ===================================================================
# state.slot_id — save_progress falls back to TraversalState.slot_id
# ===================================================================


class TestStateSlotId:
    """Verify save_progress uses state.slot_id when no kwarg is provided."""

    def test_save_progress_uses_state_slot_id(self, tmp_path: Path) -> None:
        """save_progress(state) writes to slot path when state.slot_id is set."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            assert resolver is not None

            state = mod.TraversalState(line_no=33, slot_id=0)
            mod.save_progress(state)

            slot_path: Path = resolver(tmp_path, slot_id=0)
            assert slot_path.exists(), "Slot save file should exist"
            assert int(slot_path.read_text(encoding="utf-8")) == 33

    def test_save_progress_kwarg_overrides_state_slot_id(self, tmp_path: Path) -> None:
        """save_progress(state, slot_id=1) uses kwarg even if state.slot_id=0."""
        with _save_test_module(tmp_path) as mod:
            resolver = _find_slot_resolver(mod)
            assert resolver is not None

            state = mod.TraversalState(line_no=10, slot_id=0)
            mod.save_progress(state, slot_id=1)

            path_0: Path = resolver(tmp_path, slot_id=0)
            path_1: Path = resolver(tmp_path, slot_id=1)
            assert not path_0.exists(), "state.slot_id should be overridden by kwarg"
            assert int(path_1.read_text(encoding="utf-8")) == 10

    def test_traversal_state_has_slot_id_field(self, tmp_path: Path) -> None:
        """TraversalState must have a slot_id field."""
        with _save_test_module(tmp_path) as mod:
            state = mod.TraversalState(line_no=0, slot_id=3)
            assert state.slot_id == 3
            state2 = mod.TraversalState(line_no=0)
            assert state2.slot_id is None


# ===================================================================
# Source-level wiring checks — catch restore-path regression
# ===================================================================


def test_run_traversal_slot_uses_consume_save() -> None:
    """_run_traversal_slot must call consume_save, not bare SAVE_PATH.read."""
    source = MAIN_PATH.read_text(encoding="utf-8")
    assert "consume_save(BASE_DIR, slot_id=slot_id)" in source, (
        "_run_traversal_slot must use consume_save() for slot-aware restore. "
        + "Falling back to bare SAVE_PATH.read_text breaks GUI slot isolation."
    )


def test_run_traversal_accepts_slot_id() -> None:
    """run_traversal must accept slot_id parameter."""
    source = MAIN_PATH.read_text(encoding="utf-8")
    assert "slot_id: int | None = None" in source, (
        "run_traversal must accept slot_id for GUI slot propagation."
    )


def test_runtime_dependencies_carries_slot_id() -> None:
    """TraversalRuntimeDependencies must carry slot_id."""
    ctrl_path = Path(__file__).resolve().parent.parent / "TraversalSystem" / "runtime" / "controller.py"
    source = ctrl_path.read_text(encoding="utf-8")
    assert "slot_id" in source, (
        "TraversalRuntimeDependencies must carry slot_id for slot propagation."
    )


def test_worker_passes_slot_id_to_runner() -> None:
    """CarrierAutomationWorker must pass slot_id to the traversal runner."""
    worker_path = Path(__file__).resolve().parent.parent / "TraversalSystem" / "gui" / "workers.py"
    source = worker_path.read_text(encoding="utf-8")
    assert "slot_id=" in source, (
        "CarrierAutomationWorker must pass slot_id to run_traversal."
    )
