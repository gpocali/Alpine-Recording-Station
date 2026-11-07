"""Graphical Studio VU & Peak Hold Meter widget for real-time input level setting."""

from __future__ import annotations

from typing import Callable, Optional, Tuple

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.audio_engine import (
    METER_FLOOR_DB,
    VuMeterReadings,
    dbfs_to_ratio,
    linear_to_dbfs,
)

SCALE_TICKS_DB: Tuple[int, ...] = (-60, -48, -36, -30, -24, -18, -12, -6, -3, 0)
NUM_LED_SEGMENTS: int = 60


class _VuMeterCanvas(QWidget):
    """Custom-painted LED bar graph canvas with RMS body, Peak segments, Peak Hold marker, and dBFS scale."""

    def __init__(
        self,
        on_reset_peak: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._on_reset_peak = on_reset_peak
        self._scale: float = 1.0
        self._readings = VuMeterReadings()
        self._has_device: bool = False
        self._is_muted: bool = False

        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding
        )
        self.setMinimumHeight(54)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(
            "Graphical Input VU & Peak Hold Meter (-60 to 0 dBFS).\n"
            "• Green (-60 to -18 dB): Normal signal range\n"
            "• Amber (-18 to -6 dB): Optimal nominal peak target zone\n"
            "• Red (-6 to 0 dB): Hot / approaching digital clip\n"
            "• Bright vertical marker: Maintained Peak Hold level\n"
            "Click anywhere on the meter or press [C] to reset Peak Hold."
        )

    def set_state(
        self,
        readings: VuMeterReadings,
        has_device: bool,
        is_muted: bool = False,
    ) -> None:
        self._readings = readings
        self._has_device = has_device
        self._is_muted = bool(is_muted)
        self.update()

    def apply_scale(self, scale: float) -> None:
        self._scale = max(0.55, float(scale))
        num_ch = max(1, min(2, self._readings.channels))
        base_h = 56 if num_ch == 1 else 72
        self.setMinimumHeight(max(46, int(round(base_h * self._scale))))
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._on_reset_peak is not None:
            self._on_reset_peak()
            event.accept()
            return
        super().mousePressEvent(event)

    @staticmethod
    def _segment_colors(seg_db: float) -> Tuple[QColor, QColor, QColor]:
        """Return (lit_rms_color, lit_peak_color, unlit_dim_color) for a segment's dBFS position."""
        if seg_db >= -6.0:
            # Red zone (-6 to 0 dBFS)
            return (
                QColor("#FF4444"),
                QColor("#F85149"),
                QColor("#2D1215"),
            )
        if seg_db >= -18.0:
            # Amber / nominal peak target zone (-18 to -6 dBFS)
            return (
                QColor("#F0B72F"),
                QColor("#D29922"),
                QColor("#2B2311"),
            )
        # Green safe zone (-60 to -18 dBFS)
        return (
            QColor("#3FB950"),
            QColor("#2EA043"),
            QColor("#112618"),
        )

    @staticmethod
    def _peak_hold_marker_color(hold_db: float) -> QColor:
        if hold_db >= -3.0:
            return QColor("#FF5555")
        if hold_db >= -18.0:
            return QColor("#FFDF5D")
        return QColor("#58A6FF")

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        if w <= 20 or h <= 16:
            painter.end()
            return

        num_ch = max(1, min(2, self._readings.channels))
        label_col_w = max(24.0, round(30.0 * self._scale))
        right_pad = max(10.0, round(14.0 * self._scale))
        scale_h = max(16.0, round(19.0 * self._scale))

        bar_x = label_col_w
        bar_w = max(40.0, w - label_col_w - right_pad)
        bars_area_h = max(16.0, h - scale_h - 2.0)

        gap_y = max(3.0, round(4.0 * self._scale)) if num_ch == 2 else 0.0
        single_bar_h = max(
            10.0,
            (bars_area_h - gap_y * (num_ch - 1)) / num_ch,
        )

        # Channel label font
        ch_font_px = max(9, int(round(11 * self._scale)))
        ch_font = QFont("DejaVu Sans Mono", ch_font_px, QFont.Weight.Bold)
        ch_font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(ch_font)

        ch_labels = ("IN",) if num_ch == 1 else ("L", "R")
        ch_peaks = self._readings.channel_peaks
        ch_rms = self._readings.channel_rms
        ch_holds = self._readings.channel_peak_holds

        seg_gap = max(1.0, round(1.5 * self._scale))
        seg_w = max(1.5, (bar_w - seg_gap * (NUM_LED_SEGMENTS - 1)) / NUM_LED_SEGMENTS)

        for ch_idx in range(num_ch):
            y0 = ch_idx * (single_bar_h + gap_y)

            # Draw channel label ('IN' or 'L'/'R')
            painter.setPen(QColor("#8B949E"))
            painter.drawText(
                QRectF(0.0, y0, label_col_w - 4.0, single_bar_h),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                ch_labels[ch_idx],
            )

            # Draw recessed meter track background
            track_rect = QRectF(bar_x, y0, bar_w, single_bar_h)
            painter.setPen(QPen(QColor("#21262D"), 1.0))
            painter.setBrush(QBrush(QColor("#090C10")))
            painter.drawRoundedRect(track_rect, 4.0, 4.0)

            peak_lin = (
                0.0
                if self._is_muted
                else (ch_peaks[ch_idx] if ch_idx < len(ch_peaks) else self._readings.peak_level)
            )
            rms_lin = (
                0.0
                if self._is_muted
                else (ch_rms[ch_idx] if ch_idx < len(ch_rms) else self._readings.rms_level)
            )
            hold_lin = ch_holds[ch_idx] if ch_idx < len(ch_holds) else self._readings.peak_hold_level

            active_signal = self._has_device and not self._is_muted
            peak_ratio = dbfs_to_ratio(linear_to_dbfs(peak_lin)) if active_signal else 0.0
            rms_ratio = dbfs_to_ratio(linear_to_dbfs(rms_lin)) if active_signal else 0.0
            hold_db = linear_to_dbfs(hold_lin)
            hold_ratio = dbfs_to_ratio(hold_db)

            inner_y = y0 + 2.0
            inner_h = max(4.0, single_bar_h - 4.0)

            # Draw discrete LED segments
            painter.setPen(Qt.PenStyle.NoPen)
            for seg_i in range(NUM_LED_SEGMENTS):
                seg_ratio_start = seg_i / NUM_LED_SEGMENTS
                seg_ratio_end = (seg_i + 1) / NUM_LED_SEGMENTS
                seg_db = METER_FLOOR_DB + seg_ratio_end * abs(METER_FLOOR_DB)
                rms_col, peak_col, dim_col = self._segment_colors(seg_db)

                if active_signal and peak_ratio > seg_ratio_start:
                    if rms_ratio >= seg_ratio_end:
                        fill_col = rms_col
                    else:
                        fill_col = peak_col
                else:
                    fill_col = dim_col

                sx = bar_x + seg_i * (seg_w + seg_gap)
                seg_rect = QRectF(sx, inner_y, seg_w, inner_h)
                painter.setBrush(QBrush(fill_col))
                painter.drawRoundedRect(seg_rect, 1.5, 1.5)

            # Draw subtle -18 dBFS and -6 dBFS zone boundary ticks on track
            for boundary_db, boundary_col in ((-18.0, "#30363D"), (-6.0, "#444C56")):
                bx = bar_x + dbfs_to_ratio(boundary_db) * bar_w
                painter.setPen(QPen(QColor(boundary_col), 1.0))
                painter.drawLine(int(bx), int(y0), int(bx), int(y0 + single_bar_h))

            # Draw Graphical Peak Hold Marker line & cap when a peak is held above floor
            if hold_lin > 1e-5 and hold_ratio > 0.002:
                marker_x = bar_x + min(1.0, max(0.0, hold_ratio)) * (bar_w - 2.0)
                marker_col = self._peak_hold_marker_color(hold_db)
                marker_w = max(3.0, round(3.5 * self._scale))

                # High-contrast dark halo behind the Peak Hold marker for visibility over lit segments
                halo_rect = QRectF(
                    marker_x - 1.0,
                    y0 + 0.5,
                    marker_w + 2.0,
                    single_bar_h - 1.0,
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(9, 12, 16, 210)))
                painter.drawRoundedRect(halo_rect, 1.5, 1.5)

                # Bright Peak Hold bar
                marker_rect = QRectF(
                    marker_x,
                    y0 + 1.0,
                    marker_w,
                    single_bar_h - 2.0,
                )
                painter.setBrush(QBrush(marker_col))
                painter.drawRoundedRect(marker_rect, 1.5, 1.5)

        # Draw dBFS scale ruler and labels along the bottom
        scale_y = bars_area_h + 2.0
        scale_font_px = max(8, int(round(10 * self._scale)))
        scale_font = QFont("DejaVu Sans Mono", scale_font_px)
        scale_font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(scale_font)

        for tick_db in SCALE_TICKS_DB:
            ratio = dbfs_to_ratio(float(tick_db))
            tx = bar_x + ratio * bar_w

            if tick_db >= -3:
                tick_col = QColor("#F85149")
            elif tick_db >= -18:
                tick_col = QColor("#D29922") if tick_db > -18 else QColor("#3FB950")
            else:
                tick_col = QColor("#6E7681")

            painter.setPen(QPen(tick_col, 1.0))
            painter.drawLine(int(tx), int(scale_y), int(tx), int(scale_y + 4.0))

            label_txt = "0 dB" if tick_db == 0 else str(tick_db)
            tw = 36.0 * self._scale
            if tick_db == -60:
                text_rect = QRectF(tx - 2.0, scale_y + 4.0, tw, scale_h - 4.0)
                align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            elif tick_db == 0:
                text_rect = QRectF(tx - tw + 2.0, scale_y + 4.0, tw, scale_h - 4.0)
                align = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            else:
                text_rect = QRectF(tx - tw / 2.0, scale_y + 4.0, tw, scale_h - 4.0)
                align = int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

            painter.drawText(text_rect, align, label_txt)

        painter.end()


