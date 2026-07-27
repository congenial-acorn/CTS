# Multicarrier Jump Action Handling — Audit (consolidated)

**Codebase:** CTS — Carrier Traversal System (Elite Dangerous Fleet Carrier Auto-Plotter)
**Last revised:** 2026-07-02
**Scope:** Multicarrier jump sequencing, tritium refuel gating/timing, carrier-to-process binding, scheduled-jump integration, cross-carrier coordination.
**Method:** Fresh, independent static analysis of the current working tree under `TraversalSystem/`, cross-referenced line-by-line against the operator's intended-behavior contract. Every code reference was read directly from current source.

> **Document lineage.** This single report consolidates and supersedes the two prior documents in this repo — the 2026-06-17 bug report (`MULTICARRIER_JUMP_BUG_REPORT.md`, bugs A–H) and the 2026-06-18 independent audit (`MULTICARRIER_JUMP_AUDIT_2026-06-18.md`, bugs 1–5). Both were merged here against the **current** source (as of 2026-07-02, HEAD includes `b0b1844`), and the merged findings below re-derive every status from the live code rather than trusting either prior document. Where a prior verdict no longer holds against the current source, it is corrected inline.

---

## Intended Behavior (Contract — operator's words)

1. **Binding** — The user adds commanders/carriers and uses the **thumbnail selection** to bind each carrier to a game process/window.
2. **Per-carrier config** — The user adds a route for each carrier and can toggle per-carrier settings such as auto-fuel.
3. **Refuel gating** — **Tritium refuel must be ignored when `auto_jump` is disabled** for that carrier.
4. **Jump ordering** — When multicarriers are started, the **first carrier goes through the jump sequence, then the second, and so on**.
5. **Refuel timing / non-overlap** — The refueling sequence takes place sometime after the jump, but **not when it would overlap with a planned jump sequence**.
6. **Refuel offset** — Refuel is **triggered 5 minutes after the jump time** to match the jump cooldown optimally.

> **Naming note.** The operator speaks of `auto_jump` and "auto fuel". The code field for the jump toggle is `CarrierSlotConfig.auto_plot_jumps` (default `True`) and the refuel toggle is `CarrierSlotConfig.disable_refuel` (default `False`, i.e. refuel is *on* by default; the UI checkbox is labelled "Disable Refuel"). Throughout this report `auto_jump` means `auto_plot_jumps` and "auto fuel on" means `disable_refuel == False`. The inversion is cosmetic but worth stating because contract rule #3 is sensitive to it.

---

## Executive Summary

