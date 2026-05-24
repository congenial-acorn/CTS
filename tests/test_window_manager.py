"""Tests for TraversalSystem.window_manager (Task 6).

Covers:
  - Windows backend with mocked win32gui / ctypes
  - Linux/X11 backend with mocked xdotool / xprop subprocess calls
  - Title-matching heuristics
  - Unsupported-platform fallback
  - diagnose() dry-run path
  - Smoke CLI invocation
"""
from __future__ import annotations

# pyright: reportAny=false, reportExplicitAny=false, reportUnknownLambdaType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedParameter=false, reportPrivateUsage=false, reportUnnecessaryTypeIgnoreComment=false

import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from TraversalSystem.window_manager import (  # pyright: ignore[reportMissingImports]
    FocusError,
    FocusGuard,
    WindowBinding,
    WindowBindingCoordinator,
    WindowInfo,
    _title_looks_elite,
    diagnose,
    enumerate_elite_windows,
)


# ---------------------------------------------------------------------------
# Title-matching unit tests
# ---------------------------------------------------------------------------

class TestTitleMatching:
    """Verify the Elite Dangerous title regex heuristics."""

    @pytest.mark.parametrize(
        "title",
        [
            "Elite - Dangerous (CLIENT)",
            "Elite - Dangerous (PUBLICCLIENT)",
            "ELITE DANGEROUS",
            "Elite Dangerous ( Odyssey )",
            "elite - dangerous version 4.0",
        ],
    )
    def test_elite_titles_match(self, title: str) -> None:
        assert _title_looks_elite(title) is True

    @pytest.mark.parametrize(
        "title",
        [
            "Steam",
            "Discord",
            "",
            "Visual Studio Code",
            "Elite",
            "Dangerous",
        ],
    )
    def test_non_elite_titles_do_not_match(self, title: str) -> None:
        assert _title_looks_elite(title) is False


# ---------------------------------------------------------------------------
# WindowInfo dataclass
# ---------------------------------------------------------------------------

class TestWindowInfo:
    """Verify WindowInfo construction and serialisation."""

    def test_as_dict_roundtrip(self) -> None:
        info = WindowInfo(
            handle=1234,
            pid=5678,
            title="Elite - Dangerous (CLIENT)",
            window_class="EliteDangerous",
            backend="win32",
            focusable=True,
        )
        d = info.as_dict()
        assert d["handle"] == 1234
        assert d["pid"] == 5678
        assert d["title"] == "Elite - Dangerous (CLIENT)"
        assert d["backend"] == "win32"
        assert d["focusable"] is True

    def test_default_values(self) -> None:
        info = WindowInfo(handle=1, pid=0, title="", window_class="",
                         backend="x11", focusable=False)
        assert info.pid == 0
        assert info.window_class == ""


# ---------------------------------------------------------------------------
# Windows backend tests (mocked)
# ---------------------------------------------------------------------------

