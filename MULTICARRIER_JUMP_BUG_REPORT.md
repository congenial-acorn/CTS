# Multicarrier Jump Action Handling — Bug Report

**Codebase:** CTS — Carrier Traversal System (Elite Dangerous Fleet Carrier Auto-Plotter)
**Audit date:** 2026-06-16
**Scope:** Multicarrier jump sequencing, tritium refuel gating/timing, carrier-to-process binding, and cross-carrier coordination.
**Method:** Static analysis of production source under `TraversalSystem/` cross-referenced against the intended-behavior contract and the existing test suite under `tests/`.

---

## Intended Behavior (Contract)

Per the operator's description, the system should:

1. **Binding** — The user adds commanders/carriers and uses the **thumbnail selection** to bind each carrier to a game process/window.
2. **Per-carrier config** — The user adds a route for each carrier and can toggle per-carrier settings such as auto-fuel.
3. **Refuel gating** — **Tritium refuel must be ignored when `auto_jump` is disabled** for that carrier.
4. **Jump ordering** — When multicarriers are started, the **first carrier goes through the jump sequence, then the second, and so on**.
5. **Refuel timing / non-overlap** — The refueling sequence takes place sometime after the jump, but **not when it would overlap with a planned jump sequence**. It is triggered **5 minutes after the jump time** to match the jump cooldown optimally.

---

## Executive Summary

