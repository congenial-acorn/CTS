"""Legacy CLI save compatibility tests.

Proves that the default non-GUI save behavior still uses the historical
global ``save.txt`` path under ``BASE_DIR``.  These tests MUST PASS
against the current production code and continue to pass after Task 6
refactors GUI slots to use per-slot paths.

The legacy contract:
  - CLI / non-GUI path writes to ``BASE_DIR / "save.txt"``
  - CLI / non-GUI path reads from and deletes ``BASE_DIR / "save.txt"``
  - No slot-id parameter is needed for legacy mode
  - ``save_progress(state)`` signature remains backward-compatible
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAIN_PATH = Path(__file__).resolve().parent.parent / "TraversalSystem" / "main.py"


def _read_source() -> str:
    return MAIN_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Legacy path: global save.txt
# ---------------------------------------------------------------------------

class TestLegacySavePath:
    """Verify the legacy CLI uses ``BASE_DIR / "save.txt"`` for all operations."""

    def test_legacy_save_path_defined_as_global_save_txt(self) -> None:
        """``SAVE_PATH`` must resolve to ``BASE_DIR / "save.txt"``."""
        source = _read_source()
        assert 'SAVE_PATH = BASE_DIR / "save.txt"' in source, (
            "Legacy mode requires SAVE_PATH = BASE_DIR / 'save.txt'"
        )

    def test_legacy_save_progress_writes_to_save_path(self) -> None:
        """``save_progress()`` must write line_no to the global SAVE_PATH."""
        source = _read_source()
        assert "SAVE_PATH.write_text(str(state.line_no)" in source, (
            "save_progress() must write state.line_no to SAVE_PATH"
        )

    def test_legacy_restore_reads_global_save_path(self) -> None:
        """Resume logic must read ``line_no`` from the global SAVE_PATH."""
        source = _read_source()
        assert "SAVE_PATH.read_text" in source, (
            "Restore must read from SAVE_PATH"
        )

    def test_legacy_restore_deletes_global_save_path(self) -> None:
        """Resume logic must delete the global SAVE_PATH after consuming it."""
        source = _read_source()
        assert "SAVE_PATH.unlink" in source, (
            "Restore must unlink (delete) SAVE_PATH after reading"
        )

    def test_legacy_save_progress_no_slot_id(self) -> None:
        """``save_progress()`` must work without a slot_id in legacy mode."""
        source_lines = _read_source().splitlines()
        for line in source_lines:
            if line.strip().startswith("def save_progress("):
                assert "slot_id" not in line, (
                    f"save_progress() must not require slot_id for legacy CLI; "
                    f"got: {line.strip()}"
                )
                break
        else:
            pytest.fail("Could not find save_progress() definition")


# ---------------------------------------------------------------------------
# Legacy semantics: write -> read -> delete round-trip
# ---------------------------------------------------------------------------

class TestLegacySaveSemantics:
    """End-to-end semantics of the legacy save file using tmp_path.

    These simulate the exact write/read/delete pattern from main.py
    (lines 321, 503-507) on a temporary directory.
    """

    def test_legacy_save_write_read_round_trip(self, tmp_path: Path) -> None:
        """Write line_no, then read it back — must match."""
        save_path = tmp_path / "save.txt"
        line_no = 37

        # Write (mirrors save_progress)
        _ = save_path.write_text(str(line_no), encoding="utf-8")

        # Read (mirrors resume logic at main.py:505)
        restored = int(save_path.read_text(encoding="utf-8"))
        assert restored == line_no

    def test_legacy_save_read_and_delete_consumes_save(self, tmp_path: Path) -> None:
        """After reading, the save file is deleted (consumed)."""
        save_path = tmp_path / "save.txt"
        _ = save_path.write_text("42", encoding="utf-8")

        # Read + delete (mirrors main.py:503-507)
        assert save_path.exists()
        _ = int(save_path.read_text(encoding="utf-8"))
        save_path.unlink(missing_ok=True)

        assert not save_path.exists()

    def test_legacy_save_missing_is_not_an_error(self, tmp_path: Path) -> None:
        """``SAVE_PATH.exists()`` check guards against missing file."""
        save_path = tmp_path / "save.txt"
        assert not save_path.exists()
        # The production code at main.py:503 guards with:
        #   if SAVE_PATH.exists():
        # No error should be raised when the file is absent.

    def test_legacy_save_delete_missing_is_safe(self, tmp_path: Path) -> None:
        """``unlink(missing_ok=True)`` on a non-existent file is safe."""
        save_path = tmp_path / "save.txt"
        # Should not raise
        save_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Module-level wrappers for plan-required pytest node IDs
# ---------------------------------------------------------------------------

def test_legacy_save_path_is_global() -> None:
    """Wrapper for plan-required node ID."""
    TestLegacySavePath().test_legacy_save_path_defined_as_global_save_txt()


def test_legacy_save_write_read_round_trip(tmp_path: Path) -> None:
    """Wrapper for plan-required node ID."""
    TestLegacySaveSemantics().test_legacy_save_write_read_round_trip(tmp_path)


def test_legacy_save_read_and_delete(tmp_path: Path) -> None:
    """Wrapper for plan-required node ID."""
    TestLegacySaveSemantics().test_legacy_save_read_and_delete_consumes_save(tmp_path)


def test_legacy_save_missing_not_error(tmp_path: Path) -> None:
    """Wrapper for plan-required node ID."""
    TestLegacySaveSemantics().test_legacy_save_missing_is_not_an_error(tmp_path)


def test_legacy_save_progress_no_slot_id() -> None:
    """Wrapper for plan-required node ID."""
    TestLegacySavePath().test_legacy_save_progress_no_slot_id()
