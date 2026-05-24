"""FID discovery and window/commander binding controller.

Discovers commander identities from journal ``Commander`` / ``LoadGame``
events via :class:`MultiJournalRouter`, matches them to live Elite Dangerous
windows via :class:`WindowBindingCoordinator`, and classifies each carrier
slot's binding readiness.

Fail-closed design
------------------
- **Never** infers identity from journal filenames.
- **Never** auto-binds ambiguous windows.
- Manual binding writes to in-memory runtime state **only**; nothing is
  persisted to disk.
- Unknown FIDs remain ``unbound`` until journal discovery confirms them.

Classification states map to :class:`WorkerState` values consumed by the
task-8 worker and task-10 dashboard:

============   =====================   ==========================================
Classification WorkerState            Meaning
============   =====================   ==========================================
READY          READY                   FID discovered, unique window match
UNBOUND        UNBOUND                 No FID, or FID unknown to journals
STALE          UNBOUND                 Previously bound, window gone
UNAVAILABLE    UNBOUND                 No live windows at all
NEEDS_MANUAL   NEEDS_MANUAL_BINDING    FID known, but no unique window match
AMBIGUOUS      NEEDS_MANUAL_BINDING    FID known, multiple indistinguishable wins
============   =====================   ==========================================
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from TraversalSystem.gui_config import (
    CarrierSlotConfig,
    DiscoveredCommander,
    GuiConfig,
)
from TraversalSystem.gui.worker_state import WorkerState
from TraversalSystem.multi_journal_router import MultiJournalRouter
from TraversalSystem.window_manager import (
    WindowBinding,
    WindowBindingCoordinator,
    WindowInfo,
)


# ---------------------------------------------------------------------------
# Classification enum
# ---------------------------------------------------------------------------

class SlotClassification(enum.Enum):
    """Binding readiness classification for a carrier slot."""

    READY = "ready"
    UNBOUND = "unbound"
    NEEDS_MANUAL_BINDING = "needs_manual_binding"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"


# ---------------------------------------------------------------------------
# Mapping to WorkerState (task-7 contract)
# ---------------------------------------------------------------------------

_CLASSIFICATION_TO_WORKER: dict[SlotClassification, WorkerState] = {
    SlotClassification.READY: WorkerState.READY,
    SlotClassification.UNBOUND: WorkerState.UNBOUND,
    SlotClassification.NEEDS_MANUAL_BINDING: WorkerState.NEEDS_MANUAL_BINDING,
    SlotClassification.STALE: WorkerState.UNBOUND,
    SlotClassification.UNAVAILABLE: WorkerState.UNBOUND,
    SlotClassification.AMBIGUOUS: WorkerState.NEEDS_MANUAL_BINDING,
}


def classification_to_worker_state(cls: SlotClassification) -> WorkerState:
    """Map a binding classification to the corresponding :class:`WorkerState`."""
    return _CLASSIFICATION_TO_WORKER[cls]


# ---------------------------------------------------------------------------
# Runtime binding record (in-memory only)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RuntimeBinding:
    """In-memory record of a discovered or manually bound window.

    This is **never** persisted to disk — it exists only in the running
    controller's memory so that re-classification can detect stale bindings.
    """

    fid: str
    commander_name: str
    window: WindowInfo
    startup_identity: str


# ---------------------------------------------------------------------------
# Binding snapshot (per-slot classification result)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BindingSnapshot:
    """Point-in-time binding state for a single carrier slot.

    Provides the classification, the associated FID and commander data, the
    resolved window binding (if any), the discovered commander record, and
    the list of candidate windows for UI consumption.
    """

    classification: SlotClassification
    fid: str
    commander_name: str
    window_binding: WindowBinding | None
    discovered_commander: DiscoveredCommander | None
    candidate_windows: list[WindowInfo]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_startup_identity(prefix: str, fid: str, handle: int) -> str:
    """Produce an opaque identity token for the binding coordinator."""
    return f"{prefix}:{fid}:{handle}"


def _find_window_in_list(
    window: WindowInfo,
    live: list[WindowInfo],
) -> WindowInfo | None:
    """Return the matching live window or ``None``."""
    for w in live:
        if w.handle == window.handle and w.backend == window.backend:
            return w
    return None


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class BindingController:
    """Discovers FIDs from journals and classifies slot binding readiness.

    Typical usage::

        from TraversalSystem.multi_journal_router import MultiJournalRouter
        from TraversalSystem.window_manager import enumerate_elite_windows

        router = MultiJournalRouter()
        controller = BindingController(
            router=router,
            discover_windows=enumerate_elite_windows,
        )

        # Discover commanders from journal events
        discovered = controller.discover_commanders(journal_dir)

        # Classify all slots
        snapshots = controller.classify_all(config)
    """

    def __init__(
        self,
        router: MultiJournalRouter,
        discover_windows: Callable[[], list[WindowInfo]],
    ) -> None:
        self._router = router
        self._coordinator = WindowBindingCoordinator(discover_windows)
        self._discover_windows = discover_windows
        self._runtime_bindings: dict[str, RuntimeBinding] = {}

    # -- FID discovery -------------------------------------------------------

    def discover_commanders(self, journal_dir: Path) -> list[DiscoveredCommander]:
        """Scan *journal_dir* for commander identities.

        Uses :class:`MultiJournalRouter` to read ``Commander`` / ``LoadGame``
        events.  Identity comes **exclusively** from event data — never from
        filenames.
        """
        self._router.scan_once(journal_dir)

        discovered: list[DiscoveredCommander] = []
        for fid, cmdr_state in self._router.commanders.items():
            discovered.append(DiscoveredCommander(
                name=cmdr_state.commander_name or "",
                fid=fid,
                discovered_at=cmdr_state.last_event_ts or "",
                discovery_status="confirmed" if cmdr_state.commander_name else "tentative",
            ))
        return discovered

    # -- Slot classification --------------------------------------------------

    def classify_slot(
        self,
        slot: CarrierSlotConfig,
        *,
        discovered: Sequence[DiscoveredCommander],
        live_windows: list[WindowInfo],
    ) -> BindingSnapshot:
        """Classify a single slot's binding readiness.

        Classification rules (fail-closed):

        1. No FID on slot → ``UNBOUND``
        2. FID not among discovered commanders → ``UNBOUND``
        3. Runtime binding exists and window still live → ``READY``
           Runtime binding exists but window gone → ``STALE``
        4. No live windows at all (no prior binding) → ``UNAVAILABLE``
        5. Unique window candidate via coordinator → ``READY``
        6. Multiple ambiguous windows → ``AMBIGUOUS``
        7. FID known but no window can be bound → ``NEEDS_MANUAL_BINDING``
        """
        # --- 1. No FID ------------------------------------------------------
        if not slot.fid:
            return BindingSnapshot(
                classification=SlotClassification.UNBOUND,
                fid="",
                commander_name="",
                window_binding=None,
                discovered_commander=None,
                candidate_windows=[],
            )

        # --- 2. Find matching discovered commander ---------------------------
        disc_cmdr: DiscoveredCommander | None = None
        for d in discovered:
            if d.fid == slot.fid:
                disc_cmdr = d
                break

        if disc_cmdr is None:
            return BindingSnapshot(
                classification=SlotClassification.UNBOUND,
                fid=slot.fid,
                commander_name=slot.commander_name,
                window_binding=None,
                discovered_commander=None,
                candidate_windows=[],
            )

        # --- 3. Check runtime binding BEFORE the no-windows gate -------------
        # A stale binding (window gone) is distinct from "no windows at all
        # and never had a binding".  The former is STALE; the latter is
        # UNAVAILABLE.
        runtime = self._runtime_bindings.get(slot.fid)
        if runtime is not None:
            live_match = _find_window_in_list(runtime.window, live_windows)
            if live_match is not None:
                # Still live → READY
                binding = WindowBinding.from_window(
                    target_fid=slot.fid,
                    startup_identity=runtime.startup_identity,
                    window=live_match,
                )
                return BindingSnapshot(
                    classification=SlotClassification.READY,
                    fid=slot.fid,
                    commander_name=disc_cmdr.name,
                    window_binding=binding,
                    discovered_commander=disc_cmdr,
                    candidate_windows=live_windows,
                )
            # Window gone → STALE (even if live_windows is empty)
            return BindingSnapshot(
                classification=SlotClassification.STALE,
                fid=slot.fid,
                commander_name=disc_cmdr.name,
                window_binding=None,
                discovered_commander=disc_cmdr,
                candidate_windows=live_windows,
            )

        # --- 4. No live windows → UNAVAILABLE --------------------------------
        if not live_windows:
            return BindingSnapshot(
                classification=SlotClassification.UNAVAILABLE,
                fid=slot.fid,
                commander_name=disc_cmdr.name,
                window_binding=None,
                discovered_commander=disc_cmdr,
                candidate_windows=[],
            )

        # --- 5. Try auto-bind via coordinator (unique window selection) ------
        startup_id = _make_startup_identity("auto", slot.fid, 0)
        binding = self._coordinator.resolve_binding(
            target_fid=slot.fid,
            startup_identity=startup_id,
            ambiguous_window_policy="abort",
        )

        if binding is not None:
            # Unique match → record in runtime bindings and classify READY
            self._runtime_bindings[slot.fid] = RuntimeBinding(
                fid=slot.fid,
                commander_name=disc_cmdr.name,
                window=WindowInfo(
                    handle=binding.handle,
                    pid=binding.pid,
                    title=binding.title,
                    window_class=binding.window_class,
                    backend=binding.backend,
                    focusable=True,
                ),
                startup_identity=startup_id,
            )
            return BindingSnapshot(
                classification=SlotClassification.READY,
                fid=slot.fid,
                commander_name=disc_cmdr.name,
                window_binding=binding,
                discovered_commander=disc_cmdr,
                candidate_windows=live_windows,
            )

        # --- 6. Multiple ambiguous windows -----------------------------------
        if len(live_windows) > 1:
            return BindingSnapshot(
                classification=SlotClassification.AMBIGUOUS,
                fid=slot.fid,
                commander_name=disc_cmdr.name,
                window_binding=None,
                discovered_commander=disc_cmdr,
                candidate_windows=live_windows,
            )

        # --- 7. Single window but coordinator couldn't resolve ----------------
        # Shouldn't normally happen (single window should be unique), but
        # fail-closed → needs manual binding.
        return BindingSnapshot(
            classification=SlotClassification.NEEDS_MANUAL_BINDING,
            fid=slot.fid,
            commander_name=disc_cmdr.name,
            window_binding=None,
            discovered_commander=disc_cmdr,
            candidate_windows=live_windows,
        )

    # -- Batch classification -------------------------------------------------

    def classify_all(
        self,
        config: GuiConfig,
        *,
        discovered: Sequence[DiscoveredCommander] | None = None,
        live_windows: list[WindowInfo] | None = None,
    ) -> dict[int, BindingSnapshot]:
        """Classify all carrier slots in *config*.

        If *discovered* is not provided, commanders are discovered from
        ``config.universal.journal_directory``.  If *live_windows* is not
        provided, windows are enumerated via the injected discovery callable.

        Returns a dict mapping ``slot_index`` → :class:`BindingSnapshot`.
        """
        if discovered is None:
            journal_dir = Path(config.universal.journal_directory)
            discovered_list = self.discover_commanders(journal_dir)
            config.discovered_commanders = discovered_list
        else:
            discovered_list = list(discovered)

        if live_windows is None:
            live_windows = self._discover_windows()

        # Re-evaluate stale status for all discovered commanders
        # A commander is stale if it has no runtime binding with a live window,
        # AND it cannot be auto-resolved to a live window.
        for disc in discovered_list:
            has_live = False
            runtime = self._runtime_bindings.get(disc.fid)
            if runtime is not None:
                if _find_window_in_list(runtime.window, live_windows) is not None:
                    has_live = True
            
            if not has_live:
                startup_id = _make_startup_identity("auto", disc.fid, 0)
                binding = self._coordinator.resolve_binding(
                    target_fid=disc.fid,
                    startup_identity=startup_id,
                    ambiguous_window_policy="abort",
                )
                if binding is not None:
                    has_live = True
            
            if not has_live:
                disc.discovery_status = "stale"
            else:
                # Restore status if it is no longer stale
                disc.discovery_status = "confirmed" if disc.name else "tentative"

        result: dict[int, BindingSnapshot] = {}
        for slot in config.carrier_slots:
            result[slot.slot_index] = self.classify_slot(
                slot,
                discovered=discovered_list,
                live_windows=live_windows,
            )
        return result

    # -- Manual binding -------------------------------------------------------

    def manual_bind(
        self,
        slot_index: int,
        config: GuiConfig,
        window: WindowInfo,
    ) -> BindingSnapshot:
        """Manually bind a slot to a specific window.

        Writes the selected window identity into the **in-memory** runtime
        binding state only.  No blind input or focus actions are performed.
        The binding is not persisted to disk.

        Returns the updated :class:`BindingSnapshot` for the slot.

        Raises ``ValueError`` if the slot index is out of range or the slot
        has no FID.
        """
        if slot_index < 0 or slot_index >= len(config.carrier_slots):
            raise ValueError(f"slot_index {slot_index} out of range")

        slot = config.carrier_slots[slot_index]
        if not slot.fid:
            raise ValueError("Cannot manually bind a slot with no FID")

        # Verify the selected window is still live
        live_windows = self._discover_windows()
        live_match = _find_window_in_list(window, live_windows)

        if live_match is None:
            # Window disappeared between selection and bind
            disc_cmdr = self._find_discovered(slot.fid, config)
            return BindingSnapshot(
                classification=SlotClassification.NEEDS_MANUAL_BINDING,
                fid=slot.fid,
                commander_name=slot.commander_name,
                window_binding=None,
                discovered_commander=disc_cmdr,
                candidate_windows=live_windows,
            )

        startup_id = _make_startup_identity(
            "manual", slot.fid, live_match.handle,
        )

        # Write to runtime binding (in-memory only)
        self._runtime_bindings[slot.fid] = RuntimeBinding(
            fid=slot.fid,
            commander_name=slot.commander_name,
            window=live_match,
            startup_identity=startup_id,
        )

        binding = WindowBinding.from_window(
            target_fid=slot.fid,
            startup_identity=startup_id,
            window=live_match,
        )

        # Determine if FID is discovered
        disc_cmdr = self._find_discovered(slot.fid, config)

        # Manual binding is READY only if the FID is discovered from journals
        classification = (
            SlotClassification.READY
            if disc_cmdr is not None
            else SlotClassification.NEEDS_MANUAL_BINDING
        )

        return BindingSnapshot(
            classification=classification,
            fid=slot.fid,
            commander_name=slot.commander_name or (
                disc_cmdr.name if disc_cmdr else ""
            ),
            window_binding=binding,
            discovered_commander=disc_cmdr,
            candidate_windows=live_windows,
        )

    # -- Invalidation ---------------------------------------------------------

    def invalidate_binding(self, fid: str) -> None:
        """Remove runtime binding for the given FID.

        Also invalidates the underlying coordinator binding.
        """
        _ = self._runtime_bindings.pop(fid, None)
        self._coordinator.invalidate_binding(fid)

    # -- Accessors ------------------------------------------------------------

    @property
    def runtime_bindings(self) -> dict[str, RuntimeBinding]:
        """Read-only view of current runtime bindings (for diagnostics)."""
        return dict(self._runtime_bindings)

    # -- Internal helpers -----------------------------------------------------

    def _find_discovered(
        self,
        fid: str,
        config: GuiConfig,
    ) -> DiscoveredCommander | None:
        """Look up a FID in the config's discovered_commanders list."""
        for d in config.discovered_commanders:
            if d.fid == fid:
                return d
        # Also check the router directly
        cmdr_state = self._router.commanders.get(fid)
        if cmdr_state is not None:
            return DiscoveredCommander(
                name=cmdr_state.commander_name or "",
                fid=fid,
            )
        return None
