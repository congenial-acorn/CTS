"""Cross-platform keyboard and mouse input abstraction.

Uses pydirectinput on Windows (for DirectInput game compatibility)
and pynput on Linux/macOS.
"""
from __future__ import annotations

# pyright: reportMissingImports=false, reportMissingModuleSource=false

import sys
import threading
import time
from typing import Optional

IS_WINDOWS = sys.platform == "win32"

# Dispatch here is process-global, not window-targeted. On Linux/macOS the
# module-level ``_keyboard``/``_mouse`` singletons emit to whichever window is
# currently foreground; on Windows, ``pydirectinput`` similarly targets the
# current foreground/global input state. That means
# ``FocusAwareInputHandler.ensure_focus()`` plus the subsequent keypress/click is
# not atomic across concurrent workers: another worker can steal focus after the
# focus check returns but before the input lands.
#
# ``dispatch_lock`` below closes that race for all dispatch routed through this
# process: ``FocusAwareInputHandler`` holds it across ``ensure_focus()`` + the
# primitive so the focus-then-act pair is atomic relative to every other
# lock-holding dispatcher, and no two workers interleave input. It is a
# re-entrant lock so a focus-gated primitive may call a raw primitive on the
# same thread without deadlocking. ``FocusGuard`` remains the per-binding focus
# acquirer; the lock serializes the critical section it guards.
#
# This serializes concurrent dispatch but is still not true window-targeted I/O:
# a fully robust fix would use window-targeted primitives (``SendInput``/
# ``PostMessage`` with HWND on Windows, ``xdotool --window <wid>`` on X11), which
# is deferred because it would require migrating recorded screen-absolute
# coordinates and is unverifiable for DirectInput games without hardware.
dispatch_lock = threading.RLock()
"""Process-wide re-entrant lock serializing focus-then-input critical sections
across concurrent workers (see module comment above and FocusAwareInputHandler)."""

if IS_WINDOWS:
    import pydirectinput
    # Disable pyautogui failsafe for pydirectinput as well
    pydirectinput.FAILSAFE = False
else:
    from pynput.keyboard import Key, Controller as KeyboardController
    from pynput.mouse import Button, Controller as MouseController
    
    _keyboard = KeyboardController()
    _mouse = MouseController()
    
    # Map common key names to pynput Key objects
    _SPECIAL_KEYS = {
        "space": Key.space,
        "enter": Key.enter,
        "return": Key.enter,
        "tab": Key.tab,
        "backspace": Key.backspace,
        "escape": Key.esc,
        "esc": Key.esc,
        "up": Key.up,
        "down": Key.down,
        "left": Key.left,
        "right": Key.right,
        "shift": Key.shift,
        "ctrl": Key.ctrl,
        "alt": Key.alt,
        "delete": Key.delete,
        "home": Key.home,
        "end": Key.end,
        "pageup": Key.page_up,
        "pagedown": Key.page_down,
        "insert": Key.insert,
        "f1": Key.f1,
        "f2": Key.f2,
        "f3": Key.f3,
        "f4": Key.f4,
        "f5": Key.f5,
        "f6": Key.f6,
        "f7": Key.f7,
        "f8": Key.f8,
        "f9": Key.f9,
        "f10": Key.f10,
        "f11": Key.f11,
        "f12": Key.f12,
        "capslock": Key.caps_lock,
        "numlock": Key.num_lock,
        "scrolllock": Key.scroll_lock,
        "printscreen": Key.print_screen,
        "pause": Key.pause,
        "win": Key.cmd,
        "command": Key.cmd,
        "menu": Key.menu,
    }


def _get_key(key: str):
    """Convert a key string to pynput key object (Linux/macOS only)."""
    key_lower = key.lower()
    if key_lower in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[key_lower]
    # For regular characters, return the character itself
    return key.lower()


def press(key: str) -> None:
    """Press and release a key."""
    if IS_WINDOWS:
        pydirectinput.press(key)
    else:
        k = _get_key(key)
        _keyboard.press(k)
        time.sleep(0.01)  # Small delay to ensure key registration
        _keyboard.release(k)


def keyDown(key: str) -> None:
    """Press and hold a key."""
    if IS_WINDOWS:
        pydirectinput.keyDown(key)
    else:
        k = _get_key(key)
        _keyboard.press(k)


def keyUp(key: str) -> None:
    """Release a key."""
    if IS_WINDOWS:
        pydirectinput.keyUp(key)
    else:
        k = _get_key(key)
        _keyboard.release(k)


def click(x: Optional[int] = None, y: Optional[int] = None, button: str = "left") -> None:
    """Click the mouse at the specified position or current position."""
    if IS_WINDOWS:
        import pyautogui
        if x is not None and y is not None:
            pyautogui.click(x, y)
        else:
            pyautogui.click()
    else:
        if x is not None and y is not None:
            _mouse.position = (x, y)
        btn = Button.left if button == "left" else Button.right
        _mouse.click(btn)


def moveTo(x: int, y: int) -> None:
    """Move the mouse to the specified position."""
    if IS_WINDOWS:
        import pyautogui
        pyautogui.moveTo(x, y)
    else:
        _mouse.position = (x, y)


def typewrite(text: str, interval: float = 0.0) -> None:
    """Type text character by character."""
    if IS_WINDOWS:
        pydirectinput.typewrite(text, interval=interval)
    else:
        for char in text:
            _keyboard.press(char)
            _keyboard.release(char)
            if interval > 0:
                time.sleep(interval)
