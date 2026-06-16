from __future__ import annotations

# pyright: reportImplicitRelativeImport=false

import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, cast, final

try:
    from ..config import TraversalOptions
except ImportError:  # pragma: no cover - script execution fallback
    from config import TraversalOptions  # type: ignore[reportMissingImports]


TraversalStatus = Literal[
    "starting",
    "running",
    "waiting",
    "error",
    "complete",
    "stopped",
]
TraversalCallable = Callable[["TraversalRuntimeContext"], bool]
StatusCallback = Callable[[TraversalStatus], None]


class TraversalStopped(RuntimeError):
    """Raised when traversal execution is cancelled."""


class InvalidTraversalOptions(TypeError):
    """Raised when a controller receives incompatible option values."""


def _as_int(value: object, default: int) -> int:
    if value is None:
        return default
    return int(cast(int | str, value))


def coerce_traversal_options(
    options: TraversalOptions | Mapping[str, object] | object,
) -> TraversalOptions:
    if isinstance(options, TraversalOptions):
        return options

    if not isinstance(options, Mapping):
        raise InvalidTraversalOptions(
            "Traversal options must be a TraversalOptions instance or mapping."
        )

    mapping = cast(Mapping[str, object], options)
    required = ("webhook_url", "journal_directory", "route_file")
    missing = [name for name in required if name not in mapping]
    if missing:
        raise InvalidTraversalOptions(
            f"Missing traversal option(s): {', '.join(missing)}"
        )

    return TraversalOptions(
        webhook_url=str(cast(str | object, mapping["webhook_url"])),
        journal_directory=Path(str(cast(str | Path | object, mapping["journal_directory"]))),
        route_file=Path(str(cast(str | Path | object, mapping["route_file"]))),
        route_position=_as_int(mapping.get("route_position"), 0),
        tritium_slot=_as_int(mapping.get("tritium_slot"), 0),
        auto_plot_jumps=bool(mapping.get("auto_plot_jumps", True)),
        disable_refuel=bool(mapping.get("disable_refuel", False)),
        refuel_mode=_as_int(mapping.get("refuel_mode"), 0),
        single_discord_message=bool(mapping.get("single_discord_message", False)),
        shutdown_on_complete=bool(mapping.get("shutdown_on_complete", True)),
        multi_commander_enabled=bool(mapping.get("multi_commander_enabled", False)),
        target_fid=str(mapping.get("target_fid", "")),
        auto_detect_window=bool(mapping.get("auto_detect_window", True)),
        focus_timeout_seconds=_as_int(mapping.get("focus_timeout_seconds"), 5),
        ambiguous_window_policy=str(mapping.get("ambiguous_window_policy", "abort")),
    )


@final
class TraversalRuntimeDependencies:
    __slots__ = ("journal", "window", "focus", "sequence_queue", "slot_id")

    def __init__(
        self,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        slot_id: int | None = None,
    ) -> None:
        self.journal = journal
        self.window = window
        self.focus = focus
        self.sequence_queue = sequence_queue
        self.slot_id = slot_id


@final
class TraversalRuntimeContext:
    __slots__ = (
        "options",
        "dependencies",
        "cancel_event",
        "_status_callback",
        "_sleep",
        "_last_status",
    )

    def __init__(
        self,
        *,
        options: TraversalOptions,
        dependencies: TraversalRuntimeDependencies,
        cancel_event: threading.Event,
        status_callback: StatusCallback | None,
        sleep: Callable[[float], None],
    ) -> None:
        self.options = options
        self.dependencies = dependencies
        self.cancel_event = cancel_event
        self._status_callback = status_callback
        self._sleep = sleep
        self._last_status: TraversalStatus | None = None

    def transition(self, status: TraversalStatus) -> None:
        if self._last_status == status:
            return
        self._last_status = status
        if self._status_callback is not None:
            self._status_callback(status)

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise TraversalStopped("Traversal cancelled.")

    def wait(self, seconds: float, *, poll_interval: float = 1.0) -> None:
        remaining = max(0.0, seconds)
        self.transition("waiting")
        if remaining == 0:
            self.raise_if_cancelled()
            return

        interval = poll_interval if poll_interval > 0 else 1.0
        while remaining > 0:
            self.raise_if_cancelled()
            step = min(interval, remaining)
            if self.cancel_event.wait(step):
                self.raise_if_cancelled()
            else:
                self._sleep(0)
            remaining -= step

        self.raise_if_cancelled()
        self.transition("running")


@final
class TraversalController:
    def __init__(self, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep

    def run(
        self,
        traversal_callable: TraversalCallable,
        options: TraversalOptions | Mapping[str, object] | object,
        *,
        journal: object = None,
        window: object = None,
        focus: object = None,
        sequence_queue: object = None,
        cancel_event: threading.Event | None = None,
        status_callback: StatusCallback | None = None,
        slot_id: int | None = None,
    ) -> bool:
        runtime_context = TraversalRuntimeContext(
            options=coerce_traversal_options(options),
            dependencies=TraversalRuntimeDependencies(
                journal=journal,
                window=window,
                focus=focus,
                sequence_queue=sequence_queue,
                slot_id=slot_id,
            ),
            cancel_event=cancel_event or threading.Event(),
            status_callback=status_callback,
            sleep=self._sleep,
        )

        runtime_context.transition("starting")

        try:
            runtime_context.raise_if_cancelled()
            runtime_context.transition("running")
            result = traversal_callable(runtime_context)
            runtime_context.raise_if_cancelled()
        except TraversalStopped:
            runtime_context.transition("stopped")
            return False
        except Exception:
            runtime_context.transition("error")
            return False

        runtime_context.transition("complete" if result else "error")
        return bool(result)
