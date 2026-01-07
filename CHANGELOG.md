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