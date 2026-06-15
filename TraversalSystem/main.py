from __future__ import annotations

# pyright: reportMissingImports=false, reportMissingModuleSource=false, reportImplicitRelativeImport=false

import datetime
import json
import os
import random
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Protocol, Tuple, cast
import urllib.error
import urllib.request

import pyautogui
import pyperclip
import pytz
import tzlocal

try:
    from .config import BASE_DIR, TraversalOptions, load_settings
    from .discordhandler import DiscordHandler
    from .multi_journal_router import CTSJournalFacade, MultiJournalRouter
    from .reshandler import Reshandler
    from .platform_utils import (
        get_screen_resolution,
        system_shutdown,
        IS_WINDOWS,
    )
    from . import input_handler
    from .runtime.controller import (
        TraversalController,
        TraversalRuntimeContext,
        TraversalStopped,
    )
    from .traversal_journal import JournalScanLoop
except ImportError:
    from config import BASE_DIR, TraversalOptions, load_settings  # type: ignore[reportMissingImports]
    from discordhandler import DiscordHandler  # type: ignore[reportMissingImports]
    from multi_journal_router import CTSJournalFacade, MultiJournalRouter  # type: ignore[reportMissingImports]
    from reshandler import Reshandler  # type: ignore[reportMissingImports]
    from platform_utils import (  # type: ignore[reportMissingImports]
        get_screen_resolution,
        system_shutdown,
        IS_WINDOWS,
    )
    import input_handler  # type: ignore[reportMissingImports]
    from runtime.controller import (  # type: ignore[reportMissingImports]
        TraversalController,
        TraversalRuntimeContext,
        TraversalStopped,
    )
    from traversal_journal import JournalScanLoop  # type: ignore[reportMissingImports]

# Get the screen resolution in a cross-platform manner
screen_width, screen_height = get_screen_resolution()
pyautogui.FAILSAFE = False

SEQUENCE_DIR = BASE_DIR / "sequences"
SAVE_PATH = BASE_DIR / "save.txt"
DEFAULT_JUMP_PLOT_ESTIMATE_SECONDS = 30.0
DEFAULT_RESTOCK_ESTIMATE_SECONDS = 60.0


def resolve_save_path(base_dir: Path, *, slot_id: int) -> Path:
    """Return the deterministic save path for a given GUI slot."""
    return base_dir / f"save-slot-{slot_id}.txt"


class InputHandlerAdapter(Protocol):
    def press(self, key: str) -> None: ...
    def keyDown(self, key: str) -> None: ...
    def keyUp(self, key: str) -> None: ...
    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
    ) -> None: ...
    def moveTo(self, x: int, y: int) -> None: ...


class SubmissionHandleAdapter(Protocol):
    def result(self, timeout: float | None = None) -> object: ...


def _resolve_input_handler(
    focus_handler: InputHandlerAdapter | None = None,
) -> InputHandlerAdapter:
    return focus_handler if focus_handler is not None else input_handler


def parse_version_tag(tag: str) -> int:
    cleaned_tag = tag.strip().lstrip("vV")
    prerelease = cleaned_tag.split("-", 1)[0]
    parts = prerelease.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"Invalid version tag: {tag}")
    return int("".join(parts))


GITHUB_REPO_OWNER = "congenial-acorn"
GITHUB_REPO_NAME = "CATS"
GITHUB_RELEASES_API = (
    f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases/latest"
)
LOCAL_VERSION_TAG = "v2.0.0-alpha.1"
LOCAL_VERSION = parse_version_tag(LOCAL_VERSION_TAG)
VERSION_CHECK_USER_AGENT = "CTS-Version-Check"


