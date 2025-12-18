from __future__ import annotations

import ctypes
import datetime
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Tuple
import urllib.error
import urllib.request

import psutil
import pyautogui
import pydirectinput
import pyperclip
import pytz
import tzlocal

from config import BASE_DIR, TraversalOptions, load_settings
from discordhandler import DiscordHandler
from journalwatcher import JournalWatcher
from reshandler import Reshandler


user32 = ctypes.windll.user32
ctypes.windll.shcore.SetProcessDpiAwareness(2)

# Get the screen resolution
screen_width, screen_height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
pyautogui.FAILSAFE = False

SEQUENCE_DIR = BASE_DIR / "sequences"
SAVE_PATH = BASE_DIR / "save.txt"


def parse_version_tag(tag: str) -> int:
    cleaned_tag = tag.strip().lstrip("vV")
    parts = cleaned_tag.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"Invalid version tag: {tag}")
    return int("".join(parts))


GITHUB_REPO_OWNER = "congenial-acorn"
GITHUB_REPO_NAME = "CATS"
GITHUB_RELEASES_API = (
    f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases/latest"
)
LOCAL_VERSION_TAG = "v1.3.1"
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


def slight_random_time(base: float) -> float:
    return random.random() + base


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


def latest_journal_path(journal_dir: Path) -> Path:
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


def follow_button_sequence(sequence_dir: Path, sequence_name: str) -> None:
    sequence_path = sequence_dir / sequence_name
    if sequence_path.suffix == "":
        sequence_path = sequence_path.with_suffix(".txt")

    if not sequence_path.exists():
        print(f"Sequence file missing: {sequence_path}")
        return

    for line in sequence_path.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, duration = line.split(":", 1)
            pydirectinput.keyDown(key)
            time.sleep(slight_random_time(float(duration)))
            pydirectinput.keyUp(key)
        else:
            wait_time = 0.1
            key = line

            if "-" in line:
                key, wait_raw = line.split("-", 1)
                wait_time = float(wait_raw)

            pydirectinput.press(key)
            time.sleep(slight_random_time(wait_time))


def restock_tritium(options: TraversalOptions, sequence_dir: Path) -> None:
    if not options.auto_plot_jumps or options.disable_refuel:
        return

    restock_order = ["restock_fc", "open_cargo_transfer", "restock_cargo"]

    for step in restock_order:
        if options.refuel_mode == 2 and (sequence_dir / "squadron" / f"{step}.txt").exists():
            follow_button_sequence(sequence_dir, f"squadron/{step}.txt")
        else:
            follow_button_sequence(sequence_dir, f"{step}.txt")

        if step == "open_cargo_transfer":
            if options.refuel_mode == 1:
                pydirectinput.press("w")
                time.sleep(slight_random_time(0.1))

            for _ in range(options.tritium_slot):
                if options.refuel_mode in (1, 2):
                    pydirectinput.press("s")
                else:
                    pydirectinput.press("w")
                time.sleep(slight_random_time(0.1))

    print("Refuel process completed.")


def jump_to_system(
    system_name: str,
    options: TraversalOptions,
    res_handler: Reshandler,
    journal_watcher: JournalWatcher,
    sequence_dir: Path,
) -> Tuple[int, datetime.datetime]:
    if not options.auto_plot_jumps:
        pyperclip.copy(system_name.lower())
        print(f"alert:Please plot the jump to {system_name}. It has been copied to your clipboard.")
        while journal_watcher.last_carrier_request() != system_name:
            time.sleep(1)

        current_time = datetime.datetime.now(datetime.timezone.utc)
        departure_time_str = journal_watcher.departureTime
        departure_time = datetime.datetime.strptime(
            departure_time_str, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=pytz.UTC)

        delta = departure_time - current_time

        return int(delta.total_seconds()), departure_time

    if options.refuel_mode == 2:
        follow_button_sequence(sequence_dir, "squadron/jump_nav_1.txt")
    else:
        follow_button_sequence(sequence_dir, "jump_nav_1.txt")

    pyautogui.moveTo(res_handler.sysNameX, res_handler.sysNameUpperY)
    time.sleep(slight_random_time(0.1))
    pydirectinput.press("space")
    pyperclip.copy(system_name.lower())
    time.sleep(slight_random_time(1.0))
    pydirectinput.keyDown("ctrl")
    time.sleep(slight_random_time(0.1))
    pydirectinput.press("v")
    time.sleep(slight_random_time(0.1))
    pydirectinput.keyUp("ctrl")
    time.sleep(slight_random_time(3.0))
    pyautogui.moveTo(res_handler.sysNameX, res_handler.sysNameLowerY)
    time.sleep(slight_random_time(0.1))
    pydirectinput.press("space")
    time.sleep(slight_random_time(0.1))
    pyautogui.moveTo(res_handler.jumpButtonX, res_handler.jumpButtonY)
    time.sleep(slight_random_time(0.1))
    pydirectinput.press("space")

    time.sleep(6)

    if journal_watcher.last_carrier_request() != system_name:
        print("Jump appears to have failed.")
        follow_button_sequence(sequence_dir, "jump_fail.txt")
        return 0, 0

    current_time = datetime.datetime.now(datetime.timezone.utc)
    departure_time_str = journal_watcher.departureTime
    departure_time = datetime.datetime.strptime(
        departure_time_str, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=pytz.UTC)

    delta = departure_time - current_time

    pydirectinput.press("backspace")
    time.sleep(slight_random_time(0.1))
    pydirectinput.press("backspace")

    return int(delta.total_seconds()), departure_time


