"""Multi-file journal router for Elite Dangerous carrier traversal.

Directory-scanning router that tails every ``Journal*.log`` file by byte
offset, buffers partial lines, and routes carrier state per commander FID.

Identity is established exclusively from ``Commander`` / ``LoadGame`` events.
No filename-based routing or whole-file snapshots are used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CARRIER_EVENTS: frozenset[str] = frozenset({
    "CarrierJumpRequest",
    "CarrierJump",
    "CarrierJumpCancelled",
    "CarrierStats",
})
MAX_PENDING_CARRIER_EVENTS: int = 100


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
    continued_next_part: int | None = None
    identity_ambiguity: str | None = None
    identity_error: str | None = None
    last_timestamp: str | None = None
    last_mtime_ns: int = 0
    pending_events: list[dict[str, Any]] = field(default_factory=list)
    pending_overflow: bool = False


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
    # Per-field freshness timestamps (not for discovery/UI — last_event_ts is).
    _ts_request: str | None = None
    _ts_fuel: str | None = None
    _ts_jump: str | None = None
    _ts_cancel: str | None = None
    is_squadron_carrier: bool = False


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
        self._resolve_rollover_inheritance()

    def _resolve_rollover_inheritance(self) -> None:
        """Attempt identity inheritance for pending no-FID successor files.

        A successor file that has ``part`` set but no ``fid`` can inherit
        identity from a unique predecessor whose ``continued_next_part``
        matches the successor's ``part``.  Ambiguous matches (multiple
        candidates) fail closed.
        """
        for fstate in self.files.values():
            if fstate.fid or fstate.identity_error or fstate.part is None:
                continue
            matches = [
                ps for ps in self.files.values()
                if (
                    ps.expecting_continued
                    and ps.continued_next_part == fstate.part
                    and ps.fid
                    and not ps.identity_error
                )
            ]
            if len(matches) == 1:
                pred = matches[0]
                fstate.fid = pred.fid
                fstate.commander_name = pred.commander_name
                self._replay_pending(fstate)
            elif len(matches) > 1:
                fids = [ps.fid for ps in matches]
                fstate.identity_ambiguity = (
                    f"Multiple predecessors match Part={fstate.part}: "
                    f"FIDs={fids}"
                )

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
            self._cleanup_active_file(state)
            self._reset_file_state_after_replacement(
                state, last_mtime_ns=mtime_ns,
            )
        else:
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

    def _cleanup_active_file(self, file_state: FileTailState) -> None:
        """Remove *file_state.file_path* from its commander's active_files."""
        fid = file_state.fid
        if not fid:
            return
        cmdr = self.commanders.get(fid)
        if cmdr is not None:
            cmdr.active_files.discard(file_state.file_path)

    @staticmethod
    def _reset_file_state_after_replacement(
        file_state: FileTailState,
        *,
        last_mtime_ns: int,
    ) -> None:
        """Reset all mutable fields after truncation/replacement."""
        file_state.offset = 0
        file_state.partial_buffer = b""
        file_state.part = None
        file_state.fid = None
        file_state.commander_name = None
        file_state.active = True
        file_state.saw_shutdown = False
        file_state.expecting_continued = False
        file_state.continued_next_part = None
        file_state.identity_error = None
        file_state.last_timestamp = None
        file_state.last_mtime_ns = last_mtime_ns
        file_state.pending_events = []
        file_state.pending_overflow = False
        file_state.identity_ambiguity = None

    @staticmethod
    def _apply_carrier_event(cmdr: CommanderState, evt: dict[str, Any]) -> None:
        event_name = evt.get("event")
        ts: str | None = evt.get("timestamp") or None

        if event_name == "CarrierJumpRequest":
            if ts is None or ts >= (cmdr._ts_request or ""):
                cmdr._ts_request = ts
                cmdr.last_carrier_request = evt.get("SystemName")
                cmdr.departure_time = evt.get("DepartureTime")
                cmdr.has_jumped = False
                cmdr.jump_cancelled = False
        elif event_name == "CarrierStats":
            fuel = evt.get("FuelLevel")
            if fuel is not None:
                if ts is None or ts >= (cmdr._ts_fuel or ""):
                    cmdr._ts_fuel = ts
                    cmdr.last_fuel = float(fuel)
            carrier_type = evt.get("CarrierType")
            if carrier_type is not None:
                cmdr.is_squadron_carrier = (carrier_type == "SquadronCarrier")
        elif event_name == "CarrierJump":
            if ts is None or ts >= (cmdr._ts_jump or ""):
                cmdr._ts_jump = ts
                cmdr.has_jumped = True
        elif event_name == "CarrierJumpCancelled":
            if ts is None or ts >= (cmdr._ts_cancel or ""):
                cmdr._ts_cancel = ts
                cmdr.jump_cancelled = True
                cmdr.last_carrier_request = None
                cmdr.departure_time = None

    def _replay_pending(self, file_state: FileTailState) -> None:
        if not file_state.pending_events or not file_state.fid:
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
        for pending_evt in file_state.pending_events:
            ts = pending_evt.get("timestamp")
            if ts and ts > (cmdr.last_event_ts or ""):
                cmdr.last_event_ts = ts
            self._apply_carrier_event(cmdr, pending_evt)
        file_state.pending_events.clear()

    def _fid_mismatch(self, file_state: FileTailState, new_fid: str | None, source: str) -> bool:
        if (
            file_state.fid
            and new_fid
            and new_fid != file_state.fid
        ):
            self._cleanup_active_file(file_state)
            file_state.identity_error = (
                f"{source} FID {new_fid!r} conflicts with "
                f"established {file_state.fid!r}"
            )
            file_state.active = False
            return True
        return False

    def _handle_event(self, file_state: FileTailState, evt: dict[str, Any]) -> None:
        if file_state.identity_error:
            return

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
            new_fid = evt.get("FID")
            if self._fid_mismatch(file_state, new_fid, "Commander"):
                return
            file_state.commander_name = evt.get("Name")
            file_state.fid = new_fid
            if new_fid:
                self._replay_pending(file_state)
        elif event_name == "LoadGame":
            new_fid = evt.get("FID")
            if self._fid_mismatch(file_state, new_fid, "LoadGame"):
                return
            file_state.commander_name = evt.get(
                "Commander", file_state.commander_name,
            )
            if new_fid is not None:
                file_state.fid = new_fid
                self._replay_pending(file_state)
        elif event_name == "Shutdown":
            self._cleanup_active_file(file_state)
            file_state.saw_shutdown = True
            file_state.active = False
            return
        elif event_name == "Continued":
            file_state.expecting_continued = True
            next_part = evt.get("Part") or evt.get("part")
            if next_part is not None:
                file_state.continued_next_part = int(next_part)
            return
        elif event_name == "Location":
            if not file_state.fid:
                return

        if not file_state.fid:
            if event_name in CARRIER_EVENTS:
                file_state.pending_events.append(dict(evt))
                if len(file_state.pending_events) > MAX_PENDING_CARRIER_EVENTS:
                    file_state.pending_events.pop(0)
                    file_state.pending_overflow = True
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

        if event_name in CARRIER_EVENTS:
            self._apply_carrier_event(cmdr, evt)


# ---------------------------------------------------------------------------
# Selected-commander facade
# ---------------------------------------------------------------------------

class CTSJournalFacade:
    """Read-only view over the router for a single target commander.

    This is the interface that traversal code should use for per-commander
    journal state.
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

    def is_squadron_carrier(self) -> bool:
        cmdr = self.state()
        return bool(cmdr and cmdr.is_squadron_carrier)

    def reset_cancel(self) -> None:
        cmdr = self.state()
        if cmdr is not None:
            cmdr.jump_cancelled = False
