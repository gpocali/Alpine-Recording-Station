"""Graphical Storage Utilization & Recording Time Remaining Monitor widget."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.config_manager import (
    STORAGE_CRITICAL_USED_PERCENT,
    STORAGE_WARN_USED_PERCENT,
    StorageStatus,
)

STORAGE_SCALE_TICKS: Tuple[int, ...] = (0, 25, 50, 80, 90, 100)
NUM_STORAGE_SEGMENTS: int = 50


class _StorageUsageCanvas(QWidget):
    """Custom-painted studio bar graph showing 0..100% drive utilization and 80%/90% threshold zones."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scale: float = 1.0
        self._status = StorageStatus(storage_path=Path("/storage"))

        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding
        )
        self.setMinimumHeight(34)
        self.setToolTip(
            "Drive Utilization Bar for /storage (0% to 100%).\n"
            "• Green (0% to 80%): Normal storage capacity range\n"
            "• Amber (80% to 90%): Drive getting full — plan to offload or clean up files\n"
            "• Red (90% to 100%): Drive critically full — immediate attention required"
        )

    def set_status(self, status: StorageStatus) -> None:
        self._status = status
        self.update()

    def apply_scale(self, scale: float) -> None:
        self._scale = max(0.55, float(scale))
        self.setMinimumHeight(max(28, int(round(36 * self._scale))))
        self.update()

    @staticmethod
    def _segment_colors(seg_pct: float) -> Tuple[QColor, QColor]:
        """Return (lit_color, unlit_dim_color) for a segment's percentage position (0..100]."""
        if seg_pct > STORAGE_CRITICAL_USED_PERCENT:
            return (QColor("#FF4444"), QColor("#2D1215"))
        if seg_pct > STORAGE_WARN_USED_PERCENT:
            return (QColor("#F0B72F"), QColor("#2B2311"))
        return (QColor("#3FB950"), QColor("#112618"))

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        if w <= 20 or h <= 14:
            painter.end()
            return

        label_col_w = max(24.0, round(30.0 * self._scale))
        right_pad = max(10.0, round(14.0 * self._scale))
        scale_h = max(14.0, round(16.0 * self._scale))

        bar_x = label_col_w
        bar_w = max(40.0, w - label_col_w - right_pad)
        bar_h = max(10.0, h - scale_h - 2.0)
        y0 = 0.0

        # Draw left 'DSK' label aligned with VU meter 'IN'/'L'/'R' labels
        ch_font_px = max(8, int(round(10 * self._scale)))
        ch_font = QFont("DejaVu Sans Mono", ch_font_px, QFont.Weight.Bold)
        ch_font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(ch_font)
        painter.setPen(QColor("#8B949E"))
        painter.drawText(
            QRectF(0.0, y0, label_col_w - 4.0, bar_h),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            "DSK",
        )

        # Recessed track background
        track_rect = QRectF(bar_x, y0, bar_w, bar_h)
        painter.setPen(QPen(QColor("#21262D"), 1.0))
        painter.setBrush(QBrush(QColor("#090C10")))
        painter.drawRoundedRect(track_rect, 4.0, 4.0)

        used_pct = (
            min(100.0, max(0.0, float(self._status.used_percent)))
            if self._status.available
            else 0.0
        )
        seg_gap = max(1.0, round(1.5 * self._scale))
        seg_w = max(
            1.5,
            (bar_w - seg_gap * (NUM_STORAGE_SEGMENTS - 1)) / NUM_STORAGE_SEGMENTS,
        )
        inner_y = y0 + 2.0
        inner_h = max(4.0, bar_h - 4.0)

        painter.setPen(Qt.PenStyle.NoPen)
        for seg_i in range(NUM_STORAGE_SEGMENTS):
            seg_pct_start = (seg_i / NUM_STORAGE_SEGMENTS) * 100.0
            seg_pct_end = ((seg_i + 1) / NUM_STORAGE_SEGMENTS) * 100.0
            lit_col, dim_col = self._segment_colors(seg_pct_end)

            if self._status.available and used_pct > seg_pct_start:
                fill_col = lit_col
            else:
                fill_col = dim_col

            sx = bar_x + seg_i * (seg_w + seg_gap)
            seg_rect = QRectF(sx, inner_y, seg_w, inner_h)
            painter.setBrush(QBrush(fill_col))
            painter.drawRoundedRect(seg_rect, 1.5, 1.5)

        # Draw threshold boundary lines at 80% (Warning) and 90% (Critical)
        for boundary_pct, boundary_col in (
            (STORAGE_WARN_USED_PERCENT, "#D29922"),
            (STORAGE_CRITICAL_USED_PERCENT, "#F85149"),
        ):
            bx = bar_x + (boundary_pct / 100.0) * bar_w
            painter.setPen(QPen(QColor(boundary_col), 1.5))
            painter.drawLine(int(bx), int(y0), int(bx), int(y0 + bar_h))

        # Draw percentage scale ticks along the bottom
        scale_y = bar_h + 1.0
        scale_font_px = max(8, int(round(9 * self._scale)))
        scale_font = QFont("DejaVu Sans Mono", scale_font_px)
        scale_font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(scale_font)

        for tick_pct in STORAGE_SCALE_TICKS:
            ratio = tick_pct / 100.0
            tx = bar_x + ratio * bar_w

            if tick_pct >= 90:
                tick_col = QColor("#F85149")
            elif tick_pct >= 80:
                tick_col = QColor("#D29922")
            else:
                tick_col = QColor("#6E7681")

            painter.setPen(QPen(tick_col, 1.0))
            painter.drawLine(int(tx), int(scale_y), int(tx), int(scale_y + 3.0))

            label_txt = f"{tick_pct}%"
            tw = 36.0 * self._scale
            if tick_pct == 0:
                text_rect = QRectF(tx - 2.0, scale_y + 3.0, tw, scale_h - 3.0)
                align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            elif tick_pct == 100:
                text_rect = QRectF(tx - tw + 2.0, scale_y + 3.0, tw, scale_h - 3.0)
                align = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            else:
                text_rect = QRectF(tx - tw / 2.0, scale_y + 3.0, tw, scale_h - 3.0)
                align = int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

            painter.drawText(text_rect, align, label_txt)

        painter.end()


