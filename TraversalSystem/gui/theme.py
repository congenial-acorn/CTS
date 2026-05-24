"""Elite Dangerous theme constants and styles."""

# Colors
ED_ORANGE = "#FF7100"
ED_ORANGE_DARK = "#B34F00"
ED_CYAN = "#00F0FF"
ED_CYAN_DARK = "#00A8B3"
ED_DARK_BG = "#0B0C10"
ED_PANEL_BG = "#1F2833"
ED_TEXT = "#C5C6C7"
ED_BORDER = "#45A29E"

STYLESHEET = f"""
QMainWindow {{
    background-color: {ED_DARK_BG};
    color: {ED_TEXT};
}}

QWidget {{
    font-family: "Courier New", monospace;
    font-size: 14px;
}}

QPushButton {{
    background-color: {ED_PANEL_BG};
    color: {ED_ORANGE};
    border: 1px solid {ED_ORANGE};
    padding: 5px;
}}

QPushButton:hover {{
    background-color: {ED_ORANGE_DARK};
    color: {ED_DARK_BG};
}}

QPushButton#startAllButton {{
    color: {ED_CYAN};
    border-color: {ED_CYAN};
}}

QPushButton#startAllButton:hover {{
    background-color: {ED_CYAN_DARK};
    color: {ED_DARK_BG};
}}

QListWidget, QListView {{
    background-color: {ED_PANEL_BG};
    border: 1px solid {ED_BORDER};
    color: {ED_TEXT};
}}

QStatusBar {{
    background-color: {ED_PANEL_BG};
    color: {ED_ORANGE};
    border-top: 1px solid {ED_ORANGE};
}}
"""