def fetch_latest_release_version() -> Tuple[int, str]:
    request = urllib.request.Request(
        GITHUB_RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": VERSION_CHECK_USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)

    tag_name = payload.get("tag_name")
    if not tag_name:
        raise ValueError("No tag_name in GitHub release response.")

    return parse_version_tag(tag_name), tag_name


def warn_if_outdated() -> None:
    try:
        latest_version, latest_tag = fetch_latest_release_version()
    except (urllib.error.URLError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"Version check skipped: {exc}")
        return
    except Exception as exc:  # safeguard against unexpected errors
        print(f"Version check skipped: {exc}")
        return

    if latest_version > LOCAL_VERSION:
        print(
            f"Update available. You are on {LOCAL_VERSION_TAG}, but the latest release is "
            f"{latest_tag}. Please download the newest version from GitHub. "
            f"https://github.com/congenial-acorn/CTS/releases/latest"
        )
        time.sleep(3)
    


@dataclass(slots=True)
class TraversalState:
    line_no: int = 0
    saved_resume: bool = False
    latest_journal: Path | None = None
    game_ready: bool = False
    stop_journal: threading.Event = field(default_factory=threading.Event)
    journal_thread: threading.Thread | None = None
    route_complete: bool = False
    slot_id: int | None = None


def slight_random_time(base: float) -> float:
    return random.random() + base


def _wait_for_duration(
    seconds: float,
    *,
    runtime_context: TraversalRuntimeContext | None = None,
    cancel_event: threading.Event | None = None,
    randomize: bool = True,
) -> None:
    delay = slight_random_time(seconds) if randomize else seconds
    if runtime_context is not None:
        runtime_context.wait(delay)
        runtime_context.raise_if_cancelled()
        return
    if cancel_event is not None:
        if cancel_event.wait(delay):
            raise TraversalStopped("Traversal cancelled.")
        return
    time.sleep(delay)


def load_route_list(route_file: Path) -> List[str]:
    if route_file.suffix.lower() == ".csv":
        return _load_carrier_csv(route_file)

    content = route_file.read_text(encoding="utf-8").strip()
    route = [line.strip() for line in content.splitlines() if line.strip()]
    if not route:
        raise ValueError("Route file is empty. Exiting...")
    return route


def _load_carrier_csv(route_file: Path) -> List[str]:
    def extract_names(rows: Iterable[str]) -> List[str]:
        systems: List[str] = []
        for row in rows:
            parts = row.split(",")
            if not parts:
                continue
            name = parts[0].strip().strip('"')
            if name and name.lower() != "system name":
                systems.append(name)
        return systems

    lines = route_file.read_text(encoding="utf-8").splitlines()
    route = extract_names(lines[1:])  # skip header if present
    if not route:
        raise ValueError("Route file is empty. Exiting...")
    return route


def _find_newest_journal(journal_dir: Path) -> Path:
    directory = journal_dir.expanduser()
    if not directory.is_dir():
        raise FileNotFoundError(f"Journal directory not found: {directory}")

    files = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.name.startswith("Journal")
    ]
    if not files:
        raise FileNotFoundError(f"No journal files found in {directory}")

    return max(files, key=lambda p: p.stat().st_mtime)