class StorageMonitorWidget(QFrame):
    """Studio telemetry panel for /storage drive utilization and recording time remaining at current settings."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scale: float = 1.0
        self._status = StorageStatus(storage_path=Path("/storage"))
        self._is_recording: bool = False
        self._blink_on: bool = True

        self.setObjectName("StorageMonitorFrame")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(3)

        # Header Row: Storage path/status, utilization readout, data rate, remaining time badge, alert badge
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)

        self.title_label = QLabel("● STORAGE (/storage)")
        header_row.addWidget(self.title_label)

        header_row.addStretch(1)

        self.usage_label = QLabel("USED: 0.0%  •  FREE: --")
        header_row.addWidget(self.usage_label)

        self.rate_label = QLabel("RATE: -- MB/min")
        header_row.addWidget(self.rate_label)

        self.remaining_label = QLabel("TIME REMAINING: --h --m --s")
        self.remaining_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.remaining_label.setToolTip(
            "Estimated recording time remaining on /storage at the current Bit Depth, "
            "Sample Rate, and Channel/Studio Mode settings"
        )
        header_row.addWidget(self.remaining_label)

        self.status_badge = QLabel("OK")
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_badge.setToolTip(
            "Drive space health indicator:\n"
            "• OK: < 80% used and > 30 min remaining\n"
            "• LOW SPACE: >= 80% used or <= 30 min remaining\n"
            "• CRITICAL / FULL: >= 90% used or <= 10 min remaining"
        )
        header_row.addWidget(self.status_badge)

        layout.addLayout(header_row)

        self.canvas = _StorageUsageCanvas(parent=self)
        layout.addWidget(self.canvas)

        self._refresh_readouts()

    @property
    def status(self) -> StorageStatus:
        return self._status

    def value(self) -> int:
        """Return integer drive utilization percentage (0..100)."""
        return int(round(self._status.used_percent))

    def remaining_seconds(self) -> float:
        """Return estimated recording time remaining in seconds."""
        return float(self._status.remaining_seconds)

    def set_status(
        self,
        status: StorageStatus,
        is_recording: bool = False,
        blink_on: bool = True,
    ) -> None:
        """Update storage utilization bar, remaining recording time readout, and warning/critical styling."""
        self._status = status
        self._is_recording = bool(is_recording)
        self._blink_on = bool(blink_on)
        self.canvas.set_status(status)
        self._refresh_readouts()

    def apply_scale(self, scale: float) -> None:
        self._scale = max(0.55, float(scale))
        self.canvas.apply_scale(self._scale)
        self._refresh_readouts()

    def _refresh_readouts(self) -> None:
        st = self._status
        small_px = max(10, int(round(12 * self._scale)))
        badge_pad_v = max(2, int(round(3 * self._scale)))
        badge_pad_h = max(6, int(round(9 * self._scale)))
        ch_desc = (
            "Studio Stereo (2ch)"
            if st.studio_mode
            else ("Stereo (2ch)" if st.effective_channels >= 2 else "Mono (1ch)")
        )

        # Frame styling & title based on utilization level
        if not st.available or st.level == "offline":
            frame_bg = "#12161C"
            frame_border = "1px solid #30363D"
            title_txt = f"○ STORAGE OFFLINE ({st.storage_path})"
            title_col = "#6E7681"
        elif st.level == "full":
            frame_bg = "#240D11" if self._blink_on else "#190A0D"
            frame_border = "2px solid #FF6B7A"
            title_txt = "⛔ STORAGE DRIVE FULL — NO RECORDING SPACE REMAINING!"
            title_col = "#FF6B7A"
        elif st.level == "critical":
            frame_bg = "#200E12" if self._blink_on else "#160B0E"
            border_c = "#FF6B7A" if (self._is_recording and self._blink_on) else "#F85149"
            frame_border = f"2px solid {border_c}"
            title_txt = "⚠ STORAGE CRITICALLY FULL — OFFLOAD FILES SOON"
            title_col = "#FF6B7A"
        elif st.level == "warning":
            frame_bg = "#19150B"
            frame_border = "2px solid #D29922"
            title_txt = "⚠ STORAGE GETTING FULL — LOW DISK SPACE WARNING"
            title_col = "#FFDF5D"
        else:
            frame_bg = "#12161C"
            frame_border = "1px solid #262C34"
            title_txt = "● STORAGE & RECORDING TIME"
            title_col = "#3FB950"

        self.setStyleSheet(
            f"QFrame#StorageMonitorFrame {{ background-color: {frame_bg}; border: {frame_border}; border-radius: 8px; }}"
        )

        self.title_label.setText(title_txt)
        self.title_label.setStyleSheet(
            f"border: none; color: {title_col}; font-weight: bold; font-size: {small_px}px;"
        )

        # Drive utilization readout
        if not st.available:
            self.usage_label.setText("DRIVE: UNAVAILABLE")
            usage_col = "#6E7681"
        else:
            self.usage_label.setText(
                f"USED: {st.used_percent:4.1f}% ({st.formatted_used} / {st.formatted_total})  •  "
                f"FREE: {st.formatted_free}"
            )
            if st.is_critical:
                usage_col = "#FF6B7A"
            elif st.is_warning:
                usage_col = "#FFDF5D"
            else:
                usage_col = "#C9D1D9"

        self.usage_label.setStyleSheet(
            f"border: none; color: {usage_col}; font-family: monospace; font-weight: bold; font-size: {small_px}px;"
        )

        # Data rate readout for current settings
        self.rate_label.setText(
            f"{st.mb_per_minute:.1f} MB/min ({st.bit_depth}b/{st.sample_rate // 1000}k/{st.effective_channels}ch)"
        )
        self.rate_label.setStyleSheet(
            f"border: none; color: #8B949E; font-family: monospace; font-size: {small_px}px;"
        )
        self.rate_label.setToolTip(
            f"Current recording data rate: {st.mb_per_minute:.2f} MB/min "
            f"({st.bit_depth}-bit, {st.sample_rate:,} Hz, {ch_desc})"
        )

        # Recording Time Remaining badge
        if not st.available:
            rem_txt = "TIME REMAINING: --h --m --s"
            rem_fg = "#8B949E"
            rem_bg = "#161B22"
            rem_border = "#30363D"
        else:
            rem_txt = f"TIME REMAINING: {st.formatted_remaining}"
            if st.is_critical:
                rem_fg = "#FFFFFF"
                rem_bg = "#8E1523"
                rem_border = "#FF6B7A"
            elif st.is_warning:
                rem_fg = "#FFDF5D"
                rem_bg = "#3B2300"
                rem_border = "#F0B72F"
            else:
                rem_fg = "#58A6FF"
                rem_bg = "#13233A"
                rem_border = "#388BFD"

        self.remaining_label.setText(rem_txt)
        self.remaining_label.setStyleSheet(
            f"background-color: {rem_bg}; color: {rem_fg}; border: 1px solid {rem_border}; "
            f"border-radius: 4px; padding: {badge_pad_v}px {badge_pad_h}px; "
            f"font-family: monospace; font-weight: bold; font-size: {small_px}px;"
        )

        # Status Alert Pill Badge
        if not st.available or st.level == "offline":
            badge_txt = "OFFLINE"
            b_fg, b_bg, b_border = "#6E7681", "#161B22", "#30363D"
        elif st.level == "full":
            badge_txt = "⛔ DRIVE FULL"
            b_fg, b_bg, b_border = "#FFFFFF", "#D72638", "#FF6B7A"
        elif st.level == "critical":
            badge_txt = "⚠ CRITICAL"
            b_fg, b_bg, b_border = "#FFFFFF", "#D72638", "#FF6B7A"
        elif st.level == "warning":
            badge_txt = "⚠ LOW SPACE"
            b_fg, b_bg, b_border = "#FFFFFF", "#9E6A03", "#FFDF5D"
        else:
            badge_txt = "SPACE OK"
            b_fg, b_bg, b_border = "#3FB950", "#112618", "#2EA043"

        self.status_badge.setText(badge_txt)
        self.status_badge.setStyleSheet(
            f"background-color: {b_bg}; color: {b_fg}; border: 1px solid {b_border}; "
            f"border-radius: 4px; padding: {badge_pad_v}px {badge_pad_h}px; "
            f"font-family: monospace; font-weight: bold; font-size: {small_px}px;"
        )
