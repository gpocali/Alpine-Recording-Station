"""Storage Capacity & Recording Time Remaining Monitor widget (Circular Ring Gauge + Smooth Capacity Pill + Digital Readout)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QPen,
)
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


class _StorageRingGauge(QWidget):
    """Compact circular donut/arc gauge showing 0..100% drive utilization (visually distinct from horizontal VU bars)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scale: float = 1.0
        self._status = StorageStatus(storage_path=Path("/storage"))
        self.setFixedSize(44, 44)

    def set_status(self, status: StorageStatus) -> None:
        self._status = status
        self.update()

    def apply_scale(self, scale: float) -> None:
        self._scale = max(0.55, float(scale))
        sz = max(34, int(round(44 * self._scale)))
        self.setFixedSize(sz, sz)
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        side = min(w, h)
        if side <= 12:
            painter.end()
            return

        stroke = max(3.5, round(4.8 * self._scale))
        pad = stroke / 2.0 + 2.0
        rect = QRectF(
            (w - side) / 2.0 + pad,
            (h - side) / 2.0 + pad,
            side - 2.0 * pad,
            side - 2.0 * pad,
        )

        # Background circular ring track (270-degree sweep starting at 225 deg)
        start_angle_16 = 225 * 16
        total_span_16 = -270 * 16

        track_pen = QPen(QColor("#21262D"), stroke)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(rect, start_angle_16, total_span_16)

        st = self._status
        pct = min(100.0, max(0.0, float(st.used_percent))) if st.available else 0.0

        if not st.available:
            arc_color = QColor("#484F58")
            text_color = QColor("#6E7681")
        elif st.is_critical:
            arc_color = QColor("#F85149")
            text_color = QColor("#FF6B7A")
        elif st.is_warning:
            arc_color = QColor("#F0B72F")
            text_color = QColor("#FFDF5D")
        else:
            arc_color = QColor("#388BFD")
            text_color = QColor("#E6EDF3")

        if st.available and pct > 0.2:
            fill_span_16 = int(round(total_span_16 * (pct / 100.0)))
            fill_pen = QPen(arc_color, stroke)
            fill_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(fill_pen)
            painter.drawArc(rect, start_angle_16, fill_span_16)

        # Percentage readout centered inside the circular ring
        font_px = max(8, int(round(10 * self._scale)))
        font = QFont("DejaVu Sans Mono", font_px, QFont.Weight.Bold)
        font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(font)
        painter.setPen(text_color)
        pct_text = f"{int(round(pct))}%" if st.available else "--"
        painter.drawText(
            QRectF(0.0, 0.0, w, h),
            int(Qt.AlignmentFlag.AlignCenter),
            pct_text,
        )
        painter.end()