def follow_button_sequence(
    sequence_dir: Path,
    sequence_name: str,
    *,
    focus_handler: InputHandlerAdapter | None = None,
    runtime_context: TraversalRuntimeContext | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    handler = _resolve_input_handler(focus_handler)
    sequence_path = sequence_dir / sequence_name
    if sequence_path.suffix == "":
        sequence_path = sequence_path.with_suffix(".txt")

    if not sequence_path.exists():
        print(f"Sequence file missing: {sequence_path}")
        return

    for line in sequence_path.read_text(encoding="utf-8").splitlines():
        if runtime_context is not None:
            runtime_context.raise_if_cancelled()
        if ":" in line:
            key, duration = line.split(":", 1)
            handler.keyDown(key)
            try:
                _wait_for_duration(
                    float(duration),
                    runtime_context=runtime_context,
                    cancel_event=cancel_event,
                )
            finally:
                handler.keyUp(key)
        else:
            wait_time = 0.1
            key = line

            if "-" in line:
                key, wait_raw = line.split("-", 1)
                wait_time = float(wait_raw)

            handler.press(key)
            _wait_for_duration(
                wait_time,
                runtime_context=runtime_context,
                cancel_event=cancel_event,
            )


def restock_tritium(
    options: TraversalOptions,
    sequence_dir: Path,
    *,
    focus_handler: InputHandlerAdapter | None = None,
    runtime_context: TraversalRuntimeContext | None = None,
) -> None:
    handler = _resolve_input_handler(focus_handler)
    if runtime_context is not None:
        runtime_context.raise_if_cancelled()
    if not options.auto_plot_jumps or options.disable_refuel:
        return

    restock_order = ["restock_fc", "open_cargo_transfer", "restock_cargo"]

    for step in restock_order:
        if options.refuel_mode == 2 and (sequence_dir / "squadron" / f"{step}.txt").exists():
            follow_button_sequence(
                sequence_dir,
                f"squadron/{step}.txt",
                focus_handler=handler,
                runtime_context=runtime_context,
            )
        else:
            follow_button_sequence(
                sequence_dir,
                f"{step}.txt",
                focus_handler=handler,
                runtime_context=runtime_context,
            )

        if step == "open_cargo_transfer":
            if options.refuel_mode == 1:
                handler.press("w")
                _wait_for_duration(0.1, runtime_context=runtime_context)

            for _ in range(options.tritium_slot):
                if options.refuel_mode in (1, 2):
                    handler.press("s")
                else:
                    handler.press("w")
                _wait_for_duration(0.1, runtime_context=runtime_context)

    print("Refuel process completed.")


def jump_to_system(
    system_name: str,
    options: TraversalOptions,
    res_handler: Reshandler,
    journal: object,
    sequence_dir: Path,
    runtime_context: TraversalRuntimeContext | None = None,
    focus_handler: InputHandlerAdapter | None = None,
) -> Tuple[int, datetime.datetime | int]:
    handler = _resolve_input_handler(focus_handler)
    if runtime_context is not None:
        runtime_context.raise_if_cancelled()

    if not options.auto_plot_jumps:
        _prepare_manual_jump_plot(system_name)
        return _wait_for_manual_jump_confirmation(
            system_name,
            journal,
            runtime_context,
        )

    if options.refuel_mode == 2:
        follow_button_sequence(
            sequence_dir,
            "squadron/jump_nav_1.txt",
            focus_handler=handler,
            runtime_context=runtime_context,
        )
    else:
        follow_button_sequence(
            sequence_dir,
            "jump_nav_1.txt",
            focus_handler=handler,
            runtime_context=runtime_context,
        )

    if runtime_context is not None:
        runtime_context.raise_if_cancelled()

    handler.moveTo(res_handler.sysNameX, res_handler.sysNameUpperY)
    _wait_for_duration(0.1, runtime_context=runtime_context)
    handler.press("space")
    pyperclip.copy(system_name.lower())
    _wait_for_duration(1.0, runtime_context=runtime_context)
    handler.keyDown("ctrl")
    try:
        _wait_for_duration(0.1, runtime_context=runtime_context)
        handler.press("v")
        _wait_for_duration(0.1, runtime_context=runtime_context)
    finally:
        handler.keyUp("ctrl")
    _wait_for_duration(3.0, runtime_context=runtime_context)
    handler.moveTo(res_handler.sysNameX, res_handler.sysNameLowerY)
    _wait_for_duration(0.1, runtime_context=runtime_context)
    handler.press("space")
    _wait_for_duration(0.1, runtime_context=runtime_context)
    handler.moveTo(res_handler.jumpButtonX, res_handler.jumpButtonY)
    _wait_for_duration(0.1, runtime_context=runtime_context)
    handler.press("space")

    _wait_for_duration(6, runtime_context=runtime_context, randomize=False)

    facade = cast(CTSJournalFacade, journal)
    if facade.last_carrier_request() != system_name:
        print("Jump appears to have failed.")
        follow_button_sequence(
            sequence_dir,
            "jump_fail.txt",
            focus_handler=handler,
            runtime_context=runtime_context,
        )
        return 0, 0

    current_time = datetime.datetime.now(datetime.timezone.utc)
    departure_time_str = facade.departure_time()
    if not departure_time_str:
        return 0, 0
    departure_time = datetime.datetime.strptime(
        departure_time_str, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=pytz.UTC)

    delta = departure_time - current_time

    handler.press("backspace")
    _wait_for_duration(0.1, runtime_context=runtime_context)
    handler.press("backspace")

    return int(delta.total_seconds()), departure_time


def _prepare_manual_jump_plot(system_name: str) -> None:
    pyperclip.copy(system_name.lower())
    print(f"alert:Please plot the jump to {system_name}. It has been copied to your clipboard.")


def _wait_for_manual_jump_confirmation(
    system_name: str,
    journal: object,
    runtime_context: TraversalRuntimeContext | None,
) -> Tuple[int, datetime.datetime | int]:
    facade = cast(CTSJournalFacade, journal)
    while facade.last_carrier_request() != system_name:
        if runtime_context is not None:
            runtime_context.wait(1)
        else:
            time.sleep(1)

    current_time = datetime.datetime.now(datetime.timezone.utc)
    departure_time_str = facade.departure_time()
    if not departure_time_str:
        return 0, 0
    departure_time = datetime.datetime.strptime(
        departure_time_str, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=pytz.UTC)

    delta = departure_time - current_time

    return int(delta.total_seconds()), departure_time


def _run_coordinated_jump_plot(
    *,
    sequence_queue: object | None,
    queue_slot_id: str | None,
    system_name: str,
    options: TraversalOptions,
    res_handler: Reshandler,
    journal: object,
    sequence_dir: Path,
    runtime_context: TraversalRuntimeContext | None,
    focus_handler: InputHandlerAdapter | None,
    deadline: float | None = None,
) -> Tuple[int, datetime.datetime | int]:
    """Run a coordinated jump plot sequence through the queue.

    This helper serializes the jump plotting attempt to prevent input conflicts.
    Worker threads remain concurrent.
    Automation blocks (jump/restock) are serialized.
    Retries stay outside queue blocks.
    In manual mode, only clipboard and alert preparation are queued. The human/journal
    waiting phase runs outside the queue block.
    """
    if sequence_queue is None or queue_slot_id is None or runtime_context is None:
        return jump_to_system(
            system_name,
            options,
            res_handler,
            journal,
            sequence_dir,
            runtime_context,
            focus_handler=focus_handler,
        )

    submit_jump_plot = getattr(sequence_queue, "submit_jump_plot", None)
    if not callable(submit_jump_plot):
        return jump_to_system(
            system_name,
            options,
            res_handler,
            journal,
            sequence_dir,
            runtime_context,
            focus_handler=focus_handler,
        )

    effective_deadline = time.monotonic() if deadline is None else deadline

    if not options.auto_plot_jumps:
        handle = cast(
            SubmissionHandleAdapter,
            submit_jump_plot(
                slot_id=queue_slot_id,
                run=lambda: _prepare_manual_jump_plot(system_name),
                deadline=effective_deadline,
                estimated_duration=DEFAULT_JUMP_PLOT_ESTIMATE_SECONDS,
                cancel_event=runtime_context.cancel_event,
            ),
        )
        _ = handle.result()
        return _wait_for_manual_jump_confirmation(
            system_name,
            journal,
            runtime_context,
        )

    handle = cast(
        SubmissionHandleAdapter,
        submit_jump_plot(
            slot_id=queue_slot_id,
            run=lambda: jump_to_system(
                system_name,
                options,
                res_handler,
                journal,
                sequence_dir,
                runtime_context,
                focus_handler=focus_handler,
            ),
            deadline=effective_deadline,
            estimated_duration=DEFAULT_JUMP_PLOT_ESTIMATE_SECONDS,
            cancel_event=runtime_context.cancel_event,
        ),
    )
    return cast(Tuple[int, datetime.datetime | int], handle.result())


def save_progress(
    state: TraversalState,
    *,
    slot_id: int | None = None,
) -> None:
    effective = slot_id if slot_id is not None else state.slot_id
    if effective is not None:
        resolve_save_path(BASE_DIR, slot_id=effective).write_text(
            str(state.line_no), encoding="utf-8"
        )
    else:
        # Legacy CLI path: writes to global SAVE_PATH
        SAVE_PATH.write_text(str(state.line_no), encoding="utf-8")
    print("Progress saved...")


def consume_save(base_dir: Path, *, slot_id: int | None = None) -> int | None:
    """Read and delete a save file, returning the stored line_no or None."""
    if slot_id is not None:
        path = resolve_save_path(base_dir, slot_id=slot_id)
        if path.exists():
            value = int(path.read_text(encoding="utf-8"))
            path.unlink(missing_ok=True)
            return value
        return None
    if SAVE_PATH.exists():
        value = int(SAVE_PATH.read_text(encoding="utf-8"))
        SAVE_PATH.unlink(missing_ok=True)
        return value
    return None


def _register_jump_deadline(
    sequence_queue: object,
    *,
    slot_id: str,
    deadline: float,
) -> None:
    register = getattr(sequence_queue, "register_jump_deadline", None)
    if callable(register):
        _ = register(slot_id=slot_id, deadline=deadline)


def _clear_jump_deadline(sequence_queue: object, *, slot_id: str) -> None:
    clear = getattr(sequence_queue, "clear_jump_deadline", None)
    if callable(clear):
        _ = clear(slot_id=slot_id)


def _run_coordinated_restock(
    *,
    sequence_queue: object | None,
    queue_slot_id: str | None,
    cancel_event: threading.Event,
    runtime_context: TraversalRuntimeContext | None = None,
    options: TraversalOptions,
    sequence_dir: Path,
    focus_handler: InputHandlerAdapter | None,
) -> None:
    """Run a coordinated tritium restock sequence through the queue.

    This helper serializes the restock action to prevent input conflicts.
    Worker threads remain concurrent.
    Automation blocks (jump/restock) are serialized.
    Retries stay outside queue blocks.
    """
    if options.disable_refuel:
        return
    if sequence_queue is None or queue_slot_id is None:
        restock_tritium(
            options,
            sequence_dir,
            focus_handler=focus_handler,
            runtime_context=runtime_context,
        )
        return

    submit_restock = getattr(sequence_queue, "submit_restock", None)
    if not callable(submit_restock):
        restock_tritium(
            options,
            sequence_dir,
            focus_handler=focus_handler,
            runtime_context=runtime_context,
        )
        return

    effective_cancel_event = (
        runtime_context.cancel_event if runtime_context is not None else cancel_event
    )

    handle = cast(
        SubmissionHandleAdapter,
        submit_restock(
            slot_id=queue_slot_id,
            run=lambda: restock_tritium(
                options,
                sequence_dir,
                focus_handler=focus_handler,
                runtime_context=runtime_context,
            ),
            estimated_duration=DEFAULT_RESTOCK_ESTIMATE_SECONDS,
            cancel_event=effective_cancel_event,
        ),
    )
    _ = handle.result()


def handle_critical_error(
    message: str,
    state: TraversalState,
    options: TraversalOptions,
    discord_messenger: DiscordHandler,
    route_name: str,
) -> None:
    print(message)
    discord_messenger.post_to_discord(
        "Critical Error",
        options.webhook_url,
        route_name,
        "An error has occurred with the Flight Computer.",
        "It's possible the game has crashed, or servers were taken down.",
        "Please wait for the carrier to resume navigation.",
        "o7",
    )
    save_progress(state)
    raise RuntimeError("Critical error stopped carrier slot.")


def run_traversal(
    options: TraversalOptions | Mapping[str, object],
    *,
    journal: object = None,
    window: object = None,
    focus: object = None,
    cancel_event: threading.Event | None = None,
    status_callback=None,
    controller: TraversalController | None = None,
    slot_id: int | None = None,
) -> bool:
    runtime_controller = controller or TraversalController()
    return runtime_controller.run(
        _run_traversal_slot,
        options,
        journal=journal,
        window=window,
        focus=focus,
        cancel_event=cancel_event,
        status_callback=status_callback,
        slot_id=slot_id,
    )


def _run_traversal_slot(runtime_context: TraversalRuntimeContext) -> bool:
    options = runtime_context.options
    slot_id: int | None = cast(int | None, runtime_context.dependencies.slot_id)
    journal_dependency = runtime_context.dependencies.journal
    focus_dependency = cast(
        InputHandlerAdapter | None,
        runtime_context.dependencies.focus,
    )
    sequence_queue = runtime_context.dependencies.sequence_queue
    journal = cast(
        CTSJournalFacade,
        journal_dependency,
    )
    discord_messenger = DiscordHandler(single_message=options.single_discord_message)
    res_handler = Reshandler(screen_width, screen_height)

    if not res_handler.supported_res:
        print("Resolution not supported, exiting...")
        return False

    state = TraversalState(
        line_no=options.route_position,
        saved_resume=options.route_position > 0,
        slot_id=slot_id,
    )
    route_length = 0
    progress_saved = False
    registered_jump_deadline = False
    next_jump_plot_deadline: float | None = None
    cooldown_deadline: float | None = None
    queue_slot_id = f"slot-{slot_id}" if slot_id is not None else None

    def maybe_save_progress() -> None:
        nonlocal progress_saved
        if progress_saved:
            return
        if state.route_complete or route_length == 0:
            return
        if state.line_no >= route_length:
            return
        save_progress(state)
        progress_saved = True

    def clear_registered_jump_deadline() -> None:
        nonlocal registered_jump_deadline
        if not registered_jump_deadline:
            return
        if queue_slot_id is not None and sequence_queue is not None:
            _clear_jump_deadline(sequence_queue, slot_id=queue_slot_id)
        registered_jump_deadline = False

    def register_next_jump_deadline(seconds_until_due: float) -> None:
        nonlocal registered_jump_deadline, next_jump_plot_deadline
        clear_registered_jump_deadline()
        next_jump_plot_deadline = time.monotonic() + max(0.0, seconds_until_due)
        if queue_slot_id is None or sequence_queue is None:
            return
        _register_jump_deadline(
            sequence_queue,
            slot_id=queue_slot_id,
            deadline=next_jump_plot_deadline,
        )
        registered_jump_deadline = True

    def _handle_jump_cancelled(system_name: str, *, revert_index: bool) -> bool:
        nonlocal progress_saved
        clear_registered_jump_deadline()
        if revert_index:
            state.line_no -= 1
        print(f"\nJump to {system_name} was cancelled. Saving progress and stopping slot.")
        save_progress(state)
        progress_saved = True
        return False

    runtime_context.wait(5)

    try:
        try:
            route_list = load_route_list(options.route_file)
        except Exception as exc:
            print(exc)
            return False
        route_length = len(route_list)

        route_name = f"Carrier Updates: Route to {route_list[-1]}"
        print(f"Destination: {route_list[-1]}")

        restored = consume_save(BASE_DIR, slot_id=slot_id)
        if restored is not None:
            print("Save file found. Setting up...")
            state.line_no = restored
            state.saved_resume = True

        if state.line_no > len(route_list):
            print(
                "Configured starting position exceeds the route length. "
                "Starting at the end of the route."
            )
            state.line_no = len(route_list)

        try:
            journal_path = _find_newest_journal(options.journal_directory)
        except Exception as exc:
            print(exc)
            return False

        for countdown in range(5, 0, -1):
            print(f"Beginning in {countdown}...")
            runtime_context.wait(1)

        jumps_left = len(route_list) + 1 - state.line_no
        final_line = route_list[-1]

        delta = datetime.timedelta()
        current_time = datetime.datetime.fromtimestamp(
            time.mktime(time.localtime()), tzlocal.get_localzone()
        )

        for idx, system in enumerate(route_list):
            if idx < state.line_no:
                continue
            delta = delta + datetime.timedelta(seconds=1320)

        arrival_time = current_time + delta
        arrival_time_discord = (
            f"<t:{arrival_time.timestamp():.0f}:f> (<t:{arrival_time.timestamp():.0f}:R>)"
        )

        done_first = False
        for idx, system in enumerate(route_list):
            clear_registered_jump_deadline()
            total_time = 0
            if idx < state.line_no:
                continue
            jumps_left -= 1

            runtime_context.wait(3)
            runtime_context.raise_if_cancelled()
            journal.reset_cancel()

            print(f"Next stop: {system}")
            print("Beginning navigation.")
            print("Please do not change windows until navigation is complete.")
            print(f"ETA: {arrival_time.strftime('%A, %I:%M%p (UTC%z)')}")

            try:
                time_to_jump = 0
                departing_time: datetime.datetime | int = 0
                while time_to_jump == 0 or departing_time == 0:
                    runtime_context.raise_if_cancelled()
                    jump_plot_deadline = None
                    if options.auto_plot_jumps:
                        jump_plot_deadline = (
                            next_jump_plot_deadline
                            if next_jump_plot_deadline is not None
                            else time.monotonic()
                        )
                    time_to_jump, departing_time = _run_coordinated_jump_plot(
                        sequence_queue=sequence_queue,
                        queue_slot_id=queue_slot_id,
                        system_name=system,
                        options=options,
                        res_handler=res_handler,
                        journal=journal,
                        sequence_dir=SEQUENCE_DIR,
                        runtime_context=runtime_context,
                        focus_handler=focus_dependency,
                        deadline=jump_plot_deadline,
                    )
                assert isinstance(departing_time, datetime.datetime)
                next_jump_plot_deadline = None

                formatted_time = str(datetime.timedelta(seconds=time_to_jump))
                departure_time_discord = f"<t:{departing_time.timestamp():.0f}:R>"

                print(
                    f"Navigation complete. Jump occurs in {formatted_time}. Counting down..."
                )

                journal.reset_jump()

                total_time = max(0, time_to_jump - 6)

                if total_time > 900:
                    arrival_time = arrival_time + datetime.timedelta(
                        seconds=total_time - 900
                    )
                    arrival_time_discord = (
                        f"<t:{arrival_time.timestamp():.0f}:f> "
                        f"(<t:{arrival_time.timestamp():.0f}:R>)"
                    )

                if done_first:
                    previous_system = route_list[idx - 1]
                    discord_messenger.post_with_fields(
                        "Carrier Jump",
                        options.webhook_url,
                        route_name,
                        f"Jump to {previous_system} successful.",
                        f"The carrier is now jumping to the {system} system.",
                        f"Jumps remaining: {jumps_left}",
                        f"Next jump: {departure_time_discord}",
                        f"Estimated time of route completion: {arrival_time_discord}",
                        "o7",
                    )
                    _wait_for_duration(2, runtime_context=runtime_context, randomize=False)
                    discord_messenger.update_fields(0, 0)
                else:
                    if not state.saved_resume:
                        discord_messenger.post_with_fields(
                            "Flight Begun",
                            options.webhook_url,
                            route_name,
                            "The Flight Computer has begun navigating the Carrier.",
                            "The Carrier's route is as follows:",
                            "\n".join(route_list),
                            f"First jump: {departure_time_discord}",
                            f"Estimated time of route completion: {arrival_time_discord}",
                            "o7",
                        )
                        _wait_for_duration(2, runtime_context=runtime_context, randomize=False)
                        discord_messenger.update_fields(0, 0)
                    else:
                        discord_messenger.post_with_fields(
                            "Flight Resumed",
                            options.webhook_url,
                            route_name,
                            "The Flight Computer has resumed navigation.",
                            f"First jump: {departure_time_discord}",
                            f"Estimated time of route completion: {arrival_time_discord}",
                            "o7",
                        )
                        _wait_for_duration(2, runtime_context=runtime_context, randomize=False)
                        discord_messenger.update_fields(0, 0)

            except Exception as exc:
                clear_registered_jump_deadline()
                print(exc)
                handle_critical_error(
                    "An error has occurred with the Flight Computer.",
                    state,
                    options,
                    discord_messenger,
                    route_name,
                )

            while total_time > 0:
                runtime_context.transition("waiting")
                print(f"Jump in {total_time:>4}s", end="\r", flush=True)
                runtime_context.raise_if_cancelled()
                if journal.jump_cancelled():
                    return _handle_jump_cancelled(system, revert_index=False)
                runtime_context.wait(1)

                match total_time:
                    case 600:
                        discord_messenger.update_fields(1, 1)
                    case 200:
                        discord_messenger.update_fields(2, 2)
                    case 190:
                        discord_messenger.update_fields(2, 3)
                    case 144:
                        discord_messenger.update_fields(2, 4)
                    case 103:
                        discord_messenger.update_fields(2, 5)
                    case 90:
                        discord_messenger.update_fields(2, 6)
                    case 75:
                        discord_messenger.update_fields(2, 7)
                    case 60:
                        discord_messenger.update_fields(3, 7)
                    case 30:
                        discord_messenger.update_fields(4, 7)

                total_time -= 1
            print()
            runtime_context.transition("running")

            print("Jumping!")

            discord_messenger.update_fields(5, 7)

            state.line_no += 1

            print("Counting down until next jump...")
            total_time = 362
            cooldown_deadline = time.monotonic() + 362.0
            if idx + 1 < len(route_list):
                register_next_jump_deadline(total_time)
            while total_time > 0:
                runtime_context.transition("waiting")
                print(f"Next jump in {total_time:>4}s", end="\r", flush=True)

                match total_time:
                    case 340:
                        discord_messenger.update_fields(6, 7)
                    case 320:
                        discord_messenger.update_fields(7, 7)
                    case 300:
                        print("\nPausing execution until jump is confirmed...")
                        completed = False
                        while not completed:
                            runtime_context.transition("waiting")
                            runtime_context.raise_if_cancelled()
                            if journal.jump_cancelled():
                                return _handle_jump_cancelled(system, revert_index=True)
                            completed = journal.has_jumped()
                            if not completed:
                                print("Jump not complete...")
                                runtime_context.wait(10)
                        assert cooldown_deadline is not None
                        total_time = max(0, int(cooldown_deadline - time.monotonic()))
                        print("Jump complete!")
                        runtime_context.transition("running")
                        discord_messenger.update_fields(8, 7)
                        print("Submitting tritium restock to shared queue...")
                        clear_registered_jump_deadline()
                        restock_started_at = time.monotonic()
                        _run_coordinated_restock(
                            sequence_queue=sequence_queue,
                            queue_slot_id=queue_slot_id,
                            cancel_event=runtime_context.cancel_event,
                            runtime_context=runtime_context,
                            options=options,
                            sequence_dir=SEQUENCE_DIR,
                            focus_handler=focus_dependency,
                        )
                        restock_elapsed = time.monotonic() - restock_started_at
                        total_time = max(0, total_time - int(restock_elapsed))
                        if idx + 1 < len(route_list) and total_time > 0:
                            register_next_jump_deadline(total_time)
                    case 151:
                        discord_messenger.update_fields(8, 8)
                    case 100:
                        discord_messenger.update_fields(8, 9)

                runtime_context.wait(1)
                total_time -= 1
            print()
            clear_registered_jump_deadline()
            runtime_context.transition("running")
            discord_messenger.update_fields(9, 9)

            done_first = True

        state.route_complete = True
        print("Route complete!")
        discord_messenger.post_to_discord(
            "Carrier Arrived",
            options.webhook_url,
            route_name,
            f"The route is complete, and the carrier has arrived at {final_line}.",
            "o7",
        )
        if options.shutdown_on_complete:
            discord_messenger.post_to_discord(
                "Carrier Arrived",
                options.webhook_url,
                route_name,
                "Shutting down computer.",
                "o7",
            )
            print("Shutting down system in 30 seconds...")
            _wait_for_duration(5, runtime_context=runtime_context, randomize=False)
            system_shutdown(30)
        else:
            print("Shutdown on completion is disabled. Exiting without powering off.")
        return True
    except KeyboardInterrupt:
        print("\nTraversal interrupted. Saving progress before exiting...")
        maybe_save_progress()
        return False
    except TraversalStopped:
        print("\nTraversal cancelled. Saving progress before exiting...")
        maybe_save_progress()
        raise
    finally:
        clear_registered_jump_deadline()
        maybe_save_progress()


def main() -> None:
    print("Autopilot Script Online")
    print(f"Screen resolution: {screen_width}x{screen_height}")
    warn_if_outdated()

    try:
        options = load_settings()
    except Exception as exc:
        print(
            "There seems to be a problem with your settings files. "
            "Ensure settings.txt and settings.ini are present in the TraversalSystem directory."
        )
        print(exc)
        os._exit(1)

    # --- CLI preflight: require target_fid and validate via facade ---
    if not options.target_fid.strip():
        print(
            "Configuration error: target_fid is required for journal traversal. "
            "Set target-fid in your settings file."
        )
        os._exit(1)

    router = MultiJournalRouter()
    try:
        router.scan_once(options.journal_directory)
    except Exception as exc:
        print(f"Journal directory scan failed: {exc}")
        os._exit(1)

    facade = CTSJournalFacade(router, options.target_fid.strip())
    if facade.state() is None:
        print(
            f"Configuration error: target_fid {options.target_fid.strip()!r} "
            f"not found in journals under {options.journal_directory}"
        )
        os._exit(1)

    scan_loop = JournalScanLoop(router, options.journal_directory)
    scan_loop.start()
    try:
        if not run_traversal(options, journal=facade):
            os._exit(1)
    except Exception:
        raise
    finally:
        scan_loop.stop()

    os._exit(0)


if __name__ == "__main__":
    main()
