"""Cross-platform window screenshot capture.

Captures window thumbnails for the ManualBindDialog's visual window
selection.  Uses platform-native APIs so that the capture can run in
a background thread without pulling in Qt dependencies.

Public API:
  - :func:`capture_window` – capture a window as a PIL Image thumbnail
  - :func:`create_placeholder` – dark placeholder image for failed captures
"""
from __future__ import annotations

import subprocess
from typing import Tuple

from PIL import Image

from TraversalSystem.platform_utils import IS_LINUX, IS_WINDOWS


def capture_window(
    handle: int,
    backend: str,
    max_size: Tuple[int, int] = (320, 240),
) -> Image.Image | None:
    """Capture a screenshot of the window identified by *handle*/*backend*.

    Returns a PIL Image scaled to fit within *max_size* (maintaining
    aspect ratio), or ``None`` if capture fails (window minimized,
    occluded, or API error).
    """
    if backend == "win32":
        return _capture_win32(handle, max_size)
    if backend == "x11":
        return _capture_x11(handle, max_size)
    return None


def create_placeholder(
    width: int = 160,
    height: int = 120,
) -> Image.Image:
    """Create a dark placeholder image matching the CTS theme panel color.

    The color matches ``ED_PANEL_BG`` (#1F2833) from theme.py.
    """
    # ED_PANEL_BG = #1F2833
    return Image.new("RGB", (width, height), (31, 40, 51))  # pyright: ignore[reportArgumentType]


def _capture_win32(
    hwnd: int,
    max_size: Tuple[int, int],
) -> Image.Image | None:
    """Capture a window on Windows using ctypes (no win32gui dependency)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]

        # Get window dimensions
        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top

        if width <= 0 or height <= 0:
            return None

        # Get window DC
        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None

        # Create compatible DC and bitmap
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        gdi32.SelectObject(mem_dc, bitmap)

        # PrintWindow with PW_RENDERFULLCONTENT=2 (captures DirectX windows)
        PW_RENDERFULLCONTENT = 2
        result = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)

        img = None
        if result:
            # Set up BITMAPINFOHEADER for GetDIBits
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", ctypes.c_uint32),
                    ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32),
                ]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = width
            bmi.biHeight = -height  # negative = top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0  # BI_RGB

            buf_size = width * height * 4
            buf = ctypes.create_string_buffer(buf_size)

            gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(bmi), 0)

            # Convert BGRA to RGB PIL Image
            raw = Image.frombytes("RGBX", (width, height), buf.raw)
            img = raw.convert("RGB")

        # Cleanup
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

        if img is not None:
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
        return img
    except Exception:
        return None


def _capture_x11(
    wid: int,
    max_size: Tuple[int, int],
) -> Image.Image | None:
    """Capture a window on Linux/X11 using xdotool + pyautogui."""
    try:
        # Get window geometry via xdotool
        geom_out = subprocess.check_output(
            ["xdotool", "getwindowgeometry", "--shell", str(wid)],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Parse "X=123\nY=456\nWIDTH=800\nHEIGHT=600" format
        geom: dict[str, int] = {}
        for line in geom_out.strip().splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                geom[key] = int(val)

        x = geom.get("X", 0)
        y = geom.get("Y", 0)
        w = geom.get("WIDTH", 0)
        h = geom.get("HEIGHT", 0)

        if w <= 0 or h <= 0:
            return None

        # Use pyautogui.screenshot with region (already a dependency)
        import pyautogui

        img = pyautogui.screenshot(region=(x, y, w, h))

        # Scale to thumbnail
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        return img
    except Exception:
        return None