class _StorageCapacityPill(QWidget):
    """Smooth continuous pill-shaped disk capacity gauge with overlaid utilization text (no LED segments or bottom ruler)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scale: float = 1.0
        self._status = StorageStatus(storage_path=Path("/storage"))
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.setMinimumHeight(26)
        self.setToolTip(
            "Persistent /storage Drive Capacity Gauge (0% to 100%).\n"
            "• Blue (0% to 80%): Normal drive utilization\n"
            "• Amber (80% to 90%): Drive getting full — offload or delete old takes soon\n"
            "• Red (90% to 100%): Drive critically full — immediate attention required"
        )

    def set_status(self, status: StorageStatus) -> None:
        self._status = status
        self.update()

    def apply_scale(self, scale: float) -> None:
        self._scale = max(0.55, float(scale))
        self.setMinimumHeight(max(22, int(round(28 * self._scale))))
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        if w <= 20 or h <= 10:
            painter.end()
            return

        radius = min(h / 2.0, max(6.0, round(10.0 * self._scale)))
        pill_rect = QRectF(1.0, 1.0, w - 2.0, h - 2.0)

        # Draw recessed pill capsule background
        painter.setPen(QPen(QColor("#30363D"), 1.0))
        painter.setBrush(QBrush(QColor("#090C10")))
        painter.drawRoundedRect(pill_rect, radius, radius)

        st = self._status
        pct = min(100.0, max(0.0, float(st.used_percent))) if st.available else 0.0
        inner_w = max(0.0, w - 4.0)
        inner_h = max(0.0, h - 4.0)

        # Draw smooth continuous fill bar
        if st.available and pct > 0.2 and inner_w > 4.0:
            fill_w = max(radius * 1.4, inner_w * (pct / 100.0))
            fill_w = min(inner_w, fill_w)
            fill_rect = QRectF(2.0, 2.0, fill_w, inner_h)

            grad = QLinearGradient(fill_rect.left(), 0.0, fill_rect.right(), 0.0)
            if st.is_critical:
                grad.setColorAt(0.0, QColor("#9E1C28"))
                grad.setColorAt(1.0, QColor("#D72638"))
            elif st.is_warning:
                grad.setColorAt(0.0, QColor("#8A5700"))
                grad.setColorAt(1.0, QColor("#D29922"))
            else:
                grad.setColorAt(0.0, QColor("#113D7A"))
                grad.setColorAt(1.0, QColor("#1F6FEB"))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(fill_rect, max(4.0, radius - 1.0), max(4.0, radius - 1.0))

        # Draw subtle 80% (WARN) and 90% (CRIT) threshold notch markers inside pill
        for thresh_pct, notch_col in (
            (STORAGE_WARN_USED_PERCENT, QColor(240, 183, 47, 150)),
            (STORAGE_CRITICAL_USED_PERCENT, QColor(248, 81, 73, 180)),
        ):
            nx = 2.0 + inner_w * (thresh_pct / 100.0)
            painter.setPen(QPen(notch_col, 1.2, Qt.PenStyle.DashLine))
            painter.drawLine(int(nx), 3, int(nx), int(h - 3))

        # Draw crisp capacity summary text centered inside the smooth pill
        font_px = max(9, int(round(11 * self._scale)))
        font = QFont("DejaVu Sans Mono", font_px, QFont.Weight.Bold)
        font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(font)

        if not st.available:
            overlay_text = "STORAGE DRIVE UNAVAILABLE"
        else:
            overlay_text = (
                f"{st.used_percent:4.1f}% USED ({st.formatted_used} / {st.formatted_total})  •  "
                f"{st.formatted_free} FREE"
            )

        painter.setPen(QColor("#F0F6FC"))
        painter.drawText(
            pill_rect,
            int(Qt.AlignmentFlag.AlignCenter),
            overlay_text,
        )
        painter.end()


class StorageMonitorWidget(QFrame):
    """Horizontal Storage Capacity & Recording Time Remaining Telemetry Strip.

    Designed with a circular donut ring gauge, a smooth continuous capacity pill bar,
    and a digital remaining-time counter box so it is visually distinct from the audio VU meter.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scale: float = 1.0
        self._status = StorageStatus(storage_path=Path("/storage"))
        self._is_recording: bool = False
        self._blink_on: bool = True

        self.setObjectName("StorageMonitorFrame")

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(12, 6, 12, 6)
        root_layout.setSpacing(12)

        # 1. Left Zone: Circular Donut Ring Gauge + Drive Title & Status Badge
        self.ring_gauge = _StorageRingGauge(parent=self)
        root_layout.addWidget(self.ring_gauge)

        drive_info_col = QVBoxLayout()
        drive_info_col.setContentsMargins(0, 0, 0, 0)
        drive_info_col.setSpacing(2)

        title_badge_row = QHBoxLayout()
        title_badge_row.setContentsMargins(0, 0, 0, 0)
        title_badge_row.setSpacing(6)

        self.title_label = QLabel("◈ /storage")
        title_badge_row.addWidget(self.title_label)

        self.status_badge = QLabel("SPACE OK")
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_badge_row.addWidget(self.status_badge)
        title_badge_row.addStretch(1)

        drive_info_col.addLayout(title_badge_row)

        self.usage_label = QLabel("FREE: --  •  USED: 0.0%")
        drive_info_col.addWidget(self.usage_label)

        root_layout.addLayout(drive_info_col)

        # 2. Center Zone: Smooth Continuous Capacity Pill Capsule
        self.canvas = _StorageCapacityPill(parent=self)
        root_layout.addWidget(self.canvas, stretch=2)

        # 3. Right Zone: Dedicated Digital Recording Time Remaining Readout Card
        self.time_box = QFrame(self)
        self.time_box.setObjectName("StorageTimeBox")
        time_box_layout = QVBoxLayout(self.time_box)
        time_box_layout.setContentsMargins(10, 3, 10, 3)
        time_box_layout.setSpacing(1)

        self.remaining_label = QLabel("TIME REMAINING: --h --m --s")
        self.remaining_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.remaining_label.setToolTip(
            "Estimated recording time remaining on /storage at the current Bit Depth, "
            "Sample Rate, and Channel/Studio Mode settings"
        )
        time_box_layout.addWidget(self.remaining_label)

        self.rate_label = QLabel("RATE: -- MB/min")
        self.rate_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        time_box_layout.addWidget(self.rate_label)

        root_layout.addWidget(self.time_box)

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
        """Update circular ring gauge, smooth capacity pill, and remaining recording time readout."""
        self._status = status
        self._is_recording = bool(is_recording)
        self._blink_on = bool(blink_on)
        self.ring_gauge.set_status(status)
        self.canvas.set_status(status)
        self._refresh_readouts()

    def apply_scale(self, scale: float) -> None:
        self._scale = max(0.55, float(scale))
        self.ring_gauge.apply_scale(self._scale)
        self.canvas.apply_scale(self._scale)
        self._refresh_readouts()

    def _refresh_readouts(self) -> None:
        st = self._status
        small_px = max(10, int(round(12 * self._scale)))
        tiny_px = max(9, int(round(11 * self._scale)))
        rem_px = max(11, int(round(14 * self._scale)))
        badge_pad_v = max(1, int(round(2 * self._scale)))
        badge_pad_h = max(6, int(round(8 * self._scale)))

        ch_desc = (
            "Studio Stereo (2ch)"
            if st.studio_mode
            else ("Stereo (2ch)" if st.effective_channels >= 2 else "Mono (1ch)")
        )
        drive_desc = st.drive_summary

        # Frame styling & title based on utilization level
        if not st.available or st.level == "offline":
            frame_bg = "#141920"
            frame_border = "1px solid #30363D"
            title_txt = f"○ STORAGE OFFLINE ({st.storage_path})"
            title_col = "#6E7681"
        elif st.level == "full":
            frame_bg = "#240D11" if self._blink_on else "#190A0D"
            frame_border = "2px solid #FF6B7A"
            title_txt = f"⚠ DRIVE FULL — {drive_desc}"
            title_col = "#FF6B7A"
        elif st.level == "critical":
            frame_bg = "#200E12" if self._blink_on else "#160B0E"
            border_c = "#FF6B7A" if (self._is_recording and self._blink_on) else "#F85149"
            frame_border = f"2px solid {border_c}"
            title_txt = f"⚠ DRIVE CRITICALLY FULL — {drive_desc}"
            title_col = "#FF6B7A"
        elif st.level == "warning":
            frame_bg = "#19150B"
            frame_border = "2px solid #D29922"
            title_txt = f"⚠ DRIVE GETTING FULL — {drive_desc}"
            title_col = "#FFDF5D"
        else:
            frame_bg = "#141920"
            frame_border = "1px solid #263040"
            title_txt = f"◈ {drive_desc}"
            title_col = "#58A6FF"

        self.setStyleSheet(
            f"QFrame#StorageMonitorFrame {{ background-color: {frame_bg}; border: {frame_border}; border-radius: 10px; }}"
        )

        self.title_label.setText(title_txt)
        self.title_label.setStyleSheet(
            f"border: none; color: {title_col}; font-weight: bold; font-size: {small_px}px;"
        )

        # Sub-caption under title
        if not st.available:
            self.usage_label.setText("DRIVE UNAVAILABLE")
            usage_col = "#6E7681"
        elif st.is_temporary_fs:
            self.usage_label.setText(
                f"⚠ UNMOUNTED (RAM TMPFS): {st.formatted_free} free of {st.formatted_total}"
            )
            usage_col = "#FFDF5D"
        else:
            self.usage_label.setText(
                f"FREE: {st.formatted_free} of {st.formatted_total} ({st.used_percent:.1f}% used)"
            )
            if st.is_critical:
                usage_col = "#FF6B7A"
            elif st.is_warning:
                usage_col = "#FFDF5D"
            else:
                usage_col = "#8B949E"

        self.usage_label.setStyleSheet(
            f"border: none; color: {usage_col}; font-family: monospace; font-size: {tiny_px}px;"
        )

        # Status Alert Pill Badge
        if not st.available or st.level == "offline":
            badge_txt = "OFFLINE"
            b_fg, b_bg, b_border = "#6E7681", "#161B22", "#30363D"
        elif st.level == "full":
            badge_txt = "⚠ DRIVE FULL"
            b_fg, b_bg, b_border = "#FFFFFF", "#D72638", "#FF6B7A"
        elif st.level == "critical":
            badge_txt = "⚠ CRITICAL"
            b_fg, b_bg, b_border = "#FFFFFF", "#D72638", "#FF6B7A"
        elif st.level == "warning":
            badge_txt = "⚠ LOW SPACE"
            b_fg, b_bg, b_border = "#FFFFFF", "#9E6A03", "#FFDF5D"
        else:
            badge_txt = "SPACE OK"
            b_fg, b_bg, b_border = "#58A6FF", "#13233A", "#388BFD"

        self.status_badge.setText(badge_txt)
        self.status_badge.setStyleSheet(
            f"background-color: {b_bg}; color: {b_fg}; border: 1px solid {b_border}; "
            f"border-radius: 4px; padding: {badge_pad_v}px {badge_pad_h}px; "
            f"font-family: monospace; font-weight: bold; font-size: {tiny_px}px;"
        )

        # Right Digital Time Remaining Box
        if not st.available:
            rem_txt = "TIME REMAINING: --h --m --s"
            rem_fg = "#8B949E"
            box_bg = "#0D1014"
            box_border = "#30363D"
        else:
            rem_txt = f"TIME REMAINING: {st.formatted_remaining}"
            if st.is_critical:
                rem_fg = "#FFFFFF"
                box_bg = "#8E1523"
                box_border = "#FF6B7A"
            elif st.is_warning:
                rem_fg = "#FFDF5D"
                box_bg = "#3B2300"
                box_border = "#F0B72F"
            else:
                rem_fg = "#F0F6FC"
                box_bg = "#0D1117"
                box_border = "#388BFD"

        self.time_box.setStyleSheet(
            f"QFrame#StorageTimeBox {{ background-color: {box_bg}; border: 1px solid {box_border}; border-radius: 6px; }}"
        )
        self.remaining_label.setText(rem_txt)
        self.remaining_label.setStyleSheet(
            f"border: none; background: transparent; color: {rem_fg}; "
            f"font-family: monospace; font-weight: bold; font-size: {rem_px}px;"
        )

        rate_col = "#FFDF5D" if st.is_critical else "#8B949E"
        self.rate_label.setText(
            f"{st.mb_per_minute:.1f} MB/min  •  {st.bit_depth}-bit / {st.sample_rate:,} Hz / {ch_desc}"
        )
        self.rate_label.setStyleSheet(
            f"border: none; background: transparent; color: {rate_col}; "
            f"font-family: monospace; font-size: {tiny_px}px;"
        )
