# CTS (Carrier Traversal System)
The Traversal System is an Elite Dangerous fleet carrier auto-plotter, autojumper, and flight computer.

This is a refactored fork of [mck-9061/CATS](https://github.com/mck-9061/CATS). The majority of the code in this repository derives from the original.

## Traversal features
* Automatic jump plotting.
* Supports personal and squadron carriers, including Drake, Fortune, Victory, Nautilus, and Javelin class carriers.
* Tritium restocking workflows for personal and squadron refuel modes.
* Route time estimation and Discord webhook updates.
* Multi-carrier graphical workflow with dynamic commander binding.
* Adjusts for variable jump timers.
* Imports routes from plain text or Spansh fleet carrier router.
* Saves and resumes if interrupted while traveling.

## Limitations and Safety Guards
* **X11-only Linux automation limitation:** Window detection and keyboard/mouse command dispatch on Linux depend on X11 APIs. This means Wayland environments are not natively supported and automated traversal will fail to send game input.
* **Ambiguous fail-closed safety rules:** If multiple game clients are detected, or if active game window focus is lost, CTS defaults to a strict fail-closed safety state. The active worker thread immediately halts and transitions the slot to an error state, preventing inputs from leaking to other applications.
* **Game Automation Disclaimer:** Automated flight sequences are thoroughly validated using mock inputs and test suites. Real-world Elite Dangerous automation has not been verified on live player accounts beyond these isolated environments. Use this tool entirely at your own risk.
* Supports Windows natively and Linux (via Proton/Wine). macOS is untested.
* Odyssey is required; Horizons is not supported.
* Default game keybinds must be configured. Reset to default keyboard+mouse if you use custom binds, a controller, or HOTAS.
* Supported screen resolutions are listed in `resolutions.md`.

## Installation

### Windows
* **Release build (recommended):** Download the latest zip from GitHub Releases. Extract the archive and open the `TraversalSystem` directory.
* **From source:** Install Python 3.12+ and dependencies, then run `python TraversalSystem/gui_main.py` for the GUI or `python TraversalSystem/main.py` for the legacy CLI.

### Linux
* Install Python 3.10+ and required system packages:
  ```bash
  # Debian/Ubuntu
  sudo apt install python3 python3-pip python3-venv xdotool xclip

  # Arch Linux
  sudo pacman -S python python-pip xdotool xclip
  ```
* Clone the repository and configure the virtual environment:
  ```bash
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
  ```
* Start the application with: `python TraversalSystem/gui_main.py`

## GUI Configuration and Workflow

CTS now uses a GUI-first workflow as the primary setup and operating path. A graphical GUI configuration tool manages multiple accounts and universal settings.

### The Configuration File (`gui_config.json`)
The graphical application stores all configurations in `gui_config.json` at the root directory. This JSON file is the single authoritative source of truth. Its schema version is pinned to `1` to ensure compatibility.

### Universal Settings
These global settings apply across all slots:
* **journal_directory:** Path to your Elite Dangerous journal folder.
* **auto_detect_window:** Automatically matches game client window handles.
* **focus_timeout_seconds:** Seconds to wait for a game window to gain focus before halting (defaults to 5).
* **ambiguous_window_policy:** If multiple game windows are detected, this policy determines the action. Setting this to `abort` halts the system immediately. Specifying `manual` lets the user select the target window handle.
* **webhook_url:** Optional Discord webhook link for status updates.
* **single_discord_message:** Edits a single webhook message instead of creating new posts.
* **shutdown_on_complete:** Powers down the system after completing the route.

### Carrier Slots
You can configure multiple carriers in individual carrier slots (0-based indices). Each slot holds independent settings:
* **Frontier ID (FID):** The unique player identifier (e.g., F123456) extracted from game journals. Slots start as `unbound` and only become `ready` when the system discovers the FID in game logs.
* **Commander Name:** The player name associated with the slot.
* **Route File:** Path to a `.txt` or `.csv` route file.
* **Tritium Slot:** Cargo inventory position offset for refueling.
* **Refuel Mode:** Refueling behavior (0 for first 8 cargo items, 1 for items after the first 8, 2 for squadron carriers).
* **Toggles:** Turn individual slot refueling or automated jump plotting on or off.

### Binding Controller
Automated operations require binding a configuration slot to a live game client window.
* **Auto-binding:** The system scans game journals to discover active commanders and their FIDs. When it detects a unique matching game window, it automatically binds the slot and transitions it to `ready`.
* **Manual binding:** If automatic matching fails, you can trigger a manual binding. This opens a selector to bind a slot to a specific Frontier ID and active window handle. If the target FID was never seen in local journals, the slot stays `unbound` as a safety guard.

### Dashboard Start and Stop Controls
The dashboard panel coordinates active automation threads:
* **Start All:** Begins traversal loops for all enabled, `ready` slots concurrently. Each carrier executes on its own isolated background thread.
* **Stop All:** Signals all active workers to halt. Affected slot workers transition gracefully from `stopping` to `stopped`.
* **Slot Enablement:** Individual slot widgets feature a checkbox. Unchecking this excludes the carrier slot from the mass start command.

## Legacy Import and Export

You can migrate your existing files or share slot configurations using the legacy import/export features.
* **Legacy Import:** Select a legacy flat `settings.ini` to read its parameters. The tool creates a new `gui_config.json` with universal settings and places the legacy values into carrier slot 0. This is a one-way migration action, not a continuous synchronization.
* **Legacy Export:** Select a carrier slot index and export its parameters combined with universal settings to a legacy-compatible `settings.ini`. A comment line is prepended to the exported file to remind you that `gui_config.json` remains the authoritative source.

## Legacy CLI Workflow (Rollback Path)

If you need to rollback to the GUI-free command-line interface, you can still do so. The legacy entrypoint remains fully functional for backward compatibility.

### Legacy Configuration (`settings.ini`)
Create a flat `settings.ini` next to the executable. Fill in these properties:
* `webhook_url=` Discord webhook URL.
* `journal_directory=` Path to Elite Dangerous journals.
* `target-fid=` The Frontier ID to automate.
* `tritium_slot=` Tritium cargo slot offset.
* `route_file=` Path to your route file.
* `route_position=` Route starting offset.
* `auto-plot-jumps=` Set to `true` to let the system plot jumps automatically.
* `disable-refuel=` Set to `true` to skip refueling operations.
* `refuel-mode=` 0 for personal (first 8), 1 for personal (after 8), 2 for squadron.
* `single-discord-message=` Set to `true` to edit a single webhook message.
* `shutdown-on-complete=` Set to `true` to turn off the computer when finished.

### Starting the Legacy Route
* Dock with your carrier.
* Position your game cursor over the "Carrier Services" option.
* Ensure your internal panel (right) is on the home tab.
* Run `python TraversalSystem/main.py` or the legacy executable.
* Tab back to the Elite Dangerous window to allow automation input.

## Refueling Setup

Refueling must have options configured correctly to function. Use the guidelines below to set your `tritium_slot` and `refuel_mode` values (either in the GUI slot editor or your legacy `settings.ini`).

### Using a PERSONAL carrier
* Fill the carrier depot to full (1000 tritium).
* Choose a ship with at least 200 cargo capacity.
* Load your ship cargo hold with tritium from your carrier cargo.
* If your tritium is in the first 8 inventory items (accessible without scrolling down):
  * Set `refuel_mode` to 0.
  * Count the number of times you must press W to navigate from "Confirm Items Transfer" to that entry.
  * Set `tritium_slot` to that counted offset.
* If your tritium is not in the first 8 inventory items:
  * Set `refuel_mode` to 1.
  * Back out of the transfer menu, then enter it again.
  * Press W once, then count the number of times you must press S to navigate to that entry.
  * Set `tritium_slot` to that counted offset.

### Using a SQUADRON carrier
* Set `refuel_mode` to 2.
* Fill the carrier depot to full (1000 tritium).
* Choose a ship with at least 200 cargo capacity.
* Load your ship cargo hold with tritium.
* Open the squadron bank commodities section.
* Hover over the topmost commodity in the inventory list.
* Count the number of times you must press S to reach the tritium you want to use (0 if it is already at the top).
* Set `tritium_slot` to that counted offset.

## Route Setup

Acquire your jump sequence from the Spansh fleet carrier router or list your target systems on consecutive lines in a plain text file. Specify this route file path in your slot configuration or legacy configuration file.

Starting index 0 begins before the first system on the list. A value of 1 skips the first entry.

## Resuming the Route

If automated traversal is interrupted by an error or user cancellation, CTS writes a `save.txt` file recording your current index along the route. Reopening the system will read this file and resume your journey from the exact location. This saved index overrides any default starting value.

## Disclaimer and Legal

Frontier Dangerous terms of service strictly prohibit automated client automation. The developers take zero responsibility for any actions taken against your game account. Use this tool entirely at your own risk.

The source code is licensed under the MIT License. This software is not associated with or endorsed by Frontier Developments.