"""Platform abstraction utilities for cross-platform compatibility."""
from __future__ import annotations

import subprocess
import sys
from typing import Tuple

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"


def get_screen_resolution() -> Tuple[int, int]:
    """Get the primary screen resolution in a cross-platform manner."""
    if IS_WINDOWS:
        import ctypes
        user32 = ctypes.windll.user32
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    elif IS_LINUX:
        try:
            # Try using xrandr first (most reliable on X11)
            output = subprocess.check_output(
                ["xrandr", "--current"],
                stderr=subprocess.DEVNULL,
                text=True
            )
            for line in output.splitlines():
                if " connected primary" in line or (" connected" in line and "*" in output):
                    # Find the resolution in the next part or same line
                    parts = line.split()
                    for part in parts:
                        if "x" in part and part[0].isdigit():
                            res = part.split("+")[0]  # Remove position offset
                            w, h = res.split("x")
                            return int(w), int(h)
            # Fallback: look for line with asterisk (current mode)
            for line in output.splitlines():
                if "*" in line:
                    parts = line.split()
                    for part in parts:
                        if "x" in part and part[0].isdigit():
                            w, h = part.split("x")
                            return int(w), int(h.split("_")[0])  # Handle refresh rate suffix
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        try:
            # Try xdpyinfo as fallback
            output = subprocess.check_output(
                ["xdpyinfo"],
                stderr=subprocess.DEVNULL,
                text=True
            )
            for line in output.splitlines():
                if "dimensions:" in line:
                    # Format: "  dimensions:    1920x1080 pixels"
                    parts = line.split()
                    for part in parts:
                        if "x" in part and part[0].isdigit():
                            w, h = part.split("x")
                            return int(w), int(h)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        # Final fallback: use pyautogui (works on most systems)
        try:
            import pyautogui
            size = pyautogui.size()
            return size.width, size.height
        except Exception:
            pass
        
        # Default fallback
        print("Warning: Could not detect screen resolution, defaulting to 1920x1080")
        return 1920, 1080
    else:
        # macOS or other - use pyautogui
        try:
            import pyautogui
            size = pyautogui.size()
            return size.width, size.height
        except Exception:
            return 1920, 1080


def open_steam_game(app_id: str = "359320") -> None:
    """Open a Steam game by app ID in a cross-platform manner."""
    steam_url = f"steam://rungameid/{app_id}"
    
    if IS_WINDOWS:
        import os
        os.startfile(steam_url)
    elif IS_LINUX:
        try:
            subprocess.Popen(
                ["xdg-open", steam_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            # Fallback: try steam directly
            subprocess.Popen(
                ["steam", f"steam://rungameid/{app_id}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
    elif IS_MACOS:
        subprocess.Popen(
            ["open", steam_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )


def system_shutdown(delay_seconds: int = 30) -> None:
    """Initiate system shutdown in a cross-platform manner."""
    import os
    
    if IS_WINDOWS:
        os.system(f"shutdown /s /t {delay_seconds}")
    elif IS_LINUX:
        # Convert seconds to minutes for Linux shutdown command
        delay_minutes = max(1, delay_seconds // 60)
        os.system(f"shutdown -h +{delay_minutes}")
    elif IS_MACOS:
        delay_minutes = max(1, delay_seconds // 60)
        os.system(f"shutdown -h +{delay_minutes}")


def get_game_launcher_process_name() -> str:
    """Get the game launcher process name for the current platform."""
    if IS_WINDOWS:
        return "EDLaunch.exe"
    else:
        # On Linux via Proton/Wine, the process might have different names
        # Common patterns for Elite Dangerous under Proton
        return "EDLaunch.exe"  # Wine/Proton still uses .exe names


def get_game_process_names() -> list[str]:
    """Get possible game process names to look for when killing the launcher."""
    if IS_WINDOWS:
        return ["EDLaunch.exe"]
    else:
        # On Linux, check for both native wine process names and potential variants
        return ["EDLaunch.exe", "EDLaunch", "steam", "reaper"]
