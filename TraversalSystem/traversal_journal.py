"""Journal abstraction layer for the CTS traversal system.

Provides:
- ``LegacyJournalFacade``: adapter wrapping ``JournalWatcher`` with the same
  read-only interface as ``CTSJournalFacade``.
- ``JournalScanLoop``: background polling thread that drives
  ``MultiJournalRouter.scan_once`` on a 1-second interval.

These classes are extracted from ``main.py`` so that integration tests can
import them without pulling in third-party GUI dependencies (pyautogui, etc.).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .multi_journal_router import MultiJournalRouter


class LegacyJournalFacade:
    """Adapter that presents the same read interface as ``CTSJournalFacade``
    but delegates to a legacy ``JournalWatcher`` instance.

    This lets the traversal loop call a uniform ``last_carrier_request()``,
    ``departure_time()``, ``has_jumped()``, ``reset_jump()`` API regardless
    of whether multicarrier mode is active.

    ``JournalWatcher`` is imported lazily inside ``__init__`` to avoid pulling
    in third-party dependencies when only ``JournalScanLoop`` is needed.
    """

    def __init__(self, watcher: Any) -> None:
        from .journalwatcher import JournalWatcher
        self._watcher: JournalWatcher = watcher

    def state(self) -> Any:
        return None

    def last_carrier_request(self) -> str | None:
        try:
            result = self._watcher.last_carrier_request()
        except AttributeError:
            return None
        return result if result else None

    def departure_time(self) -> str | None:
        dt = self._watcher.departureTime
        return dt if dt else None

    def has_jumped(self) -> bool:
        try:
            return self._watcher.get_jumped()
        except AttributeError:
            return False

    def reset_jump(self) -> None:
        self._watcher.reset_jump()

    def jump_cancelled(self) -> bool:
        """Legacy watcher has no cancel tracking; always False."""
        return False

    def reset_cancel(self) -> None:
        """No-op for legacy watcher."""

    def last_fuel(self) -> float | None:
        """Return the last known fuel level from the watcher."""
        try:
            return float(self._watcher.lastFuel)
        except (AttributeError, TypeError, ValueError):
            return None


class JournalScanLoop:
    """Background thread that polls the journal directory on a 1-second
    interval using ``MultiJournalRouter.scan_once``.

    In multicarrier mode the scan loop replaces the old
    ``start_journal_thread`` / ``JournalWatcher`` lifecycle entirely.
    In legacy mode the old ``start_journal_thread`` path is kept unchanged.
    """

    def __init__(self, router: MultiJournalRouter, journal_dir: Path) -> None:
        self.router: MultiJournalRouter = router
        self.journal_dir: Path = journal_dir
        self._stop: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.router.scan_once(self.journal_dir)
            except Exception:
                pass  # keep polling; caller handles critical errors separately
            _ = self._stop.wait(1.0)
        print("Journal scan loop halted")
