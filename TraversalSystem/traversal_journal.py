"""Journal abstraction layer for the CTS traversal system.

Provides:
- ``JournalScanLoop``: background polling thread that drives
  ``MultiJournalRouter.scan_once`` on a 1-second interval.

This class is extracted from ``main.py`` so that integration tests can
import it without pulling in third-party GUI dependencies (pyautogui, etc.).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class _ScanOnce(Protocol):
    def scan_once(self, journal_dir: Path) -> None: ...


class JournalScanLoop:
    """Background thread that polls the journal directory on a 1-second
    interval using ``MultiJournalRouter.scan_once``.

    Args:
        router: A ``MultiJournalRouter`` (or any ``_ScanOnce``-compatible)
            instance to drive.
        journal_dir: Path to the journal directory to scan.
        error_callback: Optional callable invoked with the ``Exception``
            instance whenever ``scan_once`` raises.  When *None*, errors
            are logged via the module logger at warning level.  Supply a
            callback in production to centralise error reporting (e.g.
            recording into a GUI status buffer).
        fail_fast: When *True*, the first ``scan_once`` exception causes
            the loop thread to exit immediately.  When *False* (the
            default), the loop continues polling after reporting the error
            through the callback — preserving the original production
            continuation semantics.
    """

    def __init__(
        self,
        router: _ScanOnce,
        journal_dir: Path,
        *,
        error_callback: Callable[[Exception], None] | None = None,
        fail_fast: bool = False,
    ) -> None:
        self.router: _ScanOnce = router
        self.journal_dir: Path = journal_dir
        self._error_callback: Callable[[Exception], None] | None = error_callback
        self._fail_fast: bool = fail_fast
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
            except Exception as exc:
                if self._error_callback is not None:
                    self._error_callback(exc)
                else:
                    logger.warning("Journal scan_once error: %s", exc)
                if self._fail_fast:
                    break
            _ = self._stop.wait(1.0)
        print("Journal scan loop halted")
