"""Multi-file journal router for Elite Dangerous carrier traversal.

Replaces the single-file ``JournalWatcher`` with a directory-scanning router
that tails every ``Journal*.log`` file by byte offset, buffers partial lines,
and routes carrier state per commander FID.

Identity is established exclusively from ``Commander`` / ``LoadGame`` events.
No filename-based routing or whole-file snapshots are used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FileTailState:
    """Per-file tail tracking state."""

    file_path: str
    offset: int = 0
    partial_buffer: bytes = b""
    part: int | None = None
    fid: str | None = None
    commander_name: str | None = None
    active: bool = True
    saw_shutdown: bool = False
    expecting_continued: bool = False
    last_timestamp: str | None = None
    last_mtime_ns: int = 0


@dataclass
class CommanderState:
    """Per-commander carrier state, keyed by FID."""

    fid: str
    commander_name: str | None = None
    active_files: set[str] = field(default_factory=set)
    last_event_ts: str | None = None
    last_carrier_request: str | None = None
    departure_time: str | None = None
    last_fuel: float | None = None
    has_jumped: bool = False
    jump_cancelled: bool = False


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class MultiJournalRouter:
    """Scans a journal directory and maintains per-commander state.

    Usage::

        router = MultiJournalRouter()
        router.scan_once(journal_dir)
        state = router.commanders.get("F12345")
    """

    def __init__(self) -> None:
        self.files: dict[str, FileTailState] = {}
        self.commanders: dict[str, CommanderState] = {}

    # -- public API ---------------------------------------------------------

    def scan_once(self, journal_dir: Path) -> None:
        """Scan *journal_dir* once, tailing every ``Journal*.log`` file."""
        if not journal_dir.is_dir():
            return
        for path in sorted(journal_dir.glob("Journal*.log")):
            self.tail_file(path)

    def tail_file(self, path: Path) -> None:
        """Read appended bytes from *path* and process any new events."""
        key = str(path)
        state = self.files.setdefault(key, FileTailState(file_path=key))

        if not state.active:
            return

        try:
            stat = path.stat()
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns
        except OSError:
            return

        # Detect truncation: file shrank or was replaced (same size but
        # newer mtime).  Both cases require re-reading from offset 0.
        if size < state.offset or (
            size == state.offset and mtime_ns != state.last_mtime_ns
        ):
            state.offset = 0
            state.partial_buffer = b""

        state.last_mtime_ns = mtime_ns

        if size == state.offset:
            return

        with path.open("rb") as fh:
            _ = fh.seek(state.offset)
            chunk = fh.read()
            state.offset = fh.tell()

        if not chunk:
            return

        data = state.partial_buffer + chunk
        lines = data.split(b"\n")

        # If data doesn't end with newline, the last fragment is incomplete.
        if data and not data.endswith(b"\n"):
            state.partial_buffer = lines.pop()
        else:
            state.partial_buffer = b""

        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                evt = json.loads(stripped.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            self._handle_event(state, evt)

    def reset_jump(self, fid: str) -> None:
        """Clear the jump flag for the specified commander."""
        cmdr = self.commanders.get(fid)
        if cmdr is not None:
            cmdr.has_jumped = False

    # -- internal -----------------------------------------------------------

    def _handle_event(self, file_state: FileTailState, evt: dict[str, Any]) -> None:
        event_name = evt.get("event")
        if not event_name:
            return

        ts = evt.get("timestamp")
        if ts:
            file_state.last_timestamp = ts

        # --- Identity / session events ---
        if event_name in {"FileHeader", "Fileheader"}:
            file_state.part = evt.get("part") or evt.get("Part")  # type: ignore[union-attr]
            return  # no commander state to update

        if event_name == "Commander":
            file_state.commander_name = evt.get("Name")
            file_state.fid = evt.get("FID")
        elif event_name == "LoadGame":
            file_state.commander_name = evt.get(
                "Commander", file_state.commander_name,
            )
            file_state.fid = evt.get("FID", file_state.fid)
        elif event_name == "Shutdown":
            file_state.saw_shutdown = True
            file_state.active = False
            return
        elif event_name == "Continued":
            file_state.expecting_continued = True
            return
        elif event_name == "Location":
            # Optional compatibility fallback — identity only if unbound.
            if not file_state.fid:
                return

        # If we still do not know the commander, do not mutate commander state.
        if not file_state.fid:
            return

        cmdr = self.commanders.setdefault(
            file_state.fid,
            CommanderState(
                fid=file_state.fid,
                commander_name=file_state.commander_name,
            ),
        )

        cmdr.commander_name = cmdr.commander_name or file_state.commander_name
        cmdr.active_files.add(file_state.file_path)
        if ts:
            cmdr.last_event_ts = ts

        # --- Carrier events ---
        if event_name == "CarrierJumpRequest":
            cmdr.last_carrier_request = evt.get("SystemName")
            cmdr.departure_time = evt.get("DepartureTime")
            cmdr.has_jumped = False
            cmdr.jump_cancelled = False
        elif event_name == "CarrierStats":
            fuel = evt.get("FuelLevel")
            if fuel is not None:
                cmdr.last_fuel = float(fuel)
        elif event_name == "CarrierJump":
            cmdr.has_jumped = True
        elif event_name == "CarrierJumpCancelled":
            cmdr.jump_cancelled = True
            cmdr.last_carrier_request = None
            cmdr.departure_time = None


# ---------------------------------------------------------------------------
# Selected-commander facade
# ---------------------------------------------------------------------------

class CTSJournalFacade:
    """Read-only view over the router for a single target commander.

    This is the interface that traversal code should use instead of touching
    ``JournalWatcher`` globals directly.
    """

    def __init__(self, router: MultiJournalRouter, target_fid: str) -> None:
        self.router: MultiJournalRouter = router
        self.target_fid: str = target_fid

    def state(self) -> CommanderState | None:
        return self.router.commanders.get(self.target_fid)

    def last_carrier_request(self) -> str | None:
        cmdr = self.state()
        return cmdr.last_carrier_request if cmdr else None

    def departure_time(self) -> str | None:
        cmdr = self.state()
        return cmdr.departure_time if cmdr else None

    def has_jumped(self) -> bool:
        cmdr = self.state()
        return bool(cmdr and cmdr.has_jumped)

    def reset_jump(self) -> None:
        self.router.reset_jump(self.target_fid)

    def last_fuel(self) -> float | None:
        cmdr = self.state()
        return cmdr.last_fuel if cmdr else None

    def jump_cancelled(self) -> bool:
        cmdr = self.state()
        return bool(cmdr and cmdr.jump_cancelled)

    def reset_cancel(self) -> None:
        cmdr = self.state()
        if cmdr is not None:
            cmdr.jump_cancelled = False
