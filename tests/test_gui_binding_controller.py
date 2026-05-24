"""Tests for TraversalSystem.gui.binding_controller (Task 6).

Covers:
  - FID discovery from journal Commander/LoadGame events
  - Slot classification: ready, unbound, stale, unavailable, ambiguous,
    needs_manual_binding
  - Unique high-confidence match → ready
  - Unknown FID → unbound (fail-closed)
  - Stale identity stays unbound unless current matching window appears
  - Ambiguous windows → needs_manual_binding (never auto-bind)
  - Missing windows → needs_manual_binding
  - Manual binding writes to runtime state only (in-memory, no disk)
  - Manual binding with undiscovered FID stays needs_manual_binding
  - Invalidation clears runtime binding
  - classification_to_worker_state mapping
  - classify_all batch operation
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from TraversalSystem.gui.binding_controller import (  # pyright: ignore[reportMissingImports]
    BindingController,
    BindingSnapshot,
    RuntimeBinding,
    SlotClassification,
    classification_to_worker_state,
)
from TraversalSystem.gui.worker_state import WorkerState  # pyright: ignore[reportMissingImports]
from TraversalSystem.gui_config import (  # pyright: ignore[reportMissingImports]
    CarrierSlotConfig,
    DiscoveredCommander,
    GuiConfig,
    UniversalSettings,
)
from TraversalSystem.multi_journal_router import MultiJournalRouter  # pyright: ignore[reportMissingImports]
from TraversalSystem.window_manager import (  # pyright: ignore[reportMissingImports]
    WindowInfo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _window(
    handle: int = 100,
    pid: int = 1000,
    title: str = "Elite - Dangerous (CLIENT)",
    backend: str = "x11",
    focusable: bool = True,
) -> WindowInfo:
    return WindowInfo(
        handle=handle,
        pid=pid,
        title=title,
        window_class="EliteDangerous",
        backend=backend,
        focusable=focusable,
    )


def _slot(
    slot_index: int = 0,
    fid: str = "",
    commander_name: str = "",
    state: str = "unbound",
) -> CarrierSlotConfig:
    return CarrierSlotConfig(
        slot_index=slot_index,
        fid=fid,
        commander_name=commander_name,
        state=state,
    )


def _config(
    slots: list[CarrierSlotConfig] | None = None,
    journal_dir: str = "/tmp/nonexistent",
) -> GuiConfig:
    return GuiConfig(
        universal=UniversalSettings(journal_directory=journal_dir),
        carrier_slots=slots or [],
    )


def _disc(name: str = "Cmdr1", fid: str = "F-001") -> DiscoveredCommander:
    return DiscoveredCommander(name=name, fid=fid)


def _write_journal(
    path: Path,
    fid: str,
    name: str,
) -> Path:
    """Create a minimal journal file with Commander + LoadGame events."""
    events = [
        {"event": "Fileheader", "gameversion": "4.0", "timestamp": "2026-05-23T12:00:00Z"},
        {"event": "Commander", "FID": fid, "Name": name, "timestamp": "2026-05-23T12:00:01Z"},
        {"event": "LoadGame", "FID": fid, "Commander": name, "timestamp": "2026-05-23T12:00:02Z"},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(evt) + "\n")
    return path


# ---------------------------------------------------------------------------
# classification_to_worker_state mapping
# ---------------------------------------------------------------------------

class TestClassificationMapping:
    """Verify every SlotClassification maps to the correct WorkerState."""

    @pytest.mark.parametrize(
        "cls_val, worker_val",
        [
            (SlotClassification.READY, WorkerState.READY),
            (SlotClassification.UNBOUND, WorkerState.UNBOUND),
            (SlotClassification.NEEDS_MANUAL_BINDING, WorkerState.NEEDS_MANUAL_BINDING),
            (SlotClassification.STALE, WorkerState.UNBOUND),
            (SlotClassification.UNAVAILABLE, WorkerState.UNBOUND),
            (SlotClassification.AMBIGUOUS, WorkerState.NEEDS_MANUAL_BINDING),
        ],
    )
    def test_mapping(
        self,
        cls_val: SlotClassification,
        worker_val: WorkerState,
    ) -> None:
        assert classification_to_worker_state(cls_val) is worker_val

    def test_all_classifications_have_mapping(self) -> None:
        """Every SlotClassification member maps without KeyError."""
        for cls_val in SlotClassification:
            result = classification_to_worker_state(cls_val)
            assert isinstance(result, WorkerState)


# ---------------------------------------------------------------------------
# FID discovery from journals
# ---------------------------------------------------------------------------

class TestFIDDiscovery:
    """Verify FID discovery reads Commander/LoadGame from journals."""

    def test_discovers_single_commander(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-05-23T120000.01.log"
        _write_journal(j, "F-ALPHA", "AlphaCmdr")

        router = MultiJournalRouter()
        ctrl = BindingController(router, lambda: [])

        discovered = ctrl.discover_commanders(tmp_path)
        assert len(discovered) == 1
        assert discovered[0].fid == "F-ALPHA"
        assert discovered[0].name == "AlphaCmdr"
        assert discovered[0].discovered_at != ""
        assert discovered[0].discovery_status == "confirmed"

    def test_discovers_multiple_commanders(self, tmp_path: Path) -> None:
        j1 = tmp_path / "Journal.2026-05-23T120000.01.log"
        j2 = tmp_path / "Journal.2026-05-23T120001.01.log"
        _write_journal(j1, "F-A", "CmdrA")
        _write_journal(j2, "F-B", "CmdrB")

        router = MultiJournalRouter()
        ctrl = BindingController(router, lambda: [])

        discovered = ctrl.discover_commanders(tmp_path)
        fids = {d.fid for d in discovered}
        assert fids == {"F-A", "F-B"}

    def test_no_journals_returns_empty(self, tmp_path: Path) -> None:
        router = MultiJournalRouter()
        ctrl = BindingController(router, lambda: [])
        discovered = ctrl.discover_commanders(tmp_path)
        assert discovered == []

    def test_identity_from_events_not_filename(self, tmp_path: Path) -> None:
        j = tmp_path / "Journal.2026-05-23T120000.01.log"
        _write_journal(j, "F-REAL", "RealCmdr")

        router = MultiJournalRouter()
        ctrl = BindingController(router, lambda: [])
        discovered = ctrl.discover_commanders(tmp_path)

        assert len(discovered) == 1
        assert discovered[0].fid == "F-REAL"
        assert discovered[0].name == "RealCmdr"


# ---------------------------------------------------------------------------
# Slot classification: unique match → ready
# ---------------------------------------------------------------------------

class TestUniqueMatchReady:
    """Unique high-confidence window match sets slot to READY."""

    def test_single_window_ready(self) -> None:
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])

        slot = _slot(fid="F-001", commander_name="Cmdr1")
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=[win],
        )

        assert snap.classification is SlotClassification.READY
        assert snap.window_binding is not None
        assert snap.window_binding.handle == 100
        assert snap.fid == "F-001"
        assert snap.discovered_commander is not None
        assert snap.discovered_commander.fid == "F-001"

    def test_unique_from_two_windows(self) -> None:
        """Two windows but one is clearly higher-ranked → unique match."""
        low_win = _window(handle=200, title="Elite Dangerous", focusable=False)
        high_win = _window(handle=300, title="Elite - Dangerous (CLIENT)", focusable=True)

        ctrl = BindingController(MultiJournalRouter(), lambda: [low_win, high_win])

        slot = _slot(fid="F-001")
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=[low_win, high_win],
        )

        # WindowBindingCoordinator ranks by specificity; the high_win wins
        assert snap.classification is SlotClassification.READY
        assert snap.window_binding is not None
        assert snap.window_binding.handle == 300

    def test_ready_records_runtime_binding(self) -> None:
        """Auto-bind records a RuntimeBinding in the controller."""
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])

        slot = _slot(fid="F-001")
        _ = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=[win],
        )

        assert "F-001" in ctrl.runtime_bindings
        assert ctrl.runtime_bindings["F-001"].window.handle == 100


# ---------------------------------------------------------------------------
# Slot classification: unbound
# ---------------------------------------------------------------------------

class TestUnboundClassification:
    """Unknown FID and empty FID stay UNBOUND."""

    def test_no_fid_unbound(self) -> None:
        ctrl = BindingController(MultiJournalRouter(), lambda: [])
        slot = _slot(fid="")
        snap = ctrl.classify_slot(slot, discovered=[], live_windows=[])

        assert snap.classification is SlotClassification.UNBOUND
        assert snap.fid == ""

    def test_unknown_fid_unbound(self) -> None:
        ctrl = BindingController(MultiJournalRouter(), lambda: [_window()])
        slot = _slot(fid="F-UNKNOWN")
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-OTHER")],  # different FID
            live_windows=[_window()],
        )

        assert snap.classification is SlotClassification.UNBOUND
        assert snap.discovered_commander is None


# ---------------------------------------------------------------------------
# Slot classification: unavailable
# ---------------------------------------------------------------------------

class TestUnavailableClassification:
    """FID discovered but no live windows → UNAVAILABLE."""

    def test_no_windows_unavailable(self) -> None:
        ctrl = BindingController(MultiJournalRouter(), lambda: [])
        slot = _slot(fid="F-001")
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=[],
        )

        assert snap.classification is SlotClassification.UNAVAILABLE
        assert snap.discovered_commander is not None


# ---------------------------------------------------------------------------
# Slot classification: ambiguous
# ---------------------------------------------------------------------------

class TestAmbiguousClassification:
    """Multiple indistinguishable windows → AMBIGUOUS, never auto-bind."""

    def test_equal_windows_ambiguous(self) -> None:
        w1 = _window(handle=400)
        w2 = _window(handle=500)
        windows = [w1, w2]

        ctrl = BindingController(MultiJournalRouter(), lambda: windows)
        slot = _slot(fid="F-001")
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=windows,
        )

        assert snap.classification is SlotClassification.AMBIGUOUS
        assert snap.window_binding is None
        assert len(snap.candidate_windows) == 2

    def test_ambiguous_never_auto_binds(self) -> None:
        """No runtime binding created for ambiguous case."""
        w1 = _window(handle=400)
        w2 = _window(handle=500)
        windows = [w1, w2]

        ctrl = BindingController(MultiJournalRouter(), lambda: windows)
        slot = _slot(fid="F-001")
        _ = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=windows,
        )

        assert "F-001" not in ctrl.runtime_bindings


# ---------------------------------------------------------------------------
# Slot classification: stale
# ---------------------------------------------------------------------------

class TestStaleClassification:
    """Previously bound window gone → STALE; stays UNBOUND in worker state."""

    def _setup_with_runtime_binding(self) -> tuple[BindingController, list[WindowInfo]]:
        """Create a controller with a pre-existing runtime binding."""
        win = _window(handle=100)
        windows = [win]

        ctrl = BindingController(MultiJournalRouter(), lambda: [])
        ctrl._runtime_bindings["F-001"] = RuntimeBinding(
            fid="F-001",
            commander_name="Cmdr1",
            window=win,
            startup_identity="auto:F-001:100",
        )
        return ctrl, windows

    def test_stale_when_window_gone(self) -> None:
        ctrl, _ = self._setup_with_runtime_binding()
        slot = _slot(fid="F-001")

        # Window list is now empty (window disappeared)
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=[],
        )

        assert snap.classification is SlotClassification.STALE
        assert snap.window_binding is None

    def test_stale_maps_to_unbound_worker_state(self) -> None:
        ctrl, _ = self._setup_with_runtime_binding()
        slot = _slot(fid="F-001")

        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=[_window(handle=999)],  # different window
        )

        # Runtime binding's window (handle=100) is not in live list
        assert snap.classification is SlotClassification.STALE
        assert classification_to_worker_state(snap.classification) is WorkerState.UNBOUND

    def test_stale_recovers_when_window_returns(self) -> None:
        ctrl, _ = self._setup_with_runtime_binding()
        slot = _slot(fid="F-001")
        original_win = _window(handle=100)

        # Window is back
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=[original_win],
        )

        assert snap.classification is SlotClassification.READY
        assert snap.window_binding is not None
        assert snap.window_binding.handle == 100


# ---------------------------------------------------------------------------
# Slot classification: needs_manual_binding
# ---------------------------------------------------------------------------

class TestNeedsManualBinding:
    """FID known but window can't be uniquely resolved → NEEDS_MANUAL_BINDING."""

    def test_single_window_unresolvable(self) -> None:
        """Edge case: single window exists but coordinator returns None.

        This shouldn't happen in practice (single window = unique), but
        the controller must fail-closed.
        """
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])

        # Override coordinator to return None
        ctrl._coordinator.resolve_binding = lambda **kw: None  # type: ignore[assignment]

        slot = _slot(fid="F-001")
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=[win],
        )

        assert snap.classification is SlotClassification.NEEDS_MANUAL_BINDING


