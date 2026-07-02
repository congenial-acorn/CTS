# Multicarrier Jump Action Handling — Independent Audit (2026-06-18)

**Codebase:** CTS — Carrier Traversal System (Elite Dangerous Fleet Carrier Auto-Plotter)
**Audit date:** 2026-06-18
**Scope:** Multicarrier jump sequencing, tritium refuel gating/timing, carrier-to-process binding, scheduled-jump integration, cross-carrier coordination.
**Method:** Fresh, independent static analysis of the current working tree under `TraversalSystem/` cross-referenced line-by-line against the operator's intended-behavior contract (below). This audit was performed without relying on the verdicts in the pre-existing `MULTICARRIER_JUMP_BUG_REPORT.md`; every code reference was read directly and every claim is re-derived here. Where this audit agrees or disagrees with the prior report, it says so explicitly.
**Relation to prior report:** The prior report (`MULTICARRIER_JUMP_BUG_REPORT.md`, 2026-06-17) documents bugs A–H and claims all eight are fixed. This audit **independently confirms** the A/B/C/D/E/H fixes are present in the current source, **confirms G is mitigated** (dispatch lock), and **confirms F's threshold fix is present**. It also surfaces **three issues the prior report did not raise**, the most important of which is a direct contradiction between the operator's stated contract and the code's restock-trigger timing.

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

