"""Talent Teleprompter popup window with compact studio controls, rich text, manual/autoscroll, and Whisper AI tracking."""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PyQt6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QPainter,
    QPen,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.audio_engine import AudioEngine
from app.config_manager import ConfigManager, sanitize_project_name
from app.prompter_engine import (
    DEFAULT_AUTOSCROLL_SPEED,
    DEFAULT_LOOKAHEAD_WORDS,
    DEFAULT_PROMPTER_FONT_SIZE,
    MAX_AUTOSCROLL_SPEED,
    MAX_PROMPTER_FONT_SIZE,
    MIN_AUTOSCROLL_SPEED,
    MIN_PROMPTER_FONT_SIZE,
    PrompterTracker,
    ScriptAnchor,
    WhisperSpeechWorker,
    sanitize_script_html,
)
from app.ui.styles import (
    build_main_stylesheet,
    build_pause_record_button_style,
    build_record_button_style,
    build_talkback_button_style,
    compute_scale_factor,
)

logger = logging.getLogger("AlpineRecorder.PrompterDialog")


class _MiniVuBar(QWidget):
    """Compact horizontal level indicator for the Prompter status bar."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._peak_db: float = -60.0
        self._rms_db: float = -60.0
        self._muted: bool = False
        self.setFixedSize(110, 18)

    def set_levels(self, peak_db: float, rms_db: float, muted: bool = False) -> None:
        self._peak_db = max(-60.0, min(0.0, float(peak_db)))
        self._rms_db = max(-60.0, min(0.0, float(rms_db)))
        self._muted = bool(muted)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        r = self.rect().adjusted(0, 2, 0, -2)
        p.fillRect(r, QColor("#0D1014"))
        p.setPen(QPen(QColor("#30363D"), 1))
        p.drawRect(r.adjusted(0, 0, -1, -1))

        if not self._muted:
            norm = (self._peak_db + 60.0) / 60.0
            fill_w = max(0, int(round((r.width() - 2) * norm)))
            if fill_w > 0:
                if self._peak_db >= -6.0:
                    col = QColor("#F85149")
                elif self._peak_db >= -18.0:
                    col = QColor("#F0B72F")
                else:
                    col = QColor("#3FB950")
                p.fillRect(r.left() + 1, r.top() + 1, fill_w, r.height() - 2, col)
        else:
            p.setPen(QColor("#FFA198"))
            f = p.font()
            f.setPointSize(8)
            f.setBold(True)
            p.setFont(f)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "MUTED")
        p.end()


class _PrompterTextView(QTextEdit):
    """Rich-text teleprompter display with reading guide band, wheel routing, and click-to-anchor."""

    wheel_delta = pyqtSignal(int, bool)  # (angle_y, ctrl_held)
    word_clicked = pyqtSignal(int)       # char_offset

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._edit_mode = False
        self._guide_ratio = 0.28

    def set_edit_mode(self, editing: bool) -> None:
        self._edit_mode = bool(editing)
        self.setReadOnly(not self._edit_mode)
        self.viewport().update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._edit_mode:
            super().wheelEvent(event)
            return
        delta_y = int(event.angleDelta().y())
        ctrl_held = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if delta_y != 0:
            self.wheel_delta.emit(delta_y, ctrl_held)
            event.accept()
            return
        super().wheelEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if not self._edit_mode and event.button() == Qt.MouseButton.LeftButton:
            cursor = self.cursorForPosition(event.position().toPoint())
            self.word_clicked.emit(int(cursor.position()))

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._edit_mode:
            return
        # Subtle reading guide marker on left/right edges at the reading line
        vp = self.viewport()
        guide_y = int(vp.height() * self._guide_ratio)
        p = QPainter(vp)
        p.setPen(QPen(QColor(88, 166, 255, 75), 1, Qt.PenStyle.DashLine))
        p.drawLine(0, guide_y, vp.width(), guide_y)
        p.setBrush(QColor("#58A6FF"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(0, guide_y - 10, 6, 20)
        p.drawRect(vp.width() - 6, guide_y - 10, 6, 20)
        p.end()


class PrompterDialog(QDialog):
    """Full-featured talent Teleprompter popup window tied to the Studio Deck & Remote Producer."""

    _ai_advance_signal = pyqtSignal(int, str)
    _ai_status_signal = pyqtSignal(str, str)
    _external_script_signal = pyqtSignal(str, str)  # (project_name, html_content)

    def __init__(
        self,
        config_manager: ConfigManager,
        audio_engine: AudioEngine,
        on_record_clicked: Callable[[], None],
        on_pause_clicked: Callable[[], None],
        on_talkback_pressed: Callable[[], None],
        on_talkback_released: Callable[[], None],
        on_cough_pressed: Callable[[], None],
        on_cough_released: Callable[[], None],
        on_script_saved_locally: Optional[Callable[[str, str], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.audio_engine = audio_engine
        self._on_record_clicked = on_record_clicked
        self._on_pause_clicked = on_pause_clicked
        self._on_talkback_pressed = on_talkback_pressed
        self._on_talkback_released = on_talkback_released
        self._on_cough_pressed = on_cough_pressed
        self._on_cough_released = on_cough_released
        self._on_script_saved_locally = on_script_saved_locally

        self.setWindowTitle("Studio Script Prompter")
        self.setModal(False)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        if parent is not None and parent.width() >= 640:
            pw, ph = parent.width(), parent.height()
            self._scale = compute_scale_factor(pw, ph)
            self.resize(max(780, int(pw * 0.96)), max(500, int(ph * 0.94)))
        else:
            self._scale = 1.0
            self.resize(1100, 720)

        s = self.config_manager.settings
        self._font_size: int = max(
            MIN_PROMPTER_FONT_SIZE,
            min(MAX_PROMPTER_FONT_SIZE, int(getattr(s, "prompter_font_size", DEFAULT_PROMPTER_FONT_SIZE) or DEFAULT_PROMPTER_FONT_SIZE)),
        )
        self._scroll_mode: str = (
            "autoscroll"
            if str(getattr(s, "prompter_scroll_mode", "direct") or "direct").lower() == "autoscroll"
            else "direct"
        )
        self._autoscroll_speed: float = max(
            MIN_AUTOSCROLL_SPEED,
            min(MAX_AUTOSCROLL_SPEED, float(getattr(s, "prompter_autoscroll_speed", DEFAULT_AUTOSCROLL_SPEED) or DEFAULT_AUTOSCROLL_SPEED)),
        )
        self._autoscroll_running: bool = False
        self._scroll_accum_px: float = 0.0
        self._ai_enabled: bool = bool(getattr(s, "prompter_ai_enabled", False))
        self._lookahead_words: int = int(
            getattr(s, "prompter_lookahead_words", DEFAULT_LOOKAHEAD_WORDS) or DEFAULT_LOOKAHEAD_WORDS
        )
        self._active_project: str = sanitize_project_name(s.project_name)
        self._current_html: str = ""
        self._is_editing_local: bool = False
        self._pre_edit_anchor: Optional[ScriptAnchor] = None
        self._talkback_key_held: bool = False
        self._cough_key_held: bool = False
        self._last_scroll_tick_mono: float = time.monotonic()

        self.tracker = PrompterTracker(lookahead_words=self._lookahead_words)
        self.whisper_worker = WhisperSpeechWorker(
            tracker=self.tracker,
            on_advance=self._emit_ai_advance,
            on_status=self._emit_ai_status,
            model_name=str(getattr(s, "prompter_whisper_model", "tiny.en") or "tiny.en"),
            custom_binary=str(getattr(s, "prompter_whisper_binary", "") or ""),
            storage_dir=self.config_manager.storage_dir,
        )

        self._ai_advance_signal.connect(self._on_ai_advanced)
        self._ai_status_signal.connect(self._on_ai_status_changed)
        self._external_script_signal.connect(self._apply_external_script_update)

        self._build_ui()
        self.setStyleSheet(build_main_stylesheet(self._scale))
        self._apply_font_size()
        self._update_mode_controls()

        self.load_script_for_project(self._active_project, preserve_position=False)

        # Connect live audio for Whisper AI
        self.audio_engine.register_live_audio_listener(self.whisper_worker.push_audio)
        if self._ai_enabled:
            self._start_ai_worker()

        # 50ms UI sync timer (status badges, buttons, VU) + 33ms smooth autoscroll timer
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(50)
        self._status_timer.timeout.connect(self._sync_studio_status)
        self._status_timer.start()

        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(33)
        self._scroll_timer.timeout.connect(self._on_autoscroll_tick)
        self._scroll_timer.start()

        self._sync_studio_status()

    # ------------------------------------------------------------------
    # UI Construction (Compact Status & Controls + Maximum Script Area)
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # 1. Compact Critical Studio Status & Transport Bar
        top_bar = QFrame()
        top_bar.setStyleSheet(
            "QFrame { background-color: #161B22; border: 1px solid #30363D; border-radius: 8px; }"
        )
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 6, 10, 6)
        top_layout.setSpacing(8)

        self.state_pill = QLabel("● STANDBY")
        self.state_pill.setStyleSheet(
            "border: 1px solid #30363D; border-radius: 5px; padding: 4px 10px; "
            "background-color: #0D1014; color: #8B949E; font-weight: 700; font-size: 12px;"
        )
        top_layout.addWidget(self.state_pill)

        self.timer_label = QLabel("00:00:00.0")
        self.timer_label.setStyleSheet(
            "border: none; font-family: 'DejaVu Sans Mono', monospace; "
            "font-size: 16px; font-weight: 700; color: #F0F6FC; padding: 0 4px;"
        )
        top_layout.addWidget(self.timer_label)

        self.mini_vu = _MiniVuBar(top_bar)
        top_layout.addWidget(self.mini_vu)

        self.project_badge = QLabel(f"Project: {self._active_project}")
        self.project_badge.setStyleSheet(
            "border: 1px solid #262C36; border-radius: 5px; padding: 3px 8px; "
            "background-color: #0D1014; color: #79C0FF; font-size: 11px; font-weight: 600;"
        )
        top_layout.addWidget(self.project_badge)

        top_layout.addStretch(1)

        # Smaller versions of critical studio transport buttons
        btn_h = max(32, int(round(36 * self._scale)))

        self.record_btn = QPushButton("● Record")
        self.record_btn.setFixedHeight(btn_h)
        self.record_btn.setMinimumWidth(96)
        self.record_btn.setToolTip("Start / Stop Recording [R]")
        self.record_btn.clicked.connect(self._on_record_clicked)
        top_layout.addWidget(self.record_btn)

        self.pause_btn = QPushButton("❚❚ Pause")
        self.pause_btn.setFixedHeight(btn_h)
        self.pause_btn.setMinimumWidth(86)
        self.pause_btn.setToolTip("Pause / Resume Recording [P]")
        self.pause_btn.clicked.connect(self._on_pause_clicked)
        top_layout.addWidget(self.pause_btn)

        self.talkback_btn = QPushButton("◉ Talkback")
        self.talkback_btn.setFixedHeight(btn_h)
        self.talkback_btn.setMinimumWidth(100)
        self.talkback_btn.setToolTip("Talkback: Click/Latch when idle, Hold PTT when recording [T]")
        self.talkback_btn.pressed.connect(self._on_talkback_pressed)
        self.talkback_btn.released.connect(self._on_talkback_released)
        top_layout.addWidget(self.talkback_btn)

        self.cough_btn = QPushButton("◉ Cough")
        self.cough_btn.setFixedHeight(btn_h)
        self.cough_btn.setMinimumWidth(88)
        self.cough_btn.setToolTip("Hold [B] for Cough Mute, or click to toggle Mute")
        self.cough_btn.pressed.connect(self._on_cough_pressed)
        self.cough_btn.released.connect(self._on_cough_released)
        top_layout.addWidget(self.cough_btn)

        self.close_btn = QPushButton("✕ Close")
        self.close_btn.setFixedHeight(btn_h)
        self.close_btn.setToolTip("Close Prompter and return to Studio Deck [Esc]")
        self.close_btn.clicked.connect(self.close)
        top_layout.addWidget(self.close_btn)

        root.addWidget(top_bar, stretch=0)

        # 2. Main Rich-Text Script Display (Devotes Majority of Space to Text)
        self.script_view = _PrompterTextView(self)
        self.script_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.script_view.setStyleSheet(
            "QTextEdit { background-color: #090C10; color: #F0F6FC; "
            "border: 1px solid #262C36; border-radius: 10px; padding: 24px 44px; "
            "selection-background-color: #1F6FEB; selection-color: #FFFFFF; }"
        )
        self.script_view.wheel_delta.connect(self._on_script_wheel)
        self.script_view.word_clicked.connect(self._on_script_word_clicked)
        root.addWidget(self.script_view, stretch=1)

        # 3. Slim Prompter Controls Ribbon (Text Size, Direct/Autoscroll, Speed, Whisper AI, Edit)
        ribbon = QFrame()
        ribbon.setStyleSheet(
            "QFrame { background-color: #161B22; border: 1px solid #30363D; border-radius: 8px; }"
        )
        rib_layout = QHBoxLayout(ribbon)
        rib_layout.setContentsMargins(10, 5, 10, 5)
        rib_layout.setSpacing(8)

        # Font Size Controls
        self.btn_font_down = QPushButton("A−")
        self.btn_font_down.setFixedSize(36, 30)
        self.btn_font_down.setToolTip("Decrease text size [-]")
        self.btn_font_down.clicked.connect(lambda: self.adjust_font_size(-3))
        rib_layout.addWidget(self.btn_font_down)

        self.lbl_font_size = QLabel(f"{self._font_size}pt")
        self.lbl_font_size.setStyleSheet(
            "border: none; color: #C9D1D9; font-family: monospace; font-weight: 700; min-width: 38px;"
        )
        self.lbl_font_size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rib_layout.addWidget(self.lbl_font_size)

        self.btn_font_up = QPushButton("A+")
        self.btn_font_up.setFixedSize(36, 30)
        self.btn_font_up.setToolTip("Increase text size [+]")
        self.btn_font_up.clicked.connect(lambda: self.adjust_font_size(3))
        rib_layout.addWidget(self.btn_font_up)

        # Mode Switch: Direct Navigation vs Autoscroll
        self.btn_scroll_mode = QPushButton("Mode: Direct")
        self.btn_scroll_mode.setFixedHeight(30)
        self.btn_scroll_mode.setToolTip(
            "Switch between Direct Navigation (keys/wheel scroll directly) and Autoscroll (keys/wheel adjust speed) [A]"
        )
        self.btn_scroll_mode.clicked.connect(self._toggle_scroll_mode)
        rib_layout.addWidget(self.btn_scroll_mode)

        self.btn_autoscroll_play = QPushButton("▶ Scroll")
        self.btn_autoscroll_play.setFixedHeight(30)
        self.btn_autoscroll_play.setToolTip("Start / Pause Autoscroll [G]")
        self.btn_autoscroll_play.clicked.connect(self._toggle_autoscroll_running)
        rib_layout.addWidget(self.btn_autoscroll_play)

        self.btn_speed_down = QPushButton("−")
        self.btn_speed_down.setFixedSize(28, 30)
        self.btn_speed_down.setToolTip("Slower autoscroll [Up in Autoscroll mode]")
        self.btn_speed_down.clicked.connect(lambda: self.adjust_autoscroll_speed(-10.0))
        rib_layout.addWidget(self.btn_speed_down)

        self.lbl_speed = QLabel(f"{int(round(self._autoscroll_speed))} px/s")
        self.lbl_speed.setStyleSheet(
            "border: none; color: #79C0FF; font-family: monospace; font-weight: 700; min-width: 58px;"
        )
        self.lbl_speed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rib_layout.addWidget(self.lbl_speed)

        self.btn_speed_up = QPushButton("+")
        self.btn_speed_up.setFixedSize(28, 30)
        self.btn_speed_up.setToolTip("Faster autoscroll [Down in Autoscroll mode]")
        self.btn_speed_up.clicked.connect(lambda: self.adjust_autoscroll_speed(10.0))
        rib_layout.addWidget(self.btn_speed_up)

        self.btn_top = QPushButton("↺ Top")
        self.btn_top.setFixedHeight(30)
        self.btn_top.setToolTip("Jump to top of script [Home]")
        self.btn_top.clicked.connect(self.scroll_to_top)
        rib_layout.addWidget(self.btn_top)

        rib_layout.addStretch(1)

        # Whisper AI Toggle & Status
        self.btn_ai_toggle = QPushButton("◎ AI Follow: OFF")
        self.btn_ai_toggle.setFixedHeight(30)
        self.btn_ai_toggle.setToolTip(
            "Toggle Whisper AI speech-to-text auto-scroll (excludes (...), [...], {...} directions)"
        )
        self.btn_ai_toggle.clicked.connect(self._toggle_ai_follow)
        rib_layout.addWidget(self.btn_ai_toggle)

        self.lbl_ai_status = QLabel(f"LA: {self._lookahead_words}w")
        self.lbl_ai_status.setStyleSheet(
            "border: none; color: #8B949E; font-size: 11px; font-weight: 600;"
        )
        rib_layout.addWidget(self.lbl_ai_status)

        # Local Edit / Save
        self.btn_edit_local = QPushButton("✎ Edit")
        self.btn_edit_local.setFixedHeight(30)
        self.btn_edit_local.setToolTip("Edit script locally without losing reading position")
        self.btn_edit_local.clicked.connect(self._toggle_local_edit)
        rib_layout.addWidget(self.btn_edit_local)

        self.btn_cancel_edit = QPushButton("Cancel")
        self.btn_cancel_edit.setFixedHeight(30)
        self.btn_cancel_edit.setVisible(False)
        self.btn_cancel_edit.clicked.connect(self._cancel_local_edit)
        rib_layout.addWidget(self.btn_cancel_edit)

        root.addWidget(ribbon, stretch=0)

    # ------------------------------------------------------------------
    # Script Loading, Position-Preserving Live Updates & Styling
    # ------------------------------------------------------------------
    def _current_scroll_ratio(self) -> float:
        sb = self.script_view.verticalScrollBar()
        if sb is None or sb.maximum() <= 0:
            return 0.0
        return max(0.0, min(1.0, float(sb.value()) / float(sb.maximum())))

    def load_script_for_project(
        self, project_name: str, preserve_position: bool = False
    ) -> None:
        clean_proj = sanitize_project_name(project_name)
        self._active_project = clean_proj
        self.project_badge.setText(f"Project: {clean_proj}")
        html_content, _mtime = self.config_manager.load_project_script(clean_proj)
        self.apply_script_html(html_content, preserve_position=preserve_position)

    def notify_external_script_update_threadsafe(
        self, project_name: str, html_content: str
    ) -> None:
        """Called from RemoteProducerServer thread when producer saves a script."""
        self._external_script_signal.emit(str(project_name or ""), str(html_content or ""))

    def _apply_external_script_update(
        self, project_name: str, html_content: str
    ) -> None:
        clean_proj = sanitize_project_name(project_name)
        if clean_proj != self._active_project:
            return
        if self._is_editing_local:
            return
        self.apply_script_html(html_content, preserve_position=True)

    def apply_script_html(
        self, html_content: str, preserve_position: bool = True
    ) -> None:
        """Render rich-text script HTML while preserving reading & scroll position if requested."""
        sanitized = sanitize_script_html(html_content)
        self._current_html = sanitized
        anchor = (
            self.tracker.capture_anchor(scroll_ratio=self._current_scroll_ratio())
            if preserve_position
            else None
        )

        sb = self.script_view.verticalScrollBar()
        old_scroll_val = sb.value() if sb is not None else 0

        if sanitized.strip():
            self.script_view.setHtml(sanitized)
        else:
            self.script_view.setHtml(
                "<p style='color:#6E7681; font-style:italic;'>"
                "No script loaded for this project yet. Click '✎ Edit' below or update the script "
                "from the Remote Producer Web UI (saved to /storage/"
                f"{self._active_project}/script.html).</p>"
            )

        self._apply_font_size()
        plain = self.script_view.toPlainText()

        if preserve_position and anchor is not None:
            new_idx, fallback_ratio = self.tracker.update_text_preserving_position(
                plain, anchor=anchor
            )
            self._apply_script_decorations()
            if self.tracker.total_tokens > 0:
                self._scroll_to_token(new_idx, instant=True)
            elif sb is not None and sb.maximum() > 0:
                sb.setValue(int(round(fallback_ratio * sb.maximum())))
            elif sb is not None:
                sb.setValue(min(old_scroll_val, sb.maximum()))
        else:
            self.tracker.set_text(plain, reset_cursor=True)
            self._apply_script_decorations()
            if sb is not None:
                sb.setValue(0)

    def _apply_font_size(self) -> None:
        font = QFont("DejaVu Sans", self._font_size)
        self.script_view.setFont(font)
        self.script_view.document().setDefaultFont(font)
        self.lbl_font_size.setText(f"{self._font_size}pt")

    def _apply_script_decorations(self) -> None:
        """Highlight non-spoken (...), [...], {...} cues in muted italic and mark active AI word."""
        if self._is_editing_local:
            self.script_view.setExtraSelections([])
            return

        selections = []
        doc = self.script_view.document()

        # 1. Style non-spoken script instructions in muted cyan-grey italic
        cue_fmt = QTextCharFormat()
        cue_fmt.setForeground(QColor("#79C0FF"))
        cue_fmt.setFontItalic(True)
        cue_fmt.setBackground(QColor("#111B27"))

        for start, end in self.tracker.non_spoken_spans:
            sel = QTextEdit.ExtraSelection()
            cur = QTextCursor(doc)
            cur.setPosition(max(0, start))
            cur.setPosition(min(doc.characterCount() - 1, end), QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = cur
            sel.format = cue_fmt
            selections.append(sel)

        # 2. Highlight current active spoken word when AI follow is active or cursor > 0
        tok = self.tracker.get_current_token()
        if tok is not None and (self._ai_enabled or tok.index > 0):
            active_fmt = QTextCharFormat()
            active_fmt.setBackground(QColor("#1F6FEB"))
            active_fmt.setForeground(QColor("#FFFFFF"))
            sel = QTextEdit.ExtraSelection()
            cur = QTextCursor(doc)
            cur.setPosition(max(0, tok.char_start))
            cur.setPosition(
                min(doc.characterCount() - 1, tok.char_end),
                QTextCursor.MoveMode.KeepAnchor,
            )
            sel.cursor = cur
            sel.format = active_fmt
            selections.append(sel)

        self.script_view.setExtraSelections(selections)

    def _scroll_to_token(self, token_index: int, instant: bool = False) -> None:
        tok = self.tracker.get_current_token()
        if tok is None or tok.index != token_index:
            tokens = self.tracker.tokens
            if not tokens or token_index >= len(tokens):
                return
            tok = tokens[max(0, token_index)]

        doc = self.script_view.document()
        cur = QTextCursor(doc)
        cur.setPosition(min(max(0, tok.char_start), max(0, doc.characterCount() - 1)))
        rect = self.script_view.cursorRect(cur)
        sb = self.script_view.verticalScrollBar()
        if sb is None:
            return
        guide_y = int(self.script_view.viewport().height() * 0.28)
        delta = rect.top() - guide_y
        target = max(0, min(sb.maximum(), sb.value() + delta))
        if instant or abs(delta) > 220:
            sb.setValue(target)
        else:
            # Smooth step toward reading guide
            sb.setValue(sb.value() + int(round(delta * 0.45)))

    # ------------------------------------------------------------------
    # Font Size, Direct Navigation & Autoscroll Controls
    # ------------------------------------------------------------------
    def adjust_font_size(self, delta: int) -> None:
        anchor = self.tracker.capture_anchor(scroll_ratio=self._current_scroll_ratio())
        self._font_size = max(
            MIN_PROMPTER_FONT_SIZE,
            min(MAX_PROMPTER_FONT_SIZE, self._font_size + int(delta)),
        )
        self.config_manager.settings.prompter_font_size = self._font_size
        self.config_manager.save(commit_lbu=False)
        self._apply_font_size()
        self._scroll_to_token(anchor.token_index, instant=True)

    def adjust_autoscroll_speed(self, delta: float) -> None:
        self._autoscroll_speed = max(
            MIN_AUTOSCROLL_SPEED,
            min(MAX_AUTOSCROLL_SPEED, self._autoscroll_speed + float(delta)),
        )
        self.config_manager.settings.prompter_autoscroll_speed = float(self._autoscroll_speed)
        self.config_manager.save(commit_lbu=False)
        self._update_mode_controls()

    def _toggle_scroll_mode(self) -> None:
        if self._scroll_mode == "direct":
            self._scroll_mode = "autoscroll"
        else:
            self._scroll_mode = "direct"
            self._autoscroll_running = False
        self.config_manager.settings.prompter_scroll_mode = self._scroll_mode
        self.config_manager.save(commit_lbu=False)
        self._update_mode_controls()

    def _toggle_autoscroll_running(self) -> None:
        if self._scroll_mode != "autoscroll":
            self._scroll_mode = "autoscroll"
            self.config_manager.settings.prompter_scroll_mode = "autoscroll"
            self.config_manager.save(commit_lbu=False)
        self._autoscroll_running = not self._autoscroll_running
        self._last_scroll_tick_mono = time.monotonic()
        self._scroll_accum_px = 0.0
        self._update_mode_controls()

    def _update_mode_controls(self) -> None:
        is_auto = self._scroll_mode == "autoscroll"
        self.btn_scroll_mode.setText("Mode: Auto-Scroll" if is_auto else "Mode: Direct")
        if is_auto:
            self.btn_scroll_mode.setStyleSheet(
                "background-color: #162436; color: #79C0FF; border: 1px solid #388BFD; font-weight: 700;"
            )
        else:
            self.btn_scroll_mode.setStyleSheet("")

        self.btn_autoscroll_play.setVisible(is_auto)
        self.btn_speed_down.setVisible(is_auto)
        self.lbl_speed.setVisible(is_auto)
        self.btn_speed_up.setVisible(is_auto)
        self.lbl_speed.setText(f"{int(round(self._autoscroll_speed))} px/s")

        if self._autoscroll_running:
            self.btn_autoscroll_play.setText("❚❚ Pause Scroll")
            self.btn_autoscroll_play.setStyleSheet(
                "background-color: #0F2D1E; color: #7EE787; border: 1px solid #2EA043; font-weight: 700;"
            )
        else:
            self.btn_autoscroll_play.setText("▶ Start Scroll")
            self.btn_autoscroll_play.setStyleSheet("")

        # AI button styling
        if self._ai_enabled:
            self.btn_ai_toggle.setText("◎ AI Follow: ON")
            self.btn_ai_toggle.setStyleSheet(
                "background-color: #0F2D1E; color: #7EE787; border: 1px solid #2EA043; font-weight: 700;"
            )
        else:
            self.btn_ai_toggle.setText("○ AI Follow: OFF")
            self.btn_ai_toggle.setStyleSheet("")

    def scroll_direct_by(self, pixels: int) -> None:
        sb = self.script_view.verticalScrollBar()
        if sb is None:
            return
        sb.setValue(max(0, min(sb.maximum(), sb.value() + int(pixels))))

    def scroll_to_top(self) -> None:
        self._autoscroll_running = False
        self.tracker.set_cursor(0)
        sb = self.script_view.verticalScrollBar()
        if sb is not None:
            sb.setValue(0)
        self._apply_script_decorations()
        self._update_mode_controls()

    def _on_autoscroll_tick(self) -> None:
        now = time.monotonic()
        dt = max(0.0, min(0.25, now - self._last_scroll_tick_mono))
        self._last_scroll_tick_mono = now
        if not self._autoscroll_running or self._is_editing_local:
            return
        sb = self.script_view.verticalScrollBar()
        if sb is None or sb.maximum() <= 0:
            return
        self._scroll_accum_px += self._autoscroll_speed * dt
        step = int(self._scroll_accum_px)
        if step >= 1:
            self._scroll_accum_px -= step
            nxt = min(sb.maximum(), sb.value() + step)
            sb.setValue(nxt)
            if nxt >= sb.maximum():
                self._autoscroll_running = False
                self._update_mode_controls()

    def _on_script_wheel(self, delta_y: int, ctrl_held: bool) -> None:
        if ctrl_held:
            self.adjust_font_size(2 if delta_y > 0 else -2)
            return
        if self._scroll_mode == "autoscroll":
            # In Autoscroll mode, wheel influences scroll speed (wheel down = faster, wheel up = slower)
            self.adjust_autoscroll_speed(8.0 if delta_y < 0 else -8.0)
        else:
            # Direct navigation mode
            step = max(32, int(self._font_size * 1.8))
            self.scroll_direct_by(-step if delta_y > 0 else step)

    def _on_script_word_clicked(self, char_offset: int) -> None:
        idx = self.tracker.set_cursor_by_char_offset(char_offset)
        self._apply_script_decorations()
        self._scroll_to_token(idx, instant=False)

    # ------------------------------------------------------------------
    # Whisper AI Voice Tracking
    # ------------------------------------------------------------------
    def reload_prompter_settings(self) -> None:
        s = self.config_manager.settings
        self._lookahead_words = int(
            getattr(s, "prompter_lookahead_words", DEFAULT_LOOKAHEAD_WORDS) or DEFAULT_LOOKAHEAD_WORDS
        )
        self.tracker.lookahead_words = self._lookahead_words
        self.whisper_worker.model_name = str(
            getattr(s, "prompter_whisper_model", "tiny.en") or "tiny.en"
        )
        self.whisper_worker.custom_binary = str(
            getattr(s, "prompter_whisper_binary", "") or ""
        )
        want_ai = bool(getattr(s, "prompter_ai_enabled", False))
        if want_ai != self._ai_enabled:
            self._ai_enabled = want_ai
            if want_ai:
                self._start_ai_worker()
            else:
                self.whisper_worker.stop()
        self.lbl_ai_status.setText(f"LA: {self._lookahead_words}w")
        self._update_mode_controls()

    def _start_ai_worker(self) -> None:
        ok = self.whisper_worker.start()
        if not ok:
            code, msg = self.whisper_worker.status
            self._on_ai_status_changed(code, msg)

    def _toggle_ai_follow(self) -> None:
        self._ai_enabled = not self._ai_enabled
        self.config_manager.settings.prompter_ai_enabled = self._ai_enabled
        self.config_manager.save(commit_lbu=False)
        if self._ai_enabled:
            self._start_ai_worker()
        else:
            self.whisper_worker.stop()
        self._apply_script_decorations()
        self._update_mode_controls()

    def _emit_ai_advance(self, new_idx: int, transcript: str) -> None:
        self._ai_advance_signal.emit(int(new_idx), str(transcript or ""))

    def _emit_ai_status(self, code: str, message: str) -> None:
        self._ai_status_signal.emit(str(code or ""), str(message or ""))

    def _on_ai_advanced(self, new_idx: int, _transcript: str) -> None:
        if self._is_editing_local:
            return
        self._apply_script_decorations()
        self._scroll_to_token(new_idx, instant=False)

    def _on_ai_status_changed(self, code: str, message: str) -> None:
        short = f"LA: {self._lookahead_words}w"
        if code == "listening":
            self.lbl_ai_status.setText(f"● AI Ready ({short})")
            self.lbl_ai_status.setStyleSheet("border: none; color: #7EE787; font-size: 11px; font-weight: 700;")
        elif code in ("unavailable", "error"):
            self.lbl_ai_status.setText(f"⚠ AI Unavailable ({short})")
            self.lbl_ai_status.setToolTip(message)
            self.lbl_ai_status.setStyleSheet("border: none; color: #F2CC60; font-size: 11px; font-weight: 700;")
        elif code == "loading":
            self.lbl_ai_status.setText(f"◌ Loading AI ({short})")
            self.lbl_ai_status.setStyleSheet("border: none; color: #79C0FF; font-size: 11px; font-weight: 600;")
        else:
            self.lbl_ai_status.setText(short)
            self.lbl_ai_status.setToolTip("Whisper AI Lookahead Window (excludes (...), [...], {...})")
            self.lbl_ai_status.setStyleSheet("border: none; color: #8B949E; font-size: 11px; font-weight: 600;")

    # ------------------------------------------------------------------
    # Local Rich-Text Edit Without Losing Position
    # ------------------------------------------------------------------
    def _toggle_local_edit(self) -> None:
        if not self._is_editing_local:
            self._pre_edit_anchor = self.tracker.capture_anchor(
                scroll_ratio=self._current_scroll_ratio()
            )
            self._autoscroll_running = False
            self._is_editing_local = True
            self.script_view.set_edit_mode(True)
            self.script_view.setExtraSelections([])
            self.btn_edit_local.setText("✓ Save Script")
            self.btn_edit_local.setStyleSheet(
                "background-color: #1F6FEB; color: #FFFFFF; border: 1px solid #388BFD; font-weight: 700;"
            )
            self.btn_cancel_edit.setVisible(True)
            self.script_view.setFocus()
            self._update_mode_controls()
        else:
            raw_html = self.script_view.toHtml()
            sanitized = sanitize_script_html(raw_html)
            self._is_editing_local = False
            self.script_view.set_edit_mode(False)
            self.btn_edit_local.setText("✎ Edit")
            self.btn_edit_local.setStyleSheet("")
            self.btn_cancel_edit.setVisible(False)
            ok, _msg, _path, _mtime = self.config_manager.save_project_script(
                sanitized, project_name=self._active_project
            )
            # Re-anchor cleanly to pre-edit reading position
            self._current_html = sanitized
            self.script_view.setHtml(sanitized)
            self._apply_font_size()
            plain = self.script_view.toPlainText()
            new_idx, fallback_ratio = self.tracker.update_text_preserving_position(
                plain, anchor=self._pre_edit_anchor
            )
            self._apply_script_decorations()
            if self.tracker.total_tokens > 0:
                self._scroll_to_token(new_idx, instant=True)
            else:
                sb = self.script_view.verticalScrollBar()
                if sb is not None and sb.maximum() > 0:
                    sb.setValue(int(round(fallback_ratio * sb.maximum())))
            if ok and self._on_script_saved_locally is not None:
                try:
                    self._on_script_saved_locally(self._active_project, sanitized)
                except Exception:
                    pass

    def _cancel_local_edit(self) -> None:
        if not self._is_editing_local:
            return
        self._is_editing_local = False
        self.script_view.set_edit_mode(False)
        self.btn_edit_local.setText("✎ Edit")
        self.btn_edit_local.setStyleSheet("")
        self.btn_cancel_edit.setVisible(False)
        self.apply_script_html(self._current_html, preserve_position=True)

    # ------------------------------------------------------------------
    # Live Studio Status Sync (Record, Pause, Mute, Talkback, VU, Timer)
    # ------------------------------------------------------------------
    def _sync_studio_status(self) -> None:
        # Check if active project changed externally
        current_proj = sanitize_project_name(self.config_manager.settings.project_name)
        if current_proj != self._active_project and not self._is_editing_local:
            self.load_script_for_project(current_proj, preserve_position=False)

        is_rec = bool(self.audio_engine.is_recording)
        is_paused = bool(self.audio_engine.is_record_paused)
        tb_active = bool(self.audio_engine.talkback_active)
        mic_live = bool(self.audio_engine.is_talent_mic_live_to_producer)
        is_muted = not mic_live

        self.whisper_worker.set_talkback_active(tb_active)

        # Elapsed timer
        elapsed = max(0.0, float(self.audio_engine.elapsed_seconds))
        hrs = int(elapsed // 3600)
        mins = int((elapsed % 3600) // 60)
        secs = elapsed % 60.0
        self.timer_label.setText(f"{hrs:02d}:{mins:02d}:{secs:04.1f}")

        # VU meter
        vu = self.audio_engine.get_vu_readings()
        self.mini_vu.set_levels(vu.peak_db, vu.rms_db, muted=is_muted)

        # State pill
        if tb_active:
            self.state_pill.setText("⚡ TALKBACK ON AIR")
            self.state_pill.setStyleSheet(
                "border: 1px solid #F0883E; border-radius: 5px; padding: 4px 10px; "
                "background-color: #3B2300; color: #FFDF5D; font-weight: 700; font-size: 12px;"
            )
        elif is_rec and is_paused:
            self.state_pill.setText("❚❚ PAUSED")
            self.state_pill.setStyleSheet(
                "border: 1px solid #D29922; border-radius: 5px; padding: 4px 10px; "
                "background-color: #341A04; color: #F2CC60; font-weight: 700; font-size: 12px;"
            )
        elif is_rec:
            self.state_pill.setText("● RECORDING")
            self.state_pill.setStyleSheet(
                "border: 1px solid #F85149; border-radius: 5px; padding: 4px 10px; "
                "background-color: #3D1114; color: #FFA198; font-weight: 700; font-size: 12px;"
            )
        elif is_muted:
            self.state_pill.setText("✕ MIC MUTED")
            self.state_pill.setStyleSheet(
                "border: 1px solid #F85149; border-radius: 5px; padding: 4px 10px; "
                "background-color: #490B11; color: #FFFFFF; font-weight: 700; font-size: 12px;"
            )
        else:
            self.state_pill.setText("● READY")
            self.state_pill.setStyleSheet(
                "border: 1px solid #2EA043; border-radius: 5px; padding: 4px 10px; "
                "background-color: #0F2D1E; color: #7EE787; font-weight: 700; font-size: 12px;"
            )

        # Compact transport buttons
        compact_scale = max(0.55, self._scale * 0.62)
        if is_rec:
            self.record_btn.setText("■ Stop")
            self.record_btn.setStyleSheet(build_record_button_style("recording", compact_scale))
        else:
            self.record_btn.setText("● Record")
            self.record_btn.setStyleSheet(build_record_button_style("ready", compact_scale))

        self.pause_btn.setEnabled(is_rec)
        if is_rec and is_paused:
            self.pause_btn.setText("▶ Resume")
            self.pause_btn.setStyleSheet(
                build_pause_record_button_style("paused", compact_scale)
            )
        elif is_rec:
            self.pause_btn.setText("❚❚ Pause")
            self.pause_btn.setStyleSheet(
                build_pause_record_button_style("ready", compact_scale)
            )
        else:
            self.pause_btn.setText("❚❚ Pause")
            self.pause_btn.setStyleSheet(
                build_pause_record_button_style("disabled", compact_scale)
            )

        if tb_active:
            self.talkback_btn.setText("⚡ Talkback")
            self.talkback_btn.setStyleSheet(
                build_talkback_button_style(True, True, compact_scale)
            )
        else:
            self.talkback_btn.setText("◉ Talkback")
            self.talkback_btn.setStyleSheet(
                build_talkback_button_style(True, False, compact_scale)
            )

        if is_muted:
            self.cough_btn.setText("✕ Muted")
            self.cough_btn.setStyleSheet(
                "background-color: #D72638; color: #FFFFFF; border: 1px solid #FF7B72; "
                "border-radius: 6px; font-weight: 700;"
            )
        else:
            self.cough_btn.setText("◉ Cough")
            self.cough_btn.setStyleSheet(
                "background-color: #161B22; color: #7EE787; border: 1px solid #2EA043; "
                "border-radius: 6px; font-weight: 700;"
            )

    # ------------------------------------------------------------------
    # Hotkeys (Studio Transport + Prompter Navigation / Autoscroll Speed)
    # ------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if self._is_editing_local:
            if key == Qt.Key.Key_Escape:
                self._cancel_local_edit()
                return
            super().keyPressEvent(event)
            return

        # Studio Transport Hotkeys (matching MainWindow & SayoDevice 1x4P)
        if key == Qt.Key.Key_T:
            if not event.isAutoRepeat() and not self._talkback_key_held:
                self._talkback_key_held = True
                self._on_talkback_pressed()
            return
        if key == Qt.Key.Key_B:
            if not event.isAutoRepeat() and not self._cough_key_held:
                self._cough_key_held = True
                self._on_cough_pressed()
            return

        if not event.isAutoRepeat():
            if key == Qt.Key.Key_R:
                self._on_record_clicked()
                return
            if key in (Qt.Key.Key_P, Qt.Key.Key_Pause):
                if self.audio_engine.is_recording:
                    self._on_pause_clicked()
                return
            if key == Qt.Key.Key_A:
                self._toggle_scroll_mode()
                return
            if key in (Qt.Key.Key_G, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._toggle_autoscroll_running()
                return
            if key == Qt.Key.Key_Home:
                self.scroll_to_top()
                return
            if key == Qt.Key.Key_Escape:
                self.close()
                return

        # Navigation & Speed keys (allow key repeat for smooth adjustment)
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.adjust_font_size(2)
            return
        if key == Qt.Key.Key_Minus:
            self.adjust_font_size(-2)
            return
        if key == Qt.Key.Key_Up:
            if self._scroll_mode == "autoscroll":
                self.adjust_autoscroll_speed(-8.0)
            else:
                self.scroll_direct_by(-max(36, int(self._font_size * 1.6)))
            return
        if key == Qt.Key.Key_Down:
            if self._scroll_mode == "autoscroll":
                self.adjust_autoscroll_speed(8.0)
            else:
                self.scroll_direct_by(max(36, int(self._font_size * 1.6)))
            return
        if key == Qt.Key.Key_PageUp:
            self.scroll_direct_by(-max(160, int(self.script_view.viewport().height() * 0.75)))
            return
        if key == Qt.Key.Key_PageDown:
            self.scroll_direct_by(max(160, int(self.script_view.viewport().height() * 0.75)))
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
                self._on_cough_released()
            return
        super().keyReleaseEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._autoscroll_running = False
        self._status_timer.stop()
        self._scroll_timer.stop()
        self.audio_engine.unregister_live_audio_listener(self.whisper_worker.push_audio)
        self.whisper_worker.stop()
        super().closeEvent(event)