# ---------------------------------------------------------------------------
# Manual binding
# ---------------------------------------------------------------------------

class TestManualBinding:
    """Manual binding writes selected window to runtime state only."""

    def test_manual_bind_ready_when_fid_discovered(self) -> None:
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])

        cfg = _config(
            slots=[_slot(0, fid="F-001", commander_name="Cmdr1")],
        )
        cfg.discovered_commanders = [_disc("Cmdr1", "F-001")]

        snap = ctrl.manual_bind(0, cfg, win)

        assert snap.classification is SlotClassification.READY
        assert snap.window_binding is not None
        assert snap.window_binding.handle == 100
        assert "F-001" in ctrl.runtime_bindings

    def test_manual_bind_in_memory_only(self, tmp_path: Path) -> None:
        """Manual binding does NOT modify the config object's state."""
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])

        cfg = _config(
            slots=[_slot(0, fid="F-001", commander_name="Cmdr1")],
        )
        cfg.discovered_commanders = [_disc("Cmdr1", "F-001")]

        _ = ctrl.manual_bind(0, cfg, win)

        # Config slot state is unchanged (still unbound)
        assert cfg.carrier_slots[0].state == "unbound"

    def test_manual_bind_undiscovered_fid_needs_manual(self) -> None:
        """Manual binding with undiscovered FID stays NEEDS_MANUAL_BINDING."""
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])

        cfg = _config(
            slots=[_slot(0, fid="F-MANUAL", commander_name="UnknownCmdr")],
        )
        # No discovered commanders

        snap = ctrl.manual_bind(0, cfg, win)

        assert snap.classification is SlotClassification.NEEDS_MANUAL_BINDING
        assert snap.window_binding is not None  # binding exists
        assert "F-MANUAL" in ctrl.runtime_bindings

    def test_manual_bind_window_disappears(self) -> None:
        """Selected window gone before bind completes → needs_manual."""
        selected_win = _window(handle=100)
        different_win = _window(handle=200)
        ctrl = BindingController(
            MultiJournalRouter(),
            lambda: [different_win],  # selected window not in live list
        )

        cfg = _config(slots=[_slot(0, fid="F-001")])

        snap = ctrl.manual_bind(0, cfg, selected_win)

        assert snap.classification is SlotClassification.NEEDS_MANUAL_BINDING
        assert snap.window_binding is None

    def test_manual_bind_rejects_no_fid(self) -> None:
        ctrl = BindingController(MultiJournalRouter(), lambda: [])
        cfg = _config(slots=[_slot(0, fid="")])

        with pytest.raises(ValueError, match="no FID"):
            ctrl.manual_bind(0, cfg, _window())

    def test_manual_bind_rejects_invalid_index(self) -> None:
        ctrl = BindingController(MultiJournalRouter(), lambda: [])
        cfg = _config(slots=[_slot(0, fid="F-001")])

        with pytest.raises(ValueError, match="out of range"):
            ctrl.manual_bind(99, cfg, _window())


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------

