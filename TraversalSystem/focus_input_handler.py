"""Focus-aware input dispatch wrapper.

Composes :class:`~TraversalSystem.window_manager.FocusGuard` with
:class:`~TraversalSystem.input_handler` primitives so that every
automated input sequence verifies the target window has focus before
dispatching keyboard/mouse events.

Design contract:
  - Each ``FocusAwareInputHandler`` is bound to one slot's
    :class:`~TraversalSystem.window_manager.WindowBinding`.
  - Before every input primitive, the handler calls
    :meth:`FocusGuard.ensure_focus`.
  - :class:`~TraversalSystem.window_manager.FocusError` propagates to
    the caller (worker / controller), which transitions the slot to
    ``error`` and stops only that slot's worker path.
  - The underlying :mod:`input_handler` module is **never** modified;
    it remains independently testable.  This module wraps it via
    dependency injection so callers can supply mock input backends.
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol

try:
    from .window_manager import FocusError, FocusGuard, WindowBinding
    from . import input_handler as _real_input_handler
except ImportError:  # pragma: no cover - script execution fallback
    from TraversalSystem.window_manager import FocusError, FocusGuard, WindowBinding  # type: ignore[reportMissingImports]
    from TraversalSystem import input_handler as _real_input_handler  # type: ignore[reportMissingImports]


# ---------------------------------------------------------------------------
# Input backend protocol (for testability)
# ---------------------------------------------------------------------------

class InputBackend(Protocol):
    """Minimal protocol covering the input primitives used by the wrapper."""

    def press(self, key: str) -> None: ...
    def keyDown(self, key: str) -> None: ...
    def keyUp(self, key: str) -> None: ...
    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
    ) -> None: ...
    def moveTo(self, x: int, y: int) -> None: ...
    def typewrite(self, text: str, interval: float = 0.0) -> None: ...


class _DefaultInputBackend:
    """Adapts the module-level ``input_handler`` functions to the protocol."""

    def press(self, key: str) -> None:
        _real_input_handler.press(key)

    def keyDown(self, key: str) -> None:
        _real_input_handler.keyDown(key)

    def keyUp(self, key: str) -> None:
        _real_input_handler.keyUp(key)

    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
    ) -> None:
        _real_input_handler.click(x, y, button)

    def moveTo(self, x: int, y: int) -> None:
        _real_input_handler.moveTo(x, y)

    def typewrite(self, text: str, interval: float = 0.0) -> None:
        _real_input_handler.typewrite(text, interval)


# ---------------------------------------------------------------------------
# Focus-check protocol (for testability)
# ---------------------------------------------------------------------------

class FocusChecker(Protocol):
    """Callable that acquires and verifies window focus.

    Must raise :class:`FocusError` when focus cannot be acquired.
    """

    def ensure_focus(self) -> None: ...


# ---------------------------------------------------------------------------
# FocusAwareInputHandler
# ---------------------------------------------------------------------------

class FocusAwareInputHandler:
    """Wraps input primitives with a per-slot focus guard.

    Every input method calls ``ensure_focus()`` first.  If focus
    verification fails, :class:`FocusError` propagates and **no input
    is dispatched**.

    Parameters
    ----------
    binding:
        The :class:`WindowBinding` for the carrier window this handler
        targets.
    focus_timeout_seconds:
        Timeout passed to :class:`FocusGuard`.
    input_backend:
        Optional override for the input layer.  Defaults to the real
        :mod:`input_handler` module.  Supply a mock for testing.
    focus_guard_factory:
        Optional factory that creates a :class:`FocusChecker` from a
        binding and timeout.  Defaults to :class:`FocusGuard`.
        Supply a mock for testing.

    Usage::

        handler = FocusAwareInputHandler(binding)
        handler.press("space")         # ensures focus, then sends key
        handler.click(100, 200)        # ensures focus, then clicks
    """

    __slots__ = ("_backend", "_focus_checker")

    def __init__(
        self,
        binding: WindowBinding,
        *,
        focus_timeout_seconds: float = 5.0,
        input_backend: InputBackend | None = None,
        focus_guard_factory: Callable[
            [WindowBinding, float], FocusChecker
        ] | None = None,
    ) -> None:
        factory = focus_guard_factory or self._default_focus_guard
        self._focus_checker: FocusChecker = factory(binding, focus_timeout_seconds)
        self._backend: InputBackend = input_backend or _DefaultInputBackend()

    @staticmethod
    def _default_focus_guard(
        binding: WindowBinding, timeout: float,
    ) -> FocusChecker:
        return FocusGuard(binding, focus_timeout_seconds=timeout)

    # -- focus gate --------------------------------------------------------

    def _ensure_focus(self) -> None:
        """Acquire and verify focus before input dispatch.

        Raises :class:`FocusError` when the target window cannot be
        focused.  The caller (worker / controller) is responsible for
        catching this and transitioning the slot to ``error``.
        """
        self._focus_checker.ensure_focus()

    # -- input primitives (each gated by focus) ----------------------------

    def press(self, key: str) -> None:
        self._ensure_focus()
        self._backend.press(key)

    def keyDown(self, key: str) -> None:
        self._ensure_focus()
        self._backend.keyDown(key)

    def keyUp(self, key: str) -> None:
        self._ensure_focus()
        self._backend.keyUp(key)

    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
    ) -> None:
        self._ensure_focus()
        self._backend.click(x, y, button)

    def moveTo(self, x: int, y: int) -> None:
        self._ensure_focus()
        self._backend.moveTo(x, y)

    def typewrite(self, text: str, interval: float = 0.0) -> None:
        self._ensure_focus()
        self._backend.typewrite(text, interval)

    # -- sequence helper ---------------------------------------------------

    def execute_sequence(
        self,
        steps: list[tuple[str, object]],
    ) -> None:
        """Execute a sequence of input steps, each guarded by focus.

        *steps* is a list of ``(method_name, args)`` tuples where
        *method_name* is one of ``"press"``, ``"keyDown"``, ``"keyUp"``,
        ``"click"``, ``"moveTo"``, ``"typewrite"``.

        Raises :class:`FocusError` at the first step where focus
        cannot be acquired; no subsequent step executes.
        """
        for method_name, args in steps:
            method = getattr(self, method_name)
            if isinstance(args, tuple):
                method(*args)
            elif isinstance(args, dict):
                method(**args)
            else:
                method(args)
