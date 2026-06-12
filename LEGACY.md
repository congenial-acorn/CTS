# Legacy Documentation

This document covers the legacy CLI workflow and migration paths for users transitioning from the older `settings.ini`-based configuration to the modern GUI workflow.

The primary interface is the GUI. See the [main README](README.md) for current documentation.

---

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