def save_progress(state: TraversalState) -> None:
    SAVE_PATH.write_text(str(state.line_no), encoding="utf-8")
    print("Progress saved...")


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
    os._exit(2)


def start_journal_thread(
    state: TraversalState,
    journal_watcher: JournalWatcher,
    journal_path: Path,
    options: TraversalOptions,
    discord_messenger: DiscordHandler,
    route_name: str,
) -> None:
    state.stop_journal.clear()
    state.latest_journal = journal_path

    def runner():
        while not state.stop_journal.is_set():
            ok = journal_watcher.process_journal(journal_path)
            if not ok:
                handle_critical_error(
                    "An error has occurred with the Flight Computer.",
                    state,
                    options,
                    discord_messenger,
                    route_name,
                )
                return
            time.sleep(1)
        print("Journal thread halted")

    state.journal_thread = threading.Thread(target=runner, daemon=True)
    state.journal_thread.start()


def open_game(
    state: TraversalState,
    options: TraversalOptions,
    res_handler: Reshandler,
    journal_watcher: JournalWatcher,
    discord_messenger: DiscordHandler,
    route_name: str,
) -> None:
    print("Re-opening game...")

    os.startfile("steam://rungameid/359320")
    time.sleep(60)

    journal_path = latest_journal_path(options.journal_directory)

    menu_loaded = False
    while not menu_loaded:
        content = journal_path.read_text(encoding="utf-8")
        if "Fileheader" in content:
            print("Menu loaded")
            menu_loaded = True
        else:
            print("Menu not loaded...")
            time.sleep(10)

    time.sleep(10)

    print("Starting game...")
    pyautogui.moveTo(res_handler.sysNameX, res_handler.sysNameLowerY)
    pyautogui.click()
    follow_button_sequence(SEQUENCE_DIR, "start_game.txt")

    loaded = False
    while not loaded:
        content = journal_path.read_text(encoding="utf-8")
        if "Location" in content:
            print("Game loaded")
            loaded = True
        else:
            print("Game not loaded...")
            pydirectinput.press("space")
            time.sleep(10)

    print("Switching to new journal...")
    journal_watcher.reset_all()
    new_journal = latest_journal_path(options.journal_directory)

    state.stop_journal.clear()
    start_journal_thread(
        state,
        journal_watcher,
        new_journal,
        options,
        discord_messenger,
        route_name,
    )

    state.game_ready = True


