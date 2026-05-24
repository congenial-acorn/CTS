"""CLI entry point for the CTS GUI.

Run with::

    python -m TraversalSystem.gui_main --smoke
    python -m TraversalSystem.gui_main --smoke --config /path/to/config.json
    python -m TraversalSystem.gui_main --smoke --assert-widgets

The ``--smoke`` flag creates a minimal ``QApplication`` offscreen-safe,
prints ``GUI_SMOKE_OK``, and exits.  ``--config`` is accepted but optional;
a missing config path does not crash smoke mode.
"""
from __future__ import annotations

import argparse
import sys


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="TraversalSystem.gui_main",
        description="CTS GUI launcher",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run a minimal smoke test and exit.",
    )
    parser.add_argument(
        "--assert-widgets",
        action="store_true",
        help="Assert that required widgets exist and print GUI_WIDGETS_OK.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to an optional GUI configuration file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    from PySide6.QtWidgets import QApplication, QMessageBox
    from TraversalSystem.gui.main_window import CTSMainWindow
    from TraversalSystem.gui_config import load_gui_config, GuiConfig, GuiConfigError

    app = QApplication.instance() or QApplication(sys.argv[:1])
    config_path = args.config or "gui_config.json"
    
    if args.smoke:
        # Config path is informational only in smoke mode; tolerate missing files.
        if args.config is not None:
            print(f"Config path noted (not loaded in smoke mode): {args.config}")

        if args.assert_widgets:
            window = CTSMainWindow(config_path=config_path)
            assert window.carrierList.objectName() == "carrierList"
            assert window.startAllButton.objectName() == "startAllButton"
            assert window.stopAllButton.objectName() == "stopAllButton"
            assert window.universalSettingsPanel.objectName() == "universalSettingsPanel"
            # Use the method, not the attribute, since it's a QMainWindow
            assert window.statusBar().objectName() == "statusBar"
            print("GUI_WIDGETS_OK")
        else:
            print("GUI_SMOKE_OK")
            
        # Process any pending events then exit.
        app.processEvents()
        return

    config = GuiConfig()
    import os
    if os.path.exists(config_path):
        try:
            config = load_gui_config(config_path)
        except GuiConfigError as e:
            QMessageBox.critical(None, "Configuration Error", str(e))
            sys.exit(1)
        except Exception as e:
            QMessageBox.critical(None, "Fatal Error", f"Unexpected error loading config: {e}")
            sys.exit(1)

    window = CTSMainWindow(config=config, config_path=config_path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