class TestInvalidation:
    """invalidate_binding clears runtime and coordinator bindings."""

    def test_invalidate_removes_runtime_binding(self) -> None:
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])
        ctrl._runtime_bindings["F-001"] = RuntimeBinding(
            fid="F-001",
            commander_name="Cmdr1",
            window=win,
            startup_identity="auto:F-001:100",
        )

        ctrl.invalidate_binding("F-001")

        assert "F-001" not in ctrl.runtime_bindings

    def test_invalidate_nonexistent_noop(self) -> None:
        ctrl = BindingController(MultiJournalRouter(), lambda: [])
        ctrl.invalidate_binding("F-NONEXISTENT")  # should not raise


# ---------------------------------------------------------------------------
# classify_all batch operation
# ---------------------------------------------------------------------------

class TestClassifyAll:
    """Batch classification of all slots in a config."""

    def test_classify_all_with_mixed_slots(self) -> None:
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])

        cfg = _config(
            slots=[
                _slot(0, fid="F-001", commander_name="Cmdr1"),
                _slot(1, fid="F-UNKNOWN"),
                _slot(2, fid=""),
            ],
        )

        discovered = [_disc("Cmdr1", "F-001")]
        snapshots = ctrl.classify_all(
            cfg,
            discovered=discovered,
            live_windows=[win],
        )

        assert len(snapshots) == 3
        assert snapshots[0].classification is SlotClassification.READY
        assert snapshots[1].classification is SlotClassification.UNBOUND
        assert snapshots[2].classification is SlotClassification.UNBOUND

    def test_classify_all_auto_discovers(self, tmp_path: Path) -> None:
        """classify_all auto-discovers if discovered not provided."""
        j = tmp_path / "Journal.2026-05-23T120000.01.log"
        _write_journal(j, "F-AUTO", "AutoCmdr")

        win = _window(handle=100)
        router = MultiJournalRouter()
        ctrl = BindingController(router, lambda: [win])

        cfg = _config(
            slots=[_slot(0, fid="F-AUTO")],
            journal_dir=str(tmp_path),
        )

        snapshots = ctrl.classify_all(cfg)

        assert len(snapshots) == 1
        assert snapshots[0].classification is SlotClassification.READY
        assert len(cfg.discovered_commanders) == 1
        assert cfg.discovered_commanders[0].fid == "F-AUTO"
        assert cfg.discovered_commanders[0].name == "AutoCmdr"

    def test_classify_all_empty_config(self) -> None:
        ctrl = BindingController(MultiJournalRouter(), lambda: [])
        cfg = _config()
        snapshots = ctrl.classify_all(cfg)

        assert snapshots == {}


