"""Cross-platform Elite Dangerous window discovery and focus enforcement.

This module provides a discovery and focus-management API for Elite
Dangerous windows on the current platform.

Backends:
  - **Windows**: uses ``ctypes`` / ``win32gui``-style enumeration via
    ``EnumWindows``.  Falls back gracefully when ``pywin32`` is absent.
    Focus uses ``SetForegroundWindow`` with the thread-input workaround.
  - **Linux/X11**: uses ``xdotool`` and ``xprop`` subprocess calls.
    Raises :class:`RuntimeError` with an actionable message when the
    required tools are missing.  Focus uses ``xdotool windowactivate
    --sync``.

Public API:
  - :func:`enumerate_elite_windows` – main entry point
  - :func:`diagnose` – dry-run diagnostic used by the smoke CLI
  - :class:`FocusGuard` – acquires and verifies window focus before
    automation input
  - :class:`FocusError` – raised when focus cannot be acquired
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Protocol, cast

from TraversalSystem.platform_utils import IS_LINUX, IS_WINDOWS


class FocusError(Exception):
    """Raised when window focus cannot be acquired or verified."""

# ---------------------------------------------------------------------------
# Title-matching heuristics
# ---------------------------------------------------------------------------

# Known Elite Dangerous window-title substrings.  Ordered from most
# specific to least so that the first match wins for *best* detection.
_ELITE_TITLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Elite\s*[-]\s*Dangerous", re.IGNORECASE),
    re.compile(r"ELITE\s*DANGEROUS", re.IGNORECASE),
    re.compile(r"Elite Dangerous", re.IGNORECASE),
]

# Process names (lowercase) that indicate an Elite Dangerous game client.
_ELITE_PROCESS_NAMES: set[str] = {
    "edlaunch.exe",
    "elitedangerous64.exe",
    "elitedangerous.exe",
    "elite - dangerous (client)",
}


def _title_looks_elite(title: str) -> bool:
    """Return *True* if *title* matches any known Elite Dangerous pattern."""
    return any(p.search(title) for p in _ELITE_TITLE_PATTERNS)


# ---------------------------------------------------------------------------
# Structured window record
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class WindowInfo:
    """A single candidate Elite Dangerous window."""

    #: Platform-specific window handle / ID (``HWND`` on Windows, decimal
    #: window-id on X11).
    handle: int
    #: Process ID of the owning process (``0`` if unavailable).
    pid: int
    #: Window title string.
    title: str
    #: Window class / WM_CLASS (empty string if unavailable).
    window_class: str
    #: Backend tag: ``"win32"`` or ``"x11"``.
    backend: str
    #: Whether the window is believed to be focusable (visible and not
    #: minimised).  Determined heuristically; does **not** attempt to
    #: acquire focus.
    focusable: bool

    def as_dict(self) -> dict[str, int | str | bool]:
        """Return a plain-dict representation for serialisation."""
        return {
            "handle": self.handle,
            "pid": self.pid,
            "title": self.title,
            "window_class": self.window_class,
            "backend": self.backend,
            "focusable": self.focusable,
        }


@dataclass(slots=True)
class WindowBinding:
    target_fid: str
    startup_identity: str
    handle: int
    pid: int
    title: str
    window_class: str
    backend: str

    @classmethod
    def from_window(
        cls,
        *,
        target_fid: str,
        startup_identity: str,
        window: WindowInfo,
    ) -> "WindowBinding":
        return cls(
            target_fid=target_fid,
            startup_identity=startup_identity,
            handle=window.handle,
            pid=window.pid,
            title=window.title,
            window_class=window.window_class,
            backend=window.backend,
        )


class _Win32GuiModule(Protocol):
    def IsWindowVisible(self, hwnd: int) -> bool: ...
    def GetWindowText(self, hwnd: int) -> str: ...
    def GetClassName(self, hwnd: int) -> str | None: ...
    def IsWindowEnabled(self, hwnd: int) -> bool: ...
    def EnumWindows(
        self,
        callback: Callable[[int, object], bool],
        ctx: object,
    ) -> None: ...


class _Win32ProcessModule(Protocol):
    def GetWindowThreadProcessId(self, hwnd: int) -> tuple[int, int]: ...


class _User32Module(Protocol):
    def IsWindowVisible(self, hwnd: int) -> int: ...
    def GetWindowTextLengthW(self, hwnd: int) -> int: ...
    def GetWindowTextW(self, hwnd: int, buffer: object, length: int) -> None: ...
    def GetWindowThreadProcessId(self, hwnd: int, pid_ref: object) -> None: ...
    def IsWindowEnabled(self, hwnd: int) -> int: ...
    def EnumWindows(self, callback: object, lparam: int) -> None: ...


class _ValueBuffer(Protocol):
    value: str


def _title_specificity(title: str) -> int:
    lowered = title.casefold()
    if "(client)" in lowered or "(publicclient)" in lowered:
        return 3
    if "elite - dangerous" in lowered:
        return 2
    if "elite dangerous" in lowered:
        return 1
    return 0


def _class_specificity(window_class: str) -> int:
    lowered = window_class.casefold()
    if not lowered:
        return 0
    if "elite" in lowered:
        return 3
    if "steam_app_359320" in lowered:
        return 2
    if "wine" in lowered or "proton" in lowered:
        return 1
    return 0


def _window_binding_score(window: WindowInfo) -> tuple[int, int, int, int]:
    return (
        1 if window.focusable else 0,
        _title_specificity(window.title),
        _class_specificity(window.window_class),
        1 if window.pid > 0 else 0,
    )


def _choose_unique_window_candidate(candidates: list[WindowInfo]) -> WindowInfo | None:
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None

    ranked = sorted(
        candidates,
        key=lambda window: (_window_binding_score(window), window.handle),
        reverse=True,
    )
    best = ranked[0]
    if len(ranked) > 1 and _window_binding_score(ranked[1]) == _window_binding_score(best):
        return None
    return best


class WindowBindingCoordinator:
    def __init__(
        self,
        discover_windows: Callable[[], list[WindowInfo]] | None = None,
    ) -> None:
        self._discover_windows: Callable[[], list[WindowInfo]] = (
            discover_windows or enumerate_elite_windows
        )
        self._bindings: dict[str, WindowBinding] = {}

    def get_live_binding(
        self,
        *,
        target_fid: str,
        startup_identity: str,
    ) -> WindowBinding | None:
        binding = self._bindings.get(target_fid)
        if binding is None:
            return None

        live_windows = self._discover_windows()
        if self._binding_is_live(binding, live_windows, startup_identity):
            return binding

        _ = self._bindings.pop(target_fid, None)
        return None

    def resolve_binding(
        self,
        *,
        target_fid: str,
        startup_identity: str,
        ambiguous_window_policy: str = "abort",
    ) -> WindowBinding | None:
        existing = self.get_live_binding(
            target_fid=target_fid,
            startup_identity=startup_identity,
        )
        if existing is not None:
            return existing

        live_windows = self._discover_windows()
        chosen = _choose_unique_window_candidate(live_windows)
        if chosen is None:
            if ambiguous_window_policy == "abort":
                return None
            raise RuntimeError(
                f"Unsupported ambiguous window policy: {ambiguous_window_policy}"
            )

        binding = WindowBinding.from_window(
            target_fid=target_fid,
            startup_identity=startup_identity,
            window=chosen,
        )
        self._bindings[target_fid] = binding
        return binding

    def invalidate_binding(self, target_fid: str) -> None:
        _ = self._bindings.pop(target_fid, None)

    @staticmethod
    def _binding_is_live(
        binding: WindowBinding,
        live_windows: list[WindowInfo],
        startup_identity: str,
    ) -> bool:
        if binding.startup_identity != startup_identity:
            return False

        for window in live_windows:
            if window.handle != binding.handle or window.backend != binding.backend:
                continue
            if window.pid != binding.pid:
                return False
            return True
        return False


# ---------------------------------------------------------------------------
# Windows backend
# ---------------------------------------------------------------------------

def _enumerate_windows_win32() -> list[WindowInfo]:
    """Enumerate candidate Elite windows on Windows via ``ctypes`` / ``win32gui``.

    Uses ``ctypes`` as the primary path so that ``pywin32`` is optional.
    When ``win32gui`` is available it is preferred for richer metadata.
    """
    import importlib

    results: list[WindowInfo] = []

    try:
        win32gui = cast(
            _Win32GuiModule,
            cast(object, importlib.import_module("win32gui")),
        )
        win32process = cast(
            _Win32ProcessModule,
            cast(object, importlib.import_module("win32process")),
        )

        def _cb(hwnd: int, _ctx: object) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title or not _title_looks_elite(title):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            cls: str = ""
            try:
                raw_cls = win32gui.GetClassName(hwnd)
                cls = raw_cls if raw_cls is not None else ""
            except Exception:
                pass
            focusable = bool(win32gui.IsWindowEnabled(hwnd))
            results.append(WindowInfo(
                handle=hwnd,
                pid=pid,
                title=title,
                window_class=cls,
                backend="win32",
                focusable=focusable,
            ))
            return True

        win32gui.EnumWindows(_cb, None)
        return results
    except ImportError:
        pass

    # --- ctypes fallback (no pywin32) ---
    import ctypes
    from ctypes import wintypes

    user32 = cast(
        _User32Module,
        cast(object, ctypes.windll.user32),
    )  # type: ignore[attr-defined]

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _ctypes_cb(hwnd: int, _lparam: int) -> int:
        if not user32.IsWindowVisible(hwnd):
            return 1
        length = user32.GetWindowTextLengthW(hwnd) + 1
        buf = cast(_ValueBuffer, ctypes.create_unicode_buffer(length))
        user32.GetWindowTextW(hwnd, buf, length)
        title = buf.value
        if not title or not _title_looks_elite(title):
            return 1
        pid_dword = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_dword))
        results.append(WindowInfo(
            handle=hwnd,
            pid=pid_dword.value,
            title=title,
            window_class="",
            backend="win32",
            focusable=bool(user32.IsWindowEnabled(hwnd)),
        ))
        return 1

    user32.EnumWindows(EnumWindowsProc(_ctypes_cb), 0)
    return results


# ---------------------------------------------------------------------------
# Linux / X11 backend
# ---------------------------------------------------------------------------

def _run_xdotool(args: list[str]) -> str:
    """Run ``xdotool`` with *args* and return stdout; raise on error."""
    try:
        return subprocess.check_output(
            ["xdotool", *args],
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "xdotool is not installed.  Install it with:  "
            + "sudo apt install xdotool   (Debian/Ubuntu)  |  "
            + "sudo pacman -S xdotool   (Arch)"
        )


def _run_xprop(args: list[str]) -> str:
    """Run ``xprop`` with *args* and return stdout; raise on error."""
    try:
        return subprocess.check_output(
            ["xprop", *args],
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "xprop is not installed.  Install it with:  "
            + "sudo apt install x11-utils   (Debian/Ubuntu)  |  "
            + "sudo pacman -S xorg-xprop   (Arch)"
        )


def _get_window_pid_x11(wid: int) -> int:
    """Return the PID owning window *wid*, or ``0`` on failure."""
    try:
        out = _run_xprop(["-id", str(wid), "_NET_WM_PID"])
        m = re.search(r"_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)", out)
        if m:
            return int(m.group(1))
    except RuntimeError:
        pass
    return 0


def _get_window_class_x11(wid: int) -> str:
    """Return WM_CLASS for window *wid*, or empty string on failure."""
    try:
        out = _run_xprop(["-id", str(wid), "WM_CLASS"])
        m = re.search(r'WM_CLASS\(STRING\)\s*=\s*"([^"]*)"', out)
        if m:
            return m.group(1)
    except RuntimeError:
        pass
    return ""


def _is_focusable_x11(wid: int) -> bool:
    """Heuristically determine if *wid* is focusable (visible & mapped)."""
    try:
        out = _run_xprop(["-id", str(wid), "WM_STATE"])
        # If WM_STATE exists the window is mapped (visible).
        return "WM_STATE" in out
    except RuntimeError:
        return False


def _enumerate_windows_x11() -> list[WindowInfo]:
    """Enumerate candidate Elite windows on Linux/X11 via ``xdotool`` + ``xprop``."""
    # Get all visible window IDs from xdotool
    out = _run_xdotool(["search", "--onlyvisible", "--name", ""])
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    results: list[WindowInfo] = []

    for line in lines:
        try:
            wid = int(line)
        except ValueError:
            continue

        # Get window title
        try:
            title_out = _run_xdotool(["getwindowname", str(wid)])
            title = title_out.strip()
        except (subprocess.CalledProcessError, RuntimeError):
            continue

        if not title or not _title_looks_elite(title):
            continue

        pid = _get_window_pid_x11(wid)
        cls = _get_window_class_x11(wid)
        focusable = _is_focusable_x11(wid)

        results.append(WindowInfo(
            handle=wid,
            pid=pid,
            title=title,
            window_class=cls,
            backend="x11",
            focusable=focusable,
        ))

    return results


# ---------------------------------------------------------------------------
# Focus enforcement
# ---------------------------------------------------------------------------

def _focus_window_win32(hwnd: int, timeout_seconds: float) -> None:
    """Acquire and verify foreground focus on *hwnd* (Windows).

    Uses the ``AttachThreadInput`` workaround required by
    ``SetForegroundWindow`` when the calling thread does not own the
    current foreground window.
    """
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    GetCurrentThreadId = ctypes.windll.kernel32.GetCurrentThreadId  # type: ignore[attr-defined]
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    AttachThreadInput = user32.AttachThreadInput
    SetForegroundWindow = user32.SetForegroundWindow
    GetForegroundWindow = user32.GetForegroundWindow
    ShowWindow = user32.ShowWindow

    SW_RESTORE = 9

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        fg_hwnd = GetForegroundWindow()
        if fg_hwnd == hwnd:
            return

        fg_tid = GetWindowThreadProcessId(fg_hwnd, None)
        our_tid = GetCurrentThreadId()

        if fg_tid != our_tid and fg_tid != 0:
            _ = AttachThreadInput(our_tid, fg_tid, True)
            _ = ShowWindow(hwnd, SW_RESTORE)
            _ = SetForegroundWindow(hwnd)
            _ = AttachThreadInput(our_tid, fg_tid, False)
        else:
            _ = ShowWindow(hwnd, SW_RESTORE)
            _ = SetForegroundWindow(hwnd)

        time.sleep(0.05)

        if GetForegroundWindow() == hwnd:
            return

    raise FocusError(
        f"Failed to acquire foreground focus on HWND {hwnd} "
        + f"within {timeout_seconds}s"
    )


def _focus_window_x11(wid: int, timeout_seconds: float) -> None:
    """Acquire and verify focus on *wid* (Linux/X11).

    Uses ``xdotool windowactivate --sync`` followed by an active-window
    verification check.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            _ = _run_xdotool(["windowactivate", "--sync", str(wid)])
        except (subprocess.CalledProcessError, RuntimeError):
            time.sleep(0.05)
            continue

        try:
            active_out = _run_xdotool(["getactivewindow"]).strip()
            if active_out and int(active_out) == wid:
                return
        except (subprocess.CalledProcessError, RuntimeError, ValueError):
            pass

        time.sleep(0.05)

    raise FocusError(
        f"Failed to acquire focus on X11 window {wid} "
        + f"within {timeout_seconds}s"
    )