The thumbnail binding, per-carrier toggle wiring, auto-jump refuel gate, and queue-level jump/restock serialization are **correctly wired** (rules #1, #2, #3, #4, #5 all hold at the block level). The prior round of fixes (A–H) are genuinely present in the current source.

However, this audit found **three issues not captured by the prior report**, headed by a **HIGH-severity spec conformance defect**:

| # | Bug | Severity | Status |
|---|-----|----------|--------|
| **1** | **Restock trigger fires at "5 min REMAINING" (~62 s after jump), not "5 min AFTER the jump" (300 s) as the contract states** | **High (spec violation)** | **New — needs operator clarification** |
| **2** | Manual-mode carrier still registers phantom cooldown-based jump deadlines, skewing other carriers' restock feasibility in a mixed manual+auto fleet | Medium | New |
| **3** | Restock queue-timeout cannot abort an already-dispatched restock; the worker moves on while the queue stays blocked, producing a timing/state split-brain | Low | New |
| **4** | Per-slot `Start` does not arm the first-cycle ordering barrier (prior Bug D, only partially addressed) | Low (edge) | Carried forward |
| **5** | No retry bound on `jump_to_system` failure → unbounded retry loop | Low (pre-existing, not multicarrier-specific) | Pre-existing |

Independent re-verification of the prior A–H bugs (all read against current source):

| Prior # | Title | Current state | Evidence (current source) |
|---|---|---|---|
| A | Restock timeout killed the worker via cancel-event aliasing | **FIXED** | `main.py:685-720` — restock gets a dedicated `restock_cancel_event`; worker event polled one-way at `:707-712`. Regression test `test_coordinated_restock_stops_promptly_on_user_stop` + assertion `runtime_context.cancel_event.is_set() is False` (`tests/test_multicarrier_jump_queue.py:887`). |
| B | Scheduled-jump `slot_id` collided with worker `slot_id` | **FIXED** | `dashboard.py:661` — submission uses `slot_id=f"slot-{slot_index}-scheduled"`; worker still uses `f"slot-{slot_id}"` (`main.py:811`). No collision. |
| C | Scheduled-jump deadline (`time.monotonic()`) didn't gate restocks | **FIXED** | `scheduled_jump.py:153-157` — deadline submitted as `time.monotonic() + SCHEDULED_JUMP_ESTIMATE_SECONDS` (5.0 s), so it survives the strict `> now` filter at `sequence_queue.py:385`. |
| D | Per-slot `Start` skipped the first-cycle ordering barrier | **PARTIAL** | `worker_controller.py:249-260` resets the shared first-cycle base on per-slot start, but still does **not** call `_arm_first_cycle_barrier`. Only `start_all_ready` (`:341`) arms it. See Bug 4 below. |
| E | `schedule()` rejected past times while `_build_target_datetime` rolled them forward | **FIXED** | `scheduled_jump.py:106-111` — `schedule()` now rolls `target_dt <= now` forward by one day, consistent with `_build_target_datetime` (`:198-199`). |
| F | Exact-integer milestones skipped under recalibration | **FIXED (cosmetic)** | Cooldown milestones at `main.py:1075, 1085-1088` latch on threshold (`total_time <= trigger` + `fired_milestones` set). Pre-jump loop (`:1036-1054`) still uses exact `match`, but that loop is a pure linear descent with no recalibration, so exact match is correct there. |
| G | Global input singletons → non-atomic focus-then-dispatch | **MITIGATED (documented)** | Process-wide re-entrant `dispatch_lock` held across focus+dispatch in `scheduled_jump.py:173,179,227` and `input_handler.py`. True window-targeted dispatch (SendInput/PostMessage HWND, xdotool --window) still deferred. |
| H | Timeout test masked Bug A by injecting a fresh event | **FIXED** | `tests/test_multicarrier_jump_queue.py:887-889` asserts the worker event is NOT set and the restock event is a distinct object; new `test_coordinated_restock_stops_promptly_on_user_stop` at `:897`. |

---

## Bug 1 — Restock trigger timing contradicts the contract (HIGH, spec violation)

> **This is the single most important finding.** The operator's contract and the code disagree on *when* the tritium restock fires, and the two timings are separated by ~4 minutes. The prior report explicitly adopted the code's interpretation without flagging the conflict with the operator's words. This needs operator resolution before any "fix," because the correct fix depends on which side is right.

**Location:** `TraversalSystem/main.py:69-70, 1067, 1090`

**The contract says (rule #6):** the refueling sequence *"is triggered 5 minutes after the jump time."*

**The code does:** the restock fires when **5 minutes REMAIN** on the cooldown clock.

```python
# main.py:67-70
CARRIER_COOLDOWN_SECONDS = 362
"""Elite Dangerous carrier jump cooldown: 6 min + 2 s buffer ..."""
RESTOCK_TRIGGER_REMAINING_SECONDS = 300
"""Trigger tritium restock when 5 minutes REMAIN on the cooldown clock
(fires ~62 s into the 362 s cooldown ...). NOT 5 min elapsed — ..."""
```

```python
# main.py:1067-1090 (cooldown loop, abridged)
total_time = CARRIER_COOLDOWN_SECONDS          # 362
cooldown_deadline = time.monotonic() + 362
...
if idx + 1 < len(route_list):
    register_next_jump_deadline(total_time)
while total_time > 0:
    ...
    if not restock_triggered and total_time <= RESTOCK_TRIGGER_REMAINING_SECONDS:  # <= 300 REMAINING
        restock_triggered = True
        # ... restock fires here ...
```

With `CARRIER_COOLDOWN_SECONDS = 362`, "5 min remaining" means the trigger condition is first satisfied when `total_time` counts down to 300, i.e. **~62 seconds after the jump**. The contract's "5 minutes after the jump time" is **~300 seconds after the jump**. The two differ by **~238 seconds (~4 minutes)**: the code refuels roughly 4 minutes *earlier* than the contract states.

**Why this matters / why the two readings are incompatible.** The contract also says (rule #5) the restock "must not overlap with a planned jump sequence." Given the code's 362 s cooldown:

| Reading | Restock fires at | Restock (~30–90 s) ends at | Time left before next jump | Overlaps? |
|---|---|---|---|---|
| **Code: "5 min remaining"** | jump + ~62 s | jump + ~92–152 s | ~210–270 s | **No** ✓ |
| **Contract literal: "5 min after jump"** | jump + 300 s | jump + ~330–390 s | **−28 to +32 s** | **Yes — violates rule #5** ✗ |

So if the 362 s cooldown constant is correct, the operator's literal "5 min after the jump" is **self-contradictory** with the no-overlap rule (a 60–90 s restock started at +300 s cannot finish before +362 s). The code's "5 min remaining" is the only timing consistent with both the 362 s cooldown *and* rule #5. Conversely, if the operator's "5 min after the jump" is the genuine intent, then the cooldown is not 362 s — it must be much longer (the real-world Elite Dangerous carrier post-jump lockout is widely cited closer to 15 minutes), in which case **both** `CARRIER_COOLDOWN_SECONDS = 362` **and** the trigger constant are wrong.

**Prior-report stance.** `MULTICARRIER_JUMP_BUG_REPORT.md:30` asserts without hedging: *"It is triggered 5 minutes before the cooldown ends ('5 min remaining')…"* and ships a test (`test_timing_constants_are_documented_and_correct`) that bakes in that interpretation. That stance silently overrides the operator's stated contract rather than surfacing the discrepancy.

**Evidence.**
- `main.py:69`: `RESTOCK_TRIGGER_REMAINING_SECONDS = 300` with docstring "5 minutes REMAIN … NOT 5 min elapsed."
- `main.py:1090`: `if not restock_triggered and total_time <= RESTOCK_TRIGGER_REMAINING_SECONDS:` — fires on *remaining*, not *elapsed*.
- `main.py:67`: `CARRIER_COOLDOWN_SECONDS = 362` — the 6 min 2 s cycle the trigger is measured against.
- `MULTICARRIER_JUMP_BUG_REPORT.md:30` — prior report codifies "5 min remaining" as the contract.

**Recommendation.** Do **not** change code until the operator confirms intent. Two resolution paths:
- **(A) Operator confirms "5 min remaining" was the intent** (loose wording). → No code change; update the contract wording and the prior report to say "5 min remaining" so the doc/test/code/operator agree.
- **(B) Operator confirms literal "5 min after jump."** → Two sub-fixes are then required together: (1) `CARRIER_COOLDOWN_SECONDS` must reflect the *real* post-jump lockout (likely ~900 s, not 362 s); (2) the trigger must be re-expressed in *elapsed* terms (`time.monotonic() - jump_time >= 300`) rather than `total_time <= 300`. With a ~900 s cooldown and a +300 s trigger, a 60–90 s restock finishes at +360–390 s, leaving a safe ~510–540 s margin before the next jump — finally satisfying both rule #5 and rule #6 literally.

Either way, the current state is a **contract conformance defect** until reconciled.

---

## Bug 2 — Manual-mode carrier registers phantom jump deadlines (MEDIUM)

**Location:** `TraversalSystem/main.py:1078-1079` (not gated on `auto_plot_jumps`); interacts with `sequence_queue.py:160-165, 377-390, 412-419`.

**Description.** At the top of every cooldown loop the worker unconditionally registers a "next jump" deadline with the shared queue so other carriers can decide whether a restock is feasible:

```python
# main.py:1078-1079
if idx + 1 < len(route_list):
    register_next_jump_deadline(total_time)     # total_time = 362
```

This registration is **not gated on `options.auto_plot_jumps`**. In manual mode (`auto_plot_jumps == False`) the worker does not actually plot the next jump at `now + 362 s` — it instead copies the next system name to the clipboard (`_prepare_manual_jump_plot`, `main.py:476-478`) and then blocks indefinitely inside `_wait_for_manual_jump_confirmation` (`main.py:481-503`) until the *human* plots the jump in-game. The real time of the next jump is therefore unknown and is whatever the operator chooses, not `now + 362 s`.

Nevertheless the queue now holds a registered deadline of `now + 362 s` for this slot. Other (automatic) carriers consult `_earliest_jump_deadline_locked` (`sequence_queue.py:377-390`), which includes `_registered_jump_deadlines.values()` (`:387`), when deciding restock feasibility (`_restock_is_feasible`, `:412-419`). So an auto carrier's restock can be **deferred** (or, combined with Bug 3's split-brain, mistimed) based on a deadline the manual carrier has no intention of honoring.

**Impact.** Only affects a **mixed fleet** where at least one slot is manual (`auto_plot_jumps = False`) and at least one is automatic, and they share the `SequenceQueue` (the standard multicarrier path). The auto carrier's restocks may be needlessly delayed, or a restock that the manual carrier's real (later) jump would have permitted may be suppressed. Pure-automatic and pure-manual fleets are unaffected.

**Evidence.**
- `main.py:1078-1079`: `register_next_jump_deadline` call has no `if options.auto_plot_jumps:` guard (contrast the jump-deadline *resolution* at `:932-937` which *is* guarded).
- `main.py:481-503`: `_wait_for_manual_jump_confirmation` blocks for an unbounded, operator-controlled time, so `now + 362 s` is not the real next-jump time.
- `sequence_queue.py:387, 412-419`: registered deadlines directly feed restock feasibility for *other* slots.

**Recommendation.** Skip `register_next_jump_deadline` when `not options.auto_plot_jumps` (and likewise skip the `register_next_jump_deadline` re-registration at `main.py:1136`). Manual slots contribute no predictable jump timing, so they should not constrain the shared queue's feasibility math.

---

## Bug 3 — Restock queue-timeout cannot abort an already-dispatched restock (LOW)

**Location:** `TraversalSystem/main.py:705-731`, `TraversalSystem/sequence_queue.py:295-314, 331-338`.

**Description.** `_run_coordinated_restock` bounds its wait on the shared queue to `RESTOCK_QUEUE_WAIT_TIMEOUT_SECONDS = 180 s` (`main.py:705, 714-721`). On timeout it sets the restock's dedicated cancel event and returns so the slot can continue its cooldown:

```python
# main.py:714-721
if remaining <= 0:
    print("Restock deferred: … Skipping restock this cycle; …")
    restock_cancel_event.set()   # cancels only this restock, not the slot
    return
```

The intent ("skip for this cycle, retry next cooldown") is correct **only when the restock has not yet started running**. The queue only honors `cancel_event` at two points: before dispatch (`sequence_queue.py:303-305`) and when pruning *pending* entries (`_prune_cancelled_pending_locked`, `:331-338`). Once a block is dispatched it runs to completion — the worker thread is inside `block.run()` (`:306`) and `cancel_event` is **not** re-checked during execution.

So if the 180 s timeout fires *while the restock is mid-execution* (a real possibility — `estimate_restock_duration` already budgets 30–90 s of inputs plus focus overhead, and queue contention can push dispatch late), three things happen simultaneously:

1. `_run_coordinated_restock` returns; the worker recalculates `total_time` (`main.py:1133-1134`) and continues its cooldown loop as if the restock were skipped.
2. The queue worker is still inside the restock `run()` lambda (`main.py:694-699`), executing real game inputs against this carrier's window.
3. The queue's serialization invariant (`_active` is set, `sequence_queue.py:326`) blocks **every other carrier's** jump/restock submission until this restock finishes.

The net effect is a split-brain: the originating worker believes the restock was skipped, while the queue is still busy executing it and blocking all peers. The worker proceeds to re-register its next jump deadline (`main.py:1136`) and, when its cooldown expires, submits its next jump-plot block — which then stalls in the queue behind the still-running restock, producing an unbounded additional delay not accounted for in any deadline.

**Impact.** Low likelihood (requires sustained cross-carrier contention that pushes a restock's queue-wait past 180 s *and* then dispatches it within that window), but when it hits the timing/deadline math diverges from reality and a carrier's next jump can be silently delayed by up to one restock duration beyond its registered deadline. No input corruption (the queue still serializes), but contract rule #5 (no overlap with a planned jump) is evaluated against stale deadlines.

**Evidence.**
- `sequence_queue.py:306`: `result = block.run()` — no `cancel_event` check inside the running block.
- `sequence_queue.py:303-305`: cancel check is pre-dispatch only.
- `main.py:714-721`: timeout path returns immediately; nothing awaits or coordinates with a potentially-running restock.
- `main.py:1133-1136`: worker recomputes `total_time` and re-registers the deadline on the assumption the restock was skipped.

**Recommendation.** Either (a) on timeout, if the block has already been dispatched, wait for it to finish before returning (so the time accounting is truthful), or (b) thread `runtime_context.raise_if_cancelled` calls into the restock `run()` body (they partly exist via `follow_button_sequence` at `main.py:312`, but `restock_tritium`'s own input-emitting paths between sequence calls are not all covered) so a post-timeout cancel can actually interrupt a running restock. Option (a) is simpler and sufficient for timing correctness.

---

## Bug 4 — Per-slot `Start` still does not arm the first-cycle ordering barrier (LOW, carried forward)

> This is the prior report's Bug D. It is only partially addressed in the current source, so it is carried forward here for completeness. This audit's independent reading agrees with the prior verdict.

**Location:** `TraversalSystem/gui/worker_controller.py:239-311` (no barrier call), `:329-346, 385-403` (barrier is armed only by `start_all_ready`).

**Description.** `start_slot` (per-slot Start button) resets the shared first-cycle base (`worker_controller.py:258-260`) but never calls `_arm_first_cycle_barrier`. The barrier defaults to satisfied (`_first_cycle_satisfied = True`, `sequence_queue.py:103`), so when a lone jump block is the only pending item the queue dispatches it immediately regardless of deadline ordering. If the operator clicks Start on slot 1 then Start on slot 0, slot 1's first jump can dispatch before slot 0's block is even submitted.

**Why this is low.** The documented multicarrier entry point is **Start All**, which *does* arm the barrier (`worker_controller.py:341`). The bug only manifests when an operator manually starts slots in non-index order via the per-slot buttons, and only on the very first jump cycle (subsequent cycles are pinned by registered cooldown deadlines). A user clicking per-slot Start is explicitly choosing an order.

**Evidence.**
- `worker_controller.py:239-311`: no `_arm_first_cycle_barrier` call in the per-slot path; `:341` is the only call site.
- `sequence_queue.py:103, 232-245`: barrier open by default; latch logic only engages when armed with `expected_count >= 2`.

**Recommendation (unchanged).** Either lift the barrier-arming call into `start_slot` (counting currently-READY siblings) for strict ordering from any entry point, or document the per-slot path as "submission/click order" and gate `start_btn` while READY siblings remain unstarted so the order is deterministic by construction.

---

## Bug 5 — Unbounded retry on jump-plot failure (LOW, pre-existing)

**Location:** `TraversalSystem/main.py:926-949`.

**Description.** The inner `while time_to_jump == 0 or departing_time == 0:` loop retries `_run_coordinated_jump_plot` with no retry counter. `jump_to_system` returns `(0, 0)` on failure (`main.py:457, 462`) after playing `jump_fail.txt`; the loop then submits another full jump-plot block. If the failure is persistent (wrong system name, UI drift, broken journal), this loops forever, each iteration consuming a queue block and emitting inputs.

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
- **Restock/jump non-overlap (rule #5).** `_restock_is_feasible` checks `now + estimated_duration < earliest_jump_deadline` (`sequence_queue.py:412-419`), and `_select_next_locked` defers a non-feasible restock in favor of the earliest jump (`:369-375`). The restock estimate includes focus overhead (`main.py:88-94`). Confirmed by `test_future_jump_deadline_allows_restock_to_run_before_pending_jump`, `test_soon_jump_deadline_defers_restock_until_after_pending_jump`. (Caveat: the scheduled-jump deadline now gates correctly after the Bug C fix; and see Bug 2 above for the manual-slot phantom-deadline gap.)
- **Restock skipped on the final route element.** `if idx + 1 < len(route_list):` guards both the deadline registration and the restock call (`main.py:1078, 1121`). Test: `test_traversal_slot_skips_restock_on_final_route_element`.
- **Bug A fix (restock cancel-event isolation).** Verified: `restock_cancel_event` is a fresh `threading.Event()` (`main.py:688`), the worker event is polled one-way (`:707-712`), and the regression test asserts `runtime_context.cancel_event.is_set() is False` after timeout (`tests/test_multicarrier_jump_queue.py:887`) plus a dedicated `test_coordinated_restock_stops_promptly_on_user_stop` (`:897`).
- **Bug B fix (namespaced scheduled-jump key).** Verified: `dashboard.py:661` submits as `slot-{idx}-scheduled`; the worker key remains `slot-{idx}` (`main.py:811`). No collision.

---

## Suggested fix priority

1. **Bug 1** (restock trigger timing vs contract) — **do not touch code first**; get the operator to disambiguate "5 min after jump" vs "5 min remaining." The correct fix is path-dependent (constant change + trigger re-expression, or doc/test reconciliation). This is the only finding that potentially indicates *wrong runtime behavior* rather than an edge case.
2. **Bug 2** (manual-mode phantom deadlines) — one-line guard `if options.auto_plot_jumps:` around the two `register_next_jump_deadline` call sites in `main.py`. Cheap, removes a real cross-carrier scheduling skew in mixed fleets.
3. **Bug 3** (restock timeout split-brain) — on timeout, check whether the block has dispatched; if so, await its completion before returning so the worker's time accounting stays truthful.
4. **Bug 4** (per-slot Start barrier) — either lift the barrier call or gate `start_btn`; document the chosen semantics.
5. **Bug 5** (unbounded jump retry) — add a retry bound and transition to `ERROR` on exhaustion.

---

## Verification appendix (2026-06-18)

- **Source basis:** `TraversalSystem/` working tree as of 2026-06-18. Every line reference above was read directly from the current source, not transcribed from the prior report.
- **Method:** the five contract rules were each traced from their UI entry point (`slot_editor.py`, `dashboard.py`) through config (`gui_config.py`) into the worker (`worker_controller.py`, `workers.py`) and the traversal loop (`main.py`, `runtime/controller.py`), and through the serialization layer (`sequence_queue.py`, `scheduled_jump.py`). The prior report's A–H claims were re-derived rather than trusted.
- **No code was modified.** This is an audit-only deliverable per the operator's request ("analyze … write your bug report to a .md file").
- **Outstanding ambiguity:** Bug 1 cannot be resolved by code inspection alone — it requires the operator to confirm whether "5 minutes after the jump time" is literal (path B: cooldown constant + trigger both wrong) or loose wording for "5 min remaining" (path A: doc/test reconciliation only).
