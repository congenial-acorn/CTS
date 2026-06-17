# Multicarrier Jump Action Handling — Bug Report

**Codebase:** CTS — Carrier Traversal System (Elite Dangerous Fleet Carrier Auto-Plotter)
**Audit date:** 2026-06-17
**Scope:** Multicarrier jump sequencing, tritium refuel gating/timing, carrier-to-process binding, scheduled-jump integration, and cross-carrier coordination.
**Method:** Fresh static analysis of current production source under `TraversalSystem/` (HEAD = `b194972`) cross-referenced against the intended-behavior contract and the test suite under `tests/`. This report supersedes the 2026-06-16 audit; the prior findings were re-verified against current code and most have been resolved by commit `b194972` ("fix(multicarrier): enforce deterministic jump ordering and harden restock scheduling").

> **Independent hostile verification — 2026-06-17.** Every finding below was re-checked against the source line-by-line, and Bug A was reproduced with an executable harness. Verdicts: **A, C, D, E, G, H confirmed as written; B confirmed but scope narrowed; F partially refuted** (the stated root cause is incorrect and the claimed restock-trigger impact is unreachable). Per-bug verdicts are recorded inline under each heading and summarized in the verification table below.

> **✅ Fixes applied — 2026-06-17 (all eight).** Full test suite green (581 passed), including a new Bug A regression guard that injects `runtime_context` exactly as production does and asserts the worker event stays unset.
> - **A** — Restock now gets a **dedicated** cancel event; the wait loop polls the worker event to honor a real Stop one-way, so a queue timeout skips the restock without stopping the slot. (`main.py` `_run_coordinated_restock`)
> - **B** — Scheduled-jump submissions are namespaced `slot-{idx}-scheduled`, so they no longer delete the worker's registered cooldown deadline. (`gui/dashboard.py`)
> - **C** — Scheduled-jump deadline submitted as `time.monotonic() + SCHEDULED_JUMP_ESTIMATE_SECONDS`, so it gates concurrent restocks while remaining the earliest jump. (`gui/scheduled_jump.py`)
> - **D** — Per-slot `start_slot` documented as submission-order and now clears any stale shared first-cycle base so manual starts stay deterministic; strict slot-order remains the "Start All" path. (`gui/worker_controller.py`)
> - **E** — `schedule()` now rolls an elapsed time-of-day forward to tomorrow (consistent with `_build_target_datetime`) instead of raising. (`gui/scheduled_jump.py`; test updated)
> - **F** — Cooldown Discord milestones and the restock trigger now latch on a "crossed this threshold" basis instead of exact-integer equality, so a post-restock recalibration jump cannot skip them. (`main.py`)
> - **G** — Added a process-wide re-entrant `dispatch_lock`; `FocusAwareInputHandler` and the scheduled-jump click hold it across focus+dispatch, making the focus-then-act pair atomic across workers. True window-targeted I/O remains deferred (documented inline). (`input_handler.py`, `focus_input_handler.py`, `gui/scheduled_jump.py`)
> - **H** — Timeout test rewritten to use the production `runtime_context`; added `test_coordinated_restock_stops_promptly_on_user_stop`. (`tests/test_multicarrier_jump_queue.py`)

---

## Intended Behavior (Contract)

Per the operator's description, the system should:

1. **Binding** — The user adds commanders/carriers and uses the **thumbnail selection** to bind each carrier to a game process/window.
2. **Per-carrier config** — The user adds a route for each carrier and can toggle per-carrier settings such as auto-fuel.
3. **Refuel gating** — **Tritium refuel must be ignored when `auto_jump` is disabled** for that carrier.
4. **Jump ordering** — When multicarriers are started, the **first carrier goes through the jump sequence, then the second, and so on**.
5. **Refuel timing / non-overlap** — The refueling sequence takes place sometime after the jump, but **not when it would overlap with a planned jump sequence**. It is triggered **5 minutes before the cooldown ends** ("5 min remaining" — see `main.py:69-70`) to maximize the post-restock buffer ahead of the next jump.

---

## Executive Summary

Commit `b194972` resolved six of the eight issues from the 2026-06-16 audit (old bugs 1, 2, 3, 5, 7, 8) and added a timeout for old bug 4. The remaining unfixed item is old bug 6 (global input singletons), which is now at least explicitly documented.

However, **the restock-timeout fix introduced a new HIGH-severity regression**: the timeout handler aliases the worker's cancel event, so a deferred restock kills the entire carrier worker instead of being skipped. The test suite misses this because it injects a fresh `threading.Event()` instead of the production cancel event.

