"""Main fullscreen studio window for the Alpine Recording Station."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeyEvent, QResizeEvent
from PyQt6.QtWidgets import (
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
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.audio_engine import AudioDeviceCapabilities, AudioEngine
from app.config_manager import (
    ConfigManager,
    RecordingEntry,
    generate_recording_filename,
    sanitize_project_name,
)
from app.network_info import get_primary_ip_display
from app.samba_manager import SambaManager
from app.ui.on_screen_keyboard import OnScreenKeyboardController
from app.ui.settings_dialog import SettingsDialog
from app.ui.styles import (
    build_main_stylesheet,
    build_record_button_style,
    build_talkback_button_style,
    compute_scale_factor,
)
from app.ui.vu_meter import VuMeterWidget

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Primary responsive fullscreen UI for the Alpine Recording Station."""

    def __init__(
        self,
        config_manager: ConfigManager,
        audio_engine: AudioEngine,
        samba_manager: SambaManager,
        fullscreen: bool = True,
    ) -> None:
        super().__init__()
        self.config_manager = config_manager
        self.audio_engine = audio_engine
        self.samba_manager = samba_manager
        self._fullscreen_requested = fullscreen

        self.setWindowTitle("Alpine Recording Station")
        self.setMinimumSize(640, 420)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._scale = 1.0
        self._active_input_device: Optional[AudioDeviceCapabilities] = None
        self._active_output_device: Optional[AudioDeviceCapabilities] = None
        self._project_recordings: List[RecordingEntry] = []
        self._talkback_key_held = False

        self.osk_controller = OnScreenKeyboardController(
            is_enabled=lambda: bool(self.config_manager.settings.on_screen_keyboard),
            on_committed=self._on_osk_field_committed,
            parent=self,
        )

        self._build_ui()
        self.osk_controller.register_line_edit(
            self.project_edit,
            title="Project Prefix",
            hint="Output WAV format: <ProjectName>_yyyy-mm-dd_HH.MM.SS_TZ.wav",
        )
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
        self.main_layout.setContentsMargins(18, 14, 18, 8)
        self.main_layout.setSpacing(12)

        # 1. Top Bar: Project Name Prefix, Active Hardware Summary, Mode Toggle, Settings
        top_frame = QFrame()
        top_frame.setStyleSheet(
            "QFrame { background-color: #161B22; border: 1px solid #30363D; border-radius: 8px; }"
        )
        top_layout = QHBoxLayout(top_frame)
        top_layout.setContentsMargins(12, 8, 12, 8)
        top_layout.setSpacing(12)

        proj_label = QLabel("Project Prefix:")
        proj_label.setStyleSheet("border: none; font-weight: bold;")
        top_layout.addWidget(proj_label)

        self.project_edit = QLineEdit(self.config_manager.settings.project_name)
        self.project_edit.setPlaceholderText("Enter Project Name...")
        self.project_edit.setToolTip(
            "Project name prefix for output WAV files (<Project>_yyyy-mm-dd_HH.MM.SS_TZ.wav).\n"
            "Only takes matching this project prefix are shown in the list below."
        )
        self.project_edit.textChanged.connect(self._on_project_name_changed)
        self.project_edit.returnPressed.connect(self.setFocus)
        top_layout.addWidget(self.project_edit, stretch=2)

        self.project_osk_btn = QPushButton("⌨")
        self.project_osk_btn.setToolTip(
            "Open On-Screen Touch Keyboard to edit Project Prefix"
        )
        self.project_osk_btn.clicked.connect(
            lambda: self.osk_controller.open_for_line_edit(self.project_edit)
        )
        top_layout.addWidget(self.project_osk_btn)

        self.filename_preview_label = QLabel()
        self.filename_preview_label.setStyleSheet(
            "border: none; color: #8B949E; font-family: monospace;"
        )
        top_layout.addWidget(self.filename_preview_label, stretch=2)

        self.mode_toggle_btn = QPushButton()
        self.mode_toggle_btn.setToolTip(
            "Toggle between Normal Mode and Studio Mode (L=Mic, R=0.5s Tone + Talkback) [Hotkey: M]"
        )
        self.mode_toggle_btn.clicked.connect(self._on_toggle_studio_mode)
        top_layout.addWidget(self.mode_toggle_btn)

        self.settings_btn = QPushButton("⚙ Settings && Samba")
        self.settings_btn.setToolTip(
            "Open Audio Hardware, Recording Settings, and Samba Share User Administration [Hotkey: F2]"
        )
        self.settings_btn.clicked.connect(self._open_settings_dialog)
        top_layout.addWidget(self.settings_btn)

        self.main_layout.addWidget(top_frame)

        # 2. Hardware & Format Status Strip
        status_row = QHBoxLayout()
        status_row.setContentsMargins(4, 0, 4, 0)
        self.hw_status_label = QLabel()
        self.hw_status_label.setStyleSheet("color: #58A6FF; font-weight: 600;")
        status_row.addWidget(self.hw_status_label, stretch=1)

        self.format_badge_label = QLabel()
        self.format_badge_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.format_badge_label.setStyleSheet(
            "color: #8B949E; font-family: monospace; font-weight: bold;"
        )
        status_row.addWidget(self.format_badge_label)
        self.main_layout.addLayout(status_row)

        # 3. Center Studio Deck: Elapsed Time Counter + Record/Stop Button + Talkback Button
        deck_frame = QFrame()
        deck_frame.setStyleSheet(
            "QFrame#StudioDeck { background-color: #0D1014; border: 1px solid #21262D; border-radius: 12px; }"
        )
        deck_frame.setObjectName("StudioDeck")
        deck_layout = QVBoxLayout(deck_frame)
        deck_layout.setContentsMargins(16, 12, 16, 14)
        deck_layout.setSpacing(10)

        # Talkback visual indicator banner
        self.talkback_banner = QLabel("STUDIO MODE READY — RECORDING MIC TO LEFT CHANNEL")
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
            parent=deck_frame,
        )
        self.level_bar = self.vu_meter
        deck_layout.addWidget(self.vu_meter)

        # Primary Transport Controls Row (Record/Stop + Studio Talkback)
        controls_row = QHBoxLayout()
        controls_row.setSpacing(16)

        self.record_btn = QPushButton("●  RECORD")
        self.record_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.record_btn.setToolTip(
            "Start or stop audio recording to /storage [Hotkey: R or Space]"
        )
        self.record_btn.clicked.connect(self._on_record_button_clicked)
        controls_row.addWidget(self.record_btn, stretch=3)

        self.talkback_btn = QPushButton("🎙  TALKBACK\n(HOLD FOR NOTES)")
        self.talkback_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.talkback_btn.setToolTip(
            "Momentary Push-to-Talk (Studio Mode): Hold down to mute Left channel, "
            "emit a 0.5s marker tone on Right channel, and record spoken notes on Right channel "
            "until released [Hotkey: Hold T]"
        )
        self.talkback_btn.pressed.connect(self._on_talkback_pressed)
        self.talkback_btn.released.connect(self._on_talkback_released)
        controls_row.addWidget(self.talkback_btn, stretch=2)

        deck_layout.addLayout(controls_row, stretch=1)
        self.main_layout.addWidget(deck_frame, stretch=3)

        # 4. Bottom Pane: Scrollable Project Recordings List & Reference Playback
        takes_frame = QFrame()
        takes_frame.setStyleSheet(
            "QFrame#TakesPane { background-color: #161B22; border: 1px solid #30363D; border-radius: 10px; }"
        )
        takes_frame.setObjectName("TakesPane")
        takes_layout = QVBoxLayout(takes_frame)
        takes_layout.setContentsMargins(12, 10, 12, 10)
        takes_layout.setSpacing(8)

        takes_header_row = QHBoxLayout()
        self.takes_title_label = QLabel("Project Recordings")
        self.takes_title_label.setObjectName("HeaderTitle")
        takes_header_row.addWidget(self.takes_title_label)
        takes_header_row.addStretch(1)

        self.playback_status_label = QLabel("Select a take below for reference playback")
        self.playback_status_label.setStyleSheet("color: #8B949E; font-family: monospace;")
        takes_header_row.addWidget(self.playback_status_label)

        self.play_btn = QPushButton("▶ Play / Pause")
        self.play_btn.setToolTip(
            "Play or pause the selected project recording for reference [Hotkey: P]"
        )
        self.play_btn.clicked.connect(self._on_play_pause_clicked)
        takes_header_row.addWidget(self.play_btn)

        self.stop_play_btn = QPushButton("■ Stop")
        self.stop_play_btn.setToolTip(
            "Stop reference audio playback [Hotkey: S]"
        )
        self.stop_play_btn.clicked.connect(self._on_stop_playback_clicked)
        takes_header_row.addWidget(self.stop_play_btn)

        self.refresh_takes_btn = QPushButton("↻ Refresh")
        self.refresh_takes_btn.setToolTip(
            "Rescan /storage for project recordings and audio devices [Hotkey: F5]"
        )
        self.refresh_takes_btn.clicked.connect(self._on_manual_refresh)
        takes_header_row.addWidget(self.refresh_takes_btn)

        takes_layout.addLayout(takes_header_row)

        self.recordings_list = QListWidget()
        self.recordings_list.setToolTip(
            "Finished WAV recordings in /storage matching the current Project Prefix. "
            "Double-click or press [P] to play back."
        )
        self.recordings_list.itemDoubleClicked.connect(
            lambda _item: self._on_play_pause_clicked(force_new=True)
        )
        self.recordings_list.currentRowChanged.connect(
            lambda _row: self._update_playback_buttons_state()
        )
        takes_layout.addWidget(self.recordings_list, stretch=1)

        self.main_layout.addWidget(takes_frame, stretch=2)

        # 5. Subtle Bottom Status Bar showing Device IP & Samba Share Path
        self.ip_bar_label = QLabel(get_primary_ip_display())
        self.ip_bar_label.setObjectName("SubtleIpBar")
        self.ip_bar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_layout.addWidget(self.ip_bar_label)

        self._update_filename_preview()

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

        timer_px = max(32, int(round(62 * self._scale)))
        timer_font = QFont("DejaVu Sans Mono", timer_px, QFont.Weight.Bold)
        timer_font.setStyleHint(QFont.StyleHint.Monospace)
        self.timer_label.setFont(timer_font)

        btn_min_h = max(72, int(round(130 * self._scale)))
        self.record_btn.setMinimumHeight(btn_min_h)
        self.talkback_btn.setMinimumHeight(btn_min_h)
        self.vu_meter.apply_scale(self._scale)

        self._refresh_transport_styles()

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

        self._refresh_transport_styles()

    def _refresh_transport_styles(self) -> None:
        settings = self.config_manager.settings
        is_rec = self.audio_engine.is_recording
        dev = self._active_input_device

        # Mode toggle button text
        if settings.studio_mode:
            self.mode_toggle_btn.setText("Mode: STUDIO (L=Mic / R=Talkback)")
            self.mode_toggle_btn.setStyleSheet(
                "background-color: #1F3A5F; color: #58A6FF; border: 1px solid #388BFD; font-weight: bold;"
            )
        else:
            ch_txt = "Stereo" if settings.channels >= 2 else "Mono"
            self.mode_toggle_btn.setText(f"Mode: NORMAL ({ch_txt})")
            self.mode_toggle_btn.setStyleSheet("")
        self.mode_toggle_btn.setEnabled(not is_rec)
        self.settings_btn.setEnabled(not is_rec)
        self.project_edit.setEnabled(not is_rec)
        self.project_osk_btn.setVisible(bool(settings.on_screen_keyboard))
        self.project_osk_btn.setEnabled(not is_rec)

        # Hardware & Format labels
        if dev is None:
            self.hw_status_label.setText(
                "⚠ No Valid Audio Interface Connected — Connect USB Audio Interface or Check Settings [F2]"
            )
            self.hw_status_label.setStyleSheet("color: #F85149; font-weight: bold;")
            self.format_badge_label.setText("OFFLINE")
        else:
            ch_desc = (
                "STUDIO STEREO (L:Mic / R:Talkback)"
                if settings.studio_mode
                else ("STEREO (2ch)" if settings.channels >= 2 else "MONO (1ch)")
            )
            self.hw_status_label.setText(f"Input: {dev.display_name}")
            self.hw_status_label.setStyleSheet("color: #3FB950; font-weight: 600;")
            self.format_badge_label.setText(
                f"{settings.bit_depth}-bit  •  {settings.sample_rate:,} Hz  •  {ch_desc}"
            )

        # Record / Stop Button State (Red = recording, Green = ready, Grey = invalid/no device)
        if is_rec:
            self.record_btn.setEnabled(True)
            self.record_btn.setText("■   STOP RECORDING")
            self.record_btn.setStyleSheet(
                build_record_button_style("recording", self._scale)
            )
            self.timer_label.setStyleSheet("color: #FF6B7A; font-weight: bold;")
        elif dev is not None:
            self.record_btn.setEnabled(True)
            self.record_btn.setText("●   RECORD")
            self.record_btn.setStyleSheet(
                build_record_button_style("ready", self._scale)
            )
            self.timer_label.setStyleSheet("color: #F0F6FC; font-weight: bold;")
        else:
            self.record_btn.setEnabled(False)
            self.record_btn.setText("NO AUDIO INTERFACE")
            self.record_btn.setStyleSheet(
                build_record_button_style("disabled", self._scale)
            )
            self.timer_label.setStyleSheet("color: #6E7681; font-weight: bold;")

        # Talkback button & visual banner state
        tb_active = self.audio_engine.talkback_active
        tb_enabled = bool(settings.studio_mode and dev is not None)
        self.talkback_btn.setVisible(bool(settings.studio_mode))
        self.talkback_btn.setEnabled(tb_enabled)
        self.talkback_btn.setStyleSheet(
            build_talkback_button_style(tb_active, tb_enabled, self._scale)
        )

        if settings.studio_mode:
            self.talkback_banner.setVisible(True)
            if tb_active:
                self.talkback_btn.setText("🎙  TALKBACK ACTIVE\n(RECORDING RIGHT CH)")
                self.talkback_banner.setText(
                    "⚡ TALKBACK ACTIVE — LEFT CH MUTED • 0.5s TONE + MIC RECORDING ON RIGHT CH ⚡"
                )
                self.talkback_banner.setStyleSheet(
                    "background-color: #E36209; color: #FFFFFF; font-weight: bold; "
                    "padding: 6px; border-radius: 6px; border: 2px solid #FFDF5D;"
                )
            else:
                self.talkback_btn.setText("🎙  TALKBACK\n(HOLD FOR NOTES)")
                self.talkback_banner.setText(
                    "STUDIO MODE: MIC RECORDING ON LEFT CH  •  HOLD TALKBACK [T] FOR RIGHT CH NOTES"
                )
                self.talkback_banner.setStyleSheet(
                    "background-color: #161B22; color: #58A6FF; font-weight: 600; "
                    "padding: 6px; border-radius: 6px; border: 1px solid #1F6FEB;"
                )
        else:
            self.talkback_banner.setVisible(False)

        self._update_playback_buttons_state()

    # -------------------------------------------------------------------------
    # Recording & Talkback Actions
    # -------------------------------------------------------------------------
    def _on_record_button_clicked(self) -> None:
        if self.audio_engine.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        self._sync_hardware_state()
        dev = self._active_input_device
        if dev is None:
            return

        settings = self.config_manager.settings
        filename = generate_recording_filename(settings.project_name)
        output_path = self.config_manager.storage_dir / filename

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
            on_error=self._on_recording_error_threadsafe,
        )
        self._refresh_transport_styles()

    def _stop_recording(self) -> None:
        self._talkback_key_held = False
        self.audio_engine.set_talkback(False)
        finished_path = self.audio_engine.stop_recording()
        self._refresh_transport_styles()
        self._refresh_recordings_list(select_path=finished_path)

    def _on_reset_peak_hold(self) -> None:
        self.audio_engine.reset_peak_hold()
        readings = self.audio_engine.get_vu_readings()
        self.vu_meter.set_readings(
            readings,
            is_recording=self.audio_engine.is_recording,
            has_device=(self._active_input_device is not None),
        )
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
        if not self.config_manager.settings.studio_mode:
            return
        self.audio_engine.set_talkback(True)
        self._refresh_transport_styles()

    def _on_talkback_released(self) -> None:
        self.audio_engine.set_talkback(False)
        self._refresh_transport_styles()

    def _on_toggle_studio_mode(self) -> None:
        if self.audio_engine.is_recording:
            return
        s = self.config_manager.settings
        s.studio_mode = not s.studio_mode
        if s.studio_mode:
            s.channels = 2
        self.config_manager.save(commit_lbu=True)
        self._sync_hardware_state()
        self._refresh_transport_styles()

    # -------------------------------------------------------------------------
    # Project Prefix & Recordings List + Reference Playback
    # -------------------------------------------------------------------------
    def _on_project_name_changed(self, new_text: str) -> None:
        self.config_manager.settings.project_name = new_text
        self.config_manager.save()
        self._update_filename_preview()
        self._refresh_recordings_list()

    def _on_osk_field_committed(self, _line_edit: QLineEdit, _text: str) -> None:
        self.config_manager.save(commit_lbu=True)
        self.setFocus()

    def _update_filename_preview(self) -> None:
        sample_name = generate_recording_filename(
            self.config_manager.settings.project_name
        )
        self.filename_preview_label.setText(f"Next: {sample_name}")

    def _refresh_recordings_list(self, select_path: Optional[Path] = None) -> None:
        prefix = sanitize_project_name(self.config_manager.settings.project_name)
        self.takes_title_label.setText(f"Project Recordings  [{prefix}_*.wav]")

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

        selected_row = 0
        for idx, entry in enumerate(self._project_recordings):
            ch_label = "Stereo" if entry.channels == 2 else f"{entry.channels}ch"
            meta = (
                f"{entry.filename}    |    "
                f"{entry.formatted_duration}    |    "
                f"{entry.bit_depth}-bit {entry.sample_rate:,} Hz {ch_label}    |    "
                f"{entry.formatted_size}"
            )
            item = QListWidgetItem(meta)
            item.setData(Qt.ItemDataRole.UserRole, entry.filepath)
            self.recordings_list.addItem(item)
            if current_selected_path and entry.filepath == current_selected_path:
                selected_row = idx

        if self._project_recordings:
            self.recordings_list.setCurrentRow(selected_row)
        self.recordings_list.blockSignals(False)
        self._update_playback_buttons_state()

    def _get_selected_recording_path(self) -> Optional[Path]:
        item = self.recordings_list.currentItem()
        if item is None:
            return None
        path = item.data(Qt.ItemDataRole.UserRole)
        return Path(path) if path else None

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
                0, self._update_playback_buttons_state
            ),
            on_error=lambda err: QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self, "Playback Error", f"Could not play WAV file:\n{err}"
                ),
            ),
        )
        self._update_playback_buttons_state()

    def _on_stop_playback_clicked(self) -> None:
        self.audio_engine.stop_playback()
        self._update_playback_buttons_state()

    def _update_playback_buttons_state(self, paused: Optional[bool] = None) -> None:
        is_rec = self.audio_engine.is_recording
        has_selection = self._get_selected_recording_path() is not None
        is_playing = self.audio_engine.is_playing
        is_paused = self.audio_engine.is_paused if paused is None else paused

        self.play_btn.setEnabled(not is_rec and has_selection)
        self.stop_play_btn.setEnabled(not is_rec and is_playing)

        if is_playing:
            if is_paused:
                self.play_btn.setText("▶ Resume")
            else:
                self.play_btn.setText("⏸ Pause")
        else:
            self.play_btn.setText("▶ Play / Pause")
            if not is_rec:
                self.playback_status_label.setText(
                    f"{len(self._project_recordings)} matching take(s) in /storage"
                )

    def _on_manual_refresh(self) -> None:
        self._sync_hardware_state(force_probe=True)
        self._refresh_recordings_list()
        self.ip_bar_label.setText(get_primary_ip_display())

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

    # -------------------------------------------------------------------------
    # Periodic Timers & Keyboard Hotkeys
    # -------------------------------------------------------------------------
    def _on_ui_tick(self) -> None:
        is_rec = self.audio_engine.is_recording
        readings = self.audio_engine.get_vu_readings()
        self.vu_meter.set_readings(
            readings,
            is_recording=is_rec,
            has_device=(self._active_input_device is not None),
        )

        if is_rec:
            elapsed = self.audio_engine.elapsed_seconds
            hrs = int(elapsed // 3600)
            mins = int((elapsed % 3600) // 60)
            secs = elapsed % 60.0
            self.timer_label.setText(f"{hrs:02d}:{mins:02d}:{secs:04.1f}")
        elif self.audio_engine.is_playing:
            pos = self.audio_engine.play_position_sec
            dur = self.audio_engine.play_duration_sec
            p_m, p_s = divmod(int(pos), 60)
            d_m, d_s = divmod(int(dur), 60)
            fname = (
                self.audio_engine.playing_filepath.name
                if self.audio_engine.playing_filepath
                else ""
            )
            state_str = "PAUSED" if self.audio_engine.is_paused else "PLAYING"
            self.playback_status_label.setText(
                f"{state_str}: {fname}  [{p_m:02d}:{p_s:02d} / {d_m:02d}:{d_s:02d}]"
            )

    def _on_background_tick(self) -> None:
        self.ip_bar_label.setText(get_primary_ip_display())
        self._update_filename_preview()
        if not self.audio_engine.is_recording and not self.audio_engine.is_playing:
            self._sync_hardware_state()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Ignore single-key hotkeys while user is typing in the Project Name input
        if self.project_edit.hasFocus():
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
                self.setFocus()
                return
            super().keyPressEvent(event)
            return

        key = event.key()

        # Momentary Push-to-Talk Hotkey [T] (ignoring X11 auto-repeat!)
        if key == Qt.Key.Key_T:
            if not event.isAutoRepeat() and not self._talkback_key_held:
                self._talkback_key_held = True
                self._on_talkback_pressed()
            return

        if event.isAutoRepeat():
            return

        if key in (Qt.Key.Key_R, Qt.Key.Key_Space):
            if self.record_btn.isEnabled():
                self._on_record_button_clicked()
            return
        if key == Qt.Key.Key_C:
            self._on_reset_peak_hold()
            return
        if key == Qt.Key.Key_M:
            self._on_toggle_studio_mode()
            return
        if key == Qt.Key.Key_P:
            self._on_play_pause_clicked()
            return
        if key == Qt.Key.Key_S:
            self._on_stop_playback_clicked()
            return
        if key == Qt.Key.Key_F2 or (
            key == Qt.Key.Key_Comma
            and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            self._open_settings_dialog()
            return
        if key == Qt.Key.Key_F5:
            self._on_manual_refresh()
            return
        if key == Qt.Key.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            return

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_T and not event.isAutoRepeat():
            if self._talkback_key_held:
                self._talkback_key_held = False
                self._on_talkback_released()
            return
        super().keyReleaseEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.audio_engine.stop_monitoring()
        self.audio_engine.stop_recording()
        self.audio_engine.stop_playback()
        super().closeEvent(event)
