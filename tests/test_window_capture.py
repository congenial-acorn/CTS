"""Tests for TraversalSystem.window_capture."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from PIL import Image
import pytest

from TraversalSystem.window_capture import capture_window, create_placeholder


class TestUnknownBackend:
    def test_returns_none_for_unknown_backend(self):
        result = capture_window(123, "unknown_backend")
        assert result is None


class TestWin32Capture:
    def test_success_returns_image(self):
        """When _capture_win32 returns an image, capture_window passes it through."""
        fake_img = Image.new("RGB", (4, 4), (0, 0, 255))
        with patch(
            "TraversalSystem.window_capture._capture_win32", return_value=fake_img
        ):
            result = capture_window(123, "win32")
            assert result is not None
            assert isinstance(result, Image.Image)

    def test_failure_returns_none(self):
        """When _capture_win32 fails, capture_window returns None."""
        with patch("TraversalSystem.window_capture._capture_win32", return_value=None):
            result = capture_window(123, "win32")
            assert result is None


class TestX11Capture:
    def test_success_returns_image(self):
        """When _capture_x11 returns an image, capture_window passes it through."""
        fake_img = Image.new("RGB", (320, 240), (100, 150, 200))
        with patch(
            "TraversalSystem.window_capture._capture_x11", return_value=fake_img
        ):
            result = capture_window(456, "x11")
            assert result is not None
            assert isinstance(result, Image.Image)

    def test_failure_returns_none(self):
        """When _capture_x11 fails, capture_window returns None."""
        with patch("TraversalSystem.window_capture._capture_x11", return_value=None):
            result = capture_window(456, "x11")
            assert result is None


class TestPlaceholder:
    def test_placeholder_default_dimensions(self):
        img = create_placeholder()
        assert img.size == (160, 120)

    def test_placeholder_custom_dimensions(self):
        img = create_placeholder(200, 150)
        assert img.size == (200, 150)

    def test_placeholder_color(self):
        """Placeholder uses ED_PANEL_BG color RGB(31, 40, 51)."""
        img = create_placeholder(10, 10)
        pixel = img.getpixel((0, 0))
        assert pixel == (31, 40, 51)


class TestScaling:
    def test_capture_respects_max_size(self):
        """capture_window passes max_size to the backend implementation."""
        large_img = Image.new("RGB", (1920, 1080), (100, 100, 100))
        with patch(
            "TraversalSystem.window_capture._capture_win32",
            return_value=large_img,
        ) as mock_cap:
            result = capture_window(123, "win32", max_size=(160, 120))
            assert result is not None
            # Verify max_size was passed through to _capture_win32
            mock_cap.assert_called_once_with(123, (160, 120))


class TestDispatchRouting:
    def test_win32_dispatches_to_capture_win32(self):
        """capture_window routes 'win32' backend to _capture_win32."""
        with patch(
            "TraversalSystem.window_capture._capture_win32", return_value=None
        ) as mock_win:
            capture_window(42, "win32")
            mock_win.assert_called_once()

    def test_x11_dispatches_to_capture_x11(self):
        """capture_window routes 'x11' backend to _capture_x11."""
        with patch(
            "TraversalSystem.window_capture._capture_x11", return_value=None
        ) as mock_x11:
            capture_window(42, "x11")
            mock_x11.assert_called_once()