| # | Bug | Severity | Status |
|---|-----|----------|--------|
| **A** | **Restock queue-timeout cancels the entire worker (cancel_event aliasing)** | **High** | **New regression, untested** |
| **B** | Scheduled-jump `slot_id` collides with worker `slot_id`, clearing the worker's registered cooldown deadline | ~~Medium~~ → Low–Med (verified, scope narrowed) | New |
| **C** | Scheduled-jump deadline (`time.monotonic()`) does not gate restocks → click can be delayed by another carrier's (or its own) feasible restock | Medium (verified) | New |
| **D** | Per-slot `Start` button does not arm the first-cycle ordering barrier → manual out-of-order starts dispatch in submission order, not slot order | Low (verified, edge case) | New (edge case) |
| **E** | `schedule()` rejects times that have passed today, but `_build_target_datetime` rolls past times to tomorrow — inconsistent; cannot schedule for tomorrow's UTC time once today's slot has elapsed | Low (verified) | Pre-existing |
| **F** | ~~Cooldown `total_time == <integer>` matchers skip milestones under `wait(1)` jitter~~ — **partially refuted**: counter is jitter-immune; only post-restock recalibration can skip a sub-300 Discord refresh; restock trigger is robust | ~~Low~~ → Cosmetic | Pre-existing |
| **G** | Global input singletons (`_keyboard`/`_mouse` on Linux; `pydirectinput` state on Windows) — focus-then-dispatch is non-atomic across concurrent workers | Low–Med | Pre-existing (now documented at `input_handler.py:16-26`) |
| **H** | `_run_coordinated_restock` timeout test (`test_coordinated_restock_skips_on_queue_timeout`) uses a fresh `threading.Event()` rather than the runtime's event — masks bug A | Low (test gap) | New |

---

## Independent verification verdicts (2026-06-17)

| # | Verdict | Adjusted severity | Verifier note |
|---|---------|-------------------|---------------|
| **A** | **CONFIRMED** | High (unchanged) | Reproduced with a harness: in the production path (`runtime_context` not `None`) `effective_cancel_event` *is* the worker event; `handle.cancel()` on timeout sets it; the next `runtime_context.wait(1)` raises `TraversalStopped` → worker stops. Trace and line refs all check out. |
| **B** | **CONFIRMED — scope narrowed** | Low–Med (was Med) | The `slot-{index}` collision and unconditional `pop()` (`sequence_queue.py:279`) are real, and worker + scheduled-jump keys do collide (`main.py:786` ↔ `dashboard.py:656`). But the deletion only bites when a scheduled jump *fires* while an auto-plot worker is mid-cooldown **on the same carrier** — a self-conflicting configuration (two jump clickers on one window). Real, but narrower than "Medium" implies. |
| **C** | **CONFIRMED** | Med (unchanged) | `time.monotonic()` deadline (`scheduled_jump.py:142`) is excluded by the strict `> now` filter (`sequence_queue.py:385`); `_select_next_locked` returns a feasible restock ahead of a pending jump (`:369-373`). Even a *lone* scheduled jump is delayed if any restock is pending (no other deadline ⇒ restock always feasible). |
| **D** | **CONFIRMED** | Low edge (unchanged) | `start_slot` (`worker_controller.py:239-311`) has no barrier-arming call; only `start_all_ready` arms it (`:325`). Manual non-index start = user-chosen order, and the documented "Start All" path is correct — genuinely an edge case. |
| **E** | **CONFIRMED** | Low (unchanged) | `schedule()` rejects `<= now` same-day (`scheduled_jump.py:103`) while `_build_target_datetime` rolls forward a day (`:183`). Inconsistent as described. |
| **F** | **PARTIALLY REFUTED** | Cosmetic (was Low) | The stated root cause is wrong — see the rewritten Bug F section. `total_time -= 1` is a pure counter decrement, independent of wall-clock/`wait` jitter, so it hits **every** integer on the linear descent; no milestone is skipped by GC/scheduling jitter. The only real skip source is the post-restock **recalibration** (`main.py:1086, 1104`), which can only skip the sub-300 Discord cases. The claimed restock-trigger miss is **unreachable**: `total_time` is unconditionally re-initialized to `362` at `main.py:1042`, so `== 300` is always hit before any recalibration, and no save-resume path enters the cooldown loop mid-count. |
| **G** | **CONFIRMED** | Low–Med, documented (unchanged) | Hazard comment present verbatim at `input_handler.py:16-26`; singletons at `:36-37`. No behavioral change. |
| **H** | **CONFIRMED** | Low test gap (unchanged) | Test is green (re-run); uses `runtime_context=None` + a fresh `threading.Event()` + a mock `_TimeoutHandle` whose `cancel()` only flips a local flag — it never wires the handle to a runtime, so the aliasing side effect of Bug A is never observed. |

---

## Status of prior audit (2026-06-16) findings