# ---------------------------------------------------------------------------
# Stale recovery scenario
# ---------------------------------------------------------------------------

class TestStaleRecovery:
    """Stale identity recovers when a matching window/session appears."""

    def test_stale_to_ready_recovery(self) -> None:
        win1 = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win1])

        # First classify → READY, creates runtime binding
        slot = _slot(fid="F-001")
        disc = [_disc("Cmdr1", "F-001")]

        snap1 = ctrl.classify_slot(slot, discovered=disc, live_windows=[win1])
        assert snap1.classification is SlotClassification.READY

        # Window disappears → STALE
        snap2 = ctrl.classify_slot(slot, discovered=disc, live_windows=[])
        assert snap2.classification is SlotClassification.STALE

        # Window returns → READY
        snap3 = ctrl.classify_slot(slot, discovered=disc, live_windows=[win1])
        assert snap3.classification is SlotClassification.READY

    def test_stale_stays_unbound_in_worker_state(self) -> None:
        """STALE maps to UNBOUND worker state (fail-closed)."""
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [])
        ctrl._runtime_bindings["F-001"] = RuntimeBinding(
            fid="F-001",
            commander_name="Cmdr1",
            window=win,
            startup_identity="auto:F-001:100",
        )

        slot = _slot(fid="F-001")
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=[],  # window gone
        )

        assert snap.classification is SlotClassification.STALE
        assert classification_to_worker_state(snap.classification) is WorkerState.UNBOUND


