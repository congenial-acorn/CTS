# Multi-Carrier Sequence Queue Design

## Problem

When multiple fleet carriers run concurrently, their automation phases (jump plotting and tritium restocking) can overlap. All carrier threads send keyboard/mouse input through the global `input_handler` module, which targets whichever window has OS focus — not a specific carrier's game client. Overlapping automation sends keystrokes to the wrong window.

### Root Causes

1. **No startup stagger**: `WorkerController.start_all_ready()` starts all carrier threads back-to-back with zero delay. All carriers hit `jump_to_system` at the same ~10 second mark.

2. **No ongoing coordination**: Each carrier's timing is driven by the game server's departure time, which is independent per carrier. Random drift causes automation windows to re-align after N jumps regardless of initial offset.

3. **Untargeted input dispatch**: `restock_tritium` and `jump_to_system` both call `follow_button_sequence` -> `input_handler.press()` directly. The `FocusAwareInputHandler` (which gates inputs per-window via `FocusGuard.ensure_focus()`) is constructed and injected as a dependency but never used by the traversal loop.

4. **Shared save file**: `SAVE_PATH = BASE_DIR / "save.txt"` is a single global file. All carrier threads read/write the same path, causing progress corruption.

5. **Unsynchronized restock thread**: Tritium restocking is spawned as a daemon thread (`main.py:774`) with no completion signal, lock, or coordination. It can bleed into the next cycle's automation.

## Game Mechanics

### Jump Cycle Timeline

```
Plot jump ────► jump countdown (~15-20 min) ────► JUMP ────► cooldown (~5 min) ────► Plot next jump
(automation)     (passive wait)                            (passive)               (automation)
```

### Restock Windows

Tritium restocking can occur in two windows, both with generous time budgets:

| Window | When | Duration | Notes |
|--------|------|----------|-------|
| **A** (preferred) | After jump, during cooldown | ~4:40 | After jump confirmation, before next plot |
| **B** | After plot, before jump | ~3:20 | Early in the jump countdown period |

Current code restocks at 150s remaining in the post-jump window — suboptimal because it wastes most of Window A.

### No Hard Deadline

Restocking has no hard deadline relative to the jump timer. The only constraint is that it happens within one of the two windows.

### Jump Plot Time Priority

Jump plotting is time-sensitive — it should execute immediately when the cooldown expires (or slightly before) to minimize total travel time. Every second of dead time between cooldown expiry and jump plotting adds directly to route duration.

## Design Decision: Deadline-Aware Sequence Queue

### Architecture

A shared queue that serializes all automation sequences across carriers. Carrier threads own their own timing loops but submit automation blocks to the queue instead of executing them directly.

```
Carrier thread 0:  ──[submit jump-plot]──wait──countdown──[submit restock]──wait──loop
Carrier thread 1:  ──[submit jump-plot]──wait──countdown──[submit restock]──wait──loop
                                    |
                                    v
Queue consumer:    [focus win0 -> plot] -> [focus win1 -> plot] -> ... -> [focus win0 -> restock] -> ...
```

### Sequence Block Granularity

Each block is a coarse logical operation — not individual keystrokes. The natural blocks:

| Block | Duration | Priority | When |
|-------|----------|----------|------|
| **Jump plot** | ~30s | **High** | Cooldown expires |
| **Restock** | ~30-60s | **Low** | After jump confirmation |

### Scheduling: Look-Ahead Feasibility Check

Before starting a restock, the queue consumer checks whether it would complete before the earliest pending jump-plot deadline:

```
if current_time + restock_estimate < next_jump_plot_deadline:
    run restock
else:
    wait for the jump-plot instead
```

This avoids the worst case of a jump-plot being delayed by an in-progress restock. The consumer needs visibility into all carriers' upcoming deadlines — carrier threads register their next jump-plot deadline in shared state that the consumer can read.

### Why Not a Timed Stagger

| Timed stagger | Sequence queue |
|---------------|---------------|
| Must estimate "safe" delay values | Self-adjusting — sequences take as long as they take |
| Drift over time as server departure times diverge | Immune to drift — serialization is structural |
| Overlap re-emerges after N jumps | Cannot overlap by construction |
| Doesn't handle sequences running longer than expected | Next sequence waits |

### What the Queue Consumer Does

1. Pop highest-priority pending item from the queue
2. `FocusGuard.ensure_focus()` targeting the correct carrier's game window
3. Execute the automation sequence
4. Signal completion back to the submitting carrier thread
5. Unblocking the carrier thread to continue its timing loop

### Existing Code That Supports This

- **`FocusAwareInputHandler`** (`focus_input_handler.py`): Already implements per-window focus gating on every input primitive. Currently constructed and injected but unused by the traversal loop.
- **`WindowBinding`** (`window_manager.py`): Per-slot window handle binding already exists.
- **`WorkerController`** (`gui/worker_controller.py`): Already coordinates per-slot threads and state machines.
- **`TraversalRuntimeContext`** (`runtime/controller.py`): Provides cancellable wait and status transitions — the carrier thread's timing loop infrastructure.

### What Needs To Change

- The traversal loop (`_run_traversal_slot` in `main.py`) must use the injected `FocusAwareInputHandler` instead of the bare `input_handler` module.
- Automation phases (`jump_to_system`, `restock_tritium`) must submit to the shared queue and block until completion, rather than executing directly.
- Restock timing should move from 150s remaining to right after jump confirmation (~300s remaining) to use Window A optimally.
- Jump-plot deadlines must be registered in shared state so the consumer can perform the look-ahead feasibility check.
- The shared `save.txt` path needs per-slot isolation.