| Old # | Title | Current status | Evidence |
|---|---|---|---|
| 1 | Scheduled jump bypasses SequenceQueue | **FIXED** | `dashboard.py:651-663` now constructs a `submit_func` from `peek_shared_sequence_queue().submit_jump_plot`; `scheduled_jump.py:139-144` invokes it inside `_tick`. When no queue exists, the click fires directly (legacy behavior). |
| 2 | First-cycle ordering non-deterministic | **FIXED** | `SequenceQueue.claim_first_cycle_deadline` / `arm_first_cycle_barrier` (`sequence_queue.py:173-245`), invoked by `WorkerController._arm_first_cycle_batch` (`worker_controller.py:325, 369-387`) and `main.py:_resolve_first_cycle_jump_deadline` (`main.py:102-128`). Tests: `test_first_cycle_deadline_is_deterministic_slot_order`, `test_first_cycle_deadline_orders_concurrent_workers_by_slot`, `test_first_cycle_barrier_holds_head_block_until_siblings_arrive`, `test_first_cycle_barrier_releases_on_timeout_when_sibling_missing`. |
| 3 | Restock estimate excludes focus time | **FIXED** | `estimate_restock_duration` now adds `RESTOCK_FOCUS_OVERHEAD_PER_INPUT_SECONDS * (RESTOCK_INPUTS_FIXED + RESTOCK_INPUTS_PER_SLOT * tritium_slot)` (`main.py:88-94`). Test: `test_estimate_restock_duration_includes_focus_overhead`. |
| 4 | Unbounded restock wait | **PARTIALLY FIXED — introduced bug A** | `handle.result(timeout=RESTOCK_QUEUE_WAIT_TIMEOUT_SECONDS=180)` added (`main.py:697-706`), but `handle.cancel()` aliases `runtime_context.cancel_event` → kills the worker (see Bug A). |
| 5 | Restock trigger timing semantics | **FIXED (docs)** | Named constant `RESTOCK_TRIGGER_REMAINING_SECONDS = 300` with docstring explicitly stating "5 min REMAIN… NOT 5 min elapsed" (`main.py:69-70`). Test: `test_timing_constants_are_documented_and_correct`. |
| 6 | Global input singletons race | **NOT FIXED (documented)** | Comment block at `input_handler.py:16-26` now explicitly documents the hazard and names the architectural fix (`SendInput`/`PostMessage` HWND, `xdotool --window`). |
| 7 | Restock runs on the final jump | **FIXED** | `_run_coordinated_restock` is now guarded by `if idx + 1 < len(route_list):` (`main.py:1091`). Test: `test_traversal_slot_skips_restock_on_final_route_element`. |
| 8 | Undocumented magic numbers | **FIXED** | `CARRIER_COOLDOWN_SECONDS`, `RESTOCK_TRIGGER_REMAINING_SECONDS`, `ESTIMATED_CYCLE_SECONDS`, `JOURNAL_CONFIRMATION_TIMEOUT_SECONDS`, `RESTOCK_*`, `FIRST_CYCLE_SLOT_ORDER_OFFSET_SECONDS`, `RESTOCK_QUEUE_WAIT_TIMEOUT_SECONDS` are all named with docstrings (`main.py:64-85`). |

---

## Bug A — Restock queue-timeout cancels the entire worker (HIGH)

> **✅ Verified 2026-06-17 — CONFIRMED.** Reproduced with a standalone harness replicating the exact `effective_cancel_event = runtime_context.cancel_event` wiring (`main.py:679-681`) and `handle.cancel()` (`main.py:705`): after the `QueueTimeoutError` path the worker's own cancel event reads `is_set() == True`. The downstream halt is verified by inspection — `runtime_context.wait` calls `raise_if_cancelled` (`controller.py:135-157`), `TraversalController.run` catches `TraversalStopped` → `stopped` → returns `False` (`controller.py:199-201`). All eight trace steps and every line reference in this section check out against current source. Severity **High** is correct.

**Location:** `TraversalSystem/main.py:679-706`, `TraversalSystem/sequence_queue.py:67-71`, `TraversalSystem/runtime/controller.py:135-157`

**Description.** The 180 s timeout that "skips" a deferred restock actually signals cancellation on the **same** `threading.Event` the runtime uses to stop the whole slot. The event object is shared by reference, not copied.

Trace (all in the same worker thread, same Event instance):

1. `WorkerController._build_request` creates one `threading.Event()` per slot (`worker_controller.py:414`).
2. `CarrierAutomationWorker.run` forwards it as `cancel_event=self._request.cancel_event` (`workers.py:71`).
3. `run_traversal` → `TraversalController.run` stores it as `runtime_context.cancel_event` (`controller.py:123, 187`). Same object.
4. `_run_coordinated_restock` computes `effective_cancel_event = runtime_context.cancel_event` (`main.py:679-681`) and passes it as `submit_restock(cancel_event=effective_cancel_event)` (`main.py:694`).
5. The `SubmissionHandle` holds that exact event (`sequence_queue.py:269`).
6. On timeout, the handler calls `handle.cancel()` (`main.py:705`), which does `self.cancel_event.set(); self._cancelled = True` (`sequence_queue.py:67-71`).
7. `runtime_context.cancel_event` is now set. The next `runtime_context.wait(1)` in the cooldown loop (`main.py:1108`) calls `raise_if_cancelled()` internally (`controller.py:147-151`) and raises `TraversalStopped`.
8. `TraversalController.run` catches `TraversalStopped`, transitions to `stopped`, returns `False` (`controller.py:199-201`). The worker reports "stopped" and the slot terminates.

The docstring explicitly intends the opposite behavior:

```python
# main.py:81-82
RESTOCK_QUEUE_WAIT_TIMEOUT_SECONDS = 180.0
"""… on timeout the restock is skipped for this cycle and retried next cooldown."""
```