# ---------------------------------------------------------------------------
# Fail-closed guarantees
# ---------------------------------------------------------------------------

class TestFailClosed:
    """Verify fail-closed guarantees."""

    def test_never_infers_identity_from_filename(self, tmp_path: Path) -> None:
        """Journal filename does not determine FID — events do."""
        j = tmp_path / "Journal.2026-05-23T120000.01.log"
        # File has no identity events
        with j.open("w") as fh:
            fh.write(json.dumps({"event": "Fileheader", "gameversion": "4.0", "timestamp": "2026-01-01T00:00:00Z"}) + "\n")
            fh.write(json.dumps({"event": "CarrierStats", "FuelLevel": 800, "timestamp": "2026-01-01T00:01:00Z"}) + "\n")

        router = MultiJournalRouter()
        ctrl = BindingController(router, lambda: [])
        discovered = ctrl.discover_commanders(tmp_path)

        assert discovered == []

    def test_ambiguous_never_auto_binds(self) -> None:
        """Multiple windows with same score → AMBIGUOUS, no binding created."""
        w1 = _window(handle=400, title="Elite - Dangerous (CLIENT)", focusable=True)
        w2 = _window(handle=500, title="Elite - Dangerous (CLIENT)", focusable=True)
        windows = [w1, w2]

        ctrl = BindingController(MultiJournalRouter(), lambda: windows)
        slot = _slot(fid="F-001")
        snap = ctrl.classify_slot(
            slot,
            discovered=[_disc("Cmdr1", "F-001")],
            live_windows=windows,
        )

        assert snap.classification is SlotClassification.AMBIGUOUS
        assert snap.window_binding is None
        assert "F-001" not in ctrl.runtime_bindings

    def test_manual_binding_no_persist(self, tmp_path: Path) -> None:
        """Manual binding only writes to in-memory state, not config."""
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])

        cfg = _config(
            slots=[_slot(0, fid="F-001", commander_name="Cmdr1", state="unbound")],
        )
        cfg.discovered_commanders = [_disc("Cmdr1", "F-001")]

        _ = ctrl.manual_bind(0, cfg, win)

        # Config slot state is unchanged
        assert cfg.carrier_slots[0].state == "unbound"

        # But runtime binding exists
        assert "F-001" in ctrl.runtime_bindings


# ---------------------------------------------------------------------------
# BindingSnapshot data integrity
# ---------------------------------------------------------------------------

class TestBindingSnapshotIntegrity:
    """Verify snapshots carry correct data."""

    def test_ready_snapshot_fields(self) -> None:
        win = _window(handle=100)
        ctrl = BindingController(MultiJournalRouter(), lambda: [win])

        slot = _slot(fid="F-001", commander_name="Cmdr1")
        disc = _disc("Cmdr1", "F-001")

        snap = ctrl.classify_slot(
            slot, discovered=[disc], live_windows=[win],
        )

        assert snap.fid == "F-001"
        assert snap.commander_name == "Cmdr1"
        assert snap.window_binding is not None
        assert snap.window_binding.target_fid == "F-001"
        assert snap.discovered_commander is disc
        assert snap.candidate_windows == [win]

    def test_unbound_snapshot_no_binding(self) -> None:
        ctrl = BindingController(MultiJournalRouter(), lambda: [])
        slot = _slot(fid="")

        snap = ctrl.classify_slot(slot, discovered=[], live_windows=[])

        assert snap.window_binding is None
        assert snap.discovered_commander is None
        assert snap.candidate_windows == []
