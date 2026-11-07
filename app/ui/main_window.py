"""Main fullscreen studio window for the Alpine Recording Station."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeyEvent, QResizeEvent
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
    QTabWidget,
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
from app.remote_producer import RemoteProducerServer
from app.samba_manager import SambaManager
from app.ui.on_screen_keyboard import OnScreenKeyboardController
from app.ui.remote_producer_dialog import RemoteProducerDialog
from app.ui.settings_dialog import SettingsDialog
from app.ui.styles import (
    build_main_stylesheet,
    build_pause_record_button_style,
    build_record_button_style,
    build_talkback_button_style,
    compute_scale_factor,
)
from app.ui.storage_monitor import StorageMonitorWidget
from app.ui.vu_meter import VuMeterWidget

logger = logging.getLogger(__name__)


class ProjectPrefixDialog(QDialog):
    """Modal dialog for viewing and editing the Project Prefix."""

    def __init__(
        self,
        config_manager: ConfigManager,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.setWindowTitle("Set Project Prefix")
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
            title="Project Prefix",
            hint="Output WAV format: <ProjectName>_yyyy-mm-dd_HH.MM.SS_TZ.wav",
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

        title_label = QLabel("Project Prefix")
        title_label.setObjectName("HeaderTitle")
        layout.addWidget(title_label)

        desc_label = QLabel(
            "Enter the project name prefix for output WAV files.\n"
            "Only takes matching this project prefix are shown in the Project Recordings list."
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
            "Open On-Screen Touch Keyboard to edit Project Prefix"
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
        self.save_btn.setToolTip("Save the Project Prefix and update the recordings list [Hotkey: Enter]")
        self.save_btn.setDefault(True)
        self.save_btn.setStyleSheet(
            "background-color: #1F6FEB; color: #FFFFFF; font-weight: bold;"
        )
        self.save_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.save_btn)

        layout.addLayout(btn_row)

    def _update_preview(self, text: str) -> None:
        sample = generate_recording_filename(text)
        self.preview_label.setText(f"Next: {sample}")


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
                "<b>Record</b> (Green) starts capturing audio to a new timestamped WAV file in "
                "<code>/storage</code> named <code>&lt;ProjectPrefix&gt;_yyyy-mm-dd_HH.MM.SS_TZ.wav</code>.<br>"
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
                "TALKBACK  [Hotkey: Hold T]",
                "Available in <b>Studio Mode</b>. Hold down <b>Talkback</b> (or hold <code>[T]</code>) "
                "for momentary push-to-talk slating and session notes:<br>"
                "• Mutes the Left (Voiceover) channel so notes stay off the primary performance track.<br>"
                "• Emits a configurable press marker tone on the Right (Talkback) channel.<br>"
                "• Records spoken notes onto the Right channel while held (and sends them to the Remote Producer).<br>"
                "• Emits a configurable release marker tone on the Right channel when released."
            ),
            self._build_section_card(
                "COUGH / MUTED  [Hotkey: Hold B or Click]",
                "Dual-function talent mute and privacy control:<br>"
                "• <b>Hold Down (or Hold [B] — Momentary Cough Mute):</b> Mutes the local microphone "
                "completely (both to the local recording and to the Remote Producer) for as long as the "
                "button is held. Releasing immediately unmutes.<br>"
                "• <b>Quick Click — Privacy Mute Toggle:</b> Toggles whether your live microphone is "
                "streamed to connected Remote Producers while between takes.<br>"
                "• <b>Flashing Red Alert:</b> Whenever either momentary Cough Mute or Privacy Mute is "
                "active, the button displays <b>Muted</b> and flashes bright red to draw immediate attention."
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
                "PROJECT PREFIX && REFERENCE PLAYBACK  [Hotkeys: P, S, F5]",
                "• <b>Project Prefix:</b> Sets the filename prefix for new WAV recordings and filters the "
                "Project Recordings list to show only takes belonging to the current project.<br>"
                "• <b>Play / Pause [P] && Stop [S]:</b> Auditions the selected WAV take in the list through "
                "the configured Playback Output Device.<br>"
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
                "• <b>On-Screen Keyboard:</b> Enables or disables the pop-up touch keyboard for text fields."
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
                "• <code>Hold T</code> : Momentary Studio Mode Talkback<br>"
                "• <code>Hold B</code> : Momentary Talent Cough Mute<br>"
                "• <code>C</code> : Reset VU Peak Hold && Clip Indicator<br>"
                "• <code>M</code> : Toggle Studio Mode / Normal Mode (when idle)<br>"
                "• <code>S</code> : Stop Reference Playback<br>"
                "• <code>F1</code> : Open Help && Reference Guide<br>"
                "• <code>F2</code> or <code>Ctrl+,</code> : Open Settings && Samba Administration<br>"
                "• <code>F3</code> : Open Remote Producer && SSL Setup<br>"
                "• <code>F5</code> : Refresh Storage && Audio Hardware<br>"
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
        self._cough_key_held = False
        self._cough_btn_press_mono: float = 0.0
        self._last_observed_tb_active = False
        self._last_observed_remote_tb = False
        self._last_observed_producer_count = 0
        self._last_observed_mute_state = False
        self._pause_blink_phase = True
        self._last_storage_refresh_monotonic: float = 0.0

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
        self.main_layout.setContentsMargins(18, 14, 18, 8)
        self.main_layout.setSpacing(12)

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

        # 3. Center Studio Deck: Elapsed Time Counter + Record/Pause/Stop + Talkback Button
        self.deck_frame = QFrame()
        self.deck_frame.setStyleSheet(
            "QFrame#StudioDeck { background-color: #0D1014; border: 1px solid #21262D; border-radius: 12px; }"
        )
        self.deck_frame.setObjectName("StudioDeck")
        deck_layout = QVBoxLayout(self.deck_frame)
        deck_layout.setContentsMargins(16, 12, 16, 14)
        deck_layout.setSpacing(10)

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
            "Momentary Push-to-Talk in Studio Mode [Hotkey: Hold T]"
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
        self.main_layout.addWidget(self.deck_frame, stretch=3)

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
        takes_layout.setContentsMargins(12, 10, 12, 10)
        takes_layout.setSpacing(8)

        takes_header_row = QHBoxLayout()
        self.takes_title_label = QLabel("Project Recordings")
        self.takes_title_label.setObjectName("HeaderTitle")
        takes_header_row.addWidget(self.takes_title_label)

        self.project_prefix_btn = QPushButton("✎ Project Prefix")
        self.project_prefix_btn.setToolTip(
            "Open dialog to view or change the Project Prefix for output WAV files and take filtering"
        )
        self.project_prefix_btn.clicked.connect(self._open_project_prefix_dialog)
        takes_header_row.addWidget(self.project_prefix_btn)

        takes_header_row.addStretch(1)

        self.playback_status_label = QLabel("")
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

        btn_min_h = max(68, int(round(120 * self._scale)))
        self.record_btn.setFixedHeight(btn_min_h)
        self.pause_record_btn.setFixedHeight(btn_min_h)
        self.talkback_btn.setFixedHeight(btn_min_h)
        self.cough_privacy_btn.setFixedHeight(btn_min_h)

        # Static relative widths for header and playback bar buttons so they never resize on click
        self.remote_producer_btn.setFixedWidth(max(195, int(round(250 * self._scale))))
        self.settings_btn.setFixedWidth(max(150, int(round(190 * self._scale))))
        self.help_btn.setFixedWidth(max(90, int(round(115 * self._scale))))
        self.project_prefix_btn.setFixedWidth(max(135, int(round(170 * self._scale))))
        self.play_btn.setFixedWidth(max(130, int(round(165 * self._scale))))
        self.stop_play_btn.setFixedWidth(max(90, int(round(115 * self._scale))))
        self.refresh_takes_btn.setFixedWidth(max(105, int(round(135 * self._scale))))

        self.vu_meter.apply_scale(self._scale)
        self.storage_monitor.apply_scale(self._scale)

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

        self.audio_engine.set_talkback_output_device(self._active_output_device)
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

        # Talent Mic Privacy & Cough Button state on Studio Deck (flashes red when muted)
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
                if self.audio_engine.remote_talkback_active:
                    self.talkback_banner.setText("⚡ STUDIO MODE — REMOTE PRODUCER TALKBACK")
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

        self._refresh_storage_status()
        self._update_playback_buttons_state()

    def _refresh_storage_status(self) -> None:
        """Query /storage drive utilization and update the StorageMonitorWidget."""
        status = self.config_manager.get_storage_status()
        self.storage_monitor.set_status(
            status,
            is_recording=self.audio_engine.is_recording,
            blink_on=self._pause_blink_phase,
        )
        self._last_storage_refresh_monotonic = time.monotonic()

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
        readings = self.audio_engine.get_vu_readings()
        self.vu_meter.set_readings(
            readings,
            is_recording=self.audio_engine.is_recording,
            has_device=(self._active_input_device is not None),
            is_paused=self.audio_engine.is_record_paused,
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
        if (
            not self.config_manager.settings.studio_mode
            or self.audio_engine.is_record_paused
        ):
            return
        self.audio_engine.set_talkback(True)
        self._refresh_transport_styles()

    def _on_talkback_released(self) -> None:
        self.audio_engine.set_talkback(False)
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
        active_take = rec_path.name if rec_path is not None else (
            self._project_recordings[0].filename if self._project_recordings else ""
        )
        return {
            "project_name": self.config_manager.settings.project_name or "Session",
            "take_count": len(self._project_recordings),
            "active_take_name": active_take,
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
        self._update_filename_preview()
        self._refresh_recordings_list()

    def _on_osk_field_committed(self, _line_edit: QLineEdit, _text: str) -> None:
        self.config_manager.save(commit_lbu=True)
        self.setFocus()

    def _update_filename_preview(self) -> None:
        rec_path = self.audio_engine.current_recording_path
        if self.audio_engine.is_record_paused and rec_path is not None:
            self.filename_preview_label.setText(f"❚❚ Paused File: {rec_path.name}")
            self.filename_preview_label.setStyleSheet(
                "border: none; color: #FFDF5D; font-family: monospace; font-weight: bold;"
            )
            return
        if self.audio_engine.is_recording and rec_path is not None:
            self.filename_preview_label.setText(f"● Recording: {rec_path.name}")
            self.filename_preview_label.setStyleSheet(
                "border: none; color: #FF6B7A; font-family: monospace; font-weight: bold;"
            )
            return
        sample_name = generate_recording_filename(
            self.config_manager.settings.project_name
        )
        self.filename_preview_label.setText(f"Next: {sample_name}")
        self.filename_preview_label.setStyleSheet(
            "border: none; color: #8B949E; font-family: monospace;"
        )

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
        self._refresh_storage_status()
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
                self.play_btn.setText("❚❚ Pause")
        else:
            self.play_btn.setText("▶ Play / Pause")
            if not is_rec:
                self.playback_status_label.setText(
                    f"{len(self._project_recordings)} take(s)"
                )

    def _on_manual_refresh(self) -> None:
        self.config_manager._ensure_storage_mounted(force=True)
        self._sync_hardware_state(force_probe=True)
        self._refresh_recordings_list()
        self.ip_bar_label.setText(get_primary_ip_display())

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
        elif getattr(dlg, "exit_to_console_requested", False):
            self.close()

    # -------------------------------------------------------------------------
    # Periodic Timers & Keyboard Hotkeys
    # -------------------------------------------------------------------------
    def _on_ui_tick(self) -> None:
        is_rec = self.audio_engine.is_recording
        is_rec_paused = self.audio_engine.is_record_paused
        readings = self.audio_engine.get_vu_readings()
        self.vu_meter.set_readings(
            readings,
            is_recording=is_rec,
            has_device=(self._active_input_device is not None),
            is_paused=is_rec_paused,
        )

        # Detect asynchronous changes from Remote Producer (talkback, client connection, or mute state)
        tb_now = self.audio_engine.talkback_active
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
            or rem_tb_now != self._last_observed_remote_tb
            or prod_count_now != self._last_observed_producer_count
            or mute_active_now != self._last_observed_mute_state
            or blink_now != self._pause_blink_phase
        ):
            self._last_observed_tb_active = tb_now
            self._last_observed_remote_tb = rem_tb_now
            self._last_observed_producer_count = prod_count_now
            self._last_observed_mute_state = mute_active_now
            self._pause_blink_phase = blink_now
            self._refresh_transport_styles()

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
        if key == Qt.Key.Key_P:
            if self.audio_engine.is_recording:
                self._on_pause_record_button_clicked()
            else:
                self._on_play_pause_clicked()
            return
        if key == Qt.Key.Key_S:
            self._on_stop_playback_clicked()
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
