"""Main fullscreen studio window for the Alpine Recording Station."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QPainter,
    QPen,
    QResizeEvent,
)
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.audio_engine import AudioDeviceCapabilities, AudioEngine
from app.config_manager import (
    ConfigManager,
    RecordingEntry,
    format_clock_display,
    generate_recording_filename,
    sanitize_project_name,
)
from app.network_info import (
    SystemResourceMonitor,
    SystemResourceStatus,
    get_primary_ip_display,
)
from app.remote_producer import RemoteProducerServer
from app.samba_manager import SambaManager
from app.ui.file_manager_dialog import (
    DeleteFileConfirmDialog,
    StorageFileManagerDialog,
)
from app.ui.on_screen_keyboard import OnScreenKeyboardController
from app.ui.remote_producer_dialog import RemoteProducerDialog
from app.ui.settings_dialog import SettingsDialog
from app.ui.styles import (
    build_main_stylesheet,
    build_pause_record_button_style,
    build_playback_button_style,
    build_record_button_style,
    build_stop_playback_button_style,
    build_studio_clock_badge_style,
    build_system_resource_badge_style,
    build_talkback_button_style,
    compute_scale_factor,
)
from app.ui.storage_monitor import StorageMonitorWidget
from app.ui.vu_meter import VuMeterWidget

logger = logging.getLogger(__name__)

ROLE_RECORDING_PATH = int(Qt.ItemDataRole.UserRole)
ROLE_PLAYBACK_ACTIVE = int(Qt.ItemDataRole.UserRole) + 1
ROLE_PLAYBACK_PAUSED = int(Qt.ItemDataRole.UserRole) + 2
ROLE_PLAYBACK_PROGRESS = int(Qt.ItemDataRole.UserRole) + 3
ROLE_PLAYBACK_POS_SEC = int(Qt.ItemDataRole.UserRole) + 4
ROLE_PLAYBACK_DUR_SEC = int(Qt.ItemDataRole.UserRole) + 5
ROLE_RECORDING_FILENAME = int(Qt.ItemDataRole.UserRole) + 6
ROLE_RECORDING_META = int(Qt.ItemDataRole.UserRole) + 7


def format_playback_time(seconds: float) -> str:
    """Format playback position or duration in seconds as MM:SS (or HH:MM:SS)."""
    total = max(0, int(round(seconds)))
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_clip_progress_bar(ratio: float, width: int = 10) -> str:
    """Build a compact inline BMP playhead bar (e.g. '━━━●──────  35%') for a playing clip row."""
    clamped = max(0.0, min(1.0, float(ratio)))
    w = max(4, int(width))
    head_idx = min(w - 1, int(round(clamped * (w - 1))))
    before = "━" * head_idx
    after = "─" * max(0, w - 1 - head_idx)
    pct = int(round(clamped * 100.0))
    return f"{before}●{after} {pct:3d}%"


def abbreviate_text_to_width(
    text: str, font_metrics: QFontMetrics, max_width_px: float
) -> str:
    """Abbreviate text with '...' if its pixel width exceeds max_width_px."""
    raw = str(text or "")
    limit = max(24, int(max_width_px))
    if font_metrics.horizontalAdvance(raw) <= limit:
        return raw
    ellipsis = "..."
    ell_w = font_metrics.horizontalAdvance(ellipsis)
    if limit <= ell_w + 4:
        return ellipsis
    lo = 0
    hi = len(raw)
    best = ellipsis
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = raw[:mid].rstrip() + ellipsis
        if font_metrics.horizontalAdvance(candidate) <= limit:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


class RecordingClipItemDelegate(QStyledItemDelegate):
    """Custom list delegate for project recordings.

    - Draws a proportional background progress fill on the actively playing clip row
      (without a redundant slider bar).
    - Reserves space on the rightmost side of the selected row for the inline Play/Pause button.
    - Abbreviates long filenames with '...' when unselected so columns never overlap,
      and smoothly scrolls long filenames horizontally when selected to reveal the full name.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scale: float = 1.0
        self._inline_button_reserve_px: int = 128
        self._scroll_start_mono: float = time.monotonic()
        self._selected_row_scrolling: bool = False

    @property
    def is_selected_row_scrolling(self) -> bool:
        return self._selected_row_scrolling

    def apply_scale(self, scale: float, inline_button_width: int = 120) -> None:
        self._scale = max(0.55, float(scale))
        self._inline_button_reserve_px = max(84, int(inline_button_width))

    def reset_scroll_timer(self) -> None:
        self._scroll_start_mono = time.monotonic()

    @staticmethod
    def compute_name_scroll_offset(
        overflow_px: float, elapsed_sec: float, scale: float = 1.0
    ) -> float:
        """Compute horizontal marquee pixel offset for a selected row whose name overflows."""
        if overflow_px <= 0.5:
            return 0.0
        pause_start = 0.85
        pause_end = 1.10
        speed_px_per_sec = max(32.0, 52.0 * max(0.7, float(scale)))
        scroll_dur = max(0.4, float(overflow_px) / speed_px_per_sec)
        cycle_dur = pause_start + scroll_dur + pause_end
        t = max(0.0, float(elapsed_sec)) % cycle_dur
        if t <= pause_start:
            return 0.0
        if t >= pause_start + scroll_dur:
            return float(overflow_px)
        return min(float(overflow_px), (t - pause_start) * speed_px_per_sec)

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # type: ignore[override]
        base = super().sizeHint(option, index)
        row_h = max(34, int(round(38 * self._scale)))
        return QSize(base.width(), max(base.height(), row_h))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index,
    ) -> None:
        is_active = bool(index.data(ROLE_PLAYBACK_ACTIVE))
        is_paused = bool(index.data(ROLE_PLAYBACK_PAUSED))
        raw_prog = index.data(ROLE_PLAYBACK_PROGRESS)
        progress = max(0.0, min(1.0, float(raw_prog if raw_prog is not None else 0.0)))
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        filename = str(index.data(ROLE_RECORDING_FILENAME) or "")
        meta_text = str(index.data(ROLE_RECORDING_META) or "")
        if not filename:
            raw_display = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            parts = raw_display.split("    |    ", 1)
            filename = parts[0]
            for badge_prefix in ("▶ PLAYING  ", "❚❚ PAUSED  "):
                if filename.startswith(badge_prefix):
                    filename = filename[len(badge_prefix) :]
                    break
            meta_text = parts[1] if len(parts) > 1 else ""

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(option.rect).adjusted(2.0, 1.0, -2.0, -1.0)
        radius = 5.0

        # 1. Row background & proportional background progress meter (when active)
        if is_active:
            if is_paused:
                bg_color = QColor("#241A08") if is_selected else QColor("#1B1408")
                fill_color = QColor(210, 153, 34, 86)
                accent_color = QColor("#F0B72F")
                name_color = QColor("#FFDF5D")
                meta_color = QColor("#F0B72F")
            else:
                bg_color = QColor("#0E2238") if is_selected else QColor("#0B1929")
                fill_color = QColor(56, 139, 253, 90)
                accent_color = QColor("#58A6FF")
                name_color = QColor("#79C0FF")
                meta_color = QColor("#A5D6FF")

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg_color)
            painter.drawRoundedRect(rect, radius, radius)

            # Background progress fill across the playing clip row (no slider bar)
            if progress > 0.0:
                fill_w = max(4.0, rect.width() * progress)
                fill_rect = QRectF(rect.left(), rect.top(), fill_w, rect.height())
                painter.setBrush(fill_color)
                painter.drawRoundedRect(fill_rect, radius, radius)

            border_pen = QPen(accent_color, 1.8 if is_selected else 1.0)
            painter.setPen(border_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, radius, radius)
        elif is_selected:
            painter.setPen(QPen(QColor("#388BFD"), 1.5))
            painter.setBrush(QColor("#1A2B42"))
            painter.drawRoundedRect(rect, radius, radius)
            name_color = QColor("#FFFFFF")
            meta_color = QColor("#C9D1D9")
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#161B22"))
            painter.drawRoundedRect(rect, radius, radius)
            painter.setPen(QPen(QColor("#21262D"), 1.0))
            painter.drawLine(
                int(rect.left() + 6.0),
                int(rect.bottom()),
                int(rect.right() - 6.0),
                int(rect.bottom()),
            )
            name_color = QColor("#E6EDF3")
            meta_color = QColor("#8B949E")

        # 2. Non-overlapping column layout (reserving right space on selected row for Play/Pause button)
        pad_l = max(10.0, round(12.0 * self._scale))
        if is_selected:
            pad_r = float(self._inline_button_reserve_px) + max(14.0, round(18.0 * self._scale))
        else:
            pad_r = max(10.0, round(12.0 * self._scale))

        font = QFont(option.font)
        font.setBold(bool(is_active or is_selected))
        painter.setFont(font)
        fm = painter.fontMetrics()

        avail_total_w = max(80.0, rect.width() - pad_l - pad_r)
        meta_sep = f"    |    {meta_text}" if meta_text else ""
        meta_w = float(fm.horizontalAdvance(meta_sep)) if meta_sep else 0.0
        max_meta_w = avail_total_w * 0.62
        if meta_w > max_meta_w and meta_sep:
            meta_sep = abbreviate_text_to_width(meta_sep, fm, max_meta_w)
            meta_w = float(fm.horizontalAdvance(meta_sep))

        badge_str = ("❚❚ PAUSED  " if is_paused else "▶ PLAYING  ") if is_active else ""
        badge_w = float(fm.horizontalAdvance(badge_str)) if badge_str else 0.0

        avail_name_w = max(48.0, avail_total_w - meta_w - badge_w)
        cursor_x = rect.left() + pad_l

        # Draw active state badge ('▶ PLAYING  ' or '❚❚ PAUSED  ')
        if badge_str:
            painter.setPen(name_color)
            badge_rect = QRectF(cursor_x, rect.top(), badge_w, rect.height())
            painter.drawText(
                badge_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                badge_str,
            )
            cursor_x += badge_w

        # Draw filename (abbreviated when unselected and too long; scrolling when selected and too long)
        full_name_w = float(fm.horizontalAdvance(filename))
        name_rect = QRectF(cursor_x, rect.top(), avail_name_w, rect.height())
        painter.setPen(name_color)

        if full_name_w <= avail_name_w:
            if is_selected:
                self._selected_row_scrolling = False
            painter.drawText(
                name_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                filename,
            )
            meta_start_x = cursor_x + full_name_w
        elif not is_selected:
            abbreviated = abbreviate_text_to_width(filename, fm, avail_name_w)
            painter.drawText(
                name_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                abbreviated,
            )
            meta_start_x = cursor_x + avail_name_w
        else:
            # Selected row with long filename: smoothly scroll to show entire name
            self._selected_row_scrolling = True
            overflow_px = full_name_w - avail_name_w
            offset_px = self.compute_name_scroll_offset(
                overflow_px,
                time.monotonic() - self._scroll_start_mono,
                self._scale,
            )
            painter.save()
            painter.setClipRect(name_rect)
            scroll_draw_rect = QRectF(
                name_rect.left() - offset_px,
                name_rect.top(),
                full_name_w + 8.0,
                name_rect.height(),
            )
            painter.drawText(
                scroll_draw_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                filename,
            )
            painter.restore()
            meta_start_x = cursor_x + avail_name_w

        # Draw metadata (elapsed/total time or duration, format, size)
        if meta_sep:
            painter.setPen(name_color if is_active else meta_color)
            meta_rect = QRectF(
                meta_start_x,
                rect.top(),
                max(0.0, rect.right() - pad_r - meta_start_x),
                rect.height(),
            )
            painter.drawText(
                meta_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                meta_sep,
            )

        painter.restore()



