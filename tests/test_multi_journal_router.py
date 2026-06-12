"""Tests for the multi-file journal router (Task 3).

Covers:
- Two commanders routed independently from one directory
- Per-commander carrier state isolation
- Simultaneous append activity
- Rollover via Continued event
- Incomplete JSON line buffering and completion
- Identity gating (no state mutation before Commander/LoadGame)
- Shutdown deactivation
- CTSJournalFacade selected-commander access
- CarrierJumpCancelled clears only the owning commander's state
- Location fallback identity
- File truncation/reset handling
"""
from __future__ import annotations

import json
from pathlib import Path

from TraversalSystem.multi_journal_router import (  # pyright: ignore[reportMissingImports]
    CTSJournalFacade,
    MultiJournalRouter,
)

FIXTURES = Path(__file__).parent / "fixtures" / "journals"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_lines(path: Path, events: list[dict[str, object]]) -> None:
    """Append JSON-line events to *path*."""
    with path.open("a", encoding="utf-8") as fh:
        for evt in events:
            _ = fh.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _evt(event: str, **kw: object) -> dict[str, object]:
    _ = kw.setdefault("timestamp", "2026-04-25T12:00:00Z")
    return {"event": event, **kw}


# ---------------------------------------------------------------------------
# Fixture directory: two commanders (static)
# ---------------------------------------------------------------------------

class TestTwoCommanderFixtures:
    """Validate the checked-in fixture directory."""

    def test_scan_discovers_both_commanders(self) -> None:
        router = MultiJournalRouter()
        router.scan_once(FIXTURES / "two_commanders")
        assert "FID-PRIMARY" in router.commanders
        assert "FID-SECONDARY" in router.commanders

    def test_primary_carrier_state(self) -> None:
        router = MultiJournalRouter()
        router.scan_once(FIXTURES / "two_commanders")
        p = router.commanders["FID-PRIMARY"]
        assert p.commander_name == "PrimaryCmdr"
        assert p.last_carrier_request == "Sol"
        assert p.departure_time == "2026-04-25T12:15:00Z"
        assert p.has_jumped is True
        assert p.last_fuel == 800.0

    def test_secondary_carrier_state(self) -> None:
        router = MultiJournalRouter()
        router.scan_once(FIXTURES / "two_commanders")
        s = router.commanders["FID-SECONDARY"]
        assert s.commander_name == "SecondaryCmdr"
        assert s.last_carrier_request == "Deciat"
        assert s.departure_time == "2026-04-25T12:20:00Z"
        assert s.has_jumped is False
        assert s.last_fuel == 600.0


# ---------------------------------------------------------------------------
# Two commanders via tmp_path (dynamic)
# ---------------------------------------------------------------------------

