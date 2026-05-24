"""Smoke tests that validate the pytest harness itself."""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Sequence
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from tests.conftest import MockWindowInfo


# ---- Harness sanity -------------------------------------------------------

class TestHarnessSanity:
    """Verify the harness and core fixtures work correctly."""

    def test_pytest_importable(self) -> None:
        """pytest itself is importable."""
        import pytest as _pt  # noqa: F401 – just checking importability
        assert _pt.__version__

    def test_fixtures_dir_exists(self, fixtures_dir: Path) -> None:
        """The ``tests/fixtures/`` directory is present."""
        assert fixtures_dir.is_dir()


# ---- Journal fixtures -----------------------------------------------------

class TestJournalFixtures:
    """Verify journal-related fixtures create usable files."""

    def test_tmp_journal_exists(self, tmp_journal: Path) -> None:
        assert tmp_journal.exists()
        assert tmp_journal.name.endswith(".log")

    def test_journal_events_append(self, tmp_journal: Path, journal_events: Callable[[Sequence[dict[str, object]]], None]) -> None:
        journal_events([
            {"event": "CarrierJumpRequest", "SystemName": "Sol"},
        ])
        lines = tmp_journal.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        evt: dict[str, object] = json.loads(lines[0])
        assert evt["event"] == "CarrierJumpRequest"
        assert evt["SystemName"] == "Sol"
        assert "timestamp" in evt

    def test_multi_commander_startup_block(
        self, multi_commander_startup_block: list[dict[str, object]],
    ) -> None:
        assert len(multi_commander_startup_block) >= 4
        events = [e["event"] for e in multi_commander_startup_block]
        assert "Fileheader" in events
        assert "CarrierJumpRequest" in events

    def test_continued_journal(
        self, continued_journal: tuple[Path, Path],
    ) -> None:
        part1, part2 = continued_journal
        assert part1.exists()
        assert part2.exists()
        # part2 should contain at least one line
        content = part2.read_text(encoding="utf-8").strip()
        assert content
        evt: dict[str, object] = json.loads(content.splitlines()[0])
        assert evt["event"] == "Fileheader"
        assert evt.get("Part") == 2


# ---- Window enumeration fixtures ------------------------------------------

class TestWindowFixtures:
    """Verify mock window-enumeration fixtures."""

    def test_mock_window_enum_length(self, mock_window_enum: list[MockWindowInfo]) -> None:
        assert len(mock_window_enum) == 4

    def test_elite_window_present(self, mock_window_enum: list[MockWindowInfo]) -> None:
        titles = [w.title for w in mock_window_enum]
        assert "Elite - Dangerous (CLIENT)" in titles

    def test_mock_window_attributes(self, mock_window_enum: list[MockWindowInfo]) -> None:
        elite = [w for w in mock_window_enum if "Elite" in w.title][0]
        assert elite.visible is True
        assert elite.hwnd > 0
