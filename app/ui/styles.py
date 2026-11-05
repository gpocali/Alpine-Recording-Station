"""Resolution-aware responsive styling for the Alpine Recording Station UI."""

from __future__ import annotations


def compute_scale_factor(width: int, height: int) -> float:
    """Compute a responsive UI scale factor relative to a 1280x720 baseline."""
    w_scale = max(0.55, width / 1280.0)
    h_scale = max(0.55, height / 720.0)
    return min(w_scale, h_scale)


def build_main_stylesheet(scale: float = 1.0) -> str:
    """Generate a high-contrast studio dark stylesheet scaled to screen dimensions."""
    base_font = max(11, int(round(14 * scale)))
    small_font = max(10, int(round(12 * scale)))
    header_font = max(13, int(round(17 * scale)))
    input_pad_v = max(6, int(round(8 * scale)))
    input_pad_h = max(8, int(round(12 * scale)))
    btn_pad_v = max(8, int(round(10 * scale)))
    btn_pad_h = max(12, int(round(16 * scale)))
    radius = max(6, int(round(8 * scale)))

    return f"""
    QMainWindow, QDialog {{
        background-color: #111418;
        color: #E6EDF3;
        font-family: "DejaVu Sans", "Segoe UI", "Liberation Sans", sans-serif;
        font-size: {base_font}px;
    }}

    QLabel {{
        color: #E6EDF3;
        font-size: {base_font}px;
    }}

    QLabel#HeaderTitle {{
        font-size: {header_font}px;
        font-weight: bold;
        color: #F0F6FC;
    }}

    QLabel#SubtleIpBar {{
        color: #8B949E;
        font-size: {small_font}px;
        background-color: #0D1014;
        padding: 4px 10px;
        border-top: 1px solid #21262D;
    }}

    QLineEdit, QComboBox, QListWidget {{
        background-color: #161B22;
        color: #F0F6FC;
        border: 1px solid #30363D;
        border-radius: {radius}px;
        padding: {input_pad_v}px {input_pad_h}px;
        font-size: {base_font}px;
        selection-background-color: #1F6FEB;
        selection-color: #FFFFFF;
    }}

    QLineEdit:focus, QComboBox:focus, QListWidget:focus {{
        border: 2px solid #388BFD;
    }}

    QComboBox::drop-down {{
        border: none;
        width: {max(24, int(round(32 * scale)))}px;
    }}

    QListWidget::item {{
        padding: {max(6, int(round(8 * scale)))}px;
        border-bottom: 1px solid #21262D;
    }}

    QListWidget::item:selected {{
        background-color: #1F6FEB;
        color: #FFFFFF;
        font-weight: bold;
    }}

    QPushButton {{
        background-color: #21262D;
        color: #F0F6FC;
        border: 1px solid #30363D;
        border-radius: {radius}px;
        padding: {btn_pad_v}px {btn_pad_h}px;
        font-size: {base_font}px;
        font-weight: 600;
    }}

    QPushButton:hover {{
        background-color: #30363D;
        border-color: #8B949E;
    }}

    QPushButton:pressed {{
        background-color: #161B22;
    }}

    QPushButton:disabled {{
        background-color: #1A1E24;
        color: #6E7681;
        border-color: #21262D;
    }}

    QTabWidget::pane {{
        border: 1px solid #30363D;
        background-color: #161B22;
        border-radius: {radius}px;
    }}

    QTabBar::tab {{
        background-color: #0D1014;
        color: #8B949E;
        padding: {btn_pad_v}px {btn_pad_h * 2}px;
        border: 1px solid #30363D;
        border-bottom: none;
        border-top-left-radius: {radius}px;
        border-top-right-radius: {radius}px;
        font-size: {base_font}px;
        font-weight: 600;
    }}

    QTabBar::tab:selected {{
        background-color: #161B22;
        color: #F0F6FC;
        border-bottom: 2px solid #388BFD;
    }}

    QToolTip {{
        background-color: #1F242C;
        color: #F0F6FC;
        border: 1px solid #58A6FF;
        padding: 6px 10px;
        font-size: {small_font}px;
    }}
    """


def build_record_button_style(state: str, scale: float = 1.0) -> str:
    """Return stylesheet for the primary Record/Stop button ('ready', 'recording', 'disabled')."""
    font_px = max(18, int(round(30 * scale)))
    radius = max(12, int(round(18 * scale)))

    if state == "recording":
        # Red when actively recording
        return f"""
        QPushButton {{
            background-color: #D72638;
            color: #FFFFFF;
            border: 3px solid #FF6B7A;
            border-radius: {radius}px;
            font-size: {font_px}px;
            font-weight: bold;
            letter-spacing: 1px;
        }}
        QPushButton:hover {{
            background-color: #E5384B;
        }}
        QPushButton:pressed {{
            background-color: #B01B2B;
        }}
        """
    if state == "ready":
        # Green when stopped and able to record
        return f"""
        QPushButton {{
            background-color: #1F883D;
            color: #FFFFFF;
            border: 3px solid #3FB950;
            border-radius: {radius}px;
            font-size: {font_px}px;
            font-weight: bold;
            letter-spacing: 1px;
        }}
        QPushButton:hover {{
            background-color: #269F48;
        }}
        QPushButton:pressed {{
            background-color: #18692F;
        }}
        """
    # Grey and inactive when no valid audio interface is present
    return f"""
    QPushButton {{
        background-color: #2D333B;
        color: #768390;
        border: 3px solid #444C56;
        border-radius: {radius}px;
        font-size: {font_px}px;
        font-weight: bold;
        letter-spacing: 1px;
    }}
    """


def build_talkback_button_style(active: bool, enabled: bool, scale: float = 1.0) -> str:
    """Return stylesheet for the Studio Mode Talkback momentary push-to-talk button."""
    font_px = max(16, int(round(24 * scale)))
    radius = max(12, int(round(16 * scale)))

    if not enabled:
        return f"""
        QPushButton {{
            background-color: #1C2128;
            color: #545D68;
            border: 2px dashed #30363D;
            border-radius: {radius}px;
            font-size: {font_px}px;
            font-weight: bold;
        }}
        """
    if active:
        return f"""
        QPushButton {{
            background-color: #E36209;
            color: #FFFFFF;
            border: 4px solid #FFDF5D;
            border-radius: {radius}px;
            font-size: {font_px}px;
            font-weight: bold;
        }}
        """
    return f"""
    QPushButton {{
        background-color: #1F3A5F;
        color: #E6EDF3;
        border: 3px solid #388BFD;
        border-radius: {radius}px;
        font-size: {font_px}px;
        font-weight: bold;
    }}
    QPushButton:hover {{
        background-color: #264875;
    }}
    """