class TestTwoCommandersDynamic:
    """Two commanders writing to separate journal files simultaneously."""

    def test_independent_carrier_state(self, tmp_path: Path) -> None:
        j1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        j2 = tmp_path / "Journal.2026-04-25T120001.01.log"

        _write_lines(j1, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-A", Name="CmdrA"),
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z"),
        ])
        _write_lines(j2, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-B", Name="CmdrB"),
            _evt("CarrierJumpRequest", SystemName="Deciat",
                  DepartureTime="2026-04-25T12:20:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        a = router.commanders["F-A"]
        b = router.commanders["F-B"]
        assert a.last_carrier_request == "Sol"
        assert b.last_carrier_request == "Deciat"
        assert a.has_jumped is False
        assert b.has_jumped is False

    def test_simultaneous_append(self, tmp_path: Path) -> None:
        j1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        j2 = tmp_path / "Journal.2026-04-25T120001.01.log"

        _write_lines(j1, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-A", Name="CmdrA"),
        ])
        _write_lines(j2, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-B", Name="CmdrB"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        assert "F-A" in router.commanders
        assert "F-B" in router.commanders

        # Simulate simultaneous appends
        _write_lines(j1, [
            _evt("CarrierStats", FuelLevel=700),
            _evt("CarrierJump"),
        ])
        _write_lines(j2, [
            _evt("CarrierStats", FuelLevel=500),
        ])

        router.scan_once(tmp_path)

        assert router.commanders["F-A"].has_jumped is True
        assert router.commanders["F-A"].last_fuel == 700.0
        assert router.commanders["F-B"].has_jumped is False
        assert router.commanders["F-B"].last_fuel == 500.0


# ---------------------------------------------------------------------------
# Identity gating
# ---------------------------------------------------------------------------

class TestIdentityGating:
    """Commander state must NOT be mutated before FID is known."""

    def test_no_commander_state_before_identity(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("CarrierStats", FuelLevel=800),
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        assert len(router.commanders) == 0

    def test_events_buffered_until_identity_then_applied(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("CarrierStats", FuelLevel=800),
            _evt("Commander", FID="F-X", Name="X"),
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        # CarrierStats before identity should NOT have created a commander
        # But Commander event creates the commander, and the subsequent
        # CarrierJumpRequest does update state.
        x = router.commanders["F-X"]
        assert x.commander_name == "X"
        assert x.last_carrier_request == "Sol"
        # Pre-identity CarrierStats should be replayed after Commander
        # establishes identity.
        assert x.last_fuel == 800.0

    def test_loadgame_establishes_identity(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("LoadGame", FID="F-LG", Commander="LoadCmdr"),
            _evt("CarrierStats", FuelLevel=900),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        lg = router.commanders["F-LG"]
        assert lg.commander_name == "LoadCmdr"
        assert lg.last_fuel == 900.0


# ---------------------------------------------------------------------------
# Rollover via Continued
# ---------------------------------------------------------------------------

class TestRollover:
    """Continued event links successor files to the same commander."""

    def test_continued_marks_file(self, tmp_path: Path) -> None:
        part1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(part1, [
            _evt("Fileheader", gameversion="4.0", Part=1),
            _evt("Commander", FID="F-R", Name="RollCmdr"),
            _evt("Continued", Part=2),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        fs = router.files[str(part1)]
        assert fs.expecting_continued is True
        assert fs.part == 1

    def test_successor_file_binds_same_fid(self, tmp_path: Path) -> None:
        part1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        part2 = tmp_path / "Journal.2026-04-25T120000.02.log"

        _write_lines(part1, [
            _evt("Fileheader", gameversion="4.0", Part=1),
            _evt("Commander", FID="F-R", Name="RollCmdr"),
            _evt("Continued", Part=2),
        ])
        _write_lines(part2, [
            _evt("Fileheader", gameversion="4.0", Part=2),
            _evt("LoadGame", FID="F-R", Commander="RollCmdr"),
            _evt("CarrierStats", FuelLevel=750),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        r = router.commanders["F-R"]
        assert r.last_fuel == 750.0
        # Both files should be in active_files
        assert str(part1) in r.active_files
        assert str(part2) in r.active_files


# ---------------------------------------------------------------------------
# Partial line buffering
# ---------------------------------------------------------------------------

class TestPartialLineBuffering:
    """Incomplete JSON lines are held until completed on a later scan."""

    def test_truncated_line_completed_on_next_scan(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        j.touch()

        # Write full events plus an incomplete trailing line
        full_events = [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-P", Name="PartialCmdr"),
        ]
        for e in full_events:
            _ = j.write_bytes(
                j.read_bytes() + (json.dumps(e) + "\n").encode(),
            )

        # Write a partial line (no trailing newline)
        partial = '{"event":"CarrierStats","FuelLevel":500,'
        _ = j.write_bytes(j.read_bytes() + partial.encode())

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        # Commander should exist but fuel not yet recorded
        p = router.commanders["F-P"]
        assert p.commander_name == "PartialCmdr"
        assert p.last_fuel is None

        # Complete the partial line
        completion = '"timestamp":"2026-04-25T12:00:05Z"}\n'
        _ = j.write_bytes(j.read_bytes() + completion.encode())

        router.scan_once(tmp_path)
        assert router.commanders["F-P"].last_fuel == 500.0

    def test_buffer_across_multiple_scans(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"

        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-MP", Name="MultiPartial"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        # Write first chunk (incomplete)
        with j.open("ab") as fh:
            _ = fh.write(b'{"event":"CarrierJumpRequest","SystemN')
        router.scan_once(tmp_path)
        assert router.commanders["F-MP"].last_carrier_request is None

        # Write second chunk completing the line
        with j.open("ab") as fh:
            _ = fh.write(b'ame":"Sol","DepartureTime":"2026-04-25T12:15:00Z"}\n')
        router.scan_once(tmp_path)
        assert router.commanders["F-MP"].last_carrier_request == "Sol"


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

class TestShutdown:
    """Shutdown event deactivates file tailing."""

    def test_shutdown_stops_tailing(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-S", Name="ShutCmdr"),
            _evt("Shutdown"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        fs = router.files[str(j)]
        assert fs.saw_shutdown is True
        assert fs.active is False

        # Write more data — should be ignored
        _write_lines(j, [
            _evt("CarrierStats", FuelLevel=100),
        ])
        router.scan_once(tmp_path)
        assert router.commanders["F-S"].last_fuel is None


# ---------------------------------------------------------------------------
# CarrierJumpCancelled
# ---------------------------------------------------------------------------

class TestCarrierJumpCancelled:
    """Cancellation clears only the owning commander's pending state."""

    def test_cancelled_clears_pending(self, tmp_path: Path) -> None:
        j1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        j2 = tmp_path / "Journal.2026-04-25T120001.01.log"

        _write_lines(j1, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-C1", Name="Cancel1"),
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z"),
        ])
        _write_lines(j2, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-C2", Name="Cancel2"),
            _evt("CarrierJumpRequest", SystemName="Deciat",
                  DepartureTime="2026-04-25T12:20:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        # Cancel F-C1
        _write_lines(j1, [
            _evt("CarrierJumpCancelled"),
        ])
        router.scan_once(tmp_path)

        c1 = router.commanders["F-C1"]
        c2 = router.commanders["F-C2"]
        assert c1.jump_cancelled is True
        assert c1.last_carrier_request is None
        assert c1.departure_time is None
        # F-C2 unaffected
        assert c2.jump_cancelled is False
        assert c2.last_carrier_request == "Deciat"
        assert c2.departure_time == "2026-04-25T12:20:00Z"


# ---------------------------------------------------------------------------
# Location fallback
# ---------------------------------------------------------------------------

class TestLocationFallback:
    """Location event is accepted as a compatibility identity source."""

    def test_location_after_identity_updates_last_event_ts(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-LOC", Name="LocCmdr"),
            _evt("Location", StarSystem="Sol",
                  timestamp="2026-04-25T12:05:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        loc = router.commanders["F-LOC"]
        assert loc.last_event_ts == "2026-04-25T12:05:00Z"

    def test_location_before_identity_ignored(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Location", StarSystem="Sol"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        assert len(router.commanders) == 0


# ---------------------------------------------------------------------------
# File truncation / replacement
# ---------------------------------------------------------------------------

class TestFileTruncation:
    """If a file shrinks (truncated/replaced), offset resets cleanly."""

    def test_truncated_file_resets_offset(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-T", Name="TruncCmdr"),
            _evt("CarrierStats", FuelLevel=800),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        assert router.commanders["F-T"].last_fuel == 800.0

        # Truncate and write fresh content
        _ = j.write_text("", encoding="utf-8")
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-T", Name="TruncCmdr"),
            _evt("CarrierStats", FuelLevel=600),
        ])

        router.scan_once(tmp_path)
        assert router.commanders["F-T"].last_fuel == 600.0


# ---------------------------------------------------------------------------
# CTSJournalFacade
# ---------------------------------------------------------------------------

class TestCTSJournalFacade:
    """Selected-commander facade routes to the correct FID only."""

    def _setup_router(self, tmp_path: Path) -> MultiJournalRouter:
        j1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        j2 = tmp_path / "Journal.2026-04-25T120001.01.log"

        _write_lines(j1, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-FAC1", Name="Facade1"),
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z"),
            _evt("CarrierJump"),
        ])
        _write_lines(j2, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-FAC2", Name="Facade2"),
            _evt("CarrierStats", FuelLevel=500),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        return router

    def test_facade_reads_target_only(self, tmp_path: Path) -> None:
        router = self._setup_router(tmp_path)
        f1 = CTSJournalFacade(router, "F-FAC1")
        f2 = CTSJournalFacade(router, "F-FAC2")

        assert f1.last_carrier_request() == "Sol"
        assert f1.has_jumped() is True
        assert f1.departure_time() == "2026-04-25T12:15:00Z"

        assert f2.last_carrier_request() is None
        assert f2.has_jumped() is False
        assert f2.last_fuel() == 500.0

    def test_facade_reset_jump(self, tmp_path: Path) -> None:
        router = self._setup_router(tmp_path)
        f1 = CTSJournalFacade(router, "F-FAC1")
        assert f1.has_jumped() is True
        f1.reset_jump()
        assert f1.has_jumped() is False
        # Other commander unaffected
        assert router.commanders["F-FAC2"].has_jumped is False

    def test_facade_unknown_fid_returns_none(self, tmp_path: Path) -> None:
        router = self._setup_router(tmp_path)
        f_unknown = CTSJournalFacade(router, "F-NOPE")
        assert f_unknown.state() is None
        assert f_unknown.last_carrier_request() is None
        assert f_unknown.departure_time() is None
        assert f_unknown.has_jumped() is False
        assert f_unknown.last_fuel() is None


# ---------------------------------------------------------------------------
# Reset jump via router
# ---------------------------------------------------------------------------

class TestResetJump:
    """Router.reset_jump clears only the specified commander."""

    def test_reset_target_only(self, tmp_path: Path) -> None:
        j1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        j2 = tmp_path / "Journal.2026-04-25T120001.01.log"

        _write_lines(j1, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-RJ1", Name="RJ1"),
            _evt("CarrierJump"),
        ])
        _write_lines(j2, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-RJ2", Name="RJ2"),
            _evt("CarrierJump"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert router.commanders["F-RJ1"].has_jumped is True
        assert router.commanders["F-RJ2"].has_jumped is True

        router.reset_jump("F-RJ1")
        assert router.commanders["F-RJ1"].has_jumped is False
        assert router.commanders["F-RJ2"].has_jumped is True

    def test_reset_nonexistent_fid_noop(self) -> None:
        router = MultiJournalRouter()
        router.reset_jump("F-NONEXISTENT")  # should not raise


# ---------------------------------------------------------------------------
# scan_once safety
# ---------------------------------------------------------------------------

class TestScanOnceSafety:
    """scan_once handles missing/unreadable directories gracefully."""

    def test_nonexistent_dir_noop(self) -> None:
        router = MultiJournalRouter()
        router.scan_once(Path("/nonexistent/path"))
        assert len(router.files) == 0
        assert len(router.commanders) == 0

    def test_empty_dir_noop(self, tmp_path: Path) -> None:
        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        assert len(router.files) == 0


# ---------------------------------------------------------------------------
# No filename-based routing
# ---------------------------------------------------------------------------

class TestNoFilenameRouting:
    """Verify identity comes from events, not filenames."""

    def test_identity_from_events_not_filename(self, tmp_path: Path) -> None:
        # Filename says "JournalA" but events say FID-MISMATCH
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-MISMATCH", Name="RealCmdr"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert "F-MISMATCH" in router.commanders
        assert router.commanders["F-MISMATCH"].commander_name == "RealCmdr"


# ---------------------------------------------------------------------------
# FileHeader / Fileheader variant
# ---------------------------------------------------------------------------

class TestFileHeaderVariants:
    """Both FileHeader and Fileheader (case variant) are handled."""

    def test_lowercase_fileheader(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0", Part=1),
            _evt("Commander", FID="F-FH1", Name="FH1"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        fs = router.files[str(j)]
        assert fs.part == 1
        assert fs.fid == "F-FH1"

    def test_mixed_case_fileheader(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            {"event": "FileHeader", "gameversion": "4.0", "Part": 2,
             "timestamp": "2026-04-25T12:00:00Z"},
            _evt("Commander", FID="F-FH2", Name="FH2"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        fs = router.files[str(j)]
        assert fs.part == 2
        assert fs.fid == "F-FH2"


# ---------------------------------------------------------------------------
# FID consistency protection (EDCM-style)
# ---------------------------------------------------------------------------

class TestFIDConsistency:
    """A file that changes its non-empty FID is permanently invalidated."""

    def test_identity_change_commander_to_loadgame_invalidates(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-A", Name="CmdrA"),
            _evt("LoadGame", FID="F-B", Commander="CmdrB"),
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert "F-A" in router.commanders
        fa = router.commanders["F-A"]
        assert fa.commander_name == "CmdrA"
        assert fa.last_carrier_request is None
        assert fa.departure_time is None

        assert "F-B" not in router.commanders

        fs = router.files[str(j)]
        assert fs.identity_error is not None
        assert "F-B" in fs.identity_error
        assert fs.active is False

    def test_identity_change_commander_to_commander_invalidates(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-A", Name="CmdrA"),
            _evt("Commander", FID="F-C", Name="CmdrC"),
            _evt("CarrierJumpRequest", SystemName="Deciat",
                  DepartureTime="2026-04-25T12:20:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert "F-A" in router.commanders
        assert "F-C" not in router.commanders
        assert router.commanders["F-A"].last_carrier_request is None

        fs = router.files[str(j)]
        assert fs.identity_error is not None
        assert fs.active is False

    def test_fid_consistency_matching_fid_no_invalidation(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-A", Name="CmdrA"),
            _evt("LoadGame", FID="F-A", Commander="CmdrA"),
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert router.commanders["F-A"].last_carrier_request == "Sol"
        fs = router.files[str(j)]
        assert fs.identity_error is None
        assert fs.active is True

    def test_fid_consistency_loadgame_without_fid_preserves(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-A", Name="CmdrA"),
            _evt("LoadGame", Commander="CmdrA"),
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert router.commanders["F-A"].last_carrier_request == "Sol"
        fs = router.files[str(j)]
        assert fs.identity_error is None

    def test_fid_consistency_subsequent_scans_still_ignored(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-A", Name="CmdrA"),
            _evt("LoadGame", FID="F-B", Commander="CmdrB"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        _write_lines(j, [
            _evt("CarrierJumpRequest", SystemName="Deciat",
                  DepartureTime="2026-04-25T12:20:00Z"),
        ])
        router.scan_once(tmp_path)

        assert router.commanders["F-A"].last_carrier_request is None
        assert "F-B" not in router.commanders


# ---------------------------------------------------------------------------
# Predecessor-first rollover (no identity in successor)
# ---------------------------------------------------------------------------

class TestPredecessorFirstRollover:
    """Predecessor Part 1 has identity + Continued; successor Part 2 has carrier
    events only — no LoadGame or Commander in Part 2."""

    def test_part2_carrier_events_inherit_fid(self, tmp_path: Path) -> None:
        part1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        part2 = tmp_path / "Journal.2026-04-25T120000.02.log"

        _write_lines(part1, [
            _evt("Fileheader", gameversion="4.0", Part=1),
            _evt("Commander", FID="F-R", Name="RollCmdr"),
            _evt("Continued", Part=2),
        ])
        _write_lines(part2, [
            _evt("Fileheader", gameversion="4.0", Part=2),
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z"),
            _evt("CarrierJump"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        r = router.commanders["F-R"]
        assert r.last_carrier_request == "Sol"
        assert r.has_jumped is True
        assert str(part2) in r.active_files


# ---------------------------------------------------------------------------
# Successor-first / late-binding rollover
# ---------------------------------------------------------------------------

class TestSuccessorFirstLateBinding:
    """Successor Part 2 scanned first with carrier events but no identity;
    Part 1 identity arrives on a later scan."""

    def test_buffered_part2_replayed_after_part1_identity(
        self, tmp_path: Path,
    ) -> None:
        part1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        part2 = tmp_path / "Journal.2026-04-25T120000.02.log"

        _write_lines(part2, [
            _evt("Fileheader", gameversion="4.0", Part=2),
            _evt("CarrierJumpRequest", SystemName="Deciat",
                  DepartureTime="2026-04-25T12:20:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        assert "F-LATE" not in router.commanders

        _write_lines(part1, [
            _evt("Fileheader", gameversion="4.0", Part=1),
            _evt("Commander", FID="F-LATE", Name="LateCmdr"),
            _evt("Continued", Part=2),
        ])

        router.scan_once(tmp_path)

        late = router.commanders["F-LATE"]
        assert late.last_carrier_request == "Deciat"
        assert str(part2) in late.active_files


# ---------------------------------------------------------------------------
# Ambiguous rollover (fail-closed)
# ---------------------------------------------------------------------------

class TestAmbiguousRollover:
    """Two predecessors with different FIDs both expect Part=2; successor
    must not inherit from either and must buffer events."""

    def test_no_inherited_identity_no_mutation(self, tmp_path: Path) -> None:
        pred1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        pred2 = tmp_path / "Journal.2026-04-25T120001.01.log"
        succ = tmp_path / "Journal.2026-04-25T120002.01.log"

        _write_lines(pred1, [
            _evt("Fileheader", gameversion="4.0", Part=1),
            _evt("Commander", FID="F-AMB1", Name="Amb1"),
            _evt("Continued", Part=2),
        ])
        _write_lines(pred2, [
            _evt("Fileheader", gameversion="4.0", Part=1),
            _evt("Commander", FID="F-AMB2", Name="Amb2"),
            _evt("Continued", Part=2),
        ])
        _write_lines(succ, [
            _evt("Fileheader", gameversion="4.0", Part=2),
            _evt("CarrierJumpRequest", SystemName="Shinrarta",
                  DepartureTime="2026-04-25T12:25:00Z"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert router.commanders["F-AMB1"].last_carrier_request is None
        assert router.commanders["F-AMB2"].last_carrier_request is None

        succ_state = router.files[str(succ)]
        assert succ_state.fid is None
        assert succ_state.identity_error is None
        assert len(succ_state.pending_events) > 0


# ---------------------------------------------------------------------------
# Buffer overflow (100-event cap)
# ---------------------------------------------------------------------------

class TestBufferOverflow:
    """Pre-identity buffer caps at 100 carrier events; overflow drops oldest."""

    def test_101_events_cap_at_100(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"

        events: list[dict[str, object]] = [
            _evt("Fileheader", gameversion="4.0"),
        ]
        for i in range(101):
            events.append(_evt(
                "CarrierStats", FuelLevel=i,
                timestamp=f"2026-04-25T12:{i // 60:02d}:{i % 60:02d}Z",
            ))
        events.append(_evt("Commander", FID="F-OVF", Name="OverflowCmdr"))

        _write_lines(j, events)

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        fs = router.files[str(j)]
        assert len(fs.pending_events) == 0
        assert fs.pending_overflow is True

        ovf = router.commanders["F-OVF"]
        assert ovf.last_fuel == 100.0


# ---------------------------------------------------------------------------
# Truncation full reset
# ---------------------------------------------------------------------------

class TestTruncationFullReset:
    """Truncating and rewriting with a different FID must reset all mutable
    file state so no stale-mismatch error fires."""

    def test_fid_change_after_truncation_no_mismatch(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"

        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-TR-A", Name="TruncA"),
            _evt("CarrierStats", FuelLevel=800),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)
        assert router.commanders["F-TR-A"].last_fuel == 800.0
        assert str(j) in router.commanders["F-TR-A"].active_files

        _ = j.write_text("", encoding="utf-8")
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-TR-B", Name="TruncB"),
            _evt("CarrierStats", FuelLevel=600),
        ])

        router.scan_once(tmp_path)

        assert "F-TR-B" in router.commanders
        assert router.commanders["F-TR-B"].last_fuel == 600.0
        assert str(j) not in router.commanders["F-TR-A"].active_files

        fs = router.files[str(j)]
        assert fs.identity_error is None
        assert fs.fid == "F-TR-B"


# ---------------------------------------------------------------------------
# Stale timestamp protection
# ---------------------------------------------------------------------------

class TestStaleTimestampProtection:
    """Older carrier events must not overwrite newer carrier-field state."""

    def test_older_reread_does_not_overwrite_newer(
        self, tmp_path: Path,
    ) -> None:
        j1 = tmp_path / "Journal.2026-04-25T120000.01.log"
        j2 = tmp_path / "Journal.2026-04-25T120001.01.log"

        _write_lines(j1, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-TS", Name="TsCmdr"),
        ])
        _write_lines(j2, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-TS", Name="TsCmdr"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        _write_lines(j1, [
            _evt("CarrierJumpRequest", SystemName="Sol",
                  DepartureTime="2026-04-25T12:15:00Z",
                  timestamp="2026-04-25T12:15:00Z"),
        ])
        router.scan_once(tmp_path)
        assert router.commanders["F-TS"].last_carrier_request == "Sol"

        _write_lines(j2, [
            _evt("CarrierJumpRequest", SystemName="Deciat",
                  DepartureTime="2026-04-25T12:10:00Z",
                  timestamp="2026-04-25T12:10:00Z"),
        ])
        router.scan_once(tmp_path)

        assert router.commanders["F-TS"].last_carrier_request == "Sol"


# ---------------------------------------------------------------------------
# Equal timestamp tie
# ---------------------------------------------------------------------------

class TestEqualTimestampTie:
    """Same-timestamp events use later file-order event deterministically."""

    def test_same_timestamp_later_file_order_wins(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"

        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-TIE", Name="TieCmdr"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        _write_lines(j, [
            _evt("CarrierStats", FuelLevel=100,
                  timestamp="2026-04-25T12:30:00Z"),
            _evt("CarrierStats", FuelLevel=200,
                  timestamp="2026-04-25T12:30:00Z"),
        ])
        router.scan_once(tmp_path)

        assert router.commanders["F-TIE"].last_fuel == 200.0


# ---------------------------------------------------------------------------
# active_files cleanup on deactivation
# ---------------------------------------------------------------------------

class TestActiveFilesCleanup:
    """active_files must be cleaned when a file is deactivated or resets."""

    def test_shutdown_removes_from_active_files(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-CLN", Name="CleanCmdr"),
            _evt("CarrierStats", FuelLevel=500),
            _evt("Shutdown"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        cln = router.commanders["F-CLN"]
        assert cln.last_fuel == 500.0
        assert str(j) not in cln.active_files

    def test_identity_mismatch_removes_from_active_files(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-ID1", Name="Id1"),
            _evt("CarrierStats", FuelLevel=300),
            _evt("LoadGame", FID="F-ID2", Commander="Id2"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert "F-ID1" in router.commanders
        assert str(j) not in router.commanders["F-ID1"].active_files


# ---------------------------------------------------------------------------
# Squadron carrier detection
# ---------------------------------------------------------------------------

class TestSquadronCarrierDetection:
    """CarrierType field from CarrierStats sets is_squadron_carrier flag."""

    def test_carrier_stats_squadron_sets_flag(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-SQ", Name="SqCmdr"),
            _evt("CarrierStats", FuelLevel=800,
                  CarrierType="SquadronCarrier"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert router.commanders["F-SQ"].is_squadron_carrier is True
        # Fuel parsing unaffected
        assert router.commanders["F-SQ"].last_fuel == 800.0

    def test_carrier_stats_personal_carrier_keeps_false(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-PC", Name="PcCmdr"),
            _evt("CarrierStats", FuelLevel=600,
                  CarrierType="PlayerCarrier"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert router.commanders["F-PC"].is_squadron_carrier is False

    def test_carrier_stats_missing_type_defaults_false(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-DF", Name="DefaultCmdr"),
            _evt("CarrierStats", FuelLevel=700),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        assert router.commanders["F-DF"].is_squadron_carrier is False

    def test_facade_is_squadron_carrier_returns_true(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-SQ", Name="SqCmdr"),
            _evt("CarrierStats", FuelLevel=800,
                  CarrierType="SquadronCarrier"),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        facade = CTSJournalFacade(router, "F-SQ")
        assert facade.is_squadron_carrier() is True

    def test_facade_is_squadron_carrier_unknown_fid_returns_false(
        self, tmp_path: Path,
    ) -> None:
        router = MultiJournalRouter()
        facade = CTSJournalFacade(router, "F-NOPE")
        assert facade.is_squadron_carrier() is False

    def test_regression_existing_carrier_stats_no_carrier_type(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-REG", Name="RegCmdr"),
            _evt("CarrierStats", FuelLevel=750),
        ])

        router = MultiJournalRouter()
        router.scan_once(tmp_path)

        cmdr = router.commanders["F-REG"]
        assert cmdr.is_squadron_carrier is False
        assert cmdr.last_fuel == 750.0


# ---------------------------------------------------------------------------
# Binding controller squadron propagation
# ---------------------------------------------------------------------------

class TestBindingSquadronPropagation:
    """discover_commanders() propagates is_squadron_carrier to DiscoveredCommander."""

    def test_discover_commanders_propagates_squadron_flag(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-SQ", Name="SqCmdr"),
            _evt("CarrierStats", FuelLevel=800,
                  CarrierType="SquadronCarrier"),
        ])

        from TraversalSystem.gui.binding_controller import BindingController

        bc = BindingController(
            router=MultiJournalRouter(),
            discover_windows=lambda: [],
        )
        discovered = bc.discover_commanders(tmp_path)

        sq = next(d for d in discovered if d.fid == "F-SQ")
        assert sq.is_squadron_carrier is True

    def test_discover_commanders_personal_carrier_flag_false(
        self, tmp_path: Path,
    ) -> None:
        j = tmp_path / "Journal.2026-04-25T120000.01.log"
        _write_lines(j, [
            _evt("Fileheader", gameversion="4.0"),
            _evt("Commander", FID="F-PC", Name="PcCmdr"),
            _evt("CarrierStats", FuelLevel=600),
        ])

        from TraversalSystem.gui.binding_controller import BindingController

        bc = BindingController(
            router=MultiJournalRouter(),
            discover_windows=lambda: [],
        )
        discovered = bc.discover_commanders(tmp_path)

        pc = next(d for d in discovered if d.fid == "F-PC")
        assert pc.is_squadron_carrier is False