**Impact.** Under sustained cross-carrier queue contention (3+ tightly cycling carriers), a deferred restock times out after 180 s and the entire carrier slot halts mid-route. The user sees "Slot N stopped" with no indication that the cause was a restock deferral. Route progress is saved (`maybe_save_progress()` in the `finally` block, `main.py:1148-1150`), but the carrier will not continue.

This is a regression introduced by the well-intentioned fix for old bug 4. The old "unbounded wait" was replaced by a wait whose cancellation signal is the worker's kill switch.

**Evidence.**
- `main.py:694`: `cancel_event=effective_cancel_event` where `effective_cancel_event = runtime_context.cancel_event` (`:680`).
- `sequence_queue.py:69`: `self.cancel_event.set()` inside `cancel()`.
- `controller.py:147-151`: `runtime_context.wait` raises `TraversalStopped` when `cancel_event` is set.
- The timeout path returns normally from `_run_coordinated_restock`, but the next `runtime_context.wait(1)` at `main.py:1108` is guaranteed to raise.
- No code path in `runtime/controller.py` or `main.py` ever clears `cancel_event` once set.

**Test gap (Bug H).** `tests/test_multicarrier_jump_queue.py:839-873` (`test_coordinated_restock_skips_on_queue_timeout`) is green but does not exercise the bug. It constructs `cancel_event=threading.Event()` (a *fresh* event, `:862`) and `runtime_context=None` (`:863`), so the handle's `cancel_event` is disconnected from any runtime. The assertion `assert handle.cancelled is True` passes without ever observing the side effect on a runtime.

**Recommendation.** Pass a **dedicated** event to `submit_restock` — one that is *not* `runtime_context.cancel_event`. The queue worker already propagates cancellation to the running block via the event (the `runtime_context.raise_if_cancelled()` calls inside `restock_tritium`/`follow_button_sequence`), but for a *deferred* (never-started) restock, only the handle needs to be cancelled, not the worker. Minimal fix:

```python
# main.py, inside _run_coordinated_restock
restock_cancel_event = threading.Event()
# Chain user-initiated stop onto the restock event so a real Stop still aborts:
def _propagate():
    if runtime_context.cancel_event.wait(timeout=0):
        restock_cancel_event.set()
# (or wire via runtime_context.cancel_event as a one-way proxy)

handle = submit_restock(
    slot_id=queue_slot_id,
    run=lambda: restock_tritium(...),
    estimated_duration=estimate_restock_duration(options.tritium_slot),
    cancel_event=restock_cancel_event,   # ← not the runtime event
)
try:
    _ = handle.result(timeout=RESTOCK_QUEUE_WAIT_TIMEOUT_SECONDS)
except QueueTimeoutError:
    print("Restock deferred …")
    restock_cancel_event.set()           # only cancels this restock
    return
```

The fix must also add a regression test that injects `runtime_context.cancel_event` exactly as production does, and asserts the event is **not** set after the timeout fires.

---

## Bug B — Scheduled-jump `slot_id` collides with worker `slot_id` (MEDIUM)

> **✅ Verified 2026-06-17 — CONFIRMED, scope narrowed to Low–Med.** Confirmed: the scheduled-jump lambda submits under `slot_id=f"slot-{slot_index}"` (`dashboard.py:656`), the worker keys everything under `queue_slot_id = f"slot-{slot_id}"` (`main.py:786`), and `_submit` unconditionally does `self._registered_jump_deadlines.pop(slot_id, None)` for every jump submission (`sequence_queue.py:279`) — so a scheduled-jump fire deletes the worker's registered cooldown deadline. The schedule button and start button are both enabled in `READY` with no mutual gating (`dashboard.py:421-427` vs `:388-391`), confirming the described race is possible. **Caveat the report understates:** the worker only *registers* a deadline while it is RUNNING (cooldown loop), and the scheduled jump only submits when its countdown hits zero. So the collision requires a scheduled jump to fire *while an auto-plot worker is mid-cooldown on the very same carrier* — i.e. two independent jump clickers aimed at one game window, which is already a self-conflicting setup. The mechanism is real but the operational window is narrow; treat as Low–Med rather than Medium.

**Location:** `TraversalSystem/gui/dashboard.py:651-661`, `TraversalSystem/sequence_queue.py:278-279`

**Description.** The scheduled-jump integration uses `slot_id=f"slot-{slot_index}"` for its `submit_jump_plot` submission — the same key the worker uses for its own jump blocks and registered cooldown deadlines. The queue treats all jump blocks with the same `slot_id` as belonging to one slot, and `_submit` does:

```python
# sequence_queue.py:278-279 (inside _submit, kind == "jump_plot")
_ = self._registered_jump_deadlines.pop(slot_id, None)
```

So submitting a scheduled jump **deletes** whatever cooldown deadline the worker for that slot had registered via `register_jump_deadline` (`main.py:813-817`). Other carriers' restock feasibility checks no longer see this slot's pending jump, so a restock may be deemed "feasible" and overlap the slot's actual next jump.