class TestEnumerateWindowsWindowsBackend:
    """Simulate win32gui-based enumeration on Windows."""

    @pytest.fixture()
    def _force_windows(self) -> Any:
        """Patch platform detection to simulate Windows."""
        with patch("TraversalSystem.window_manager.IS_WINDOWS", True), \
             patch("TraversalSystem.window_manager.IS_LINUX", False):
            yield

    def test_finds_elite_via_win32gui(self, _force_windows: Any) -> None:
        """win32gui backend returns Elite windows correctly."""
        mock_win32gui = MagicMock()
        mock_win32process = MagicMock()

        # Simulate EnumWindows calling the callback with two HWNDs
        def fake_enum_windows(cb: Any, ctx: Any) -> None:
            # First: visible Elite window
            mock_win32gui.IsWindowVisible.return_value = True
            mock_win32gui.GetWindowText.side_effect = [
                "Elite - Dangerous (CLIENT)",  # first call
            ]
            mock_win32process.GetWindowThreadProcessId.return_value = (0, 4242)
            mock_win32gui.GetClassName.return_value = "EliteDangerous"
            mock_win32gui.IsWindowEnabled.return_value = True
            cb(1001, None)

        mock_win32gui.EnumWindows = fake_enum_windows

        with patch.dict(sys.modules, {
            "win32gui": mock_win32gui,
            "win32process": mock_win32process,
        }):
            results = enumerate_elite_windows()

        assert len(results) == 1
        w = results[0]
        assert w.handle == 1001
        assert w.pid == 4242
        assert w.title == "Elite - Dangerous (CLIENT)"
        assert w.backend == "win32"
        assert w.focusable is True

    def test_skips_non_elite_windows(self, _force_windows: Any) -> None:
        """Non-Elite windows are filtered out."""
        mock_win32gui = MagicMock()
        mock_win32process = MagicMock()

        def fake_enum_windows(cb: Any, ctx: Any) -> None:
            mock_win32gui.IsWindowVisible.return_value = True
            titles = iter(["Steam", "Elite - Dangerous (CLIENT)"])
            mock_win32gui.GetWindowText.side_effect = lambda h: next(titles)
            mock_win32process.GetWindowThreadProcessId.return_value = (0, 1111)
            mock_win32gui.GetClassName.return_value = ""
            mock_win32gui.IsWindowEnabled.return_value = True
            cb(2001, None)  # Steam — should be skipped
            cb(2002, None)  # Elite — should be kept

        mock_win32gui.EnumWindows = fake_enum_windows

        with patch.dict(sys.modules, {
            "win32gui": mock_win32gui,
            "win32process": mock_win32process,
        }):
            results = enumerate_elite_windows()

        assert len(results) == 1
        assert results[0].title == "Elite - Dangerous (CLIENT)"

    def test_ctypes_fallback_when_no_pywin32(self, _force_windows: Any) -> None:
        """When pywin32 is missing, falls back to ctypes enumeration."""
        with patch.dict(sys.modules, {"win32gui": None, "win32process": None}):
            mock_user32 = MagicMock()
            mock_user32.IsWindowVisible.return_value = 1
            buf = MagicMock()
            buf.value = "Elite - Dangerous (CLIENT)"
            mock_user32.GetWindowTextLengthW.return_value = 30
            mock_user32.GetWindowTextW.return_value = None
            mock_user32.GetWindowThreadProcessId.return_value = None
            mock_user32.IsWindowEnabled.return_value = 1
            mock_user32.EnumWindows.side_effect = lambda cb, lp: cb(5001, 0)

            mock_windll = MagicMock()
            mock_windll.user32 = mock_user32

            mock_wt = MagicMock()
            mock_wt.HWND = int
            mock_wt.LPARAM = int
            mock_wt.BOOL = int
            pid_mock = MagicMock()
            pid_mock.value = 9999
            mock_wt.DWORD.return_value = pid_mock

            import ctypes as real_ctypes

            saved = {}
            to_inject = {
                "windll": mock_windll,
                "WINFUNCTYPE": lambda *a: (lambda f: f),
                "byref": lambda x: x,
                "create_unicode_buffer": lambda *a, **kw: buf,
            }
            for attr, val in to_inject.items():
                saved[attr] = getattr(real_ctypes, attr, None)
                setattr(real_ctypes, attr, val)
            saved_wt = sys.modules.get("ctypes.wintypes")
            sys.modules["ctypes.wintypes"] = mock_wt

            try:
                results = enumerate_elite_windows()
            finally:
                for attr, orig in saved.items():
                    if orig is not None:
                        setattr(real_ctypes, attr, orig)
                    elif hasattr(real_ctypes, attr):
                        try:
                            delattr(real_ctypes, attr)
                        except AttributeError:
                            pass
                if saved_wt is not None:
                    sys.modules["ctypes.wintypes"] = saved_wt
                else:
                    _ = sys.modules.pop("ctypes.wintypes", None)

        assert len(results) == 1
        assert results[0].backend == "win32"

    def test_invisible_windows_skipped(self, _force_windows: Any) -> None:
        """Invisible windows are not included even if title matches."""
        mock_win32gui = MagicMock()
        mock_win32process = MagicMock()

        def fake_enum_windows(cb: Any, ctx: Any) -> None:
            mock_win32gui.IsWindowVisible.return_value = False
            cb(3001, None)

        mock_win32gui.EnumWindows = fake_enum_windows

        with patch.dict(sys.modules, {
            "win32gui": mock_win32gui,
            "win32process": mock_win32process,
        }):
            results = enumerate_elite_windows()

        assert len(results) == 0


# ---------------------------------------------------------------------------
# Linux / X11 backend tests (mocked)
# ---------------------------------------------------------------------------

