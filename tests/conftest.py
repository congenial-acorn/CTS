"""Reusable pytest fixtures for CTS multicarrier tests.

Provides:
- qapp: a singleton offscreen QApplication shared across the entire session.
- tmp_journal: a temporary directory containing a single Elite Dangerous
  JSON-line journal file, pre-populated with a configurable sequence of events.
- journal_events: helper that appends events to the temp journal.
- multi_commander_startup_block: a pre-built sequence of journal events
  representing a multi-commander handoff startup.
- continued_journal: a pair of journal files simulating a rollover
  ("Continued" file pattern).
- mock_window_enum: a lightweight stub for window-enumeration callbacks.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Generator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# Force offscreen rendering for all Qt tests so no display is needed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ---------------------------------------------------------------------------
# Root fixture directory (checked into the tree for deterministic data)
# ---------------------------------------------------------------------------
FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_journal_lines(path: Path, events: Sequence[dict[str, Any]]) -> None:
    """Append *events* as JSON-lines to *path*."""
    with path.open("a", encoding="utf-8") as fh:
        for evt in events:
            _ = fh.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _make_event(
    event: str,
    timestamp: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a minimal Elite Dangerous journal event dict."""
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    return {"timestamp": ts, "event": event, **extra}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture()
def tmp_journal(tmp_path: Path) -> Path:
    """Return a path to a fresh journal file inside a temp directory."""
    journal = tmp_path / "Journal.2026-01-01T000000.01.log"
    journal.touch()
    return journal


@pytest.fixture()
def journal_events(tmp_journal: Path) -> Callable[[Sequence[dict[str, Any]]], None]:
    """Return a helper callable that appends events to *tmp_journal*.

    Usage::

        def test_something(journal_events):
            journal_events([
                {"event": "CarrierJumpRequest", "SystemName": "Sol"},
            ])
    """
    def _append(events: Sequence[dict[str, Any]]) -> None:
        normalised: list[dict[str, Any]] = []
        for e in events:
            if "timestamp" not in e:
                normalised.append(_make_event(**e))
            else:
                normalised.append(e)
        _write_journal_lines(tmp_journal, normalised)

    return _append


@pytest.fixture()
def multi_commander_startup_block() -> list[dict[str, Any]]:
    """A sequence of events representing multi-commander startup handoff."""
    return [
        _make_event("Fileheader", gameversion="4.0", build="r280401"),
        _make_event("Commander", Name="TestCmdr"),
        _make_event("CarrierStats", FuelLevel=800, UsedCapacity=5000),
        _make_event(
            "CarrierJumpRequest",
            SystemName="Sol",
            DepartureTime="2026-04-25T12:00:00Z",
        ),
    ]


@pytest.fixture()
def continued_journal(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(part1, part2)`` paths simulating a journal rollover.

    Elite Dangerous names continued files like:
    ``Journal.2026-01-01T000000.02.log``
    """
    part1 = tmp_path / "Journal.2026-01-01T000000.01.log"
    part2 = tmp_path / "Journal.2026-01-01T000000.02.log"
    part1.touch()
    part2.touch()

    # Write a Continuation marker in part2
    _write_journal_lines(part2, [
        _make_event("Fileheader", gameversion="4.0", build="r280401",
                     Part=2),
    ])
    return part1, part2


class MockWindowInfo:
    """Lightweight stand-in for window-enumeration data."""

    __slots__: tuple[str, ...] = ("title", "hwnd", "visible")

    def __init__(self, title: str, hwnd: int = 0, visible: bool = True) -> None:
        self.title: str = title
        self.hwnd: int = hwnd
        self.visible: bool = visible


@pytest.fixture()
def mock_window_enum() -> list[MockWindowInfo]:
    """Return a pre-built list of mock window entries.

    Includes an Elite Dangerous window and some noise windows.
    """
    return [
        MockWindowInfo("Elite - Dangerous (CLIENT)", hwnd=1001, visible=True),
        MockWindowInfo("Steam", hwnd=2001, visible=True),
        MockWindowInfo("Discord", hwnd=3001, visible=True),
        MockWindowInfo("", hwnd=4001, visible=False),
    ]


@pytest.fixture()
def fixtures_dir() -> Path:
    """Return the ``tests/fixtures/`` directory path."""
    return FIXTURES_DIR