**Mitigations already in place.** The schedule button is gated to `WorkerState.READY` (`dashboard.py:421-427`), so this can only fire when the worker is not running. **However**, after scheduling, the slot remains `READY` and `start_btn` stays enabled (`dashboard.py:388-391`). A user who schedules a jump at 14:00 UTC and then clicks Start will have two submitters racing on the same `slot_id`, with the scheduled submission silently deleting the worker's registered cooldown deadline.

**Impact.** Cross-carrier non-overlap (contract rule #5) is weakened for this slot. Other carriers may dispatch restocks into this slot's cooldown window because the gating deadline was deleted.

**Evidence.**
- `dashboard.py:656`: `slot_id=f"slot-{slot_index}"` in the `submit_func` lambda.
- `sequence_queue.py:279`: unconditional `pop(slot_id, None)` on every jump submission.
- `main.py:816`: worker registers deadline under the same `f"slot-{slot_id}"` key.
- `dashboard.py:421-427`: schedule button is enabled whenever state is `READY`, regardless of whether a schedule is already counting down.

**Recommendation.** Either (a) namespace the scheduled-jump `slot_id` (e.g. `f"slot-{slot_index}-scheduled"`) so it does not collide with worker keys, or (b) disable `start_btn` while a schedule is active for the same slot (mirror the `sj_schedule_btn` gating).

---

## Bug C — Scheduled-jump deadline does not gate restocks (MEDIUM)

> **✅ Verified 2026-06-17 — CONFIRMED.** `_tick` submits the deadline as `time.monotonic()` (`scheduled_jump.py:140-144`); `_earliest_jump_deadline_locked` filters with strict `block.handle.deadline > now` (`sequence_queue.py:385`), so by evaluation time the deadline is in the past and excluded from the gating set; `_select_next_locked` returns a feasible restock ahead of the pending jump (`:369-373`). Note the jump block is still *dispatched* (it stays in the unfiltered `jumps` sort at `:350-353`) — only its *gating* is lost. Additional observation: because an absent gating deadline makes any restock feasible (`_restock_is_feasible` returns `True` when `jump_deadline is None`, `:417-418`), even a single scheduled jump with one pending restock is delayed; cross-carrier contention is not required. Severity **Medium** stands.

**Location:** `TraversalSystem/gui/scheduled_jump.py:142`, `TraversalSystem/sequence_queue.py:377-390, 369-375`

**Description.** When the scheduled-jump fires through the queue, the deadline it submits is `time.monotonic()` — i.e. "now" (`scheduled_jump.py:142`). The earliest-jump-deadline computation that gates restock feasibility uses a strict `> now` filter:

```python
# sequence_queue.py:380-386
deadlines = [
    block.handle.deadline
    for block in self._pending
    if block.kind == "jump_plot"
       and block.handle.deadline is not None
       and block.handle.deadline > now    # ← strictly greater
]
```

By the time the queue worker evaluates, microseconds have elapsed, so the scheduled jump's deadline is in the past and is **excluded** from the gating set. Consequently `_restock_is_feasible` does not see it, and any other carrier's restock that is feasible against *future* jump deadlines may run first. `_select_next_locked` always prefers a feasible restock over an earliest jump (`sequence_queue.py:369-375`).

**Impact.** A scheduled jump set for 14:00:00 UTC can be delayed by up to one restock duration (~30–180 s, depending on `tritium_slot` and queue contention) if another carrier has a feasible restock pending. For Elite Dangerous carrier jumps — which the user is presumably timing to a specific game window — a 1–3 minute click delay may miss the intended jump slot.

The contract says restocking "must not overlap with a planned jump sequence." A scheduled jump set for "right now" is the most-planned jump of all, yet it is the one jump whose deadline does not gate restocks.

**Evidence.**
- `scheduled_jump.py:142`: `self._submit_func(self._make_fire_block(), time.monotonic(), self._cancel_event)`.
- `sequence_queue.py:385`: `block.handle.deadline > now`.
- `sequence_queue.py:369-375`: feasible restock wins over earliest jump.

**Recommendation.** Submit the scheduled-jump deadline as `time.monotonic() + SCHEDULED_JUMP_ESTIMATE_SECONDS` (5 s) instead of `time.monotonic()`. That keeps it first in the jump sort (still earliest) while making it strictly `> now` so it participates in the restock feasibility gate. Alternatively, special-case "deadline ≤ now" jumps in `_earliest_jump_deadline_locked` to count them as gating deadlines with value `now`.

---

## Bug D — Per-slot `Start` does not arm the first-cycle ordering barrier (LOW–MEDIUM)

> **✅ Verified 2026-06-17 — CONFIRMED (Low edge case).** `_arm_first_cycle_batch` is called only from `start_all_ready` (`worker_controller.py:325`); `start_slot` (`:239-311`) contains no barrier-arming call, and `_first_cycle_satisfied` defaults to `True` (`sequence_queue.py:103`) so jump blocks dispatch on arrival. The worked example holds. Worth stating plainly: a user manually starting slots in non-index order is *explicitly choosing* that order, and the documented multicarrier entry point ("Start All") is unaffected — so this is closer to "manual override behaves as manually ordered" than a contract violation. Low is the right altitude.

**Location:** `TraversalSystem/gui/worker_controller.py:313-346, 369-387, 239-311`

**Description.** `_arm_first_cycle_batch` is invoked exclusively from `start_all_ready` (`worker_controller.py:325`). The per-slot path `_on_start_slot` (`dashboard.py:695-698`) → `start_slot` (`worker_controller.py:239-311`) never arms the barrier.

Without the barrier, the queue dispatches whichever jump block arrives first while the queue is idle. `claim_first_cycle_deadline` still produces slot-ordered deadlines (`base + slot_index * 0.001`), but if only one block is pending at dispatch time the sort is moot — that block runs immediately regardless of its deadline.

Worked example: the user clicks Start on slot 1, then Start on slot 0. Slot 1's worker claims its deadline (`base + 0.001`), submits, and — queue idle — dispatches instantly. Slot 0's worker claims `base + 0.000` and submits a moment later, but slot 1 is already running. Slot 1 jumps first, slot 0 second. Contract rule #4 is violated.

**Impact.** Edge case. The "Start All" path — the documented multicarrier entry point — is unaffected (it arms the barrier). The bug only manifests when a user manually starts slots in non-index order via the per-slot buttons, and only on the first jump cycle (subsequent cycles are pinned by registered cooldown deadlines).

**Evidence.**
- `worker_controller.py:313-346`: `start_all_ready` is the only caller of `_arm_first_cycle_batch`.
- `worker_controller.py:239-311`: `start_slot` contains no barrier-arming call.
- `dashboard.py:695-698`: `_on_start_slot` calls `start_slot` directly.
- `sequence_queue.py:103`: `_first_cycle_satisfied` defaults to `True`, so the barrier is open by default and jump blocks dispatch on arrival.

**Recommendation.** If strict slot-index ordering is required regardless of entry point, lift `_arm_first_cycle_batch` into `start_slot` (counting currently-`READY` siblings). If the contract only covers the batch path, document the per-slot behavior as "submission order" and gate `start_btn` while siblings are still `READY` to make the ordering deterministic by construction.

---

## Bug E — Scheduled-jump time rollover is inconsistent (LOW)

> **✅ Verified 2026-06-17 — CONFIRMED.** `schedule()` rejects any same-day `target_dt <= now` (`scheduled_jump.py:100-104`) while `_build_target_datetime` rolls a past time forward by a day (`:180-184`). The inconsistency and the "can't schedule for tomorrow" UX consequence are exactly as described. Low.

**Location:** `TraversalSystem/gui/scheduled_jump.py:99-104, 174-185`

**Description.** `schedule()` refuses any target whose combined datetime is `<= now`:

```python
# scheduled_jump.py:100-104
target_dt = datetime.datetime.combine(now.date(), target_utc, tzinfo=datetime.timezone.utc)
if target_dt <= now:
    raise ValueError("Time is in the past")
```

But the per-tick helper `_build_target_datetime` rolls past times forward by a day:

```python
# scheduled_jump.py:180-184
target = datetime.datetime.combine(now.date(), self._target_time, tzinfo=datetime.timezone.utc)
if target < now:
    target += datetime.timedelta(days=1)
```

The two are inconsistent. If the user wants to schedule for 14:00 UTC tomorrow and the local "now" is 15:00 UTC today, `schedule()` raises "Time is in the past" — even though `_build_target_datetime` would have happily computed tomorrow's 14:00 UTC. The only way to schedule "tomorrow" is to wait until midnight UTC and then schedule for a time later in the day.

**Impact.** Minor UX surprise. Users attempting to schedule a jump more than ~24h in advance, or schedule "for tomorrow morning" in the evening, will see an unhelpful "Time is in the past" error.

**Recommendation.** Either (a) have `schedule()` use `_build_target_datetime` and only reject if the rolled-forward time is still `<= now` (impossible unless `target_utc` equals `now`'s time-of-day to the second), or (b) explicitly accept a `target_date` parameter and document the 24h-only window.

---

## Bug F — Integer-equality milestones on a drifting countdown (COSMETIC — partially refuted)

> **⚠️ Verified 2026-06-17 — PARTIALLY REFUTED. The original root cause is wrong and the claimed functional restock-trigger impact is unreachable.** The section has been rewritten below to reflect what the code actually does. Net residual issue: at most a single skipped *Discord embed field refresh* on the sub-300 cases after a slow restock. Downgraded from Low to Cosmetic.

**Location:** `TraversalSystem/main.py:1002-1031, 1046-1109`

**What the report got wrong.** The original claim was that `runtime_context.wait(1)` jitter (GC pressure, signal delivery, scheduling skew) can make `total_time` "skip a milestone." This is **incorrect.** `total_time` is a pure software counter: it is initialized and then mutated only by `total_time -= 1` (`main.py:1031, 1109`) and by the two explicit recalibrations after a restock (`:1086, 1104`). It is **not** derived from wall-clock time inside the loop, so no amount of `wait()` jitter can make it skip an integer on the linear descent — it visits **every** value from its start down to 0. The `match`/`==` matchers therefore fire reliably on the linear portion of both loops regardless of timing jitter. The cited `controller.py:139-157` "not wall-clock-accurate" property is real but irrelevant to whether `total_time` skips values.

**What is actually true (the residual, much smaller issue).** The only way `total_time` skips an integer is the post-restock recalibration:
- `total_time = max(0, int(cooldown_deadline - time.monotonic()))` (`:1086`), and
- `total_time = max(0, total_time - int(restock_elapsed))` (`:1104`).

Both execute **after** the `total_time == 300` restock trigger has already fired (they live inside the `if total_time == RESTOCK_TRIGGER_REMAINING_SECONDS:` block, `:1061`). After they run, `total_time` can jump downward by several seconds, which can step *over* a sub-300 Discord milestone case (`case 151`, `case 100` in the cooldown loop, `:1056-1059`). That — and only that — is the genuine defect: one skipped Discord embed refresh after a slow restock+confirmation. Purely cosmetic.

**Why the claimed restock-trigger miss is unreachable.** The report hedged that `total_time == 300` could be skipped "if the worker resumes from a save file directly into the cooldown phase." It cannot: the cooldown loop **unconditionally** sets `total_time = CARRIER_COOLDOWN_SECONDS` (362) at `main.py:1042` immediately before the `while` (`:1046`). There is no code path that enters that loop with any other starting value, and resume re-enters the per-route loop from the top (re-initializing `total_time`), so `300` is always hit on the linear descent before any recalibration. The restock trigger is robust.

**Evidence.**
- `main.py:1031, 1109`: `total_time -= 1` is the only in-loop counter mutation — deterministic, jitter-immune.
- `main.py:1042`: `total_time = CARRIER_COOLDOWN_SECONDS` unconditionally precedes the cooldown `while` at `:1046`; no mid-loop entry exists.
- `main.py:1061`: `== 300` trigger is reached before the recalibrations at `:1086, 1104`, which sit *inside* its block.
- `main.py:1056-1059`: sub-300 Discord cases are the only matchers a post-restock jump can step over.

**Recommendation (unchanged in spirit, scope reduced).** If the cosmetic Discord skip matters, drive the sub-300 milestone updates off `cooldown_deadline - time.monotonic()` ranges (or a "highest-milestone-not-yet-emitted" latch) rather than exact-integer `match` cases. The restock trigger needs no change.

---

## Bug G — Global input singletons (LOW–MEDIUM, documented)

> **✅ Verified 2026-06-17 — CONFIRMED (documented, no behavioral change).** The hazard comment is present verbatim at `input_handler.py:16-26`, the `_keyboard`/`_mouse` singletons at `:36-37`, and it correctly names the architectural fix (`SendInput`/`PostMessage` HWND, `xdotool --window`). `FocusGuard` narrows but does not close the focus-then-dispatch race. Accurate as written.

**Location:** `TraversalSystem/input_handler.py:16-37, 93-134`, `TraversalSystem/focus_input_handler.py:149-187`

**Description.** Same hazard class as the 2026-06-16 audit. `_keyboard = KeyboardController()` and `_mouse = MouseController()` (`input_handler.py:36-37`, Linux/macOS) and the module-level `pydirectinput` state on Windows both dispatch to whatever window is currently foreground. `FocusAwareInputHandler`'s `ensure_focus()` → `_backend.press(...)` pair is therefore not atomic across concurrent workers: another worker can steal focus in the gap. `FocusGuard` narrows but cannot close the window.

**Status.** The hazard is now explicitly documented in `input_handler.py:16-26`, including the architectural fix (window-targeted primitives: `SendInput`/`PostMessage` with HWND on Windows, `xdotool --window <wid>` on X11). No behavioral change.

**Impact.** Under concurrent multicarrier operation with focus contention, an input may land on the wrong carrier's window. `FocusGuard`'s blocking re-acquire makes this rare in practice but not impossible.

**Recommendation.** Unchanged from the prior audit: migrate dispatch to window-targeted primitives. Until then, the `FocusGuard` mitigation is the only line of defense and should be called out in operator-facing docs.

---

## What is wired up correctly

For completeness, the following parts of contract rules #1–#5 **are** correctly implemented in the current source and (where applicable) tested:

- **Thumbnail binding (rule #1):** `ManualBindDialog` (`dashboard.py:75-235`) captures per-window thumbnails on a background `_CaptureWorker` thread, emits the selected `WindowInfo` via `window_selected`, and `_on_manual_bind` (`dashboard.py:726-762`) routes it through `BindingController.manual_bind` (`binding_controller.py:414-491`), which writes only to in-memory `_runtime_bindings[fid]` (fail-closed, never persisted). Auto-bind and the FID-not-discovered safety guard are correct (`binding_controller.py:295-348, 472-480`).
- **Per-carrier toggles (rule #2):** `CarrierSlotConfig` carries independent `auto_plot_jumps`, `disable_refuel`, `route_file`, `tritium_slot`, `refuel_mode` per slot; these flow into `TraversalOptions` per worker via `WorkerController._build_options` (`worker_controller.py:577-597`).
- **Refuel ignored when `auto_jump` disabled (rule #3):** Guarded in both paths and unit-tested.
  - `restock_tritium`: `if not options.auto_plot_jumps or options.disable_refuel: return` (`main.py:350`).
  - `_run_coordinated_restock`: `if options.disable_refuel or not options.auto_plot_jumps: return` (`main.py:658`).
  - Test: `test_coordinated_restock_skipped_when_auto_plot_jumps_disabled` (`tests/test_multicarrier_jump_queue.py:819`).
- **First-cycle jump ordering via the queue (rule #4, batch path):** `claim_first_cycle_deadline` + `arm_first_cycle_barrier` produce a deterministic `(deadline, sequence)` sort that resolves to slot-index order once all siblings have arrived. Tests: `test_first_cycle_deadline_is_deterministic_slot_order`, `test_first_cycle_deadline_orders_concurrent_workers_by_slot`, `test_first_cycle_barrier_holds_head_block_until_siblings_arrive`, `test_first_cycle_barrier_releases_on_timeout_when_sibling_missing`.
- **Queue-level non-overlap (rule #5):** `SequenceQueue._restock_is_feasible` (`sequence_queue.py:412-419`) correctly checks `now + estimated_duration < earliest_jump_deadline`, and `_select_next_locked` defers a restock when the earliest pending jump deadline is too soon. Tests: `test_future_jump_deadline_allows_restock_to_run_before_pending_jump`, `test_soon_jump_deadline_defers_restock_until_after_pending_jump`. *Caveats: see Bug C for the scheduled-jump gap and Bug F for the integer-equality fragility.*
- **Restock skipped on the final jump:** `if idx + 1 < len(route_list):` guards `_run_coordinated_restock` (`main.py:1091`). Test: `test_traversal_slot_skips_restock_on_final_route_element`.
- **Restock estimate includes focus overhead:** `estimate_restock_duration` adds `RESTOCK_FOCUS_OVERHEAD_PER_INPUT_SECONDS * focus_inputs` (`main.py:88-94`). Test: `test_estimate_restock_duration_includes_focus_overhead`.
- **Jump confirmation timeout:** `JOURNAL_CONFIRMATION_TIMEOUT_SECONDS = 300` bounds the journal-confirmation wait inside the restock block (`main.py:1072-1082`); on timeout the slot saves progress and stops cleanly via `_handle_jump_cancelled`. Test: `test_journal_confirmation_timeout_stops_slot`.
- **Scheduled-jump queue integration (when queue is present):** `dashboard.py:651-663` constructs `submit_func` from the shared queue; `scheduled_jump.py:139-144` invokes it. The click block runs inside the queue's serialization invariant.
- **Queue serialization invariant:** At most one block runs at a time (`sequence_queue.py:295-314`). Confirmed by `test_two_carriers_serialize_jump_and_restock_through_queue`.

---

## Suggested fix priority

1. **Bug A** (restock timeout kills worker) — highest payoff and a real regression. Pass a dedicated cancel event to `submit_restock`; add a regression test that injects `runtime_context.cancel_event` exactly as production does and asserts the event is *not* set on timeout. **This also closes Bug H.**
2. **Bug B** (scheduled-jump `slot_id` collision) — namespace the scheduled-jump submission key or disable `start_btn` while a schedule is active.
3. **Bug C** (scheduled-jump deadline does not gate restocks) — submit the deadline as `time.monotonic() + SCHEDULED_JUMP_ESTIMATE_SECONDS`, or extend `_earliest_jump_deadline_locked` to count `<= now` jump deadlines as `now`.
4. **Bug D** (per-slot Start skips the barrier) — lift `_arm_first_cycle_batch` into `start_slot`, or document the per-slot path as submission-order and gate `start_btn` while siblings are `READY`.
5. **Bug G** (global input singletons) — architectural; migrate to window-targeted dispatch.
6. **Bug E** — UX hardening; align `schedule()` with `_build_target_datetime`.
7. **Bug F** — **cosmetic only after verification** (one possible skipped Discord field refresh after a slow restock; restock trigger is robust). Lowest priority; fold into a future cooldown-loop refactor if touched at all.

---

## Verification appendix (2026-06-17)

- **Source basis:** `TraversalSystem/` at the working tree for HEAD `b194972`. Lines cited above were read directly.
- **Bug A reproduction:** a standalone harness reproduced the exact wiring (`effective_cancel_event = runtime_context.cancel_event`; `submit_restock(cancel_event=…)`; `QueueTimeoutError` → `handle.cancel()`) and observed the worker's cancel event set to `True` after the timeout — confirming the regression.
- **Bug H:** `tests/test_multicarrier_jump_queue.py::test_coordinated_restock_skips_on_queue_timeout` re-run and confirmed green; inspection confirms it injects `runtime_context=None` + a fresh `threading.Event()` + a mock handle, so it cannot observe Bug A.
- **Net change vs. the analysis as filed:** A, C, D, E, G, H stand as written; **B** is confirmed but narrowed to Low–Med (requires a self-conflicting same-carrier scheduled-jump + auto-plot overlap); **F** is partially refuted — the stated jitter root cause is wrong and the restock-trigger impact is unreachable, leaving only a cosmetic Discord-refresh skip. No code was modified.