class TestEnumerateWindowsLinuxX11Backend:
    """Simulate xdotool/xprop-based enumeration on Linux/X11."""

    @pytest.fixture()
    def _force_linux(self) -> Any:
        """Patch platform detection to simulate Linux."""
        with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
             patch("TraversalSystem.window_manager.IS_LINUX", True):
            yield

    def test_finds_elite_via_xdotool(self, _force_linux: Any) -> None:
        """xdotool search + xprop returns Elite windows."""
        # xdotool search returns two window IDs
        xdotool_output = "800001\n800002\n"

        # For getwindowname: first is "Steam", second is Elite
        name_results = {
            "800001": subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Steam\n", stderr="",
            ),
            "800002": subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="Elite - Dangerous (CLIENT)\n", stderr="",
            ),
        }

        xprop_results = {
            # PID for 800002
            ("800002", "_NET_WM_PID"):
                '_NET_WM_PID(CARDINAL) = 7777\n',
            # WM_CLASS for 800002
            ("800002", "WM_CLASS"):
                'WM_CLASS(STRING) = "steam_app_359320", "steam"\n',
            # WM_STATE for 800002 (mapped / focusable)
            ("800002", "WM_STATE"):
                'WM_STATE(WM_STATE):\n\t\tstate = Normal\n',
        }

        def mock_check_output(cmd: list[str], **kwargs: Any) -> str:
            if cmd[0] == "xdotool":
                if cmd[1] == "search":
                    return xdotool_output
                if cmd[1] == "getwindowname":
                    wid = cmd[2]
                    r = name_results.get(wid)
                    if r:
                        return r.stdout
                    raise subprocess.CalledProcessError(1, cmd)
            if cmd[0] == "xprop":
                wid = cmd[2]
                prop = cmd[3]
                key = (wid, prop)
                val = xprop_results.get(key)
                if val is not None:
                    return val
                return ""
            raise FileNotFoundError(cmd[0])

        with patch("subprocess.check_output", side_effect=mock_check_output):
            results = enumerate_elite_windows()

        assert len(results) == 1
        w = results[0]
        assert w.handle == 800002
        assert w.pid == 7777
        assert w.title == "Elite - Dangerous (CLIENT)"
        assert w.window_class == "steam_app_359320"
        assert w.backend == "x11"
        assert w.focusable is True

    def test_empty_xdotool_output(self, _force_linux: Any) -> None:
        """When xdotool returns no windows, result is empty."""
        with patch("subprocess.check_output", return_value=""):
            results = enumerate_elite_windows()
        assert results == []

    def test_xdotool_missing_raises(self, _force_linux: Any) -> None:
        """Missing xdotool raises RuntimeError with install instructions."""
        with patch("subprocess.check_output", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="xdotool"):
                _ = enumerate_elite_windows()

    def test_xprop_missing_raises(self, _force_linux: Any) -> None:
        """Missing xprop during metadata fetch still returns windows.

        The xprop calls are non-fatal per-window; only the initial
        xdotool search is fatal.
        """
        call_count = 0

        def mock_check_output(cmd: list[str], **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            if cmd[0] == "xdotool":
                if cmd[1] == "search":
                    return "900001\n"
                if cmd[1] == "getwindowname":
                    return "Elite - Dangerous (CLIENT)\n"
            if cmd[0] == "xprop":
                raise FileNotFoundError("xprop")
            raise FileNotFoundError(cmd[0])

        with patch("subprocess.check_output", side_effect=mock_check_output):
            results = enumerate_elite_windows()

        # Should still return the window — xprop metadata is best-effort
        assert len(results) == 1
        assert results[0].pid == 0  # xprop failed, so no PID
        assert results[0].window_class == ""  # xprop failed
        assert results[0].focusable is False  # WM_STATE unavailable

    def test_invalid_window_id_skipped(self, _force_linux: Any) -> None:
        """Non-numeric xdotool output lines are silently skipped."""
        with patch("subprocess.check_output", return_value="notanumber\n"):
            results = enumerate_elite_windows()
        assert results == []


# ---------------------------------------------------------------------------
# Unsupported platform
# ---------------------------------------------------------------------------

class TestUnsupportedPlatform:
    """Verify behaviour on unsupported platforms (e.g., macOS)."""

    def test_raises_on_unsupported_platform(self) -> None:
        with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
             patch("TraversalSystem.window_manager.IS_LINUX", False):
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                _ = enumerate_elite_windows()


# ---------------------------------------------------------------------------
# diagnose() dry-run
# ---------------------------------------------------------------------------

class TestDiagnose:
    """Verify the dry-run diagnostic helper."""

    def test_diagnose_no_windows_found(self) -> None:
        with patch(
            "TraversalSystem.window_manager.enumerate_elite_windows",
            return_value=[],
        ):
            report = diagnose(target_fid="FID-TEST")

        assert report["target_fid"] == "FID-TEST"
        assert report["candidates"] == []
        assert "No live Elite" in report["message"]
        assert report["backend"] in {"win32", "x11", "unknown"}

    def test_diagnose_with_candidates(self) -> None:
        fake = [
            WindowInfo(
                handle=100, pid=42,
                title="Elite - Dangerous (CLIENT)",
                window_class="Test",
                backend="win32",
                focusable=True,
            ),
        ]
        with patch(
            "TraversalSystem.window_manager.enumerate_elite_windows",
            return_value=fake,
        ):
            report = diagnose()

        assert report["backend"] == "win32"
        assert len(report["candidates"]) == 1
        assert "1 candidate" in report["message"]

    def test_diagnose_handles_discovery_error(self) -> None:
        with patch(
            "TraversalSystem.window_manager.enumerate_elite_windows",
            side_effect=RuntimeError("xdotool is not installed"),
        ):
            report = diagnose()

        assert "Discovery error" in report["message"]
        assert report["candidates"] == []


class TestWindowBindingCoordinator:
    def test_bind_target_fid_to_unique_window(self) -> None:
        live_windows = [
            WindowInfo(
                handle=100,
                pid=1111,
                title="Elite Dangerous",
                window_class="",
                backend="x11",
                focusable=False,
            ),
            WindowInfo(
                handle=200,
                pid=2222,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="x11",
                focusable=True,
            ),
        ]
        coordinator = WindowBindingCoordinator(lambda: live_windows)

        binding = coordinator.resolve_binding(
            target_fid="FID-UNIQUE",
            startup_identity="cmdr:journal-1",
        )

        assert binding is not None
        assert binding.target_fid == "FID-UNIQUE"
        assert binding.handle == 200
        assert binding.pid == 2222
        assert binding.startup_identity == "cmdr:journal-1"

        live_windows[:] = [
            WindowInfo(
                handle=200,
                pid=2222,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="x11",
                focusable=True,
            ),
            WindowInfo(
                handle=300,
                pid=3333,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="x11",
                focusable=True,
            ),
        ]

        reused = coordinator.resolve_binding(
            target_fid="FID-UNIQUE",
            startup_identity="cmdr:journal-1",
        )

        assert reused == binding

        live_windows[:] = [
            WindowInfo(
                handle=200,
                pid=2222,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="x11",
                focusable=True,
            ),
        ]

        rebound = coordinator.resolve_binding(
            target_fid="FID-UNIQUE",
            startup_identity="cmdr:journal-2",
        )

        assert rebound is not None
        assert rebound is not binding
        assert rebound.handle == 200
        assert rebound.startup_identity == "cmdr:journal-2"

    def test_ambiguous_binding_aborts_without_selection(self) -> None:
        coordinator = WindowBindingCoordinator(lambda: [
            WindowInfo(
                handle=400,
                pid=4444,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="win32",
                focusable=True,
            ),
            WindowInfo(
                handle=500,
                pid=5555,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="win32",
                focusable=True,
            ),
        ])

        binding = coordinator.resolve_binding(
            target_fid="FID-AMBIGUOUS",
            startup_identity="cmdr:journal-1",
            ambiguous_window_policy="abort",
        )

        assert binding is None
        assert coordinator.get_live_binding(
            target_fid="FID-AMBIGUOUS",
            startup_identity="cmdr:journal-1",
        ) is None


# ---------------------------------------------------------------------------
# Smoke CLI invocation
# ---------------------------------------------------------------------------

class TestSmokeCLI:
    """Verify CTS_window_smoke.py can be invoked."""

    def test_smoke_cli_dry_run(self) -> None:
        """The smoke CLI runs without error even with no live windows."""
        import subprocess as sp

        result = sp.run(
            [sys.executable, "CTS_window_smoke.py", "--dry-run",
             "--target-fid", "TESTFID"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "TESTFID" in result.stdout


def test_bind_target_fid_to_unique_window() -> None:
    TestWindowBindingCoordinator().test_bind_target_fid_to_unique_window()


def test_ambiguous_binding_aborts_without_selection() -> None:
    TestWindowBindingCoordinator().test_ambiguous_binding_aborts_without_selection()


# ---------------------------------------------------------------------------
# Focus enforcement tests (Task 8)
# ---------------------------------------------------------------------------

class TestFocusWindowWindowsBackend:
    """Verify Windows focus acquisition with thread-input workaround."""

    @pytest.fixture()
    def _force_windows(self) -> Any:
        with patch("TraversalSystem.window_manager.IS_WINDOWS", True), \
             patch("TraversalSystem.window_manager.IS_LINUX", False):
            yield

    def test_focus_window_windows_backend(self, _force_windows: Any) -> None:
        mock_kernel32 = MagicMock()
        mock_user32 = MagicMock()

        mock_kernel32.GetCurrentThreadId.return_value = 100

        call_seq: list[str] = []

        def mock_get_fg() -> int:
            call_seq.append("GetForegroundWindow")
            if call_seq.count("GetForegroundWindow") <= 1:
                return 9999
            return 5555

        mock_user32.GetForegroundWindow.side_effect = mock_get_fg
        mock_user32.GetWindowThreadProcessId.return_value = 200
        mock_user32.AttachThreadInput.return_value = 1
        mock_user32.SetForegroundWindow.return_value = 1
        mock_user32.ShowWindow.return_value = 1

        mock_windll = MagicMock()
        mock_windll.user32 = mock_user32
        mock_windll.kernel32 = mock_kernel32

        with patch.dict(sys.modules, {}):
            import ctypes as real_ctypes
            saved_windll = getattr(real_ctypes, "windll", None)
            setattr(real_ctypes, "windll", mock_windll)
            try:
                binding = WindowBinding(
                    target_fid="F-FOCUS",
                    startup_identity="cmdr:j1",
                    handle=5555,
                    pid=1234,
                    title="Elite - Dangerous (CLIENT)",
                    window_class="EliteDangerous",
                    backend="win32",
                )
                guard = FocusGuard(binding, focus_timeout_seconds=2.0)
                guard.ensure_focus()
            finally:
                if saved_windll is not None:
                    setattr(real_ctypes, "windll", saved_windll)

        mock_user32.SetForegroundWindow.assert_called()


class TestFocusWindowLinuxBackend:
    """Verify Linux/X11 focus acquisition with xdotool."""

    @pytest.fixture()
    def _force_linux(self) -> Any:
        with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
             patch("TraversalSystem.window_manager.IS_LINUX", True):
            yield

    def test_focus_window_linux_backend(self, _force_linux: Any) -> None:
        xdotool_calls: list[list[str]] = []

        def mock_check_output(cmd: list[str], **kwargs: Any) -> str:
            xdotool_calls.append(cmd)
            if cmd[0] == "xdotool":
                if cmd[1] == "windowactivate":
                    assert "--sync" in cmd
                    return ""
                if cmd[1] == "getactivewindow":
                    return "7777\n"
            raise FileNotFoundError(cmd[0])

        binding = WindowBinding(
            target_fid="F-FOCUS",
            startup_identity="cmdr:j1",
            handle=7777,
            pid=5000,
            title="Elite - Dangerous (CLIENT)",
            window_class="steam_app_359320",
            backend="x11",
        )

        with patch("subprocess.check_output", side_effect=mock_check_output):
            guard = FocusGuard(binding, focus_timeout_seconds=2.0)
            guard.ensure_focus()

        assert any(
            c[1] == "windowactivate" for c in xdotool_calls
        )
        assert any(
            c[1] == "getactivewindow" for c in xdotool_calls
        )


class TestFocusFailure:
    """Verify FocusError is raised when focus cannot be verified."""

    def test_focus_error_on_timeout_windows(self) -> None:
        mock_user32 = MagicMock()
        mock_kernel32 = MagicMock()

        mock_kernel32.GetCurrentThreadId.return_value = 100
        mock_user32.GetForegroundWindow.return_value = 9999
        mock_user32.GetWindowThreadProcessId.return_value = 200
        mock_user32.AttachThreadInput.return_value = 1
        mock_user32.SetForegroundWindow.return_value = 1
        mock_user32.ShowWindow.return_value = 1

        mock_windll = MagicMock()
        mock_windll.user32 = mock_user32
        mock_windll.kernel32 = mock_kernel32

        import ctypes as real_ctypes
        saved_windll = getattr(real_ctypes, "windll", None)
        setattr(real_ctypes, "windll", mock_windll)
        try:
            with patch("TraversalSystem.window_manager.IS_WINDOWS", True), \
                 patch("TraversalSystem.window_manager.IS_LINUX", False):
                binding = WindowBinding(
                    target_fid="F-FAIL",
                    startup_identity="cmdr:j1",
                    handle=5555,
                    pid=1234,
                    title="Elite",
                    window_class="",
                    backend="win32",
                )
                guard = FocusGuard(binding, focus_timeout_seconds=0.1)
                with pytest.raises(FocusError, match="Failed to acquire"):
                    guard.ensure_focus()
        finally:
            if saved_windll is not None:
                setattr(real_ctypes, "windll", saved_windll)

    def test_focus_error_on_timeout_linux(self) -> None:
        def mock_check_output(cmd: list[str], **kwargs: Any) -> str:
            if cmd[0] == "xdotool" and cmd[1] == "getactivewindow":
                return "99999\n"
            if cmd[0] == "xdotool" and cmd[1] == "windowactivate":
                return ""
            raise FileNotFoundError(cmd[0])

        with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
             patch("TraversalSystem.window_manager.IS_LINUX", True):
            binding = WindowBinding(
                target_fid="F-FAIL",
                startup_identity="cmdr:j1",
                handle=7777,
                pid=5000,
                title="Elite",
                window_class="",
                backend="x11",
            )
            guard = FocusGuard(binding, focus_timeout_seconds=0.1)
            with patch("subprocess.check_output", side_effect=mock_check_output):
                with pytest.raises(FocusError, match="Failed to acquire"):
                    guard.ensure_focus()


def test_focus_window_windows_backend() -> None:
    import ctypes as real_ctypes
    from unittest.mock import MagicMock, patch

    mock_kernel32 = MagicMock()
    mock_user32 = MagicMock()

    mock_kernel32.GetCurrentThreadId.return_value = 100

    call_seq: list[str] = []

    def mock_get_fg() -> int:
        call_seq.append("GetForegroundWindow")
        if call_seq.count("GetForegroundWindow") <= 1:
            return 9999
        return 5555

    mock_user32.GetForegroundWindow.side_effect = mock_get_fg
    mock_user32.GetWindowThreadProcessId.return_value = 200
    mock_user32.AttachThreadInput.return_value = 1
    mock_user32.SetForegroundWindow.return_value = 1
    mock_user32.ShowWindow.return_value = 1

    mock_windll = MagicMock()
    mock_windll.user32 = mock_user32
    mock_windll.kernel32 = mock_kernel32

    saved_windll = getattr(real_ctypes, "windll", None)
    setattr(real_ctypes, "windll", mock_windll)
    try:
        with patch("TraversalSystem.window_manager.IS_WINDOWS", True), \
             patch("TraversalSystem.window_manager.IS_LINUX", False):
            binding = WindowBinding(
                target_fid="F-FOCUS",
                startup_identity="cmdr:j1",
                handle=5555,
                pid=1234,
                title="Elite - Dangerous (CLIENT)",
                window_class="EliteDangerous",
                backend="win32",
            )
            guard = FocusGuard(binding, focus_timeout_seconds=2.0)
            guard.ensure_focus()
    finally:
        if saved_windll is not None:
            setattr(real_ctypes, "windll", saved_windll)

    mock_user32.SetForegroundWindow.assert_called()


def test_focus_window_linux_backend() -> None:
    from unittest.mock import patch

    xdotool_calls: list[list[str]] = []

    def mock_check_output(cmd: list[str], **kwargs: Any) -> str:
        xdotool_calls.append(cmd)
        if cmd[0] == "xdotool":
            if cmd[1] == "windowactivate":
                return ""
            if cmd[1] == "getactivewindow":
                return "7777\n"
        raise FileNotFoundError(cmd[0])

    binding = WindowBinding(
        target_fid="F-FOCUS",
        startup_identity="cmdr:j1",
        handle=7777,
        pid=5000,
        title="Elite - Dangerous (CLIENT)",
        window_class="steam_app_359320",
        backend="x11",
    )

    with patch("TraversalSystem.window_manager.IS_WINDOWS", False), \
         patch("TraversalSystem.window_manager.IS_LINUX", True), \
         patch("subprocess.check_output", side_effect=mock_check_output):
        guard = FocusGuard(binding, focus_timeout_seconds=2.0)
        guard.ensure_focus()

    assert any(c[1] == "windowactivate" for c in xdotool_calls)
    assert any(c[1] == "getactivewindow" for c in xdotool_calls)
