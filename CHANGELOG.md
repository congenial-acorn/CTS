# [2.0.0-alpha.1]
## Added
- Graphical interface for configuring and running carrier traversal — no more manual text file editing.
- Set up multiple carriers in separate slots, each with its own commander, route file, and refuel options.
- Carrier slots auto-detect which game client belongs to which commander when possible, with manual selection as a fallback.
- Start all carriers at once from one dashboard. Each carrier runs independently — one failing doesn't stop the others.
- Import your existing `settings.ini` to migrate to the new GUI config. Export a slot back to legacy format when needed.
- Elite Dangerous themed dark interface with orange and cyan accents.

## Changed
- GUI is now the primary way to configure and run CTS.
- Command-line interface is still available as a fallback if needed.

---

# [1.4.0]
## Added
- Linux compatibility via Proton/Wine support.
- New `platform_utils.py` module for cross-platform OS operations.
- New `input_handler.py` module for cross-platform keyboard/mouse input.
- Linux installation instructions in README.
- Linux journal directory path documentation.

## Changed
- Screen resolution detection now works on Linux (via xrandr) and Windows.
- Input handling now uses `pynput` on Linux, `pydirectinput` on Windows.
- Steam game launching now uses `xdg-open` on Linux, `os.startfile` on Windows.
- System shutdown command now supports both Linux and Windows.
- Process detection updated to handle Wine/Proton process names.
- Updated requirements.txt with platform-specific dependencies.

---

# [1.3.3]
## Added
- Added version check and update system.