def run_traversal(options: TraversalOptions) -> bool:
    journal_watcher = JournalWatcher()
    discord_messenger = DiscordHandler(single_message=options.single_discord_message)
    res_handler = Reshandler(screen_width, screen_height)

    if not res_handler.supported_res:
        print("Resolution not supported, exiting...")
        return False

    state = TraversalState(
        line_no=options.route_position,
        saved_resume=options.route_position > 0,
    )
    route_length = 0
    progress_saved = False

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

    time.sleep(5)

    try:
        try:
            route_list = load_route_list(options.route_file)
        except Exception as exc:
            print(exc)
            return False
        route_length = len(route_list)

        route_name = f"Carrier Updates: Route to {route_list[-1]}"
        print(f"Destination: {route_list[-1]}")

        if SAVE_PATH.exists():
            print("Save file found. Setting up...")
            state.line_no = int(SAVE_PATH.read_text(encoding="utf-8"))
            state.saved_resume = True
            SAVE_PATH.unlink(missing_ok=True)

        if state.line_no > len(route_list):
            print(
                "Configured starting position exceeds the route length. "
                "Starting at the end of the route."
            )
            state.line_no = len(route_list)

        try:
            journal_path = latest_journal_path(options.journal_directory)
        except Exception as exc:
            print(exc)
            return False
        start_journal_thread(
            state,
            journal_watcher,
            journal_path,
            options,
            discord_messenger,
            route_name,
        )

        for countdown in range(5, 0, -1):
            print(f"Beginning in {countdown}...")
            time.sleep(1)

        jumps_left = len(route_list) + 1
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
            jumps_left -= 1
            if idx < state.line_no:
                continue

            time.sleep(3)

            print(f"Next stop: {system}")
            print("Beginning navigation.")
            print("Please do not change windows until navigation is complete.")
            print(f"ETA: {arrival_time.strftime('%A, %I:%M%p (UTC%z)')}")

            try:
                time_to_jump, departing_time = jump_to_system(
                    system, options, res_handler, journal_watcher, SEQUENCE_DIR
                )

                while time_to_jump == 0 or departing_time == 0:
                    time_to_jump, departing_time = jump_to_system(
                        system, options, res_handler, journal_watcher, SEQUENCE_DIR
                    )

                formatted_time = str(datetime.timedelta(seconds=time_to_jump))
                departure_time_discord = f"<t:{departing_time.timestamp():.0f}:R>"

                print(
                    f"Navigation complete. Jump occurs in {formatted_time}. Counting down..."
                )
                if options.power_saving:
                    print("Power saving mode is active. Closing game...")
                    state.stop_journal.set()
                    follow_button_sequence(SEQUENCE_DIR, "close_game.txt")
                    threading.Timer(
                        time_to_jump,
                        open_game,
                        args=(
                            state,
                            options,
                            res_handler,
                            journal_watcher,
                            discord_messenger,
                            route_name,
                        ),
                    ).start()
                    state.game_ready = False
                    print("Game open scheduled")
                    for proc in psutil.process_iter():
                        if proc.name() == "EDLaunch.exe":
                            proc.kill()
                    print("Launcher killed")

                journal_watcher.reset_jump()

                total_time = time_to_jump - 6

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
                    time.sleep(2)
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
                        time.sleep(2)
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
                        time.sleep(2)
                        discord_messenger.update_fields(0, 0)

            except Exception as exc:
                print(exc)
                handle_critical_error(
                    "An error has occurred with the Flight Computer.",
                    state,
                    options,
                    discord_messenger,
                    route_name,
                )

            while total_time > 0:
                print(f"Jump in {total_time:>4}s", end="\r", flush=True)
                time.sleep(1)

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

            print("Jumping!")

            discord_messenger.update_fields(5, 7)

            state.line_no += 1

            if system == final_line and options.power_saving:
                print("Counting down until jump finishes...")

                total_time = 60
                while total_time > 0:
                    print(total_time)
                    time.sleep(1)
                    total_time -= 1

                discord_messenger.update_fields(9, 9)
            else:
                print("Counting down until next jump...")
                total_time = 362
                while total_time > 0:
                    print(f"Next jump in {total_time:>4}s", end="\r", flush=True)

                    match total_time:
                        case 340:
                            discord_messenger.update_fields(6, 7)
                        case 320:
                            discord_messenger.update_fields(7, 7)
                        case 300:
                            if not options.power_saving:
                                print("\nPausing execution until jump is confirmed...")
                                completed = False
                                while not completed:
                                    completed = journal_watcher.get_jumped()
                                    if not completed:
                                        print("Jump not complete...")
                                        time.sleep(10)
                            else:
                                print("\nPausing execution until game is open and ready...")
                                while not state.game_ready:
                                    print("Game not ready...")
                                    time.sleep(10)
                                total_time = 152
                            print("Jump complete!")
                            discord_messenger.update_fields(8, 7)
                        case 151:
                            discord_messenger.update_fields(8, 8)
                        case 100:
                            discord_messenger.update_fields(8, 9)
                        case 150:
                            print("Restocking tritium...")
                            time.sleep(2)
                            threading.Thread(
                                target=restock_tritium,
                                args=(options, SEQUENCE_DIR),
                                daemon=True,
                            ).start()

                    time.sleep(1)
                    total_time -= 1
                print()
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
            f"Shutting down computer.",
            "o7",
        )
            print("Shutting down system in 30 seconds...")
            time.sleep(5)
            os.system("shutdown /s /t 30")
        else:
            print("Shutdown on completion is disabled. Exiting without powering off.")
        return True
    except KeyboardInterrupt:
        print("\nTraversal interrupted. Saving progress before exiting...")
        maybe_save_progress()
        return False
    finally:
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

    if not run_traversal(options):
        os._exit(1)

    os._exit(0)


if __name__ == "__main__":
    main()