def _focus_window(binding: WindowBinding, timeout_seconds: float) -> None:
    """Platform-dispatch focus acquisition with verification."""
    if binding.backend == "win32":
        _focus_window_win32(binding.handle, timeout_seconds)
    elif binding.backend == "x11":
        _focus_window_x11(binding.handle, timeout_seconds)
    else:
        raise FocusError(f"Unsupported backend for focus: {binding.backend}")


class FocusGuard:
    """Acquires and verifies window focus before automation sequences.

    Usage::

        guard = FocusGuard(binding, focus_timeout_seconds=5)
        guard.ensure_focus()  # raises FocusError on failure
        # ... safe to send input ...
    """

    def __init__(
        self,
        binding: WindowBinding,
        focus_timeout_seconds: float = 5.0,
    ) -> None:
        if binding is None:  # pyright: ignore[reportUnnecessaryComparison]
            raise TypeError(
                "FocusGuard requires a non-None WindowBinding. "
                + "Passing None indicates unresolved binding — "
                + "automation must not proceed."
            )
        self._binding: WindowBinding = binding
        self._timeout: float = focus_timeout_seconds

    def ensure_focus(self) -> None:
        _focus_window(self._binding, self._timeout)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enumerate_elite_windows() -> list[WindowInfo]:
    """Return a list of candidate Elite Dangerous windows on the current OS.

    The result is filtered by title heuristics and enriched with PID,
    class, and focusability metadata.  No focus operation is performed.
    """
    if IS_WINDOWS:
        return _enumerate_windows_win32()
    if IS_LINUX:
        return _enumerate_windows_x11()
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def diagnose(
    *,
    target_fid: str = "",
) -> dict[str, str | list[dict[str, int | str | bool]]]:
    """Dry-run diagnostic for the window-discovery layer.

    Returns a dict with:
    - ``platform``: current platform string
    - ``backend``: detected backend tag
    - ``candidates``: list of :meth:`WindowInfo.as_dict` dicts
    - ``target_fid``: echoed FID for caller verification
    - ``message``: human-readable summary

    This function is safe to call in CI / mock environments; when no
    live Elite windows are found it returns an empty candidate list
    rather than raising.
    """
    try:
        candidates = enumerate_elite_windows()
    except RuntimeError as exc:
        return {
            "platform": sys.platform,
            "backend": "unknown",
            "candidates": [],
            "target_fid": target_fid,
            "message": f"Discovery error: {exc}",
        }

    backend = candidates[0].backend if candidates else (
        "win32" if IS_WINDOWS else ("x11" if IS_LINUX else "unknown")
    )
    n = len(candidates)
    if n == 0:
        msg = "No live Elite Dangerous window detected on this platform."
    else:
        msg = f"Found {n} candidate Elite window(s)."
    return {
        "platform": sys.platform,
        "backend": backend,
        "candidates": [c.as_dict() for c in candidates],
        "target_fid": target_fid,
        "message": msg,
    }