class VuMeterWidget(QFrame):
    """Complete Studio VU Meter panel with graphical LED bars, Peak Hold marker, dBFS readouts, CLIP badge, and Reset Peak button."""

    def __init__(
        self,
        on_reset_peak: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._on_reset_peak = on_reset_peak
        self._scale = 1.0
        self._readings = VuMeterReadings()
        self._is_recording: bool = False
        self._has_device: bool = False
        self._is_paused: bool = False
        self._is_muted: bool = False
        self._is_talkback_override: bool = False

        self.setObjectName("VuMeterFrame")
        self.setStyleSheet(
            "QFrame#VuMeterFrame { background-color: #12161C; border: 1px solid #262C34; border-radius: 8px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)

        # Header Row: Meter status, Live dBFS, Maintained Peak Hold dBFS, CLIP badge, Reset Peak button
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)

        self.title_label = QLabel("● INPUT VU / PEAK METER")
        self.title_label.setStyleSheet(
            "border: none; color: #3FB950; font-weight: bold;"
        )
        header_row.addWidget(self.title_label)

        header_row.addStretch(1)

        self.live_db_label = QLabel("INPUT: -∞ dBFS")
        self.live_db_label.setStyleSheet(
            "border: none; color: #8B949E; font-family: monospace; font-weight: bold;"
        )
        header_row.addWidget(self.live_db_label)

        self.peak_hold_label = QLabel("PEAK HOLD: -∞ dBFS")
        self.peak_hold_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.peak_hold_label.setToolTip(
            "Maximum input peak level maintained for gain/level setting while active"
        )
        header_row.addWidget(self.peak_hold_label)

        self.clip_badge = QLabel("CLIP")
        self.clip_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clip_badge.setToolTip(
            "Digital Over / Clip indicator (lights red when input reaches -0.1 dBFS). "
            "Reduce preamp gain and click Reset Peak [C]."
        )
        header_row.addWidget(self.clip_badge)

        self.reset_peak_btn = QPushButton("↺ Reset Peak")
        self.reset_peak_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_peak_btn.setToolTip(
            "Reset maintained Peak Hold level and CLIP indicator [Hotkey: C]"
        )
        self.reset_peak_btn.clicked.connect(self._handle_reset_clicked)
        header_row.addWidget(self.reset_peak_btn)

        layout.addLayout(header_row)

        # Graphical VU + Peak Hold Canvas
        self.canvas = _VuMeterCanvas(on_reset_peak=self._handle_reset_clicked, parent=self)
        layout.addWidget(self.canvas)

        self._refresh_readouts(VuMeterReadings(), is_recording=False, has_device=False)

    def _handle_reset_clicked(self) -> None:
        if self._on_reset_peak is not None:
            self._on_reset_peak()

    def set_readings(
        self,
        readings: VuMeterReadings,
        is_recording: bool = False,
        has_device: bool = True,
        is_paused: bool = False,
        is_muted: bool = False,
        is_talkback_override: bool = False,
    ) -> None:
        """Update graphical VU meter bars, Peak Hold marker, and digital dBFS readouts."""
        self._readings = readings
        self._is_recording = bool(is_recording)
        self._has_device = bool(has_device)
        self._is_paused = bool(is_paused)
        self._is_muted = bool(is_muted)
        self._is_talkback_override = bool(is_talkback_override)
        self.canvas.set_state(readings, has_device=has_device, is_muted=is_muted)
        self._refresh_readouts(
            readings,
            is_recording=is_recording,
            has_device=has_device,
            is_paused=is_paused,
            is_muted=is_muted,
            is_talkback_override=is_talkback_override,
        )

    def _refresh_readouts(
        self,
        readings: VuMeterReadings,
        is_recording: bool,
        has_device: bool,
        is_paused: bool = False,
        is_muted: bool = False,
        is_talkback_override: bool = False,
    ) -> None:
        small_px = max(10, int(round(12 * self._scale)))
        btn_pad_v = max(2, int(round(3 * self._scale)))
        btn_pad_h = max(6, int(round(9 * self._scale)))

        # Status title
        if not has_device:
            self.title_label.setText("○ INPUT VU METER (OFFLINE)")
            self.title_label.setStyleSheet(
                f"border: none; color: #6E7681; font-weight: bold; font-size: {small_px}px;"
            )
        elif is_muted:
            self.title_label.setText("✕ INPUT VU METER (MIC MUTED)")
            self.title_label.setStyleSheet(
                f"border: none; color: #FF6B7A; font-weight: bold; font-size: {small_px}px;"
            )
        elif is_recording and is_paused:
            self.title_label.setText(
                "❚❚ INPUT VU METER (RECORDING PAUSED — FILE SAVED & CLOSED)"
            )
            self.title_label.setStyleSheet(
                f"border: none; color: #FFDF5D; font-weight: bold; font-size: {small_px}px;"
            )
        elif is_talkback_override:
            self.title_label.setText("⚡ INPUT VU METER (TALKBACK OVERRIDE)")
            self.title_label.setStyleSheet(
                f"border: none; color: #FFDF5D; font-weight: bold; font-size: {small_px}px;"
            )
        elif is_recording:
            self.title_label.setText("● INPUT VU METER (RECORDING)")
            self.title_label.setStyleSheet(
                f"border: none; color: #FF6B7A; font-weight: bold; font-size: {small_px}px;"
            )
        else:
            self.title_label.setText("● INPUT VU METER (LIVE LEVEL SET)")
            self.title_label.setStyleSheet(
                f"border: none; color: #3FB950; font-weight: bold; font-size: {small_px}px;"
            )

        # Live input dBFS readout
        if not has_device or is_muted or readings.peak_level <= 1e-4:
            self.live_db_label.setText("LIVE: -∞ dBFS")
        else:
            self.live_db_label.setText(f"LIVE: {readings.peak_db:+5.1f} dBFS")
        self.live_db_label.setStyleSheet(
            f"border: none; color: #8B949E; font-family: monospace; font-weight: bold; font-size: {small_px}px;"
        )

        # Maintained Peak Hold readout badge
        if readings.peak_hold_level <= 1e-4:
            hold_txt = "PEAK HOLD: -∞ dBFS"
            hold_fg = "#8B949E"
            hold_bg = "#161B22"
            hold_border = "#30363D"
        else:
            hold_txt = f"PEAK HOLD: {readings.peak_hold_db:+5.1f} dBFS"
            if readings.peak_hold_db >= -3.0:
                hold_fg = "#FFFFFF"
                hold_bg = "#8E1523"
                hold_border = "#F85149"
            elif readings.peak_hold_db >= -18.0:
                hold_fg = "#FFDF5D"
                hold_bg = "#2B2311"
                hold_border = "#D29922"
            else:
                hold_fg = "#58A6FF"
                hold_bg = "#13233A"
                hold_border = "#388BFD"

        self.peak_hold_label.setText(hold_txt)
        self.peak_hold_label.setStyleSheet(
            f"background-color: {hold_bg}; color: {hold_fg}; border: 1px solid {hold_border}; "
            f"border-radius: 4px; padding: {btn_pad_v}px {btn_pad_h}px; "
            f"font-family: monospace; font-weight: bold; font-size: {small_px}px;"
        )

        # CLIP badge
        if readings.clipped:
            self.clip_badge.setText("CLIP!")
            self.clip_badge.setStyleSheet(
                f"background-color: #D72638; color: #FFFFFF; border: 1px solid #FF6B7A; "
                f"border-radius: 4px; padding: {btn_pad_v}px {btn_pad_h}px; "
                f"font-family: monospace; font-weight: bold; font-size: {small_px}px;"
            )
        else:
            self.clip_badge.setText("CLIP")
            self.clip_badge.setStyleSheet(
                f"background-color: #161B22; color: #484F58; border: 1px solid #21262D; "
                f"border-radius: 4px; padding: {btn_pad_v}px {btn_pad_h}px; "
                f"font-family: monospace; font-weight: bold; font-size: {small_px}px;"
            )

        self.reset_peak_btn.setStyleSheet(
            f"QPushButton {{ background-color: #21262D; color: #E6EDF3; border: 1px solid #30363D; "
            f"border-radius: 4px; padding: {btn_pad_v}px {btn_pad_h}px; font-size: {small_px}px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: #30363D; border-color: #58A6FF; }}"
            f"QPushButton:pressed {{ background-color: #161B22; }}"
        )

    def apply_scale(self, scale: float) -> None:
        self._scale = max(0.55, float(scale))
        self.canvas.apply_scale(self._scale)
        self._refresh_readouts(
            self._readings,
            is_recording=self._is_recording,
            has_device=(
                self._has_device
                or self._readings.active
                or self._readings.peak_hold_level > 0.0
            ),
            is_paused=self._is_paused,
            is_muted=self._is_muted,
            is_talkback_override=self._is_talkback_override,
        )

    def value(self) -> int:
        """Return 0..100 meter percentage for backwards compatibility."""
        return int(round(dbfs_to_ratio(self._readings.peak_db) * 100.0))

    def setValue(self, val: int) -> None:
        """Set meter value for backwards compatibility."""
        if val <= 0:
            self.set_readings(VuMeterReadings(), is_recording=False, has_device=True)