The thumbnail binding, per-carrier toggle wiring, auto-jump refuel gate, and queue-level jump/restock serialization are **correctly wired** (rules #1, #2, #3, #4, #5 all hold at the block level). The entire prior round of fixes (A–H) are genuinely present in the current source, and the restock trigger timing concern raised on 2026-06-18 (old Bug 1) has since been **resolved by a structural refactor** (`b0b1844`, 2026-07-02) that moves the restock out of the cooldown loop entirely.

**Current open issues** (3, all Low severity):

| # | Bug | Severity | Status |
|---|-----|----------|--------|
| **3** | Restock queue-timeout cannot abort an already-dispatched restock; the worker moves on while the queue stays blocked, producing a timing/state split-brain | Low | **Open** |
| **4** | Per-slot `Start` does not arm the first-cycle ordering barrier (prior Bug D, only partially addressed) | Low (edge) | **Open, carried forward** |
| **5** | No retry bound on `jump_to_system` failure → unbounded retry loop | Low (pre-existing, not multicarrier-specific) | **Open, pre-existing** |

**Resolved / closed** (full detail in the history section below):

| Prior # | Title | Final status |
|---|---|---|
| A | Restock timeout killed the worker via cancel-event aliasing | **Fixed** |
| B | Scheduled-jump `slot_id` collided with worker `slot_id` | **Fixed** |
| C | Scheduled-jump deadline (`time.monotonic()`) didn't gate restocks | **Fixed** |
| D | Per-slot `Start` skipped the first-cycle ordering barrier | **Partial → carried forward as Bug 4** |
| E | `schedule()` rejected past times while `_build_target_datetime` rolled them forward | **Fixed** |
| F | Exact-integer milestones skipped under recalibration | **Fixed** (and extended — now necessary, not just cosmetic, see below) |
| G | Global input singletons → non-atomic focus-then-dispatch | **Mitigated (documented + dispatch_lock)** |
| H | Timeout test masked Bug A by injecting a fresh event | **Fixed** |
| 1 | Restock trigger timing contradicted the contract ("5 min remaining" vs "5 min after jump") | **Resolved by refactor** (`b0b1844`) — restock moved out of the cooldown loop |
| 2 | Manual-mode carrier registers phantom cooldown-based jump deadlines | **Intentional per operator** — won't fix |

---

## Bug 3 — Restock queue-timeout cannot abort an already-dispatched restock (LOW, open)

**Location:** `TraversalSystem/main.py:705-731`, `TraversalSystem/sequence_queue.py:295-314, 331-338`.

**Description.** `_run_coordinated_restock` bounds its wait on the shared queue to `RESTOCK_QUEUE_WAIT_TIMEOUT_SECONDS = 180 s` (`main.py:705-721`). On timeout it sets the restock's dedicated cancel event and returns so the slot can continue:

```python
# main.py:714-721
if remaining <= 0:
    print("Restock deferred: … Skipping restock this cycle; …")
    restock_cancel_event.set()   # cancels only this restock, not the slot
    return
```

The intent ("skip for this cycle, retry next cooldown") is correct **only when the restock has not yet started running**. The queue only honors `cancel_event` at two points: before dispatch (`sequence_queue.py:303-305`) and when pruning *pending* entries (`_prune_cancelled_pending_locked`, `:331-338`). Once a block is dispatched it runs to completion — the worker thread is inside `block.run()` (`:306`) and `cancel_event` is **not** re-checked during execution.

So if the 180 s timeout fires *while the restock is mid-execution* (a real possibility — `estimate_restock_duration` already budgets 30–90 s of inputs plus focus overhead, and queue contention can push dispatch late), three things happen simultaneously:

1. `_run_coordinated_restock` returns; the caller recomputes `time_to_jump` (`main.py:974-975`) and proceeds as if the restock were skipped.
2. The queue worker is still inside the restock `run()` lambda (`main.py:694-699`), executing real game inputs against this carrier's window.
3. The queue's serialization invariant (`_active` is set, `sequence_queue.py:326`) blocks **every other carrier's** jump/restock submission until this restock finishes.

The net effect is a split-brain: the originating worker believes the restock was skipped, while the queue is still busy executing it and blocking all peers. Note: after the `b0b1844` refactor the restock now fires right after the post-plot lockout (`main.py:962-975`), so a stale finish simply trims `time_to_jump` rather than re-registering a cooldown deadline — the timing skew is now bounded to the current jump-countdown window rather than cascading into the next-cycle deadline math. The split-brain still exists, but its blast radius shrank.

**Impact.** Low likelihood (requires sustained cross-carrier contention that pushes a restock's queue-wait past 180 s *and* then dispatches it within that window). When it hits, the time accounting diverges from reality and a carrier's next jump can be silently delayed by up to one restock duration beyond its expected time. No input corruption (the queue still serializes), and contract rule #5 (no overlap with a planned jump) is still enforced at dispatch selection — only the worker's local countdown drifts.

**Evidence.**
- `sequence_queue.py:306`: `result = block.run()` — no `cancel_event` check inside the running block.
- `sequence_queue.py:303-305`: cancel check is pre-dispatch only.
- `main.py:714-721`: timeout path returns immediately; nothing awaits or coordinates with a potentially-running restock.
- `main.py:974-975`: caller subtracts `restock_elapsed` from `time_to_jump` on the assumption the restock ran (or was cleanly skipped).

**Recommendation.** Either (a) on timeout, if the block has already been dispatched, wait for it to finish before returning (so the time accounting is truthful), or (b) thread `runtime_context.raise_if_cancelled` calls into the restock `run()` body so a post-timeout cancel can actually interrupt a running restock. Option (a) is simpler and sufficient for timing correctness.

---

## Bug 4 — Per-slot `Start` still does not arm the first-cycle ordering barrier (LOW, carried forward)

> This is the prior Bug D. It is only partially addressed in the current source, so it is carried forward. Independent reading against current source agrees with the prior verdict.

**Location:** `TraversalSystem/gui/worker_controller.py:239-311` (no barrier call), `:329-346, 385-403` (barrier is armed only by `start_all_ready`).

**Description.** `start_slot` (per-slot Start button) resets the shared first-cycle base (`worker_controller.py:258-260`) but never calls `_arm_first_cycle_barrier`. The barrier defaults to satisfied (`_first_cycle_satisfied = True`, `sequence_queue.py:103`), so when a lone jump block is the only pending item the queue dispatches it immediately regardless of deadline ordering. If the operator clicks Start on slot 1 then Start on slot 0, slot 1's first jump can dispatch before slot 0's block is even submitted.

**Why this is low.** The documented multicarrier entry point is **Start All**, which *does* arm the barrier (`worker_controller.py:341`). The bug only manifests when an operator manually starts slots in non-index order via the per-slot buttons, and only on the very first jump cycle (subsequent cycles are pinned by registered cooldown deadlines). A user clicking per-slot Start is explicitly choosing an order.

**Evidence.**
- `worker_controller.py:239-311`: no `_arm_first_cycle_barrier` call in the per-slot path; `:341` is the only call site.
- `sequence_queue.py:103, 232-245`: barrier open by default; latch logic only engages when armed with `expected_count >= 2`.

**Recommendation (unchanged).** Either lift the barrier-arming call into `start_slot` (counting currently-READY siblings) for strict ordering from any entry point, or document the per-slot path as "submission/click order" and gate `start_btn` while READY siblings remain unstarted so the order is deterministic by construction.

---

## Bug 5 — Unbounded retry on jump-plot failure (LOW, pre-existing, open)

**Location:** `TraversalSystem/main.py:926-949`.

**Description.** The inner `while time_to_jump == 0 or departing_time == 0:` loop (`:929`) retries `_run_coordinated_jump_plot` with no retry counter. `jump_to_system` returns `(0, 0)` on failure (`main.py:457, 462`) after playing `jump_fail.txt`; the loop then submits another full jump-plot block. If the failure is persistent (wrong system name, UI drift, broken journal), this loops forever, each iteration consuming a queue block and emitting inputs.

**Impact.** Not multicarrier-specific, but in a multicarrier fleet a stuck retry loop on one slot holds the shared queue on every iteration, starving all peers.

**Evidence.** `main.py:929` (`while time_to_jump == 0 or departing_time == 0:`) with no bound; `:457, :462` are the `(0, 0)` failure returns.

**Recommendation.** Add a bounded retry count (e.g. 3) that, on exhaustion, saves progress and transitions the slot to `ERROR` via `_handle_jump_cancelled`-style cleanup.

---

## What is wired up correctly

For completeness, the following contract surfaces were independently verified as **correct** in the current source:

- **Thumbnail binding (rule #1).** `ManualBindDialog` → `window_selected` → `BindingController.manual_bind` (`binding_controller.py`) → in-memory `_runtime_bindings[fid]` only (fail-closed, never persisted with an undiscovered FID). Auto-bind and the FID-not-discovered safety guard (`bind_slot_to_fid` keeping unknown FIDs in `"unbound"`) are correct. The slot editor forces saved slots back to `"unbound"` (`slot_editor.py:177`), so only journal discovery can promote to `"ready"`.
- **Per-carrier toggles (rule #2).** `CarrierSlotConfig` carries independent `auto_plot_jumps`, `disable_refuel`, `route_file`, `tritium_slot`, `refuel_mode`, `enabled` per slot (`gui_config.py:82-116`). The `SlotEditorWidget` reads/writes both toggles (`slot_editor.py:50-54, 112-113, 167-168`), and they flow verbatim into `TraversalOptions` via `WorkerController._build_options` (`worker_controller.py:592-613`). No cross-slot leakage.
- **Refuel ignored when `auto_jump` disabled (rule #3).** Guarded in **both** restock entry points and unit-tested:
  - `restock_tritium`: `if not options.auto_plot_jumps or options.disable_refuel: return` (`main.py:350`).
  - `_run_coordinated_restock`: `if options.disable_refuel or not options.auto_plot_jumps: return` (`main.py:658`).
  - Test: `test_coordinated_restock_skipped_when_auto_plot_jumps_disabled`.
- **Serial jump ordering at the input/block level (rule #4).** The `SequenceQueue` runs at most one block at a time (`sequence_queue.py:295-314`, `_active` gating). The first-cycle barrier (`arm_first_cycle_barrier` + `claim_first_cycle_deadline`) produces a `(deadline, sequence)` sort that resolves to slot-index order once all siblings arrive (`sequence_queue.py:195-245, 345-375`); `WorkerController.start_all_ready` arms it for the batch (`worker_controller.py:341`). Subsequent cycles stay ordered via registered cooldown deadlines. Confirmed by `test_first_cycle_deadline_is_deterministic_slot_order`, `test_first_cycle_deadline_orders_concurrent_workers_by_slot`, `test_two_carriers_serialize_jump_and_restock_through_queue`.
  - *Note on "first carrier, then second":* serialization is at the **input-block** granularity (one carrier's jump-plot clicks, then the next's), not at the whole-route granularity. Countdowns and game-side jumps remain concurrent across carriers. This is the correct design — blocking carrier 2's entire route behind carrier 1's would serialise hours of cooldowns unnecessarily — and matches the only operationally meaningful reading of rule #4.
- **Restock/jump non-overlap (rule #5).** `_restock_is_feasible` checks `now + estimated_duration < earliest_jump_deadline` (`sequence_queue.py:412-419`), and `_select_next_locked` defers a non-feasible restock in favor of the earliest jump (`:369-375`). The restock estimate includes focus overhead (`main.py:88-94`). Confirmed by `test_future_jump_deadline_allows_restock_to_run_before_pending_jump`, `test_soon_jump_deadline_defers_restock_until_after_pending_jump`. (Caveat: the scheduled-jump deadline now gates correctly after the Bug C fix.)
- **Restock skipped on the final route element.** `if idx + 1 < len(route_list):` guards the deadline registration in the cooldown loop (`main.py:1099`); the post-plot restock (`main.py:962`) sits inside the `done_first` branch which is only reached between cycles, so the final plotted jump does not enqueue a trailing restock. Test: `test_traversal_slot_skips_restock_on_final_route_element`.
- **Bug A fix (restock cancel-event isolation).** Verified: `restock_cancel_event` is a fresh `threading.Event()` (`main.py:688`), the worker event is polled one-way (`:707-712`), and the regression test asserts `runtime_context.cancel_event.is_set() is False` after timeout (`tests/test_multicarrier_jump_queue.py:887`) plus a dedicated `test_coordinated_restock_stops_promptly_on_user_stop` (`:897`).
- **Bug B fix (namespaced scheduled-jump key).** Verified: `dashboard.py:661` submits as `slot-{idx}-scheduled`; the worker key remains `slot-{idx}` (`main.py:811`). No collision.

---

## Suggested fix priority

1. **Bug 3** (restock timeout split-brain) — on timeout, check whether the block has dispatched; if so, await its completion before returning so the worker's time accounting stays truthful.
2. **Bug 4** (per-slot Start barrier) — either lift the barrier call or gate `start_btn`; document the chosen semantics.
3. **Bug 5** (unbounded jump retry) — add a retry bound and transition to `ERROR` on exhaustion.

---

## Resolved issues (history)

### Bug 1 (2026-06-18) — Restock trigger timing contradicted the contract → RESOLVED by refactor

The 2026-06-18 audit flagged a HIGH-severity spec-conformance defect: the restock fired when 5 minutes **remained** on the cooldown clock (~62 s into the 362 s cooldown), while the operator's contract said "5 minutes **after** the jump" (~300 s). The two readings differ by ~4 minutes, and the literal contract reading was self-contradictory with rule #5 under a 362 s cooldown. The audit correctly recommended **not** changing code until the operator confirmed intent.

**Resolution.** Commit `b0b1844` (2026-07-02, "fire restock after first plotted jump and derive countdowns from deadlines") sidestepped the debate entirely by restructuring the flow:

- The coordinated restock was **moved out of the cooldown loop**. It now fires immediately after the **first plotted jump lockout**, gated by `done_first` (`main.py:962-975`), i.e. right after the jump is plotted and before the jump-countdown loop begins. The first cycle (`done_first == False`) skips the restock.
- `RESTOCK_TRIGGER_REMAINING_SECONDS = 300` (`main.py:69-70`) **no longer triggers a restock**. Inside the cooldown loop it now gates a **jump-confirmation check** (`main.py:1114-1142`): when 5 min remain, the worker pauses and waits for `journal.has_jumped()` to confirm the previous jump actually completed (bounded by `JOURNAL_CONFIRMATION_TIMEOUT_SECONDS`). This is a journal-confirmation wait, not a restock trigger.

Because the restock is now decoupled from the cooldown clock — it fires at the earliest safe moment (post-plot lockout) rather than at a fixed offset into the cooldown — the "5 min remaining vs 5 min elapsed" question is **moot**. The new behavior maximizes the post-restock buffer ahead of the next jump, which is consistent with rules #5 and #6 under either original reading. No further code action is required on this item; the contract wording in rule #6 is now only loosely descriptive of the new (earlier) trigger point.

### Bug 2 (2026-06-18) — Manual-mode phantom deadlines → INTENTIONAL (won't fix)

The 2026-06-18 audit noted that `register_next_jump_deadline` (`main.py:1099-1100`) is not gated on `options.auto_plot_jumps`, so a manual-mode carrier registers a `now + 362 s` deadline it has no intention of honoring, which can skew other (automatic) carriers' restock feasibility in a mixed manual+auto fleet.

**Resolution — operator decision.** The operator confirmed this behavior is **intentional**: the auto-plotter must not attempt restocks, jumps, etc. while the user is manually plotting on another carrier. The registered deadline keeps the shared queue conservative (deferring peer restocks) during the manual plot window, which is the desired behavior. No code change; the item is closed by decision. (For a pure-automatic or pure-manual fleet the behavior is unaffected.)

### Prior bugs A–H (2026-06-17) — all fixed/mitigated

Independent re-verification of the 2026-06-17 bug report's A–H findings against current source:

| Prior # | Title | Current state | Evidence (current source) |
|---|---|---|---|
| A | Restock timeout killed the worker via cancel-event aliasing | **FIXED** | `main.py:679-731` — restock gets a dedicated `restock_cancel_event` (`:688`); worker event polled one-way at `:707-712`. Regression test `test_coordinated_restock_stops_promptly_on_user_stop` + assertion `runtime_context.cancel_event.is_set() is False` (`tests/test_multicarrier_jump_queue.py:887`). |
| B | Scheduled-jump `slot_id` collided with worker `slot_id` | **FIXED** | `dashboard.py:661` — submission uses `slot_id=f"slot-{slot_index}-scheduled"`; worker still uses `f"slot-{slot_id}"` (`main.py:811`). No collision. |
| C | Scheduled-jump deadline (`time.monotonic()`) didn't gate restocks | **FIXED** | `scheduled_jump.py:153-157` — deadline submitted as `time.monotonic() + SCHEDULED_JUMP_ESTIMATE_SECONDS` (5.0 s), so it survives the strict `> now` filter at `sequence_queue.py:385`. |
| D | Per-slot `Start` skipped the first-cycle ordering barrier | **PARTIAL → Bug 4** | `worker_controller.py:249-260` resets the shared first-cycle base on per-slot start, but still does **not** call `_arm_first_cycle_barrier`. Only `start_all_ready` (`:341`) arms it. Carried forward as Bug 4 above. |
| E | `schedule()` rejected past times while `_build_target_datetime` rolled them forward | **FIXED** | `scheduled_jump.py:106-111` — `schedule()` now rolls `target_dt <= now` forward by one day, consistent with `_build_target_datetime` (`:198-199`). |
| F | Exact-integer milestones skipped under recalibration | **FIXED (and extended)** | Cooldown milestones at `main.py:1096-1112` latch on threshold (`total_time <= trigger` + `fired_milestones` set). After `b0b1844` the **same threshold-latch pattern was extended to the jump-countdown loop** (`main.py:1048-1074`), which is now *necessary* rather than cosmetic: both countdowns derive `total_time` from a monotonic deadline (`:1061`, `:1102`) instead of decrementing, so a stalled loop can step past an integer and exact-integer `match` would silently miss values. |
| G | Global input singletons → non-atomic focus-then-dispatch | **MITIGATED (documented)** | Process-wide re-entrant `dispatch_lock` held across focus+dispatch in `scheduled_jump.py:173,179,227` and `input_handler.py`. True window-targeted dispatch (SendInput/PostMessage HWND, xdotool --window) still deferred. |
| H | Timeout test masked Bug A by injecting a fresh event | **FIXED** | `tests/test_multicarrier_jump_queue.py:887-889` asserts the worker event is NOT set and the restock event is a distinct object; `test_coordinated_restock_stops_promptly_on_user_stop` at `:897`. |

### Status of the original 2026-06-16 audit findings (oldest layer)

For completeness, the eight issues from the very first audit (2026-06-16) are all reflected in the A–H work above: old bugs 1, 2, 3, 5, 7, 8 were resolved by `b194972`; old bug 4 was partially fixed (introducing Bug A, later fixed); old bug 6 (global input singletons) remains documented/mitigated (Bug G). No item from the 2026-06-16 layer is outstanding beyond what is listed in the open-issues table.

---

## Verification appendix (2026-07-02)

- **Source basis:** `TraversalSystem/` working tree at HEAD `028e92f`, which includes the structural restock refactor `b0b1844` (2026-07-02). Every line reference above was read directly from the current source.
- **Method:** the six contract rules were each traced from their UI entry point (`slot_editor.py`, `dashboard.py`) through config (`gui_config.py`) into the worker (`worker_controller.py`, `workers.py`) and the traversal loop (`main.py`, `runtime/controller.py`), and through the serialization layer (`sequence_queue.py`, `scheduled_jump.py`). The prior reports' A–H and 1–5 claims were re-derived rather than trusted.
- **Key change since the 2026-06-18 audit:** `b0b1844` moved the coordinated restock out of the cooldown loop (now fires post-plot at `main.py:962-975`), repurposed `RESTOCK_TRIGGER_REMAINING_SECONDS` to gate a jump-confirmation wait (`main.py:1114-1142`), and converted both countdown loops to deadline-derived totals with threshold-latch milestones (`main.py:1042-1076`, `1087-1145`). This resolved old Bug 1 and extended the Bug F fix.
- **No code was modified** in the production of this report — it is an audit-only deliverable.