class ProjectPrefixDialog(QDialog):
    """Modal dialog for viewing and editing the Project Name & Subfolder."""

    def __init__(
        self,
        config_manager: ConfigManager,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.setWindowTitle("Set Project Name & Subfolder")
        self.setModal(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        if parent is not None and parent.width() >= 480:
            pw, ph = parent.width(), parent.height()
            self._scale = compute_scale_factor(pw, ph)
            self.resize(max(480, int(pw * 0.52)), max(240, int(ph * 0.36)))
        else:
            self._scale = 1.0
            self.resize(520, 260)

        self.osk_controller = OnScreenKeyboardController(
            is_enabled=lambda: bool(self.config_manager.settings.on_screen_keyboard),
            parent=self,
        )

        self._build_ui()
        self.setStyleSheet(build_main_stylesheet(self._scale))
        self.osk_controller.register_line_edit(
            self.prefix_edit,
            title="Project Name",
            hint="Stored in subfolder: <ProjectName>/<ProjectName>_yyyy-mm-dd_HH.MM.SS_TZ.wav",
        )
        self.osk_controller.attach_to_container(self)
        self._update_preview(self.prefix_edit.text())

    @property
    def project_name(self) -> str:
        return self.prefix_edit.text().strip()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        title_label = QLabel("Project Name && Subfolder")
        title_label.setObjectName("HeaderTitle")
        layout.addWidget(title_label)

        desc_label = QLabel(
            "Enter the project name. All recordings for this project are stored inside a matching "
            "subfolder in /storage and can be renamed directly from the UI after recording."
        )
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #8B949E;")
        layout.addWidget(desc_label)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self.prefix_edit = QLineEdit(self.config_manager.settings.project_name)
        self.prefix_edit.setPlaceholderText("Enter Project Name...")
        self.prefix_edit.textChanged.connect(self._update_preview)
        self.prefix_edit.returnPressed.connect(self.accept)
        input_row.addWidget(self.prefix_edit, stretch=1)

        self.osk_btn = QPushButton("⌨ Touch Keyboard")
        self.osk_btn.setToolTip(
            "Open On-Screen Touch Keyboard to edit Project Name"
        )
        self.osk_btn.setVisible(bool(self.config_manager.settings.on_screen_keyboard))
        self.osk_btn.clicked.connect(
            lambda: self.osk_controller.open_for_line_edit(self.prefix_edit)
        )
        input_row.addWidget(self.osk_btn)
        layout.addLayout(input_row)

        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet(
            "background-color: #0D1014; color: #58A6FF; font-family: monospace; "
            "padding: 8px 10px; border-radius: 6px; border: 1px solid #21262D;"
        )
        layout.addWidget(self.preview_label)

        layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Discard changes and close [Hotkey: Esc]")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("Save && Apply")
        self.save_btn.setToolTip("Save the Project Name and update the project subfolder recordings list [Hotkey: Enter]")
        self.save_btn.setDefault(True)
        self.save_btn.setStyleSheet(
            "background-color: #1F6FEB; color: #FFFFFF; font-weight: bold;"
        )
        self.save_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.save_btn)

        layout.addLayout(btn_row)

    def _update_preview(self, text: str) -> None:
        folder = sanitize_project_name(text)
        sample = generate_recording_filename(text)
        self.preview_label.setText(f"Next: {folder}/{sample}")


class RenameRecordingDialog(QDialog):
    """Modal dialog for renaming a completed WAV recording within its project folder."""

    def __init__(
        self,
        config_manager: ConfigManager,
        filepath: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.filepath = Path(filepath)
        self.renamed_path: Optional[Path] = None

        self.setWindowTitle("Rename Recording")
        self.setModal(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        if parent is not None and parent.width() >= 480:
            pw, ph = parent.width(), parent.height()
            self._scale = compute_scale_factor(pw, ph)
            self.resize(max(500, int(pw * 0.56)), max(260, int(ph * 0.40)))
        else:
            self._scale = 1.0
            self.resize(540, 280)

        self.osk_controller = OnScreenKeyboardController(
            is_enabled=lambda: bool(self.config_manager.settings.on_screen_keyboard),
            parent=self,
        )

        self._build_ui()
        self.setStyleSheet(build_main_stylesheet(self._scale))
        self.osk_controller.register_line_edit(
            self.name_edit,
            title="Rename Recording",
            hint=f"Folder: {self.filepath.parent.name}/  (.wav added automatically)",
        )
        self.osk_controller.attach_to_container(self)
        self._update_preview(self.name_edit.text())

    @property
    def new_filename(self) -> str:
        return self.name_edit.text().strip()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title_label = QLabel("Rename Recording File")
        title_label.setObjectName("HeaderTitle")
        layout.addWidget(title_label)

        desc_label = QLabel(
            f"Enter a new name to categorize this recording inside '{self.filepath.parent.name}/'.\n"
            f"Current file: {self.filepath.name}"
        )
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #8B949E;")
        layout.addWidget(desc_label)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        initial_stem = (
            self.filepath.name[:-4]
            if self.filepath.name.lower().endswith(".wav")
            else self.filepath.stem
        )
        self.name_edit = QLineEdit(initial_stem)
        self.name_edit.setPlaceholderText("Enter new filename (e.g. Character_Line_01_Select)...")
        self.name_edit.selectAll()
        self.name_edit.textChanged.connect(self._update_preview)
        self.name_edit.returnPressed.connect(self._on_save_clicked)
        input_row.addWidget(self.name_edit, stretch=1)

        self.osk_btn = QPushButton("⌨ Touch Keyboard")
        self.osk_btn.setToolTip(
            "Open On-Screen Touch Keyboard to rename the recording"
        )
        self.osk_btn.setVisible(bool(self.config_manager.settings.on_screen_keyboard))
        self.osk_btn.clicked.connect(
            lambda: self.osk_controller.open_for_line_edit(self.name_edit)
        )
        input_row.addWidget(self.osk_btn)
        layout.addLayout(input_row)

        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet(
            "background-color: #0D1014; color: #58A6FF; font-family: monospace; "
            "padding: 8px 10px; border-radius: 6px; border: 1px solid #21262D;"
        )
        layout.addWidget(self.preview_label)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        self.error_label.setStyleSheet("color: #F85149; font-weight: bold;")
        layout.addWidget(self.error_label)

        layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Cancel renaming and close [Hotkey: Esc]")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("Rename File")
        self.save_btn.setToolTip("Rename the WAV file on disk [Hotkey: Enter]")
        self.save_btn.setDefault(True)
        self.save_btn.setStyleSheet(
            "background-color: #1F6FEB; color: #FFFFFF; font-weight: bold;"
        )
        self.save_btn.clicked.connect(self._on_save_clicked)
        btn_row.addWidget(self.save_btn)

        layout.addLayout(btn_row)

    def _update_preview(self, text: str) -> None:
        self.error_label.setVisible(False)
        raw = (text or "").strip()
        if raw.lower().endswith(".wav"):
            raw = raw[:-4].strip()
        cleaned = sanitize_project_name(raw) if raw else ""
        folder = self.filepath.parent.name
        if cleaned:
            self.preview_label.setText(f"Target: {folder}/{cleaned}.wav")
        else:
            self.preview_label.setText(f"Target: {folder}/<enter filename>.wav")

    def _on_save_clicked(self) -> None:
        ok, msg, new_path = self.config_manager.rename_recording(
            self.filepath, self.name_edit.text()
        )
        if not ok or new_path is None:
            self.error_label.setText(msg)
            self.error_label.setVisible(True)
            return
        self.renamed_path = new_path
        self.accept()


class HelpDialog(QDialog):
    """Comprehensive Help & Reference Guide explaining UI controls, modes, and settings."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Help & Studio Reference Guide")
        self.setModal(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        if parent is not None and parent.width() >= 480:
            pw, ph = parent.width(), parent.height()
            self._scale = compute_scale_factor(pw, ph)
            self.resize(max(760, int(pw * 0.82)), max(520, int(ph * 0.84)))
        else:
            self._scale = 1.0
            self.resize(840, 580)

        self._build_ui()
        self.setStyleSheet(build_main_stylesheet(self._scale))

    def _build_section_card(self, title: str, body_html: str) -> QFrame:
        card = QFrame()
        card.setObjectName("CardFrame")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        hdr = QLabel(title)
        hdr.setObjectName("SectionHeader")
        layout.addWidget(hdr)

        lbl = QLabel(body_html)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #E6EDF3; line-height: 1.4;")
        layout.addWidget(lbl)
        return card

    def _wrap_scrollable(self, cards: List[QFrame]) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background-color: #161B22; border: none; }")

        container = QWidget()
        container.setStyleSheet("background-color: #161B22;")
        v_layout = QVBoxLayout(container)
        v_layout.setContentsMargins(14, 14, 14, 14)
        v_layout.setSpacing(12)
        for card in cards:
            v_layout.addWidget(card)
        v_layout.addStretch(1)
        scroll.setWidget(container)
        return scroll

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        hdr_row = QHBoxLayout()
        title = QLabel("?  Alpine Recording Station — User Guide && Reference")
        title.setObjectName("HeaderTitle")
        hdr_row.addWidget(title)
        hdr_row.addStretch(1)
        sub = QLabel("Hotkey: [F1]")
        sub.setStyleSheet("color: #8B949E; font-family: monospace; font-weight: bold;")
        hdr_row.addWidget(sub)
        root.addLayout(hdr_row)

        tabs = QTabWidget()

        # Tab 1: Studio Deck & Transport Controls
        tab_controls = self._wrap_scrollable([
            self._build_section_card(
                "RECORD / RECORDING  [Hotkey: R or Space]",
                "<b>Record</b> (Green) starts capturing audio to a new timestamped WAV file inside the "
                "project subfolder <code>/storage/&lt;ProjectName&gt;/</code> named "
                "<code>&lt;ProjectName&gt;_yyyy-mm-dd_HH.MM.SS_TZ.wav</code>.<br>"
                "While recording is active, the button displays <b>Recording</b> (Red). Clicking "
                "<b>Recording</b> stops capture and finalizes the WAV header on disk."
            ),
            self._build_section_card(
                "PAUSE / PAUSED  [Hotkey: P or Pause]",
                "<b>Pause</b> suspends an active recording. While active, the button displays "
                "<b>Paused</b> and flashes amber along with the Studio Deck border.<br>"
                "<b>Safe Disk Closure:</b> When paused, the WAV file header is immediately updated, "
                "flushed, and closed on disk so the take is safe even if power is lost. Clicking "
                "<b>Paused</b> reopens the exact same WAV file and seamlessly continues appending audio."
            ),
            self._build_section_card(
                "TALKBACK  [Hotkey: T]",
                "Available in <b>Studio Mode</b> for slating, producer talkback, and session notes:<br>"
                "• <b>When Idle (Not Recording):</b> Click <b>Talkback</b> (or press <code>[T]</code>) to "
                "<b>latch</b> Talkback ON for hands-free conversation. Click or press <code>[T]</code> again "
                "to unlatch. Starting a recording automatically unlatches Talkback and switches it to "
                "momentary Push-to-Talk.<br>"
                "• <b>While Recording (Push-to-Talk):</b> Hold down <b>Talkback</b> (or hold <code>[T]</code>) "
                "for momentary push-to-talk slating and session notes; releasing immediately ends Talkback.<br>"
                "• <b>Temporary Unmute During Talkback:</b> If the talent's microphone is muted via the "
                "<b>Cough / Muted</b> button, activating <b>Talkback</b> temporarily unmutes the microphone "
                "while Talkback is active and restores the mute when released.<br>"
                "• <b>Shared Producer && Talent Talkback:</b> If both Producer and Talent press Talkback "
                "simultaneously, Talkback stays open seamlessly without extra marker tones until both release.<br>"
                "• Mutes the Left (Voiceover) channel so notes stay off the primary performance track.<br>"
                "• Emits a configurable press marker tone into the headset and on the Right (Talkback) channel so talent knows Talkback is active.<br>"
                "• Records spoken notes onto the Right channel while active (and sends them to the Remote Producer).<br>"
                "• Emits a configurable release marker tone into the headset and on the Right channel when released."
            ),
            self._build_section_card(
                "COUGH / MUTED  [Hotkey: Hold B or Click]",
                "Dual-function talent mute and privacy control:<br>"
                "• <b>Hold Down (or Hold [B] — Momentary Cough Mute):</b> Mutes the local microphone "
                "completely (both to the local recording and to the Remote Producer) for as long as the "
                "button is held. Releasing immediately unmutes.<br>"
                "• <b>Quick Click — Mute Toggle:</b> Toggles microphone mute on or off, "
                "silencing the microphone input, the VU meter, and the Remote Producer feed.<br>"
                "• <b>Flashing Red Alert && Silent VU Meter:</b> Whenever either momentary Cough Mute or "
                "Mute Toggle is active, the button displays <b>Muted</b>, flashes bright red, and the "
                "VU meter goes silent to confirm no microphone input is being taken."
            ),
        ])
        tabs.addTab(tab_controls, "Studio Controls")

        # Tab 2: Meters, Storage & Playback
        tab_meters = self._wrap_scrollable([
            self._build_section_card(
                "INPUT VU && PEAK HOLD METER  [Hotkey: C to Reset]",
                "Displays real-time input levels from <code>-60 dBFS</code> to <code>0 dBFS</code> "
                "during both standby monitoring and active recording:<br>"
                "• <b>Green (-60 to -18 dBFS):</b> Normal signal floor and low-level speech.<br>"
                "• <b>Amber (-18 to -6 dBFS):</b> Optimal nominal voiceover peak target zone.<br>"
                "• <b>Red (-6 to 0 dBFS):</b> Hot signal approaching digital full scale.<br>"
                "• <b>PEAK HOLD && CLIP:</b> The bright vertical marker and readout retain your maximum "
                "peak level for setting preamp gain. <b>CLIP!</b> lights red at <code>-0.1 dBFS</code>. "
                "Click the meter or press <code>[C]</code> to reset."
            ),
            self._build_section_card(
                "STORAGE CAPACITY && RECORDING TIME REMAINING",
                "Continuously monitors the <code>/storage</code> partition and calculates remaining "
                "recording time based on your active Sample Rate, Bit Depth, and Channel count:<br>"
                "• <b>Blue (0%–80% used):</b> Normal available capacity.<br>"
                "• <b>Amber (80%–90% used):</b> Warning threshold — offload takes via Samba soon.<br>"
                "• <b>Red (90%–100% used):</b> Critical threshold — flashes during recording."
            ),
            self._build_section_card(
                "SYSTEM CPU && RAM OVERLOAD INDICATOR",
                "Displayed on the right side of the bottom status bar alongside the IP address and Samba share path:<br>"
                "• <b>Normal (CPU &lt; 75%, RAM &lt; 80%):</b> Subtle telemetry badge showing live CPU % and RAM usage.<br>"
                "• <b>High Load Warning (Amber — CPU ≥ 75% or RAM ≥ 80%):</b> Elevated system load.<br>"
                "• <b>Overload Alert (Red — CPU ≥ 90% or RAM ≥ 90%):</b> System is heavily loaded and at risk of audio dropouts or instability."
            ),
            self._build_section_card(
                "PROJECT SUBFOLDER, FILE RENAMING && PLAYBACK  [Hotkeys: N, P, S, F5, F6]",
                "• <b>Project Name && Subfolder:</b> Sets the project subfolder (<code>/storage/&lt;ProjectName&gt;/</code>) "
                "and default filename prefix for new WAV recordings, and shows all takes inside that project folder.<br>"
                "• <b>Rename Recording [N or F4]:</b> Allows you to rename any completed WAV file inside the project folder "
                "directly from the UI so takes can be categorized immediately instead of only having the project and timestamp.<br>"
                "• <b>Storage File Manager [F6]:</b> Opens the fullscreen Storage File Manager to browse, filter, sort, play, "
                "rename, and delete recordings across all projects, or clean up and delete project folders with extra confirmations "
                "while watching live drive utilization and space cleared.<br>"
                "• <b>Play / Pause [P] && Stop [S]:</b> Auditions the selected WAV take through the configured Playback Output "
                "Device and simultaneously streams the playback audio to the connected <b>Remote Producer</b> so talent and "
                "producer can review a take together. The active clip's row in the list displays a live progress bar, playhead "
                "position, and elapsed/total time (use <code>[Left]</code> / <code>[Right]</code> arrow keys to seek ±3s).<br>"
                "• <b>Refresh [F5]:</b> Rescans <code>/storage</code> and connected USB audio hardware."
            ),
        ])
        tabs.addTab(tab_meters, "Meters && Playback")

        # Tab 3: Remote Producer (Guided VO) & SSL
        tab_remote = self._wrap_scrollable([
            self._build_section_card(
                "REMOTE PRODUCER (GUIDED VO)  [Hotkey: F3]",
                "Provides a low-latency, bidirectional HTTPS/WSS web console for a remote producer or "
                "director to listen to the session live and prompt the talent using Talkback."
            ),
            self._build_section_card(
                "MIX-MINUS && STUDIO MODE RIGHT-CHANNEL ROUTING",
                "• <b>Mix-Minus Monitoring:</b> The remote producer hears only the local talent microphone "
                "(when not muted), and the local talent headphones hear only the remote producer during "
                "talkback — preventing echo or feedback loops.<br>"
                "• <b>Studio Mode Right Channel:</b> When either the remote producer or local talent engages "
                "Talkback in Studio Mode, the Left (VO) channel is muted, marker tones are inserted on the "
                "Right channel, and both the remote producer and local talent mic are mixed onto the Right "
                "(Talkback) channel so the full conversation is preserved."
            ),
            self._build_section_card(
                "QR CODE, SESSION LINK && SSL CERTIFICATES",
                "• <b>Session Authentication:</b> Each session has a unique Session ID, 6-digit PIN, and "
                "one-step invite token embedded in the QR code and HTTPS link. Clicking <b>New Session && "
                "Rotate Passcode</b> immediately revokes prior tokens and disconnects previous browsers.<br>"
                "• <b>SSL / TLS Certificates:</b> Uses an auto-generated X.509 v3 self-signed certificate "
                "by default. To replace it with an issued certificate, drop <code>server.crt</code> and "
                "<code>server.key</code> (or <code>fullchain.pem</code> / <code>privkey.pem</code>) into "
                "<code>/storage/ssl</code> (the <code>ssl/</code> folder on the Samba share) and click "
                "<b>Import Issued Cert</b>."
            ),
        ])
        tabs.addTab(tab_remote, "Remote Producer")

        # Tab 4: Settings, Samba Share & Keyboard Shortcuts
        tab_settings = self._wrap_scrollable([
            self._build_section_card(
                "AUDIO && RECORDING SETTINGS  [Hotkey: F2]",
                "• <b>Input / Output Audio Interface:</b> Select the ALSA hardware capture and playback devices.<br>"
                "• <b>Bit Depth && Sample Rate:</b> Defaults automatically to the highest bit depth (e.g. 24/32-bit) "
                "and sample rate supported by your USB audio interface.<br>"
                "• <b>Recording Mode:</b> Choose <b>Normal Mode</b> (standard 1ch Mono or 2ch Stereo) or "
                "<b>Studio Mode</b> (2ch Stereo with Left = Voiceover Mic and Right = Marker Tone + Talkback).<br>"
                "• <b>Mic Input Channel:</b> Selects which hardware input channel (e.g. Input 1 / Ch 0) feeds the "
                "Mono track or Studio Mode Left/Right tracks.<br>"
                "• <b>Talkback Tone Frequency && Durations:</b> Customize the marker tone pitch (330 Hz to 1000 Hz) "
                "and independent Press (default 1.0s) and Release (default 0.5s) durations.<br>"
                "• <b>On-Screen Keyboard:</b> Enables or disables the pop-up touch keyboard for text fields.<br>"
                "• <b>Studio Clock Display:</b> Choose 12-hour (AM/PM, default) or 24-hour clock format and "
                "select your preferred regional date format (or hide the date)."
            ),
            self._build_section_card(
                "SAMBA NETWORK SHARE ADMINISTRATION",
                "Manage authenticated usernames and passwords for accessing <code>\\\\&lt;Device-IP&gt;\\storage</code> "
                "over the network. All recorded files are created with read/write/delete permissions so any "
                "authorized Samba user can retrieve takes or clean up disk space."
            ),
            self._build_section_card(
                "KEYBOARD SHORTCUTS SUMMARY",
                "• <code>R</code> or <code>Space</code> : Start / Stop Recording<br>"
                "• <code>P</code> or <code>Pause</code> : Pause / Resume Recording (or Play / Pause selected take when idle)<br>"
                "• <code>T</code> : Toggle Talkback Latch when idle; Hold for Push-to-Talk while recording<br>"
                "• <code>Hold B</code> : Momentary Talent Cough Mute<br>"
                "• <code>C</code> : Reset VU Peak Hold && Clip Indicator<br>"
                "• <code>M</code> : Toggle Studio Mode / Normal Mode (when idle)<br>"
                "• <code>N</code> or <code>F4</code> : Rename selected project recording (when idle)<br>"
                "• <code>Del</code> : Delete selected recording with confirmation (when idle)<br>"
                "• <code>S</code> : Stop Reference Playback<br>"
                "• <code>F1</code> : Open Help && Reference Guide<br>"
                "• <code>F2</code> or <code>Ctrl+,</code> : Open Settings && Samba Administration<br>"
                "• <code>F3</code> : Open Remote Producer && SSL Setup<br>"
                "• <code>F5</code> : Refresh Storage && Audio Hardware<br>"
                "• <code>F6</code> : Open Fullscreen Storage File Manager (when idle)<br>"
                "• <code>F11</code> : Toggle Fullscreen / Windowed Mode<br>"
                "• <code>Ctrl+Q</code> : Exit to Linux Console (when idle)"
            ),
        ])
        tabs.addTab(tab_settings, "Settings && Hotkeys")

        root.addWidget(tabs, stretch=1)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        close_btn = QPushButton("Close Help")
        close_btn.setDefault(True)
        close_btn.setMinimumHeight(38)
        close_btn.setMinimumWidth(140)
        close_btn.setStyleSheet(
            "background-color: #1F6FEB; color: #FFFFFF; font-weight: bold;"
        )
        close_btn.clicked.connect(self.accept)
        bottom_row.addWidget(close_btn)
        root.addLayout(bottom_row)


class MainWindow(QMainWindow):
    """Primary responsive fullscreen UI for the Alpine Recording Station."""

    def __init__(
        self,
        config_manager: ConfigManager,
        audio_engine: AudioEngine,
        samba_manager: SambaManager,
        fullscreen: bool = True,
        remote_producer_server: Optional[RemoteProducerServer] = None,
        ssl_manager: Optional[object] = None,
    ) -> None:
        super().__init__()
        self.config_manager = config_manager
        self.audio_engine = audio_engine
        self.samba_manager = samba_manager
        self._fullscreen_requested = fullscreen

        # Sync initial Talent Mic Send (Privacy / Cough) state from saved settings
        self.audio_engine.set_talent_mic_send_enabled(
            bool(getattr(self.config_manager.settings, "remote_producer_send_talent_mic", True))
        )

        if remote_producer_server is not None:
            self.remote_producer_server = remote_producer_server
            self.remote_producer_server.status_provider = self._build_remote_producer_extra_status
        else:
            self.remote_producer_server = RemoteProducerServer(
                audio_engine=self.audio_engine,
                config_manager=self.config_manager,
                ssl_manager=ssl_manager,  # type: ignore[arg-type]
                status_provider=self._build_remote_producer_extra_status,
            )
            if getattr(self.config_manager.settings, "remote_producer_enabled", False):
                self.remote_producer_server.start(
                    port=int(getattr(self.config_manager.settings, "remote_producer_port", 8443))
                )

        self.setWindowTitle("Alpine Recording Station")
        self.setMinimumSize(640, 420)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        self._scale = 1.0
        self._active_input_device: Optional[AudioDeviceCapabilities] = None
        self._active_output_device: Optional[AudioDeviceCapabilities] = None
        self._project_recordings: List[RecordingEntry] = []
        self._talkback_key_held = False
        self._talkback_btn_press_mono: float = 0.0
        self._cough_key_held = False
        self._cough_btn_press_mono: float = 0.0
        self._last_observed_tb_active = False
        self._last_observed_tb_latched = False
        self._last_observed_remote_tb = False
        self._last_observed_producer_count = 0
        self._last_observed_mute_state = False
        self._pause_blink_phase = True
        self._last_storage_refresh_monotonic: float = 0.0
        self._last_resource_refresh_monotonic: float = 0.0
        self._last_clock_text: str = ""
        self.resource_monitor = SystemResourceMonitor()
        self._last_resource_status = SystemResourceStatus()

        self.osk_controller = OnScreenKeyboardController(
            is_enabled=lambda: bool(self.config_manager.settings.on_screen_keyboard),
            on_committed=self._on_osk_field_committed,
            parent=self,
        )

        self._build_ui()
        self.osk_controller.attach_to_container(self)
        self._sync_hardware_state()
        self._refresh_recordings_list()
        self._apply_responsive_scaling()

        # Fast UI timer (50ms / 20fps) for elapsed recording timer, peak meter & playback progress
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(50)
        self._ui_timer.timeout.connect(self._on_ui_tick)
        self._ui_timer.start()

        # Slow background timer (4000ms) for IP address refresh and USB hotplug detection
        self._hotplug_timer = QTimer(self)
        self._hotplug_timer.setInterval(4000)
        self._hotplug_timer.timeout.connect(self._on_background_tick)
        self._hotplug_timer.start()

    # -------------------------------------------------------------------------
    # UI Construction
    # -------------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        self.main_layout = QVBoxLayout(central)
        self.main_layout.setContentsMargins(16, 10, 16, 6)
        self.main_layout.setSpacing(8)

        # 1. Top Bar: Next Filename Preview, Remote Producer, Settings
        top_frame = QFrame()
        top_frame.setStyleSheet(
            "QFrame { background-color: #161B22; border: 1px solid #30363D; border-radius: 8px; }"
        )
        top_layout = QHBoxLayout(top_frame)
        top_layout.setContentsMargins(12, 8, 12, 8)
        top_layout.setSpacing(12)

        self.filename_preview_label = QLabel()
        self.filename_preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.filename_preview_label.setStyleSheet(
            "border: none; color: #8B949E; font-family: monospace;"
        )
        top_layout.addWidget(self.filename_preview_label, stretch=1)

        self.remote_producer_btn = QPushButton("◎ Remote Producer")
        self.remote_producer_btn.setToolTip(
            "Open Remote Producer (Guided VO) Session Setup, QR Code, Link, and SSL Certificate Management [Hotkey: F3]"
        )
        self.remote_producer_btn.clicked.connect(self._open_remote_producer_dialog)
        top_layout.addWidget(self.remote_producer_btn)

        self.settings_btn = QPushButton("⚙ Settings && Samba")
        self.settings_btn.setToolTip(
            "Open Audio Hardware, Recording Settings, and Samba Share User Administration [Hotkey: F2]"
        )
        self.settings_btn.clicked.connect(self._open_settings_dialog)
        top_layout.addWidget(self.settings_btn)

        self.help_btn = QPushButton("? Help")
        self.help_btn.setToolTip(
            "Open Help & Reference Guide explaining UI controls, modes, and settings [Hotkey: F1]"
        )
        self.help_btn.clicked.connect(self._open_help_dialog)
        top_layout.addWidget(self.help_btn)

        self.main_layout.addWidget(top_frame)

        # 2. Hardware, Studio Clock & Format Status Strip
        status_row = QHBoxLayout()
        status_row.setContentsMargins(4, 0, 4, 0)
        status_row.setSpacing(12)
        self.hw_status_label = QLabel()
        self.hw_status_label.setStyleSheet("color: #58A6FF; font-weight: 600;")
        status_row.addWidget(self.hw_status_label, stretch=1)

        self.clock_label = QLabel()
        self.clock_label.setObjectName("StudioClockBadge")
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clock_label.setToolTip(
            "Current local studio date and time (configure 12h/24h time and regional date format in Settings [F2])"
        )
        status_row.addWidget(self.clock_label)

        self.format_badge_label = QLabel()
        self.format_badge_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.format_badge_label.setStyleSheet(
            "color: #8B949E; font-family: monospace; font-weight: bold;"
        )
        status_row.addWidget(self.format_badge_label, stretch=1)
        self.main_layout.addLayout(status_row)

        # 3. Center Studio Deck: Elapsed Time Counter + Record/Pause/Stop + Talkback Button
        self.deck_frame = QFrame()
        self.deck_frame.setStyleSheet(
            "QFrame#StudioDeck { background-color: #0D1014; border: 1px solid #21262D; border-radius: 12px; }"
        )
        self.deck_frame.setObjectName("StudioDeck")
        deck_layout = QVBoxLayout(self.deck_frame)
        deck_layout.setContentsMargins(16, 8, 16, 10)
        deck_layout.setSpacing(6)

        # Studio Mode / Paused Recording visual indicator banner
        self.talkback_banner = QLabel("STUDIO MODE")
        self.talkback_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.talkback_banner.setWordWrap(True)
        deck_layout.addWidget(self.talkback_banner)

        # Large Time Elapsed Counter
        self.timer_label = QLabel("00:00:00.0")
        self.timer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timer_label.setStyleSheet("color: #F0F6FC; font-weight: bold;")
        deck_layout.addWidget(self.timer_label)

        # Graphical Studio VU & Peak Hold Meter (active during standby monitoring & recording)
        self.vu_meter = VuMeterWidget(
            on_reset_peak=self._on_reset_peak_hold,
            parent=self.deck_frame,
        )
        self.level_bar = self.vu_meter
        deck_layout.addWidget(self.vu_meter)

        # Primary Transport Controls Row (Record/Stop + Pause/Resume + Studio Talkback + Cough/Privacy)
        controls_row = QHBoxLayout()
        controls_row.setSpacing(16)

        self.record_btn = QPushButton("●  Record")
        self.record_btn.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding
        )
        self.record_btn.setToolTip(
            "Start or stop audio recording to /storage [Hotkey: R or Space]"
        )
        self.record_btn.clicked.connect(self._on_record_button_clicked)
        controls_row.addWidget(self.record_btn, stretch=3)

        self.pause_record_btn = QPushButton("❚❚  Pause")
        self.pause_record_btn.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding
        )
        self.pause_record_btn.setToolTip(
            "Pause or resume the current recording [Hotkey: P or Pause]"
        )
        self.pause_record_btn.clicked.connect(self._on_pause_record_button_clicked)
        controls_row.addWidget(self.pause_record_btn, stretch=2)

        self.talkback_btn = QPushButton("◉  Talkback")
        self.talkback_btn.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding
        )
        self.talkback_btn.setToolTip(
            "Click or press [T] to latch Talkback when idle; hold for momentary Push-to-Talk during recording"
        )
        self.talkback_btn.pressed.connect(self._on_talkback_pressed)
        self.talkback_btn.released.connect(self._on_talkback_released)
        controls_row.addWidget(self.talkback_btn, stretch=2)

        self.cough_privacy_btn = QPushButton("◉  Cough")
        self.cough_privacy_btn.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding
        )
        self.cough_privacy_btn.setToolTip(
            "Hold (or hold [B]) for momentary Cough Mute, or click to toggle sending microphone to Remote Producer"
        )
        self.cough_privacy_btn.pressed.connect(self._on_cough_privacy_pressed)
        self.cough_privacy_btn.released.connect(self._on_cough_privacy_released)
        controls_row.addWidget(self.cough_privacy_btn, stretch=2)

        deck_layout.addLayout(controls_row, stretch=1)
        self.main_layout.addWidget(self.deck_frame, stretch=2)

        # 4. Storage Capacity & Recording Time Remaining Strip (outside Studio Deck, above Recordings Pane)
        self.storage_monitor = StorageMonitorWidget(parent=central)
        self.main_layout.addWidget(self.storage_monitor)

        # 5. Bottom Pane: Scrollable Project Recordings List & Reference Playback
        takes_frame = QFrame()
        takes_frame.setStyleSheet(
            "QFrame#TakesPane { background-color: #161B22; border: 1px solid #30363D; border-radius: 10px; }"
        )
        takes_frame.setObjectName("TakesPane")
        takes_layout = QVBoxLayout(takes_frame)
        takes_layout.setContentsMargins(12, 8, 12, 10)
        takes_layout.setSpacing(6)

        takes_header_row = QHBoxLayout()
        takes_header_row.setContentsMargins(0, 0, 0, 0)
        takes_header_row.setSpacing(10)

        self.takes_title_label = QLabel("Project Recordings")
        self.takes_title_label.setObjectName("HeaderTitle")
        takes_header_row.addWidget(self.takes_title_label)

        self.project_prefix_btn = QPushButton("✎ Project Name")
        self.project_prefix_btn.setToolTip(
            "Open dialog to view or change the Project Name & Subfolder for output WAV files"
        )
        self.project_prefix_btn.clicked.connect(self._open_project_prefix_dialog)
        takes_header_row.addWidget(self.project_prefix_btn)

        self.rename_take_btn = QPushButton("✎ Rename File")
        self.rename_take_btn.setToolTip(
            "Rename the selected WAV recording inside the project folder to categorize it directly [Hotkey: N or F4]"
        )
        self.rename_take_btn.clicked.connect(self._on_rename_recording_clicked)
        takes_header_row.addWidget(self.rename_take_btn)

        self.file_manager_btn = QPushButton("▤ File Manager")
        self.file_manager_btn.setToolTip(
            "Open fullscreen Storage File Manager to browse, play, rename, and delete files or project folders across all projects [Hotkey: F6]"
        )
        self.file_manager_btn.clicked.connect(self._open_file_manager_dialog)
        takes_header_row.addWidget(self.file_manager_btn)

        takes_header_row.addStretch(1)

        self.stop_play_btn = QPushButton("■ Stop")
        self.stop_play_btn.setToolTip(
            "Stop reference audio playback [Hotkey: S]"
        )
        self.stop_play_btn.clicked.connect(self._on_stop_playback_clicked)
        takes_header_row.addWidget(self.stop_play_btn)

        self.refresh_takes_btn = QPushButton("↻")
        self.refresh_takes_btn.setToolTip(
            "Refresh: Rescan /storage for project recordings and audio devices [Hotkey: F5]"
        )
        self.refresh_takes_btn.clicked.connect(self._on_manual_refresh)
        takes_header_row.addWidget(self.refresh_takes_btn)

        takes_layout.addLayout(takes_header_row)

        # Subtitle row directly below Project Recordings heading showing the active Project Name
        takes_sub_row = QHBoxLayout()
        takes_sub_row.setContentsMargins(2, 0, 2, 0)
        takes_sub_row.setSpacing(12)

        self.project_name_label = QLabel()
        self.project_name_label.setObjectName("ProjectNameSubLabel")
        self.project_name_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.project_name_label.setStyleSheet(
            "border: none; color: #58A6FF; font-weight: bold;"
        )
        takes_sub_row.addWidget(self.project_name_label, stretch=1)

        self.playback_status_label = QLabel("")
        self.playback_status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.playback_status_label.setStyleSheet(
            "border: none; color: #8B949E; font-family: monospace;"
        )
        takes_sub_row.addWidget(self.playback_status_label)

        takes_layout.addLayout(takes_sub_row)

        self.recordings_list = QListWidget()
        self.recordings_item_delegate = RecordingClipItemDelegate(self.recordings_list)
        self.recordings_list.setItemDelegate(self.recordings_item_delegate)
        self.recordings_list.setToolTip(
            "Finished WAV recordings in the active project subfolder (/storage/<ProjectName>/). "
            "Select a file and click its Play/Pause button on the right (or double-click / press [P]) to play back "
            "(also streamed to Remote Producer), [Left]/[Right] to seek ±3s, or click Rename File [N] to categorize."
        )
        self.recordings_list.itemDoubleClicked.connect(
            lambda _item: self._on_play_pause_clicked(force_new=True)
        )
        self.recordings_list.currentRowChanged.connect(
            self._on_selected_recording_row_changed
        )
        v_scroll = self.recordings_list.verticalScrollBar()
        if v_scroll is not None:
            v_scroll.valueChanged.connect(
                lambda _val: self._reposition_inline_play_button()
            )

        # Inline Play/Pause button rendered on the rightmost side of the selected file entry
        self.play_btn = QPushButton("▶ Play", self.recordings_list.viewport())
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setToolTip(
            "Play or pause the selected project recording for reference and stream to Remote Producer [Hotkey: P]"
        )
        self.play_btn.clicked.connect(self._on_play_pause_clicked)
        self.play_btn.setVisible(False)

        takes_layout.addWidget(self.recordings_list, stretch=1)

        self.main_layout.addWidget(takes_frame, stretch=3)

        # 5. Subtle Bottom Status Bar showing Device IP, Samba Share Path & System CPU/RAM Load
        self.bottom_bar_frame = QFrame()
        self.bottom_bar_frame.setObjectName("SubtleIpBarFrame")
        bottom_bar_layout = QHBoxLayout(self.bottom_bar_frame)
        bottom_bar_layout.setContentsMargins(10, 3, 10, 3)
        bottom_bar_layout.setSpacing(12)

        self.ip_bar_label = QLabel(get_primary_ip_display())
        self.ip_bar_label.setObjectName("SubtleIpBar")
        self.ip_bar_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        bottom_bar_layout.addWidget(self.ip_bar_label, stretch=1)

        self.sys_resource_label = QLabel()
        self.sys_resource_label.setObjectName("SystemResourceBadge")
        self.sys_resource_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.sys_resource_label.setToolTip(
            "Live System CPU & RAM Utilization Monitor.\n"
            "• Normal: CPU < 75% and RAM < 80%\n"
            "• High Load (Amber): CPU >= 75% or RAM >= 80%\n"
            "• Overload Alert (Red): CPU >= 90% or RAM >= 90% (risk of audio instability)"
        )
        bottom_bar_layout.addWidget(self.sys_resource_label)

        self.main_layout.addWidget(self.bottom_bar_frame)

        self._update_filename_preview()
        self._refresh_clock_display(force=True)
        self._refresh_system_resources()

    # -------------------------------------------------------------------------
    # Responsive Scaling & Fullscreen Management
    # -------------------------------------------------------------------------
    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._fullscreen_requested:
            self.showFullScreen()
            self._fullscreen_requested = False

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_responsive_scaling()

    def _apply_responsive_scaling(self) -> None:
        w = max(640, self.width())
        h = max(420, self.height())
        self._scale = compute_scale_factor(w, h)

        self.setStyleSheet(build_main_stylesheet(self._scale))

        timer_px = max(28, int(round(52 * self._scale)))
        timer_font = QFont("DejaVu Sans Mono", timer_px, QFont.Weight.Bold)
        timer_font.setStyleHint(QFont.StyleHint.Monospace)
        self.timer_label.setFont(timer_font)

        btn_min_h = max(60, int(round(98 * self._scale)))
        self.record_btn.setFixedHeight(btn_min_h)
        self.pause_record_btn.setFixedHeight(btn_min_h)
        self.talkback_btn.setFixedHeight(btn_min_h)
        self.cough_privacy_btn.setFixedHeight(btn_min_h)

        # Ensure header and toolbar buttons fit their text comfortably at 1920x1080 and never overlap
        self.remote_producer_btn.setMinimumWidth(max(195, int(round(250 * self._scale))))
        self.settings_btn.setMinimumWidth(max(150, int(round(195 * self._scale))))
        self.help_btn.setMinimumWidth(max(90, int(round(115 * self._scale))))
        self.project_prefix_btn.setMinimumWidth(max(140, int(round(180 * self._scale))))
        self.rename_take_btn.setMinimumWidth(max(130, int(round(170 * self._scale))))
        self.file_manager_btn.setMinimumWidth(max(140, int(round(180 * self._scale))))
        self.stop_play_btn.setMinimumWidth(max(95, int(round(125 * self._scale))))

        # Compact icon-only Refresh button
        icon_btn_w = max(38, int(round(48 * self._scale)))
        self.refresh_takes_btn.setFixedWidth(icon_btn_w)

        # Inline Play/Pause button on the right-most side of the selected file entry
        inline_play_w = max(98, int(round(128 * self._scale)))
        self.recordings_item_delegate.apply_scale(
            self._scale, inline_button_width=inline_play_w
        )

        self.vu_meter.apply_scale(self._scale)
        self.storage_monitor.apply_scale(self._scale)
        self.clock_label.setStyleSheet(build_studio_clock_badge_style(self._scale))
        self.sys_resource_label.setStyleSheet(
            build_system_resource_badge_style(self._last_resource_status.level, self._scale)
        )

        self._update_project_name_label()
        self._refresh_transport_styles()
        self._reposition_inline_play_button()

    # -------------------------------------------------------------------------
    # Hardware State & Defaults
    # -------------------------------------------------------------------------
    def _sync_hardware_state(self, force_probe: bool = False) -> None:
        """Validate selected input/output devices, apply max rate/bit-depth defaults, and start live input monitoring."""
        if self.audio_engine.is_recording:
            return

        settings = self.config_manager.settings
        capture_devs = self.audio_engine.get_capture_devices(force_probe=force_probe)
        playback_devs = self.audio_engine.get_playback_devices(force_probe=False)

        dev_map = {d.device_id: d for d in capture_devs}
        play_map = {d.device_id: d for d in playback_devs}

        selected_dev: Optional[AudioDeviceCapabilities] = None
        if settings.input_device:
            selected_dev = dev_map.get(settings.input_device)
        elif capture_devs:
            # Auto-select first USB capture device (or first available capture device)
            selected_dev = capture_devs[0]
            settings.input_device = selected_dev.device_id

        self._active_input_device = selected_dev

        if selected_dev is not None:
            # Default to maximum bit depth and sample rate if unset or unsupported
            if settings.bit_depth not in selected_dev.supported_bit_depths:
                settings.bit_depth = selected_dev.max_bit_depth
            if settings.sample_rate not in selected_dev.supported_sample_rates:
                settings.sample_rate = selected_dev.max_sample_rate
            if settings.input_channel >= selected_dev.max_channels:
                settings.input_channel = 0
            self.config_manager.save()

            # Ensure continuous input monitoring is running so the VU & Peak Hold meter works while active
            self.audio_engine.start_monitoring(
                device=selected_dev,
                sample_rate=settings.sample_rate,
                bit_depth=settings.bit_depth,
                studio_mode=settings.studio_mode,
                channels=settings.effective_channels,
                input_channel=settings.input_channel,
            )
        else:
            self.audio_engine.stop_monitoring()

        # Resolve output playback device
        if settings.output_device == "same" or not settings.output_device:
            if selected_dev and selected_dev.is_playback:
                self._active_output_device = selected_dev
            elif selected_dev and f"hw:{selected_dev.card_index},0" in play_map:
                self._active_output_device = play_map[f"hw:{selected_dev.card_index},0"]
            else:
                self._active_output_device = playback_devs[0] if playback_devs else None
        else:
            self._active_output_device = play_map.get(settings.output_device)

        self.audio_engine.set_talkback_output_device(self._active_output_device)
        self.audio_engine.set_talkback_tone_params(
            tone_hz=settings.talkback_tone_hz,
            tone_duration_sec=settings.talkback_tone_duration_sec,
            release_tone_duration_sec=settings.talkback_release_tone_duration_sec,
        )
        self._refresh_transport_styles()

    def _refresh_transport_styles(self) -> None:
        settings = self.config_manager.settings
        is_rec = self.audio_engine.is_recording
        is_rec_paused = self.audio_engine.is_record_paused
        blink_on = self._pause_blink_phase
        dev = self._active_input_device
        rec_path = self.audio_engine.current_recording_path
        rec_name = rec_path.name if rec_path is not None else ""

        self.settings_btn.setEnabled(not is_rec)
        self.project_prefix_btn.setEnabled(not is_rec)
        self.file_manager_btn.setEnabled(not is_rec)

        # Remote Producer Top Bar Button state
        if self.remote_producer_server.is_running:
            p_count = self.remote_producer_server.connected_client_count
            if self.audio_engine.remote_talkback_active:
                self.remote_producer_btn.setText("⚡ Producer: TALKBACK")
                self.remote_producer_btn.setStyleSheet(
                    "background-color: #E36209; color: #FFFFFF; border: 2px solid #FFDF5D; font-weight: bold;"
                )
            elif p_count > 0:
                self.remote_producer_btn.setText(f"● Producer: Connected ({p_count})")
                self.remote_producer_btn.setStyleSheet(
                    "background-color: #0F2D1E; color: #7EE787; border: 1px solid #2EA043; font-weight: bold;"
                )
            else:
                self.remote_producer_btn.setText(f"◎ Producer: Ready (:{self.remote_producer_server.port})")
                self.remote_producer_btn.setStyleSheet(
                    "background-color: #161F2C; color: #79C0FF; border: 1px solid #388BFD; font-weight: bold;"
                )
        else:
            self.remote_producer_btn.setText("◎ Remote Producer")
            self.remote_producer_btn.setStyleSheet("")

        # Talent Mic Privacy & Cough Button state on Studio Deck (flashes red when muted; VU meter shows Talkback Override while local Talkback is active)
        cough_font_px = max(14, int(round(20 * self._scale)))
        cough_radius = max(12, int(round(16 * self._scale)))
        cough_pad_v = max(6, int(round(8 * self._scale)))
        cough_pad_h = max(8, int(round(10 * self._scale)))
        is_mute_active = bool(
            self.audio_engine.talent_cough_muted
            or not self.audio_engine.talent_mic_send_enabled
        )
        if is_mute_active:
            self.cough_privacy_btn.setText("✕  Muted")
            if blink_on:
                bg_col = "#D72638"
                fg_col = "#FFFFFF"
                border_col = "#FF7B72"
            else:
                bg_col = "#490B11"
                fg_col = "#FFA198"
                border_col = "#D72638"
            self.cough_privacy_btn.setStyleSheet(
                f"QPushButton {{ background-color: {bg_col}; color: {fg_col}; border: 3px solid {border_col}; "
                f"border-radius: {cough_radius}px; padding: {cough_pad_v}px {cough_pad_h}px; "
                f"font-size: {cough_font_px}px; font-weight: bold; }}"
            )
        else:
            self.cough_privacy_btn.setText("◉  Cough")
            self.cough_privacy_btn.setStyleSheet(
                f"QPushButton {{ background-color: #12261A; color: #7EE787; border: 3px solid #2EA043; "
                f"border-radius: {cough_radius}px; padding: {cough_pad_v}px {cough_pad_h}px; "
                f"font-size: {cough_font_px}px; font-weight: bold; }}"
            )

        # Studio Deck Frame Border (high-visibility pulsing amber border when paused)
        if is_rec_paused:
            border_col = "#FFDF5D" if blink_on else "#D29922"
            bg_col = "#1B1408" if blink_on else "#120E06"
            self.deck_frame.setStyleSheet(
                f"QFrame#StudioDeck {{ background-color: {bg_col}; border: 3px solid {border_col}; border-radius: 12px; }}"
            )
        elif is_rec:
            self.deck_frame.setStyleSheet(
                "QFrame#StudioDeck { background-color: #0D1014; border: 2px solid #8E1523; border-radius: 12px; }"
            )
        else:
            self.deck_frame.setStyleSheet(
                "QFrame#StudioDeck { background-color: #0D1014; border: 1px solid #21262D; border-radius: 12px; }"
            )

        # Hardware & Format labels + Filename Preview
        if dev is None:
            self.hw_status_label.setText("⚠ No Audio Interface Connected")
            self.hw_status_label.setStyleSheet("color: #F85149; font-weight: bold;")
            self.format_badge_label.setText("OFFLINE")
        else:
            ch_desc = (
                "STUDIO STEREO"
                if settings.studio_mode
                else ("STEREO (2ch)" if settings.channels >= 2 else "MONO (1ch)")
            )
            if is_rec_paused:
                self.hw_status_label.setText(f"❚❚ Paused: {rec_name}")
                status_col = "#FFDF5D" if blink_on else "#F0B72F"
                self.hw_status_label.setStyleSheet(f"color: {status_col}; font-weight: bold;")
            else:
                self.hw_status_label.setText(f"Input: {dev.display_name}")
                self.hw_status_label.setStyleSheet("color: #3FB950; font-weight: 600;")
            self.format_badge_label.setText(
                f"{settings.bit_depth}-bit  •  {settings.sample_rate:,} Hz  •  {ch_desc}"
            )

        self._update_filename_preview()

        # Record / Recording & Pause / Paused Button States
        if is_rec:
            self.record_btn.setEnabled(True)
            self.record_btn.setText("■  Recording")
            self.record_btn.setStyleSheet(
                build_record_button_style("recording", self._scale)
            )
            self.pause_record_btn.setEnabled(True)
            if is_rec_paused:
                self.pause_record_btn.setText("▶  Paused")
                self.pause_record_btn.setStyleSheet(
                    build_pause_record_button_style(
                        "paused", self._scale, blink_on=blink_on
                    )
                )
                timer_col = "#FFDF5D" if blink_on else "#D29922"
                self.timer_label.setStyleSheet(f"color: {timer_col}; font-weight: bold;")
            else:
                self.pause_record_btn.setText("❚❚  Pause")
                self.pause_record_btn.setStyleSheet(
                    build_pause_record_button_style("active", self._scale)
                )
                self.timer_label.setStyleSheet("color: #FF6B7A; font-weight: bold;")
        elif dev is not None:
            self.record_btn.setEnabled(True)
            self.record_btn.setText("●  Record")
            self.record_btn.setStyleSheet(
                build_record_button_style("ready", self._scale)
            )
            self.pause_record_btn.setEnabled(False)
            self.pause_record_btn.setText("❚❚  Pause")
            self.pause_record_btn.setStyleSheet(
                build_pause_record_button_style("disabled", self._scale)
            )
            self.timer_label.setStyleSheet("color: #F0F6FC; font-weight: bold;")
        else:
            self.record_btn.setEnabled(False)
            self.record_btn.setText("●  Record")
            self.record_btn.setStyleSheet(
                build_record_button_style("disabled", self._scale)
            )
            self.pause_record_btn.setEnabled(False)
            self.pause_record_btn.setText("❚❚  Pause")
            self.pause_record_btn.setStyleSheet(
                build_pause_record_button_style("disabled", self._scale)
            )
            self.timer_label.setStyleSheet("color: #6E7681; font-weight: bold;")

        # Talkback button & visual banner state
        tb_active = self.audio_engine.talkback_active
        tb_enabled = bool(
            settings.studio_mode and dev is not None and not is_rec_paused
        )
        self.talkback_btn.setVisible(bool(settings.studio_mode))
        self.talkback_btn.setEnabled(tb_enabled)
        self.talkback_btn.setText("⚡  Talkback" if tb_active else "◉  Talkback")
        self.talkback_btn.setStyleSheet(
            build_talkback_button_style(tb_active, tb_enabled, self._scale)
        )

        if is_rec_paused:
            self.talkback_banner.setVisible(True)
            file_info = f" ({rec_name})" if rec_name else ""
            self.talkback_banner.setText(f"❚❚ RECORDING PAUSED{file_info}")
            if blink_on:
                self.talkback_banner.setStyleSheet(
                    "background-color: #E36209; color: #FFFFFF; font-weight: bold; "
                    "padding: 8px; border-radius: 6px; border: 2px solid #FFDF5D;"
                )
            else:
                self.talkback_banner.setStyleSheet(
                    "background-color: #3B2300; color: #FFDF5D; font-weight: bold; "
                    "padding: 8px; border-radius: 6px; border: 2px solid #F0B72F;"
                )
        elif settings.studio_mode:
            self.talkback_banner.setVisible(True)
            if tb_active:
                if self.audio_engine.remote_talkback_active and self.audio_engine.local_talkback_active:
                    self.talkback_banner.setText("⚡ STUDIO MODE — PRODUCER & TALENT TALKBACK")
                elif self.audio_engine.remote_talkback_active:
                    if self.audio_engine.remote_talkback_latched:
                        self.talkback_banner.setText("⚡ STUDIO MODE — REMOTE PRODUCER TALKBACK (LATCHED)")
                    else:
                        self.talkback_banner.setText("⚡ STUDIO MODE — REMOTE PRODUCER TALKBACK")
                elif self.audio_engine.local_talkback_latched:
                    self.talkback_banner.setText("⚡ STUDIO MODE — TALKBACK LATCHED")
                else:
                    self.talkback_banner.setText("⚡ STUDIO MODE — TALKBACK ACTIVE")
                self.talkback_banner.setStyleSheet(
                    "background-color: #E36209; color: #FFFFFF; font-weight: bold; "
                    "padding: 6px; border-radius: 6px; border: 2px solid #FFDF5D;"
                )
            else:
                self.talkback_banner.setText("STUDIO MODE")
                self.talkback_banner.setStyleSheet(
                    "background-color: #161B22; color: #58A6FF; font-weight: 600; "
                    "padding: 6px; border-radius: 6px; border: 1px solid #1F6FEB;"
                )
        elif self.audio_engine.remote_talkback_active:
            self.talkback_banner.setVisible(True)
            if self.audio_engine.remote_talkback_latched:
                self.talkback_banner.setText("⚡ NORMAL MODE — REMOTE PRODUCER TALKBACK (LATCHED)")
            else:
                self.talkback_banner.setText("⚡ NORMAL MODE — REMOTE PRODUCER TALKBACK")
            self.talkback_banner.setStyleSheet(
                "background-color: #E36209; color: #FFFFFF; font-weight: bold; "
                "padding: 6px; border-radius: 6px; border: 2px solid #FFDF5D;"
            )
        else:
            ch_mode_txt = "STEREO" if settings.channels >= 2 else "MONO"
            self.talkback_banner.setVisible(True)
            self.talkback_banner.setText(f"NORMAL MODE ({ch_mode_txt})")
            self.talkback_banner.setStyleSheet(
                "background-color: #161B22; color: #8B949E; font-weight: 600; "
                "padding: 6px; border-radius: 6px; border: 1px solid #30363D;"
            )

        self._refresh_vu_meter()
        self._refresh_storage_status()
        self._update_playback_buttons_state()

    def _refresh_vu_meter(self) -> None:
        readings = self.audio_engine.get_vu_readings()
        is_playing = self.audio_engine.is_playing
        is_paused = (
            self.audio_engine.is_record_paused
            if self.audio_engine.is_recording
            else (self.audio_engine.is_paused if is_playing else False)
        )
        self.vu_meter.set_readings(
            readings,
            is_recording=self.audio_engine.is_recording,
            has_device=(self._active_input_device is not None),
            is_paused=is_paused,
            is_muted=self.audio_engine.is_mic_muted,
            is_talkback_override=self.audio_engine.is_talkback_mute_override,
            is_playing=is_playing,
        )

    def _refresh_storage_status(self) -> None:
        """Query /storage drive utilization and update the StorageMonitorWidget."""
        status = self.config_manager.get_storage_status()
        self.storage_monitor.set_status(
            status,
            is_recording=self.audio_engine.is_recording,
            blink_on=self._pause_blink_phase,
        )
        self._last_storage_refresh_monotonic = time.monotonic()

    def _refresh_clock_display(self, force: bool = False) -> None:
        """Update the compact studio clock in the status strip using configured time and date formats."""
        settings = self.config_manager.settings
        text = format_clock_display(
            time_format=getattr(settings, "clock_time_format", "12h"),
            date_format=getattr(settings, "clock_date_format", "YYYY-MM-DD"),
        )
        if force or text != self._last_clock_text:
            self._last_clock_text = text
            self.clock_label.setText(text)

    def _refresh_system_resources(self) -> None:
        """Sample live system CPU and RAM usage and update the bottom bar indicator."""
        res_status = self.resource_monitor.sample()
        self._last_resource_status = res_status
        self.sys_resource_label.setText(res_status.format_bar_text())
        self.sys_resource_label.setStyleSheet(
            build_system_resource_badge_style(res_status.level, self._scale)
        )
        self._last_resource_refresh_monotonic = time.monotonic()

    # -------------------------------------------------------------------------
    # Recording, Pause/Resume & Talkback Actions
    # -------------------------------------------------------------------------
    def _on_record_button_clicked(self) -> None:
        if self.audio_engine.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _on_pause_record_button_clicked(self) -> None:
        if not self.audio_engine.is_recording:
            return
        self._talkback_key_held = False
        self.audio_engine.set_talkback(False)
        paused = self.audio_engine.toggle_pause_recording()
        self._pause_blink_phase = True
        self._refresh_transport_styles()
        if paused:
            elapsed = self.audio_engine.elapsed_seconds
            hrs = int(elapsed // 3600)
            mins = int((elapsed % 3600) // 60)
            secs = elapsed % 60.0
            self.timer_label.setText(f"❚❚ {hrs:02d}:{mins:02d}:{secs:04.1f}  [PAUSED]")

    def _start_recording(self) -> None:
        self._talkback_key_held = False
        self._sync_hardware_state()
        dev = self._active_input_device
        if dev is None:
            return

        settings = self.config_manager.settings
        proj_dir = self.config_manager.ensure_project_dir(settings.project_name)
        filename = generate_recording_filename(settings.project_name)
        output_path = proj_dir / filename

        self.audio_engine.start_recording(
            device=dev,
            output_path=output_path,
            sample_rate=settings.sample_rate,
            bit_depth=settings.bit_depth,
            studio_mode=settings.studio_mode,
            channels=settings.effective_channels,
            input_channel=settings.input_channel,
            tone_hz=settings.talkback_tone_hz,
            tone_duration_sec=settings.talkback_tone_duration_sec,
            release_tone_duration_sec=settings.talkback_release_tone_duration_sec,
            on_error=self._on_recording_error_threadsafe,
        )
        self._refresh_transport_styles()

    def _stop_recording(self) -> None:
        self._talkback_key_held = False
        self.audio_engine.set_talkback(False)
        finished_path = self.audio_engine.stop_recording()
        self.timer_label.setText("00:00:00.0")
        self._refresh_transport_styles()
        self._refresh_recordings_list(select_path=finished_path)

    def _on_reset_peak_hold(self) -> None:
        self.audio_engine.reset_peak_hold()
        self._refresh_vu_meter()
        self.setFocus()

    def _on_recording_error_threadsafe(self, err_msg: str) -> None:
        QTimer.singleShot(0, lambda: self._handle_recording_error_ui(err_msg))

    def _handle_recording_error_ui(self, err_msg: str) -> None:
        self._stop_recording()
        self._sync_hardware_state()
        QMessageBox.warning(
            self,
            "Audio Recording Error",
            f"Recording stopped due to a hardware error:\n{err_msg}",
        )

    def _on_talkback_pressed(self) -> None:
        if (
            not self.config_manager.settings.studio_mode
            or self.audio_engine.is_record_paused
        ):
            return
        self._talkback_btn_press_mono = time.monotonic()
        if not self.audio_engine.is_recording:
            # When not recording, Talkback latches on tap and unlatches on a second tap (or releases after hold)
            if self.audio_engine.local_talkback_active:
                self.audio_engine.set_talkback(False, latched=False)
            else:
                self.audio_engine.set_talkback(True, latched=True)
        else:
            # During active recording, Talkback is momentary Push-to-Talk (PTT)
            self.audio_engine.set_talkback(True, latched=False)
        self._refresh_transport_styles()

    def _on_talkback_released(self) -> None:
        held_sec = time.monotonic() - getattr(self, "_talkback_btn_press_mono", 0.0)
        if (
            not self.audio_engine.is_recording
            and self.audio_engine.local_talkback_latched
            and held_sec < 0.25
        ):
            self._refresh_transport_styles()
            return
        self.audio_engine.set_talkback(False, latched=False)
        self._refresh_transport_styles()

    def _on_cough_privacy_pressed(self) -> None:
        self._cough_btn_press_mono = time.monotonic()
        self.audio_engine.set_talent_cough_muted(True)
        self._refresh_transport_styles()

    def _on_cough_privacy_released(self) -> None:
        held_sec = time.monotonic() - self._cough_btn_press_mono
        self.audio_engine.set_talent_cough_muted(False)
        if held_sec < 0.25:
            # Quick tap toggles persistent Mic Send (Privacy Mute)
            next_state = not self.audio_engine.talent_mic_send_enabled
            self.audio_engine.set_talent_mic_send_enabled(next_state)
            self.config_manager.settings.remote_producer_send_talent_mic = next_state
            self.config_manager.save(commit_lbu=True)
        self._refresh_transport_styles()

    def _build_remote_producer_extra_status(self) -> dict:
        rec_path = self.audio_engine.current_recording_path
        play_path = self.audio_engine.playing_filepath
        active_take = rec_path.name if rec_path is not None else (
            self._project_recordings[0].filename if self._project_recordings else ""
        )
        return {
            "project_name": self.config_manager.settings.project_name or "Session",
            "take_count": len(self._project_recordings),
            "active_take_name": active_take,
            "playing_take_name": play_path.name if play_path is not None else "",
        }

    def _open_remote_producer_dialog(self) -> None:
        dlg = RemoteProducerDialog(
            remote_server=self.remote_producer_server,
            audio_engine=self.audio_engine,
            config_manager=self.config_manager,
            parent=self,
        )
        dlg.exec()
        self._refresh_transport_styles()
        self.setFocus()

    def _on_toggle_studio_mode(self) -> None:
        if self.audio_engine.is_recording:
            return
        s = self.config_manager.settings
        s.studio_mode = not s.studio_mode
        if s.studio_mode:
            s.channels = 2
        else:
            self._talkback_key_held = False
            self.audio_engine.set_talkback(False, latched=False)
        self.config_manager.save(commit_lbu=True)
        self._sync_hardware_state()
        self._refresh_transport_styles()

    # -------------------------------------------------------------------------
    # Project Prefix & Recordings List + Reference Playback
    # -------------------------------------------------------------------------
    def _open_project_prefix_dialog(self) -> None:
        if self.audio_engine.is_recording:
            return
        dlg = ProjectPrefixDialog(
            config_manager=self.config_manager,
            parent=self,
        )
        if dlg.exec():
            self._on_project_name_changed(dlg.project_name)
        self.setFocus()

    def _on_project_name_changed(self, new_text: str) -> None:
        self.config_manager.settings.project_name = new_text
        self.config_manager.save(commit_lbu=True)
        self.config_manager.ensure_project_dir(new_text)
        self._update_filename_preview()
        self._refresh_recordings_list()

    def _on_rename_recording_clicked(self) -> None:
        if self.audio_engine.is_recording:
            return
        selected_path = self._get_selected_recording_path()
        if selected_path is None or not selected_path.is_file():
            return
        if self.audio_engine.is_playing and self.audio_engine.playing_filepath == selected_path:
            self.audio_engine.stop_playback()

        dlg = RenameRecordingDialog(
            config_manager=self.config_manager,
            filepath=selected_path,
            parent=self,
        )
        if dlg.exec() and dlg.renamed_path is not None:
            self._refresh_recordings_list(select_path=dlg.renamed_path)
        else:
            self._update_playback_buttons_state()
        self.setFocus()

    def _on_delete_recording_clicked(self) -> None:
        if self.audio_engine.is_recording:
            return
        selected_path = self._get_selected_recording_path()
        if selected_path is None or not selected_path.is_file():
            return
        target_entry: Optional[RecordingEntry] = None
        for entry in self._project_recordings:
            if entry.filepath == selected_path:
                target_entry = entry
                break
        if target_entry is None:
            return

        status = self.config_manager.get_storage_status()
        dlg = DeleteFileConfirmDialog(
            entry=target_entry,
            storage_status=status,
            parent=self,
        )
        if dlg.exec():
            if self.audio_engine.is_playing and self.audio_engine.playing_filepath == selected_path:
                self.audio_engine.stop_playback()
            self.config_manager.delete_recording(selected_path)
            self._refresh_recordings_list()
            self._on_playback_finished()
        self.setFocus()

    def _open_file_manager_dialog(self) -> None:
        if self.audio_engine.is_recording:
            return
        if self.audio_engine.is_playing:
            self.audio_engine.stop_playback()

        dlg = StorageFileManagerDialog(
            config_manager=self.config_manager,
            audio_engine=self.audio_engine,
            output_device=self._active_output_device,
            rename_dialog_factory=lambda cm, fp, par: RenameRecordingDialog(
                config_manager=cm,
                filepath=fp,
                parent=par,
            ),
            fullscreen=self.isFullScreen(),
            parent=self,
        )
        dlg.exec()
        self._sync_hardware_state()
        self._update_filename_preview()
        self._refresh_recordings_list()
        self.setFocus()

    def _on_osk_field_committed(self, _line_edit: QLineEdit, _text: str) -> None:
        self.config_manager.save(commit_lbu=True)
        self.setFocus()

    def _update_filename_preview(self) -> None:
        rec_path = self.audio_engine.current_recording_path
        folder = sanitize_project_name(self.config_manager.settings.project_name)
        if self.audio_engine.is_record_paused and rec_path is not None:
            self.filename_preview_label.setText(f"❚❚ Paused File: {folder}/{rec_path.name}")
            self.filename_preview_label.setStyleSheet(
                "border: none; color: #FFDF5D; font-family: monospace; font-weight: bold;"
            )
            return
        if self.audio_engine.is_recording and rec_path is not None:
            self.filename_preview_label.setText(f"● Recording: {folder}/{rec_path.name}")
            self.filename_preview_label.setStyleSheet(
                "border: none; color: #FF6B7A; font-family: monospace; font-weight: bold;"
            )
            return
        sample_name = generate_recording_filename(
            self.config_manager.settings.project_name
        )
        self.filename_preview_label.setText(f"Next: {folder}/{sample_name}")
        self.filename_preview_label.setStyleSheet(
            "border: none; color: #8B949E; font-family: monospace;"
        )

    def _update_project_name_label(self) -> None:
        """Display the active Project Name below the Project Recordings heading (abbreviated if very long)."""
        raw_name = (self.config_manager.settings.project_name or "").strip()
        folder = sanitize_project_name(raw_name)
        display_proj = raw_name if raw_name else folder
        full_text = f"Project: {display_proj}"
        self.project_name_label.setToolTip(
            f"Active Project: {display_proj}  (Subfolder: /storage/{folder}/)"
        )
        avail_w = max(240, self.width() - int(round(320 * self._scale)))
        fm = self.project_name_label.fontMetrics()
        self.project_name_label.setText(
            abbreviate_text_to_width(full_text, fm, avail_w)
        )

    def _on_selected_recording_row_changed(self, _row: int) -> None:
        self.recordings_item_delegate.reset_scroll_timer()
        self._update_playback_buttons_state()
        self._reposition_inline_play_button()

    def _reposition_inline_play_button(self) -> None:
        """Position the Play/Pause button on the rightmost side of the currently selected file entry."""
        viewport = self.recordings_list.viewport()
        curr_item = self.recordings_list.currentItem()
        if viewport is None or curr_item is None or self.recordings_list.count() == 0:
            self.play_btn.setVisible(False)
            return

        item_rect = self.recordings_list.visualItemRect(curr_item)
        vp_rect = viewport.rect()
        if not item_rect.isValid() or not vp_rect.intersects(item_rect):
            self.play_btn.setVisible(False)
            return

        btn_w = max(98, int(round(128 * self._scale)))
        margin_v = max(3, int(round(4 * self._scale)))
        margin_r = max(6, int(round(10 * self._scale)))
        btn_h = max(22, item_rect.height() - margin_v * 2)
        btn_x = max(4, item_rect.right() - btn_w - margin_r)
        btn_y = item_rect.top() + (item_rect.height() - btn_h) // 2

        self.play_btn.setGeometry(btn_x, btn_y, btn_w, btn_h)
        self.play_btn.setVisible(True)
        self.play_btn.raise_()

    @staticmethod
    def _format_recording_meta_text(
        entry: RecordingEntry,
        is_playing_this: bool = False,
        pos_sec: float = 0.0,
        dur_sec: float = 0.0,
    ) -> str:
        """Format the metadata portion of a recording row (with elapsed / total time when playing)."""
        ch_label = "Stereo" if entry.channels == 2 else f"{entry.channels}ch"
        if not is_playing_this:
            return (
                f"{entry.formatted_duration}    |    "
                f"{entry.bit_depth}-bit {entry.sample_rate:,} Hz {ch_label}    |    "
                f"{entry.formatted_size}"
            )

        eff_dur = dur_sec if dur_sec > 0.0 else entry.duration_seconds
        clamped_pos = (
            max(0.0, min(pos_sec, eff_dur)) if eff_dur > 0.0 else max(0.0, pos_sec)
        )
        pos_str = format_playback_time(clamped_pos)
        dur_str = format_playback_time(eff_dur)
        return (
            f"{pos_str} / {dur_str}    |    "
            f"{entry.bit_depth}-bit {entry.sample_rate:,} Hz {ch_label}    |    "
            f"{entry.formatted_size}"
        )

    @staticmethod
    def _format_recording_row_text(
        entry: RecordingEntry,
        is_playing_this: bool = False,
        is_paused: bool = False,
        pos_sec: float = 0.0,
        dur_sec: float = 0.0,
    ) -> str:
        meta_part = MainWindow._format_recording_meta_text(
            entry,
            is_playing_this=is_playing_this,
            pos_sec=pos_sec,
            dur_sec=dur_sec,
        )
        if not is_playing_this:
            return f"{entry.filename}    |    {meta_part}"

        state_badge = "❚❚ PAUSED" if is_paused else "▶ PLAYING"
        return f"{state_badge}  {entry.filename}    |    {meta_part}"

    def _update_playing_clip_row(self, paused_override: Optional[bool] = None) -> None:
        """Update the line of the clip that is playing in recordings_list with live play position & progress."""
        is_playing = self.audio_engine.is_playing
        playing_path = self.audio_engine.playing_filepath if is_playing else None
        is_paused = (
            self.audio_engine.is_paused if paused_override is None else paused_override
        )
        pos_sec = self.audio_engine.play_position_sec if is_playing else 0.0
        dur_sec = self.audio_engine.play_duration_sec if is_playing else 0.0

        count = self.recordings_list.count()
        for row_idx in range(count):
            item = self.recordings_list.item(row_idx)
            if item is None or row_idx >= len(self._project_recordings):
                continue
            entry = self._project_recordings[row_idx]
            is_this_clip = bool(
                is_playing and playing_path is not None and entry.filepath == playing_path
            )
            was_active = bool(item.data(ROLE_PLAYBACK_ACTIVE))

            if is_this_clip:
                eff_dur = dur_sec if dur_sec > 0.0 else entry.duration_seconds
                progress = (
                    max(0.0, min(1.0, pos_sec / eff_dur)) if eff_dur > 0.0 else 0.0
                )
                meta_text = self._format_recording_meta_text(
                    entry,
                    is_playing_this=True,
                    pos_sec=pos_sec,
                    dur_sec=eff_dur,
                )
                row_text = self._format_recording_row_text(
                    entry,
                    is_playing_this=True,
                    is_paused=is_paused,
                    pos_sec=pos_sec,
                    dur_sec=eff_dur,
                )
                if item.text() != row_text:
                    item.setText(row_text)
                item.setData(ROLE_RECORDING_FILENAME, entry.filename)
                item.setData(ROLE_RECORDING_META, meta_text)
                item.setData(ROLE_PLAYBACK_ACTIVE, True)
                item.setData(ROLE_PLAYBACK_PAUSED, is_paused)
                item.setData(ROLE_PLAYBACK_PROGRESS, progress)
                item.setData(ROLE_PLAYBACK_POS_SEC, pos_sec)
                item.setData(ROLE_PLAYBACK_DUR_SEC, eff_dur)
            elif was_active:
                meta_text = self._format_recording_meta_text(entry, is_playing_this=False)
                row_text = self._format_recording_row_text(entry, is_playing_this=False)
                if item.text() != row_text:
                    item.setText(row_text)
                item.setData(ROLE_RECORDING_FILENAME, entry.filename)
                item.setData(ROLE_RECORDING_META, meta_text)
                item.setData(ROLE_PLAYBACK_ACTIVE, False)
                item.setData(ROLE_PLAYBACK_PAUSED, False)
                item.setData(ROLE_PLAYBACK_PROGRESS, 0.0)
                item.setData(ROLE_PLAYBACK_POS_SEC, 0.0)
                item.setData(ROLE_PLAYBACK_DUR_SEC, entry.duration_seconds)

        viewport = self.recordings_list.viewport()
        if viewport is not None:
            viewport.update()

    def _refresh_recordings_list(self, select_path: Optional[Path] = None) -> None:
        self.takes_title_label.setText("Project Recordings")
        self._update_project_name_label()

        current_selected_path: Optional[Path] = select_path
        if current_selected_path is None:
            curr_item = self.recordings_list.currentItem()
            if curr_item is not None:
                current_selected_path = curr_item.data(Qt.ItemDataRole.UserRole)

        self._project_recordings = self.config_manager.list_project_recordings(
            self.config_manager.settings.project_name
        )

        self.recordings_list.blockSignals(True)
        self.recordings_list.clear()

        playing_path = (
            self.audio_engine.playing_filepath if self.audio_engine.is_playing else None
        )
        selected_row = 0
        for idx, entry in enumerate(self._project_recordings):
            is_this_playing = bool(
                playing_path is not None and entry.filepath == playing_path
            )
            eff_dur = (
                self.audio_engine.play_duration_sec
                if (is_this_playing and self.audio_engine.play_duration_sec > 0.0)
                else entry.duration_seconds
            )
            meta_part = self._format_recording_meta_text(
                entry,
                is_playing_this=is_this_playing,
                pos_sec=self.audio_engine.play_position_sec,
                dur_sec=eff_dur,
            )
            row_text = self._format_recording_row_text(
                entry,
                is_playing_this=is_this_playing,
                is_paused=self.audio_engine.is_paused,
                pos_sec=self.audio_engine.play_position_sec,
                dur_sec=eff_dur,
            )
            item = QListWidgetItem(row_text)
            item.setData(Qt.ItemDataRole.UserRole, entry.filepath)
            item.setData(ROLE_RECORDING_FILENAME, entry.filename)
            item.setData(ROLE_RECORDING_META, meta_part)
            item.setData(ROLE_PLAYBACK_ACTIVE, is_this_playing)
            item.setData(ROLE_PLAYBACK_PAUSED, self.audio_engine.is_paused if is_this_playing else False)
            prog = (
                max(0.0, min(1.0, self.audio_engine.play_position_sec / eff_dur))
                if (is_this_playing and eff_dur > 0.0)
                else 0.0
            )
            item.setData(ROLE_PLAYBACK_PROGRESS, prog)
            item.setData(
                ROLE_PLAYBACK_POS_SEC,
                self.audio_engine.play_position_sec if is_this_playing else 0.0,
            )
            item.setData(ROLE_PLAYBACK_DUR_SEC, eff_dur)
            self.recordings_list.addItem(item)
            if current_selected_path and entry.filepath == current_selected_path:
                selected_row = idx

        if self._project_recordings:
            self.recordings_list.setCurrentRow(selected_row)
        self.recordings_list.blockSignals(False)
        self.recordings_item_delegate.reset_scroll_timer()
        self._refresh_storage_status()
        self._update_playback_buttons_state()
        self._reposition_inline_play_button()

    def _get_selected_recording_path(self) -> Optional[Path]:
        item = self.recordings_list.currentItem()
        if item is None:
            return None
        path = item.data(Qt.ItemDataRole.UserRole)
        return Path(path) if path else None

    def _on_playback_finished(self) -> None:
        if not self.audio_engine.is_recording and not self.audio_engine.is_playing:
            if self._active_input_device is not None:
                settings = self.config_manager.settings
                self.audio_engine.start_monitoring(
                    device=self._active_input_device,
                    sample_rate=settings.sample_rate,
                    bit_depth=settings.bit_depth,
                    studio_mode=settings.studio_mode,
                    channels=settings.effective_channels,
                    input_channel=settings.input_channel,
                )
        self._update_playback_buttons_state()
        self._refresh_vu_meter()

    def _on_play_pause_clicked(self, force_new: bool = False) -> None:
        if self.audio_engine.is_recording:
            return

        selected_path = self._get_selected_recording_path()
        if selected_path is None or not selected_path.is_file():
            return

        if (
            not force_new
            and self.audio_engine.is_playing
            and self.audio_engine.playing_filepath == selected_path
        ):
            paused = self.audio_engine.toggle_pause_playback()
            self._update_playback_buttons_state(paused=paused)
            return

        self.audio_engine.start_playback(
            filepath=selected_path,
            output_device=self._active_output_device,
            on_finished=lambda: QTimer.singleShot(
                0, self._on_playback_finished
            ),
            on_error=lambda err: QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self, "Playback Error", f"Could not play WAV file:\n{err}"
                ),
            ),
        )
        self._update_playback_buttons_state()
        self._refresh_vu_meter()

    def _on_stop_playback_clicked(self) -> None:
        self.audio_engine.stop_playback()
        self._on_playback_finished()

    def _on_seek_playback_relative(self, delta_sec: float) -> None:
        if not self.audio_engine.is_playing:
            return
        target = self.audio_engine.play_position_sec + float(delta_sec)
        if self.audio_engine.seek_playback(target):
            self._update_playing_clip_row()
            self._update_playback_buttons_state()

    def _update_playback_buttons_state(self, paused: Optional[bool] = None) -> None:
        is_rec = self.audio_engine.is_recording
        selected_path = self._get_selected_recording_path()
        has_selection = selected_path is not None
        is_playing = self.audio_engine.is_playing
        is_paused = self.audio_engine.is_paused if paused is None else paused
        playing_path = self.audio_engine.playing_filepath if is_playing else None
        selected_is_playing = bool(
            is_playing and selected_path is not None and playing_path == selected_path
        )

        self.rename_take_btn.setEnabled(not is_rec and has_selection)
        self.play_btn.setEnabled(not is_rec and has_selection)
        self.stop_play_btn.setEnabled(not is_rec and is_playing)
        self.stop_play_btn.setStyleSheet(
            build_stop_playback_button_style(bool(not is_rec and is_playing), self._scale)
        )

        if selected_is_playing:
            if is_paused:
                self.play_btn.setText("▶ Resume")
                self.play_btn.setStyleSheet(
                    build_playback_button_style("paused", self._scale, compact=True)
                )
            else:
                self.play_btn.setText("❚❚ Pause")
                self.play_btn.setStyleSheet(
                    build_playback_button_style("playing", self._scale, compact=True)
                )
        else:
            self.play_btn.setText("▶ Play")
            self.play_btn.setStyleSheet(
                build_playback_button_style("ready", self._scale, compact=True)
            )

        self._reposition_inline_play_button()
        self._update_playing_clip_row(paused_override=is_paused)

        if is_playing:
            pos_str = format_playback_time(self.audio_engine.play_position_sec)
            dur_str = format_playback_time(self.audio_engine.play_duration_sec)
            prod_connected = (
                self.remote_producer_server.is_running
                and self.remote_producer_server.connected_client_count > 0
            )
            if is_paused:
                self.playback_status_label.setText(
                    f"❚❚ Paused  [{pos_str} / {dur_str}]"
                )
                self.playback_status_label.setStyleSheet(
                    "color: #FFDF5D; font-family: monospace; font-weight: bold;"
                )
            elif prod_connected:
                self.playback_status_label.setText(
                    f"▶ Reviewing w/ Producer  [{pos_str} / {dur_str}]"
                )
                self.playback_status_label.setStyleSheet(
                    "color: #79C0FF; font-family: monospace; font-weight: bold;"
                )
            else:
                self.playback_status_label.setText(
                    f"▶ Playing  [{pos_str} / {dur_str}]"
                )
                self.playback_status_label.setStyleSheet(
                    "color: #79C0FF; font-family: monospace; font-weight: bold;"
                )
        elif not is_rec:
            self.playback_status_label.setText(
                f"{len(self._project_recordings)} take(s)"
            )
            self.playback_status_label.setStyleSheet(
                "color: #8B949E; font-family: monospace;"
            )

    def _on_manual_refresh(self) -> None:
        self.config_manager._ensure_storage_mounted(force=True)
        self._sync_hardware_state(force_probe=True)
        self._refresh_recordings_list()
        self.ip_bar_label.setText(get_primary_ip_display())
        self._refresh_clock_display(force=True)
        self._refresh_system_resources()

    def _open_help_dialog(self) -> None:
        dlg = HelpDialog(parent=self)
        dlg.exec()
        self.setFocus()

    def _open_settings_dialog(self) -> None:
        if self.audio_engine.is_recording:
            return
        self.audio_engine.stop_playback()
        dlg = SettingsDialog(
            config_manager=self.config_manager,
            audio_engine=self.audio_engine,
            samba_manager=self.samba_manager,
            parent=self,
        )
        if dlg.exec():
            self._sync_hardware_state()
            self._refresh_recordings_list()
            self._refresh_clock_display(force=True)
        elif getattr(dlg, "exit_to_console_requested", False):
            self.close()

    # -------------------------------------------------------------------------
    # Periodic Timers & Keyboard Hotkeys
    # -------------------------------------------------------------------------
    def _on_ui_tick(self) -> None:
        is_rec = self.audio_engine.is_recording
        is_rec_paused = self.audio_engine.is_record_paused
        self._refresh_vu_meter()
        self._refresh_clock_display()

        # Detect asynchronous changes from Remote Producer (talkback, client connection, or mute state)
        tb_now = self.audio_engine.talkback_active
        tb_latched_now = self.audio_engine.talkback_latched
        rem_tb_now = self.audio_engine.remote_talkback_active
        prod_count_now = self.remote_producer_server.connected_client_count
        mute_active_now = bool(
            self.audio_engine.talent_cough_muted
            or not self.audio_engine.talent_mic_send_enabled
        )
        now_mono = time.monotonic()
        blink_needed = bool(is_rec_paused or mute_active_now)
        blink_now = ((int(now_mono * 2.5) % 2) == 0) if blink_needed else True

        if (
            tb_now != self._last_observed_tb_active
            or tb_latched_now != self._last_observed_tb_latched
            or rem_tb_now != self._last_observed_remote_tb
            or prod_count_now != self._last_observed_producer_count
            or mute_active_now != self._last_observed_mute_state
            or blink_now != self._pause_blink_phase
        ):
            self._last_observed_tb_active = tb_now
            self._last_observed_tb_latched = tb_latched_now
            self._last_observed_remote_tb = rem_tb_now
            self._last_observed_producer_count = prod_count_now
            self._last_observed_mute_state = mute_active_now
            self._pause_blink_phase = blink_now
            self._refresh_transport_styles()

        if now_mono - self._last_resource_refresh_monotonic >= 1.5:
            self._refresh_system_resources()

        if is_rec:
            elapsed = self.audio_engine.elapsed_seconds
            hrs = int(elapsed // 3600)
            mins = int((elapsed % 3600) // 60)
            secs = elapsed % 60.0
            if is_rec_paused:
                self.timer_label.setText(
                    f"❚❚ {hrs:02d}:{mins:02d}:{secs:04.1f}  [PAUSED]"
                )
            else:
                self.timer_label.setText(f"{hrs:02d}:{mins:02d}:{secs:04.1f}")
            if now_mono - self._last_storage_refresh_monotonic >= 0.5:
                self._refresh_storage_status()
        elif self.audio_engine.is_playing:
            self._update_playing_clip_row()
            pos_str = format_playback_time(self.audio_engine.play_position_sec)
            dur_str = format_playback_time(self.audio_engine.play_duration_sec)
            prod_connected = (
                self.remote_producer_server.is_running
                and self.remote_producer_server.connected_client_count > 0
            )
            if self.audio_engine.is_paused:
                self.playback_status_label.setText(
                    f"❚❚ Paused  [{pos_str} / {dur_str}]"
                )
            elif prod_connected:
                self.playback_status_label.setText(
                    f"▶ Reviewing w/ Producer  [{pos_str} / {dur_str}]"
                )
            else:
                self.playback_status_label.setText(
                    f"▶ Playing  [{pos_str} / {dur_str}]"
                )
        elif self.recordings_item_delegate.is_selected_row_scrolling:
            viewport = self.recordings_list.viewport()
            curr_item = self.recordings_list.currentItem()
            if viewport is not None and curr_item is not None:
                viewport.update(self.recordings_list.visualItemRect(curr_item))

    def _on_background_tick(self) -> None:
        self.ip_bar_label.setText(get_primary_ip_display())
        self._refresh_system_resources()
        self._update_filename_preview()
        self.config_manager.enforce_storage_permissions()
        self._refresh_storage_status()
        if not self.audio_engine.is_recording and not self.audio_engine.is_playing:
            self._sync_hardware_state()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()

        # Momentary Push-to-Talk Hotkey [T] (ignoring X11 auto-repeat!)
        if key == Qt.Key.Key_T:
            if not event.isAutoRepeat() and not self._talkback_key_held:
                self._talkback_key_held = True
                self._on_talkback_pressed()
            return

        # Momentary Cough / Privacy Mute Hotkey [B]
        if key == Qt.Key.Key_B:
            if not event.isAutoRepeat() and not self._cough_key_held:
                self._cough_key_held = True
                self._on_cough_privacy_pressed()
            return

        if event.isAutoRepeat():
            return

        if key in (Qt.Key.Key_R, Qt.Key.Key_Space):
            if self.record_btn.isEnabled():
                self._on_record_button_clicked()
            return
        if key == Qt.Key.Key_Pause:
            if self.audio_engine.is_recording:
                self._on_pause_record_button_clicked()
            return
        if key == Qt.Key.Key_C:
            self._on_reset_peak_hold()
            return
        if key == Qt.Key.Key_M:
            self._on_toggle_studio_mode()
            return
        if key in (Qt.Key.Key_N, Qt.Key.Key_F4):
            if not self.audio_engine.is_recording:
                self._on_rename_recording_clicked()
            return
        if key == Qt.Key.Key_Delete:
            if not self.audio_engine.is_recording:
                self._on_delete_recording_clicked()
            return
        if key == Qt.Key.Key_P:
            if self.audio_engine.is_recording:
                self._on_pause_record_button_clicked()
            else:
                self._on_play_pause_clicked()
            return
        if key == Qt.Key.Key_S:
            self._on_stop_playback_clicked()
            return
        if key == Qt.Key.Key_Left:
            if self.audio_engine.is_playing:
                self._on_seek_playback_relative(-3.0)
                return
        if key == Qt.Key.Key_Right:
            if self.audio_engine.is_playing:
                self._on_seek_playback_relative(3.0)
                return
        if key == Qt.Key.Key_F1:
            self._open_help_dialog()
            return
        if key == Qt.Key.Key_F2 or (
            key == Qt.Key.Key_Comma
            and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            self._open_settings_dialog()
            return
        if key == Qt.Key.Key_F3:
            self._open_remote_producer_dialog()
            return
        if key == Qt.Key.Key_F5:
            self._on_manual_refresh()
            return
        if key == Qt.Key.Key_F6:
            if not self.audio_engine.is_recording:
                self._open_file_manager_dialog()
            return
        if key == Qt.Key.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            return
        if key == Qt.Key.Key_Q and bool(
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if not self.audio_engine.is_recording:
                self.close()
            return

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_T and not event.isAutoRepeat():
            if self._talkback_key_held:
                self._talkback_key_held = False
                self._on_talkback_released()
            return
        if event.key() == Qt.Key.Key_B and not event.isAutoRepeat():
            if self._cough_key_held:
                self._cough_key_held = False
                self._on_cough_privacy_released()
            return
        super().keyReleaseEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.remote_producer_server.stop()
        self.audio_engine.stop_monitoring()
        self.audio_engine.stop_recording()
        self.audio_engine.stop_playback()
        super().closeEvent(event)
