"""Resolution-aware responsive styling for the Alpine Recording Station UI."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QComboBox


def compute_scale_factor(width: int, height: int) -> float:
    """Compute a responsive UI scale factor relative to a 1280x720 baseline."""
    w_scale = max(0.55, width / 1280.0)
    h_scale = max(0.55, height / 720.0)
    return min(w_scale, h_scale)


def build_combo_popup_stylesheet(scale: float = 1.0) -> str:
    """Return explicit dark stylesheet for a QComboBox dropdown popup view."""
    base_font = max(11, int(round(14 * scale)))
    input_pad_v = max(6, int(round(8 * scale)))
    input_pad_h = max(8, int(round(12 * scale)))
    item_min_h = max(22, int(round(28 * scale)))

    return f"""
    QAbstractItemView {{
        background-color: #161B22;
        color: #F0F6FC;
        border: 1px solid #30363D;
        font-size: {base_font}px;
        selection-background-color: #1F6FEB;
        selection-color: #FFFFFF;
        outline: 0px;
        padding: 4px;
    }}

    QAbstractItemView::item {{
        background-color: #161B22;
        color: #F0F6FC;
        padding: {input_pad_v}px {input_pad_h}px;
        min-height: {item_min_h}px;
    }}

    QAbstractItemView::item:hover,
    QAbstractItemView::item:selected {{
        background-color: #1F6FEB;
        color: #FFFFFF;
    }}
    """


def configure_combo_box_popup(combo: QComboBox, scale: float = 1.0) -> None:
    """Configure QComboBox delegate, popup palette, and stylesheet so unselected options are readable."""
    from PyQt6.QtGui import QColor, QPalette
    from PyQt6.QtWidgets import QStyledItemDelegate

    combo.setItemDelegate(QStyledItemDelegate(combo))

    bg_color = QColor("#161B22")
    fg_color = QColor("#F0F6FC")
    sel_bg_color = QColor("#1F6FEB")
    sel_fg_color = QColor("#FFFFFF")
    disabled_bg = QColor("#1A1E24")
    disabled_fg = QColor("#6E7681")

    palette = combo.palette()
    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
    ):
        palette.setColor(group, QPalette.ColorRole.Window, bg_color)
        palette.setColor(group, QPalette.ColorRole.Base, bg_color)
        palette.setColor(group, QPalette.ColorRole.AlternateBase, bg_color)
        palette.setColor(group, QPalette.ColorRole.Button, bg_color)
        palette.setColor(group, QPalette.ColorRole.Text, fg_color)
        palette.setColor(group, QPalette.ColorRole.WindowText, fg_color)
        palette.setColor(group, QPalette.ColorRole.ButtonText, fg_color)
        palette.setColor(group, QPalette.ColorRole.Highlight, sel_bg_color)
        palette.setColor(group, QPalette.ColorRole.HighlightedText, sel_fg_color)

    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Window, disabled_bg)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, disabled_bg)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, disabled_bg)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_fg)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_fg)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_fg)

    combo.setPalette(palette)

    view = combo.view()
    if view is not None:
        view.setPalette(palette)
        view.setAutoFillBackground(True)
        view.setStyleSheet(build_combo_popup_stylesheet(scale))
        viewport = view.viewport()
        if viewport is not None:
            viewport.setPalette(palette)
            viewport.setAutoFillBackground(True)


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

    QFrame#SubtleIpBarFrame {{
        background-color: #0D1014;
        border-top: 1px solid #21262D;
        border-radius: 6px;
    }}

    QLabel#SubtleIpBar {{
        color: #8B949E;
        font-size: {small_font}px;
        background-color: transparent;
        padding: 3px 8px;
        border: none;
    }}

    QFrame#CardFrame {{
        background-color: #161B22;
        border: 1px solid #30363D;
        border-radius: {radius}px;
    }}

    QLabel#SectionHeader {{
        color: #79C0FF;
        font-size: {small_font}px;
        font-weight: 700;
        letter-spacing: 0.5px;
    }}

    QCheckBox {{
        color: #F0F6FC;
        font-size: {base_font}px;
        font-weight: 600;
        spacing: {max(6, int(round(8 * scale)))}px;
    }}

    QCheckBox:disabled {{
        color: #6E7681;
    }}

    QLineEdit, QComboBox, QListWidget, QSpinBox {{
        background-color: #161B22;
        color: #F0F6FC;
        border: 1px solid #30363D;
        border-radius: {radius}px;
        padding: {input_pad_v}px {input_pad_h}px;
        font-size: {base_font}px;
        selection-background-color: #1F6FEB;
        selection-color: #FFFFFF;
    }}

    QLineEdit:focus, QComboBox:focus, QListWidget:focus, QSpinBox:focus {{
        border: 2px solid #388BFD;
    }}

    QComboBox:disabled {{
        background-color: #1A1E24;
        color: #6E7681;
        border-color: #21262D;
    }}

    QComboBox::drop-down {{
        border: none;
        width: {max(24, int(round(32 * scale)))}px;
    }}

    QComboBoxPrivateContainer {{
        background-color: #161B22;
        color: #F0F6FC;
        border: 1px solid #30363D;
    }}

    QComboBox QAbstractItemView {{
        background-color: #161B22;
        color: #F0F6FC;
        border: 1px solid #30363D;
        selection-background-color: #1F6FEB;
        selection-color: #FFFFFF;
        outline: 0px;
        padding: 4px;
    }}

    QComboBox QAbstractItemView::item {{
        background-color: #161B22;
        color: #F0F6FC;
        padding: {input_pad_v}px {input_pad_h}px;
        min-height: {max(22, int(round(28 * scale)))}px;
    }}

    QComboBox QAbstractItemView::item:hover,
    QComboBox QAbstractItemView::item:selected {{
        background-color: #1F6FEB;
        color: #FFFFFF;
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
    font_px = max(15, int(round(22 * scale)))
    radius = max(12, int(round(18 * scale)))
    pad_v = max(6, int(round(8 * scale)))
    pad_h = max(8, int(round(12 * scale)))

    if state == "recording":
        # Red when actively recording
        return f"""
        QPushButton {{
            background-color: #D72638;
            color: #FFFFFF;
            border: 3px solid #FF6B7A;
            border-radius: {radius}px;
            padding: {pad_v}px {pad_h}px;
            font-size: {font_px}px;
            font-weight: bold;
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
            padding: {pad_v}px {pad_h}px;
            font-size: {font_px}px;
            font-weight: bold;
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
        padding: {pad_v}px {pad_h}px;
        font-size: {font_px}px;
        font-weight: bold;
    }}
    """


def build_talkback_button_style(active: bool, enabled: bool, scale: float = 1.0) -> str:
    """Return stylesheet for the Studio Mode Talkback momentary push-to-talk button."""
    font_px = max(14, int(round(20 * scale)))
    radius = max(12, int(round(16 * scale)))
    pad_v = max(6, int(round(8 * scale)))
    pad_h = max(8, int(round(10 * scale)))

    if not enabled:
        return f"""
        QPushButton {{
            background-color: #1C2128;
            color: #545D68;
            border: 3px dashed #30363D;
            border-radius: {radius}px;
            padding: {pad_v}px {pad_h}px;
            font-size: {font_px}px;
            font-weight: bold;
        }}
        """
    if active:
        return f"""
        QPushButton {{
            background-color: #E36209;
            color: #FFFFFF;
            border: 3px solid #FFDF5D;
            border-radius: {radius}px;
            padding: {pad_v}px {pad_h}px;
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
        padding: {pad_v}px {pad_h}px;
        font-size: {font_px}px;
        font-weight: bold;
    }}
    QPushButton:hover {{
        background-color: #264875;
    }}
    """


def build_pause_record_button_style(
    state: str, scale: float = 1.0, blink_on: bool = True
) -> str:
    """Return stylesheet for the Pause/Resume Recording button ('active', 'paused', 'disabled')."""
    font_px = max(14, int(round(20 * scale)))
    radius = max(12, int(round(16 * scale)))
    pad_v = max(6, int(round(8 * scale)))
    pad_h = max(8, int(round(10 * scale)))

    if state == "paused":
        if blink_on:
            return f"""
            QPushButton {{
                background-color: #E36209;
                color: #FFFFFF;
                border: 3px solid #FFDF5D;
                border-radius: {radius}px;
                padding: {pad_v}px {pad_h}px;
                font-size: {font_px}px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: #F0883E;
            }}
            QPushButton:pressed {{
                background-color: #BD561D;
            }}
            """
        return f"""
        QPushButton {{
            background-color: #5A3000;
            color: #FFDF5D;
            border: 3px solid #F0B72F;
            border-radius: {radius}px;
            padding: {pad_v}px {pad_h}px;
            font-size: {font_px}px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: #7A4300;
        }}
        QPushButton:pressed {{
            background-color: #3B2300;
        }}
        """

    if state == "active":
        return f"""
        QPushButton {{
            background-color: #9E6A03;
            color: #FFFFFF;
            border: 3px solid #F0B72F;
            border-radius: {radius}px;
            padding: {pad_v}px {pad_h}px;
            font-size: {font_px}px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: #BB8009;
        }}
        QPushButton:pressed {{
            background-color: #7D5200;
        }}
        """

    return f"""
    QPushButton {{
        background-color: #1C2128;
        color: #545D68;
        border: 3px solid #30363D;
        border-radius: {radius}px;
        padding: {pad_v}px {pad_h}px;
        font-size: {font_px}px;
        font-weight: bold;
    }}
    """


def build_system_resource_badge_style(level: str, scale: float = 1.0) -> str:
    """Return stylesheet for the bottom-bar CPU & RAM usage indicator badge."""
    font_px = max(9, int(round(11 * scale)))
    pad_v = max(2, int(round(2 * scale)))
    pad_h = max(6, int(round(8 * scale)))
    radius = max(4, int(round(5 * scale)))

    if level == "critical":
        return (
            f"background-color: #6E111A; color: #FFFFFF; border: 1px solid #FF6B7A; "
            f"border-radius: {radius}px; padding: {pad_v}px {pad_h}px; "
            f"font-family: monospace; font-weight: bold; font-size: {font_px}px;"
        )
    if level == "warning":
        return (
            f"background-color: #3B2300; color: #FFDF5D; border: 1px solid #D29922; "
            f"border-radius: {radius}px; padding: {pad_v}px {pad_h}px; "
            f"font-family: monospace; font-weight: bold; font-size: {font_px}px;"
        )
    return (
        f"background-color: #161B22; color: #8B949E; border: 1px solid #21262D; "
        f"border-radius: {radius}px; padding: {pad_v}px {pad_h}px; "
        f"font-family: monospace; font-weight: 600; font-size: {font_px}px;"
    )


def build_studio_clock_badge_style(scale: float = 1.0) -> str:
    """Return stylesheet for the compact studio clock badge in the status strip."""
    font_px = max(12, int(round(15 * scale)))
    pad_v = max(2, int(round(3 * scale)))
    pad_h = max(10, int(round(14 * scale)))
    radius = max(5, int(round(6 * scale)))
    return (
        f"background-color: #161B22; color: #F0F6FC; border: 1px solid #30363D; "
        f"border-radius: {radius}px; padding: {pad_v}px {pad_h}px; "
        f"font-family: monospace; font-weight: bold; font-size: {font_px}px;"
    )


def build_playback_button_style(state: str, scale: float = 1.0) -> str:
    """Return stylesheet for the recordings list Play / Pause / Resume control button."""
    font_px = max(11, int(round(13 * scale)))
    pad_v = max(4, int(round(6 * scale)))
    pad_h = max(10, int(round(14 * scale)))
    radius = max(4, int(round(6 * scale)))

    if state == "playing":
        return f"""
        QPushButton {{
            background-color: #0D2744;
            color: #79C0FF;
            border: 2px solid #58A6FF;
            border-radius: {radius}px;
            padding: {pad_v}px {pad_h}px;
            font-size: {font_px}px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: #13365C;
            border-color: #79C0FF;
        }}
        QPushButton:pressed {{
            background-color: #091B30;
        }}
        QPushButton:disabled {{
            background-color: #161B22;
            color: #484F58;
            border-color: #21262D;
        }}
        """

    if state == "paused":
        return f"""
        QPushButton {{
            background-color: #341A04;
            color: #FFDF5D;
            border: 2px solid #D29922;
            border-radius: {radius}px;
            padding: {pad_v}px {pad_h}px;
            font-size: {font_px}px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: #4A2506;
            border-color: #F0B72F;
        }}
        QPushButton:pressed {{
            background-color: #261202;
        }}
        QPushButton:disabled {{
            background-color: #161B22;
            color: #484F58;
            border-color: #21262D;
        }}
        """

    return f"""
    QPushButton {{
        background-color: #1F242C;
        color: #E6EDF3;
        border: 1px solid #30363D;
        border-radius: {radius}px;
        padding: {pad_v}px {pad_h}px;
        font-size: {font_px}px;
        font-weight: 600;
    }}
    QPushButton:hover {{
        background-color: #292F3A;
        border-color: #58A6FF;
    }}
    QPushButton:pressed {{
        background-color: #161B22;
    }}
    QPushButton:disabled {{
        background-color: #161B22;
        color: #484F58;
        border-color: #21262D;
    }}
    """


def build_stop_playback_button_style(active: bool, scale: float = 1.0) -> str:
    """Return stylesheet for the recordings list Stop playback button."""
    font_px = max(11, int(round(13 * scale)))
    pad_v = max(4, int(round(6 * scale)))
    pad_h = max(10, int(round(14 * scale)))
    radius = max(4, int(round(6 * scale)))

    if active:
        return f"""
        QPushButton {{
            background-color: #3D1114;
            color: #FFA198;
            border: 2px solid #F85149;
            border-radius: {radius}px;
            padding: {pad_v}px {pad_h}px;
            font-size: {font_px}px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: #54171C;
            border-color: #FF7B72;
        }}
        QPushButton:pressed {{
            background-color: #2B0B0E;
        }}
        QPushButton:disabled {{
            background-color: #161B22;
            color: #484F58;
            border-color: #21262D;
        }}
        """

    return f"""
    QPushButton {{
        background-color: #1F242C;
        color: #E6EDF3;
        border: 1px solid #30363D;
        border-radius: {radius}px;
        padding: {pad_v}px {pad_h}px;
        font-size: {font_px}px;
        font-weight: 600;
    }}
    QPushButton:disabled {{
        background-color: #161B22;
        color: #484F58;
        border-color: #21262D;
    }}
    """