The refuel-gating rule (#3) and the queue-level non-overlap rule (#5, partial) are implemented correctly and are covered by tests. However, several real bugs and design deviations exist:

| # | Bug | Severity | Status |
|---|-----|----------|--------|
| 1 | Scheduled jump bypasses the SequenceQueue entirely | **High** | Confirmed |
| 2 | First-cycle carrier ordering is non-deterministic (not slot-index order) | **Medium** | Confirmed |
| 3 | Restock `estimated_duration` excludes focus-acquisition time → can overrun jump deadlines | **Medium** | Confirmed |
| 4 | `_run_coordinated_restock` blocks the worker with no timeout (liveness hazard) | **Medium** | Confirmed |
| 5 | "5 minutes after the jump" timing is actually ~62 s after the jump (5 min *remaining*, not *elapsed*) | **Medium** | Needs clarification |
| 6 | Global input singletons (`_keyboard`/`_mouse`) create a focus/dispatch race window | Low–Med | Confirmed (mitigated) |
| 7 | Restock runs even on the final jump of the route | Low | Confirmed |
| 8 | Hardcoded magic numbers (`362`, `300`) undocumented | Low | Confirmed |

---

## Independent Hostile Verification (2026-06-16)

A second, adversarial pass was performed against live source — reading each cited
location in full rather than trusting the report — to confirm, refute, or re-scope
every claim. All cited line numbers were checked and are accurate.

**Verdict summary:**

| # | Original verdict | Verification verdict | Adjustment |
|---|------------------|----------------------|------------|
| 1 | High / Confirmed | **Confirmed** — mechanism is real | Severity is **precondition-gated**: hazard only exists when a scheduled jump and live worker automation run at the same time. High *given* that overlap; otherwise dormant. |
| 2 | Medium / Confirmed | **Confirmed** — fully reproduced from source | None. Threads are *spawned* in slot order but the ~10 s startup wait + OS scheduling erase the head start; queue sort keys are both submission-order-derived. |
| 3 | Medium / Confirmed | **Confirmed in principle, magnitude overstated** | Direction is correct (focus/subprocess time is unmodeled). But "up to 5 s per call" is the *failure* timeout, and failure **raises `FocusError` (aborts the restock)** rather than silently overrunning. On Windows an already-foreground window returns in ~0 s. Real unmodeled cost is the **X11 per-input `xdotool --sync` subprocess** (tens of ms each), not seconds. Net: **Low–Medium**, materialises only under sustained cross-window focus contention. |
| 4 | Medium / Confirmed | **Confirmed** — but "indefinite" is worst-case | The unbounded `handle.result()` is real. Starvation requires other carriers to *continuously* hold a jump deadline within ~est (30–60 s) of now; with 362 s cooldowns that is a recurring but bounded window per cycle, so realistically a **multi-cycle stall**, not literal infinity. The wired `cancel_event` still unblocks it on user stop. Medium is fair. |
| 5 | Medium / Needs clarification | **Headline is wrong; downgrade to Low (docs/semantics)** | The "~62 s after the jump" figure ignores that `total_time == 300` **blocks on journal jump-confirmation** (`main.py:1002-1023`, up to 300 s). Actual restock fires at `T_jump + 62 s + confirmation_wait`, *not* a fixed 62 s. The report's own Impact paragraph misattributes the 62 s to the confirmation wait — but the 62 s of plain countdown elapses *before* that wait begins. The literal constant `300` = 5 min is almost certainly the intended "5 minutes (remaining on cooldown)". This is a **documentation/semantics** question, not a timing defect. |
| 6 | Low–Med / Confirmed | **Confirmed** | Module-global `_keyboard`/`_mouse` (non-Windows) and the global `pydirectinput` state (Windows) both dispatch to the foreground window; focus-then-dispatch is non-atomic across concurrent workers. Mitigated by `FocusGuard`. Accurate. |
| 7 | Low / Confirmed | **Confirmed** — minor wording correction | The `total_time == 300` block (incl. `_run_coordinated_restock`) is not guarded by `idx + 1 < len(route_list)`; only deadline re-registration is. Note the confirmation wait *should* run on the final jump (to confirm arrival) — only the restock call is unwanted. "Wasted tritium/cargo" is imprecise: restock *moves* tritium cargo→tank rather than destroying it; the real cost is ~30–60 s of automation + queue occupancy. |
| 8 | Low / Confirmed | **Confirmed** | `362` (`main.py:980-981`), `300` (`:998`), `1320` (`:816`) are bare literals. Note some sibling constants *are* named (`RESTOCK_*`, `JOURNAL_CONFIRMATION_TIMEOUT_SECONDS`), so the inconsistency is real. Accurate. |

**Net:** 6 of 8 claims confirmed as written (1, 2, 4, 6, 7, 8). Bug 3 is real but its
impact is narrower than stated. Bug 5's underlying semantics question is legitimate but
its headline quantification ("~62 s") is incorrect and it is really a docs issue. No
claim was found to be wholly false, and no new high-severity issue was uncovered in the
verified regions. Per-bug detail is appended to each section below as **Verification:**.

---

## Bug 1 — Scheduled jump bypasses the SequenceQueue (HIGH)

**Location:** `TraversalSystem/gui/scheduled_jump.py:120-131`, `TraversalSystem/gui/dashboard.py:616-671`

**Description.** The "Scheduled Jump" feature (per-slot UTC-timed auto-click) dispatches its click **directly through the global input handler**, completely ignoring the shared `SequenceQueue` that exists precisely to serialize jump-plot and restock input blocks across carriers.

```python
# scheduled_jump.py — default click target is the GLOBAL input handler, not the queue
self._click_func = click_func or (lambda x, y: _input_handler.click(x, y))  # line 45

def _tick(self):
    ...
    if delta <= 0:
        self._click_func(self._button_x, self._button_y)   # line 128 — fires raw
        self._cleanup()
```

A `grep` of `scheduled_jump.py` for `submit_jump_plot | submit_restock | SequenceQueue | sequence_queue` returns **zero matches** — the queue contract is never engaged.

**Impact.** When two or more carriers are running:
- Carrier A's worker is mid-jump-plot or mid-restock, holding the queue block and (via `FocusGuard`) focus on window A.
- Carrier B's scheduled-jump `QTimer` fires. It calls `FocusGuard(binding).ensure_focus()` (`scheduled_jump.py:138, 183`) to focus window B — **stealing focus from A** — then clicks.
- Carrier A's in-flight input sequence is now being sent to the wrong window, or its `FocusGuard` re-acquires focus mid-sequence, corrupting both carriers' automation.

This directly violates contract rule #5 ("refueling must not overlap with a planned jump sequence") and the queue's serialization invariant ("at most one automation block active at any time" — `tests/test_multicarrier_integration.py:1249`).

**Evidence.** `scheduled_jump.py` contains no queue references; `dashboard.py:_on_schedule_jump` constructs a `ScheduledJumpController` and calls `.schedule()` without any queue interaction.

**Recommendation.** Route the scheduled-jump click through `SequenceQueue.submit_jump_plot` (with the slot's `cancel_event` and a real deadline), or at minimum acquire a shared queue token before focusing/clicking, so the scheduled jump cannot interleave with an active automation block.

**Verification (CONFIRMED — precondition-gated).** `ScheduledJumpController` is constructed at `dashboard.py:648` with **no** `click_func` or `focus_func` injected, so it falls back to the global defaults: `lambda x, y: _input_handler.click(x, y)` (`scheduled_jump.py:44-46`) and `lambda binding: FocusGuard(binding).ensure_focus()` (`scheduled_jump.py:184`). The grep returns zero queue references, as reported. The focus steal is real: `_tick` calls `focus(self._binding)` at `:138` then `self._click_func(...)` at `:128`. **Caveat:** this is only hazardous when a scheduled jump and worker automation are *simultaneously* active — the scheduled jump is an independent manual feature requiring a configured button coordinate + UTC time. The "High" rating is correct *conditional on* that overlap; with no workers running, the scheduled click is harmless.

---

## Bug 2 — First-cycle carrier ordering is non-deterministic (MEDIUM)

**Location:** `TraversalSystem/gui/worker_controller.py:313-336` (`start_all_ready`), `TraversalSystem/main.py:846-863` (deadline assignment), `TraversalSystem/sequence_queue.py:256-280` (`_select_next_locked`)

**Description.** Contract rule #4 requires "the first carrier goes through the jump sequence, then the second, and so on." The implementation does **not** guarantee slot-index ordering on the first jump cycle:

1. `start_all_ready` spawns one `QThread` per ready slot in a tight loop (`worker_controller.py:316, 332`). All threads run concurrently.
2. Each thread waits ~10 s (`main.py:769` then the 5..1 countdown `main.py:801-803`), then submits its first jump plot.
3. For the **first** jump, `next_jump_plot_deadline` is `None` (`main.py:723`), so the submitted deadline falls back to `time.monotonic()` — i.e. "now" (`main.py:846-851`):
   ```python
   if options.auto_plot_jumps:
       jump_plot_deadline = (
           next_jump_plot_deadline
           if next_jump_plot_deadline is not None
           else time.monotonic()      # ← all carriers submit ~identical deadlines
       )
   ```
4. `SequenceQueue._select_next_locked` orders jumps by `(deadline, sequence)` (`sequence_queue.py:260-263`). With deadlines essentially equal, the tie-breaker is `sequence`, which is assigned in **submission order** (`sequence_queue.py:197-201`).
5. Submission order is determined by OS thread scheduling, which is **non-deterministic**.

**Impact.** On the first cycle, any carrier may win the race to submit first and thus jump first. Because subsequent-cycle deadlines propagate from the first cycle (each carrier re-registers `now + remaining_cooldown` after its restock), the first-cycle order is **sticky** — whichever carrier won the first cycle tends to stay ahead. The operator's expectation that slot 0 jumps first, then slot 1, etc., is not enforced.

**Caveat.** The existing test `test_concurrent_auto_jump_plot_serializes_queue_blocks` (`tests/test_multicarrier_jump_queue.py:505-595`) only proves *serialization* of the blocks; it rigs the order by starting thread 1, waiting for it to seize the queue, and only then starting thread 2. It does not (and cannot, given the design) assert slot-index ordering under a realistic concurrent `start_all_ready`.

**Recommendation.** If slot-index ordering is required for the first cycle, either (a) seed the first-cycle deadline with a slot-index offset (e.g. `time.monotonic() + slot_index * small_epsilon`) so the `(deadline, sequence)` sort resolves to slot order, or (b) have `start_all_ready` submit a deterministic pre-batch ordering token to the queue before spawning workers.

**Verification (CONFIRMED).** `start_all_ready` (`worker_controller.py:313-336`) iterates `sorted(self._records)` and calls `start_slot` in slot order, but each `start_slot` returns immediately after `thread.start()` (`:310`) — the workers then run concurrently. Each worker does `runtime_context.wait(5)` (`main.py:769`) plus a 5→1 countdown (`main.py:801-803`) ≈ 10 s before its first submission, so the microsecond-scale spawn ordering is fully washed out by the wait + OS scheduling. The first-cycle deadline falls back to `time.monotonic()` (`main.py:847-851`), and `_select_next_locked` sorts by `(deadline, sequence)` (`sequence_queue.py:260-263`) where `sequence` is assigned in submission order (`:197-201`) — both keys derive from whichever thread submits first. Non-determinism reproduced. The test caveat is accurate: `test_concurrent_auto_jump_plot_serializes_queue_blocks` rigs order by starting thread 1, waiting on `first_started`, *then* starting thread 2 (`test_multicarrier_jump_queue.py:567-574`); no test asserts slot-index ordering under a true concurrent start.

---

## Bug 3 — Restock estimate ignores focus-acquisition time (MEDIUM)

**Location:** `TraversalSystem/main.py:71-72` (`estimate_restock_duration`), `TraversalSystem/sequence_queue.py:317-324` (`_restock_is_feasible`), `TraversalSystem/focus_input_handler.py` (focus-per-input)

**Description.** The feasibility check that gates whether a restock may run uses an estimated duration:

```python
# main.py:71
RESTOCK_FIXED_OVERHEAD_SECONDS = 30.0
RESTOCK_PER_SLOT_SECONDS = 0.6
def estimate_restock_duration(tritium_slot: int) -> float:
    return RESTOCK_FIXED_OVERHEAD_SECONDS + RESTOCK_PER_SLOT_SECONDS * tritium_slot

# sequence_queue.py:317 — gates restock execution
def _restock_is_feasible(self, restock, jump_deadline):
    if jump_deadline is None:
        return True
    return self._time_fn() + restock.handle.estimated_duration < jump_deadline
```

But the actual restock runs through `FocusAwareInputHandler`, which calls `FocusGuard.ensure_focus()` **before every primitive input** (`focus_input_handler.py`). Focus acquisition can take up to `focus_timeout_seconds` (default 5 s) per call, and a restock issues many inputs (the `restock_fc` / `open_cargo_transfer` / `restock_cargo` sequences plus per-slot navigation). None of this focus overhead is included in `estimated_duration`.

**Impact.** The queue can deem a restock "feasible" (it fits before the earliest jump deadline by the estimate), begin executing it as an atomic block, and then the real elapsed time — inflated by repeated focus acquisition — **overruns the jump deadline it was supposed to avoid**. Once a block is running, the queue cannot preempt it (`sequence_queue.py:217` just calls `block.run()` and waits). The result is exactly the overlap contract rule #5 forbids: a planned jump sequence is delayed because an "infeasible" restock slipped through.

This is exacerbated in multicarrier mode where focus contention between windows is likely.

**Recommendation.** Either (a) inflate `estimate_restock_duration` with a per-input focus-time budget (e.g. `+ num_inputs * focus_timeout_seconds`), or (b) have the restock block periodically re-check feasibility / yield the queue if it detects it will overrun, so the queue can re-schedule.

**Verification (CONFIRMED IN PRINCIPLE — magnitude overstated).** The estimate (`30 + 0.6 * tritium_slot`, `main.py:71-72`) genuinely models none of the focus/dispatch overhead, and `FocusAwareInputHandler` does call `_ensure_focus()` before every primitive (`focus_input_handler.py:160-187`). **However**, the report's "up to 5 s per call" is the worst-case *failure* path, and inspecting `_focus_window` shows two corrections: (1) on win32, an already-foreground window returns at `window_manager.py:511-512` in ~0 s, so the steady single-foreground case adds negligible time; (2) when focus genuinely cannot be acquired, `_focus_window_*` **raises `FocusError`** (`window_manager.py:531, 560`), which aborts the restock rather than silently overrunning the deadline. The real *unmodeled* cost is the **X11 path** (`window_manager.py:537-558`), which spawns `xdotool windowactivate --sync` **unconditionally** on every input (no already-active short-circuit) — tens of ms each, not seconds. So an overrun-without-abort requires sustained cross-window focus contention repeatedly succeeding just under the timeout. Real but narrow → effective severity **Low–Medium**.

---

## Bug 4 — `_run_coordinated_restock` blocks the worker indefinitely with no timeout (MEDIUM)

**Location:** `TraversalSystem/main.py:631-645`, called from `main.py:1030-1038`

**Description.** When a carrier reaches the restock trigger point, it submits a restock block and blocks on the handle:

```python
# main.py:631
handle = cast(SubmissionHandleAdapter, submit_restock(
    slot_id=queue_slot_id,
    run=lambda: restock_tritium(...),
    estimated_duration=estimate_restock_duration(options.tritium_slot),
    cancel_event=effective_cancel_event,
))
_ = handle.result()    # ← blocks with NO timeout
```

The queue will only execute this restock when `_restock_is_feasible` returns true, i.e. when `now + estimated_duration < earliest_jump_deadline`. If other carriers continuously register imminent jump deadlines (their cooldowns landing just inside this carrier's restock window), this restock can remain pending indefinitely.

**Impact.** The carrier's worker thread is frozen inside the `if total_time == 300:` block (`main.py:998`). Its cooldown countdown loop is paused, no further jumps are plotted, no cancellation is checked except via the `cancel_event` wired into the submission, and Discord status goes stale. With 3+ tightly-cycling carriers this is a realistic liveness stall, not merely a theoretical one.

The blocking nature is acknowledged by `test_restock_queue_submission_blocks_until_queue_completion` (`tests/test_multicarrier_integration.py:731-738`), but no upper bound is enforced.

**Recommendation.** Bound the wait with `handle.result(timeout=...)` and on timeout either skip the restock for this cycle (logging a miss) or fall back to a direct (non-queued) restock if no other carrier is active.

**Verification (CONFIRMED — "indefinite" is worst-case).** `handle.result()` at `main.py:645` passes no timeout, confirmed. The starvation mechanism is real but bounded in practice: `_restock_is_feasible` (`sequence_queue.py:317-324`) only defers when `now + estimated_duration >= earliest_jump_deadline`, and `_prune_stale_registered_deadlines_locked` (`:307-315`) drops deadlines `<= now`. With 362 s cooldowns, each competing carrier only blocks this restock during the ~30–60 s window when its deadline sits within `est` of now — so a realistic outcome is a **multi-cycle stall**, not literal infinity. Mitigating factor the report already notes: `effective_cancel_event` (`main.py:627-642`) is wired, so a user stop unblocks the wait via `_prune_cancelled_pending_locked` (`sequence_queue.py:242-249`). Medium severity stands.

---

## Bug 5 — Restock trigger timing: "5 minutes after the jump" is actually ~62 s after (MEDIUM — needs clarification)

**Location:** `TraversalSystem/main.py:980-1042`

**Description.** Contract rule #5 states refueling "is triggered 5 minutes after the jump time." The code triggers it at a different instant:

```python
print("Jumping!")                              # line 973  — jump departs NOW (T_jump)
...
total_time = 362                               # line 980  — cooldown counter starts at 362
cooldown_deadline = time.monotonic() + 362.0   # line 981
...
while total_time > 0:                          # line 984  — counts DOWN: 362 → 0
    ...
    if total_time == 300:                      # line 998  — triggers when 300 REMAIN
        ...
        _run_coordinated_restock(...)          # line 1030
```

The counter decrements once per second, so `total_time == 300` is reached after `362 − 300 = 62` seconds. **The restock fires at `T_jump + 62 s`, not `T_jump + 300 s`.** The literal reading of contract rule #5 ("5 minutes after the jump time") expects `T_jump + 300 s`.

Two interpretations are possible, and they imply opposite verdicts:

| Interpretation | Expected trigger | Actual | Match? |
|---|---|---|---|
| "5 minutes *elapsed* since the jump" | `T_jump + 300 s` | `T_jump + 62 s` | **No — 238 s early** |
| "5 minutes *remaining* in the cooldown" | cooldown value `== 300` | `total_time == 300` | Yes |

The phrase "to match the jump cooldown optimally" leans toward the second reading (maximize the post-restock buffer before the next jump). But the literal wording "5 minutes after the jump time" matches the first.

**Impact.** If the operator means literal `T_jump + 5 min`, then every restock is firing ~4 minutes too early, leaving the carrier sitting idle after refuel for the remainder of the cooldown — and, more importantly, the restock is occurring while the just-jumped carrier may still be loading into the new system (the jump-confirmation wait at `main.py:1001-1023` is what occupies those first ~62 s, so in practice the restock begins immediately after confirmation, which is reasonable). If the operator means "5 min remaining," the code is correct.

**Recommendation.** Confirm the intended semantics. If literal elapsed-5-minutes is intended, change the trigger to a wall-clock check (`time.monotonic() - jump_departed_at >= 300`). If "5 min remaining" is intended, update the operator-facing docs/comment to say so explicitly, since "5 minutes after the jump time" currently misreads as elapsed time.

**Verification (HEADLINE INCORRECT — downgrade to Low / docs-semantics).** The "~62 s after the jump" claim does not survive reading the actual block. At `total_time == 300` the worker does **not** immediately restock — it enters a blocking confirmation loop (`main.py:1002-1023`) that waits for `journal.has_jumped()`, polling every 10 s up to `JOURNAL_CONFIRMATION_TIMEOUT_SECONDS` (300 s), and only *after* confirmation runs `_run_coordinated_restock` (`:1030`). So the restock fires at **`T_jump + 62 s + confirmation_wait`**, a variable instant dominated by journal timing — not a fixed 62 s. The report's Impact paragraph is self-contradictory here: it claims the confirmation wait "occupies those first ~62 s," but the code shows 62 s of plain `wait(1)` countdown elapsing (362→300) *before* the confirmation loop begins. Substantively, the literal trigger constant is `300` = 5 minutes, which strongly implies the intended semantics is "trigger when 5 minutes remain on the cooldown," matching "to match the jump cooldown optimally." This is a **documentation/wording** question, not a timing defect — reduce to **Low**.

---

## Bug 6 — Global input singletons create a focus/dispatch race window (LOW–MEDIUM)

**Location:** `TraversalSystem/input_handler.py:22-23` (module-level singletons), `TraversalSystem/focus_input_handler.py` (per-input `ensure_focus` then dispatch)

**Description.** Per-slot binding is correctly scoped: each worker gets its own `FocusAwareInputHandler` wrapping its own `FocusGuard(binding)` (`worker_controller.py:373`, `focus_input_handler.py:96-188`), and `BindingController._runtime_bindings` is keyed by FID (`binding_controller.py:178`). However, the underlying dispatch layer is global:

```python
# input_handler.py:22-23 — shared across ALL workers
_keyboard = KeyboardController()
_mouse = MouseController()
```

Every `FocusAwareInputHandler.press()/click()/...` does `ensure_focus()` (which focuses *this* slot's window) and then dispatches through these **shared** global controllers, which target *whatever window is currently foreground*.

**Impact.** Between a worker's `ensure_focus()` returning and its `_keyboard.press(...)` executing, another worker's `ensure_focus()` can re-steal focus to a different window. The first worker's keypress then lands on the wrong carrier's client. The blocking nature of `FocusGuard.ensure_focus()` (it loops until its window is foreground) makes this window narrow and rare in practice, but it is **not atomic** — the contract "carrier A's inputs go to carrier A's window" is not structurally guaranteed under concurrent workers.

This is the same class of hazard as Bug 1, but inherent to the input layer rather than the scheduled-jump feature.

**Recommendation.** For hard isolation, dispatch inputs via window-targeted primitives (e.g. `SendInput`/`PostMessage` with HWND on Windows, `xdotool --window` on X11) instead of foreground-directed global controllers. At minimum, document the focus-guard-based mitigation as the only line of defense.

**Verification (CONFIRMED — mitigated).** `_keyboard = KeyboardController()` / `_mouse = MouseController()` are module-level singletons on non-Windows (`input_handler.py:22-23`); on Windows the equivalent global state lives in the imported `pydirectinput` module (`:14-17`). Both dispatch to whatever window is foreground, so the `ensure_focus()` → dispatch pair in `FocusAwareInputHandler` is not atomic across concurrent workers. Same hazard class as Bug 1, inherent to the input layer. FocusGuard's blocking re-acquire narrows but does not close the window. Rating accurate.

---

## Bug 7 — Restock runs on the final jump of the route (LOW)

**Location:** `TraversalSystem/main.py:998-1042`

**Description.** The restock trigger at `total_time == 300` is inside the per-system loop and is not guarded by `idx + 1 < len(route_list)`. Only the *deadline re-registration* afterwards is so guarded (`main.py:1041`). Consequently, after the carrier's **last** jump, the full tritium restock sequence still executes, refueling a carrier that is about to declare "Route complete!" and (optionally) shut the system down (`main.py:1053-1072`).

**Impact.** Wasted tritium/cargo and ~30–60 s of unnecessary automation (plus queue occupancy that delays other carriers) right before route completion. Not harmful, but contrary to expectation.

**Recommendation.** Guard the restock with `if idx + 1 < len(route_list):` (skip on the final iteration), or gate it on whether a subsequent jump is actually planned.

**Verification (CONFIRMED — wording correction).** The `if total_time == 300:` block (`main.py:998`), which contains `_run_coordinated_restock` (`:1030`), is not guarded by `idx + 1 < len(route_list)`; only the initial and re-registration of the next deadline are (`:982`, `:1041`). So on the final route element the worker still confirms the jump and then restocks. Two refinements to the writeup: (1) the **confirmation wait itself should** run on the final jump — it verifies arrival before "Route complete!"; only the restock call is unwanted, so a fix must guard `_run_coordinated_restock`, not the whole block. (2) "Wasted tritium/cargo" is imprecise — a restock *transfers* tritium cargo→fuel tank rather than consuming it; the genuine cost is the ~30–60 s of automation and queue occupancy ahead of route completion. Low severity stands.

---

## Bug 8 — Undocumented magic numbers in the cooldown/restock timing (LOW)

**Location:** `TraversalSystem/main.py:980` (`total_time = 362`), `main.py:998` (`if total_time == 300:`), `main.py:816` (`seconds=1320`)

**Description.** The cooldown is hardcoded to `362` seconds (6 min 2 s) and the restock trigger to `300` seconds (5 min remaining). The arrival-time estimator adds `1320` seconds (22 min) per jump. None of these are named constants or documented:

- Why `362` rather than `360` (a clean 6 min)? Presumably a buffer, but it is silent.
- Why `300` for the restock trigger? (See Bug 5.)
- The `match total_time:` Discord-update cases at `340, 320, 151, 100, 90, ...` (`main.py:949-967, 988-996`) are equally opaque.

**Impact.** If Elite Dangerous's actual carrier-jump cooldown changes, or differs from the author's assumption, these silent constants will produce wrong timing with no obvious place to fix. They also make Bug 5 harder to reason about.

**Recommendation.** Extract named constants (`CARRIER_COOLDOWN_SECONDS`, `RESTOCK_TRIGGER_REMAINING_SECONDS`, `ESTIMATED_CYCLE_SECONDS`) with docstrings citing the game mechanic they model.

**Verification (CONFIRMED).** `total_time = 362` (`main.py:980`), `cooldown_deadline = time.monotonic() + 362.0` (`:981`), `if total_time == 300:` (`:998`), and `seconds=1320` (`:816`) are all bare literals with no naming or comment. The inconsistency is notable because sibling constants *are* named at the top of the module (`RESTOCK_FIXED_OVERHEAD_SECONDS`, `RESTOCK_PER_SLOT_SECONDS`, `JOURNAL_CONFIRMATION_TIMEOUT_SECONDS`, `main.py:64-68`) — so the pattern exists; these three just weren't extracted. The opaque Discord `match total_time:` cases (`:949-967`, `:988-996`) are likewise undocumented. Accurate, Low.

---

## What is wired up correctly

For completeness, the following parts of contract rules #1–#5 **are** correctly implemented and (where applicable) tested:

- **Thumbnail binding (rule #1):** `ManualBindDialog` (`dashboard.py:72-233`) captures per-window thumbnails via `_CaptureWorker`, emits the selected `WindowInfo`, and `BindingController.manual_bind` (`binding_controller.py:414-491`) stores it in `_runtime_bindings[fid]` (in-memory only, fail-closed). Auto-binding (`classify_slot`) and the FID-not-discovered safety guard (`binding_controller.py:475-480`) are correct.
- **Per-carrier toggles (rule #2):** `CarrierSlotConfig` (`gui_config.py:71-121`) carries independent `auto_plot_jumps`, `disable_refuel`, `route_file`, `tritium_slot`, `refuel_mode` per slot, all editable in `SlotEditorWidget` (`slot_editor.py`) and flowed into `TraversalOptions` per worker (`worker_controller.py:544-564`).
- **Refuel ignored when auto_jump disabled (rule #3):** The guard exists in **both** the direct path and the queue path, and is unit-tested:
  - `restock_tritium`: `if not options.auto_plot_jumps or options.disable_refuel: return` (`main.py:298`)
  - `_run_coordinated_restock`: `if options.disable_refuel or not options.auto_plot_jumps: return` (`main.py:606`)
  - Test: `test_coordinated_restock_skipped_when_auto_plot_jumps_disabled` (`tests/test_multicarrier_jump_queue.py:792-809`) asserts `submit_restock` is not called.
- **Queue-level non-overlap (rule #5, partial):** `SequenceQueue._restock_is_feasible` (`sequence_queue.py:317-324`) correctly checks `now + estimated_duration < earliest_jump_deadline`, and `_select_next_locked` (`sequence_queue.py:256-280`) defers a restock when the pending jump deadline is too soon. This is verified by `test_future_jump_deadline_allows_restock_to_run_before_pending_jump` and `test_soon_jump_deadline_defers_restock_until_after_pending_jump` (`tests/test_multicarrier_jump_queue.py:404-431`). *Caveat: see Bug 3 for why this check is leaky in practice.*
- **Queue serialization invariant:** At most one block runs at a time (`sequence_queue.py:206-226`), confirmed by `test_two_carriers_serialize_jump_and_restock_through_queue` (`tests/test_multicarrier_integration.py:1148-1265`, asserts `max_active == 1`).

---

## Suggested fix priority

1. **Bug 1** (scheduled jump queue bypass) — highest payoff, isolates a whole class of input conflicts.
2. **Bug 3** (restock estimate vs. focus time) — closes the leak in the otherwise-correct non-overlap gate.
3. **Bug 5** (restock trigger timing) — resolve the semantics question; one-line fix either way once confirmed.
4. **Bug 2** (first-cycle ordering) — small deterministic-deadline tweak if slot-index order is required.
5. **Bug 4** (unbounded restock wait) — add a timeout fallback.
6. **Bugs 6–8** — hardening, documentation, and cleanup.
