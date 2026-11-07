"""Settings and Samba Share Administration dialog."""

from __future__ import annotations

from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.audio_engine import AudioDeviceCapabilities, AudioEngine
from app.config_manager import (
    TALKBACK_TONE_DURATION_OPTIONS,
    TALKBACK_TONE_FREQ_OPTIONS,
    ConfigManager,
)
from app.network_info import get_primary_ip_display
from app.samba_manager import SambaManager
from app.ui.on_screen_keyboard import OnScreenKeyboardController
from app.ui.styles import (
    build_main_stylesheet,
    compute_scale_factor,
    configure_combo_box_popup,
)


class SettingsDialog(QDialog):
    """Modal settings pane for Audio Hardware, Recording Mode, and Samba User Administration."""

    def __init__(
        self,
        config_manager: ConfigManager,
        audio_engine: AudioEngine,
        samba_manager: SambaManager,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.audio_engine = audio_engine
        self.samba_manager = samba_manager

        self.setWindowTitle("Settings & Samba Share Administration")
        self.setModal(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if parent is not None and parent.width() >= 480:
            pw, ph = parent.width(), parent.height()
            self._scale = compute_scale_factor(pw, ph)
            self.resize(
                max(640, int(pw * 0.78)),
                max(440, int(ph * 0.82)),
            )
        else:
            self._scale = 1.0
            self.resize(780, 540)

        self._capture_devices: List[AudioDeviceCapabilities] = []
        self._playback_devices: List[AudioDeviceCapabilities] = []
        self._device_map: Dict[str, AudioDeviceCapabilities] = {}
        self._initializing = True
        self.exit_to_console_requested = False

        self.osk_controller = OnScreenKeyboardController(
            is_enabled=self._is_osk_enabled,
            parent=self,
        )

        self._build_ui()
        self._apply_styles()
        self._register_osk_fields()
        self._populate_devices_and_settings()
        self._refresh_samba_users()
        self._initializing = False

    def _apply_styles(self) -> None:
        self.setStyleSheet(build_main_stylesheet(self._scale))
        for combo in self.findChildren(QComboBox):
            configure_combo_box_popup(combo, self._scale)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(12)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, stretch=1)

        # Tab 1: Audio & Recording Settings
        self.audio_tab = QWidget()
        self._build_audio_tab(self.audio_tab)
        self.tabs.addTab(self.audio_tab, "Audio && Recording Settings")

        # Tab 2: Samba Share Access
        self.samba_tab = QWidget()
        self._build_samba_tab(self.samba_tab)
        self.tabs.addTab(self.samba_tab, "Samba Share Administration")

        # Bottom action buttons
        bottom_bar = QHBoxLayout()
        self.ip_info_label = QLabel(get_primary_ip_display())
        self.ip_info_label.setStyleSheet("color: #8B949E;")
        bottom_bar.addWidget(self.ip_info_label)
        bottom_bar.addStretch(1)

        self.exit_kiosk_btn = QPushButton("Exit to Console")
        self.exit_kiosk_btn.setToolTip(
            "Close the recorder UI and return to the Linux console (tty1) for troubleshooting [Hotkey: Ctrl+Q]"
        )
        self.exit_kiosk_btn.setStyleSheet(
            "background-color: #21262D; color: #F85149; border: 1px solid #8E1523;"
        )
        self.exit_kiosk_btn.clicked.connect(self._on_exit_to_console)
        bottom_bar.addWidget(self.exit_kiosk_btn)

        self.rescan_btn = QPushButton("Rescan Audio Hardware")
        self.rescan_btn.setToolTip(
            "Rescan connected USB/ALSA audio interfaces [Hotkey: F5]"
        )
        self.rescan_btn.setShortcut("F5")
        self.rescan_btn.clicked.connect(self._on_rescan_devices)
        bottom_bar.addWidget(self.rescan_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Discard audio setting changes and close [Hotkey: Esc]")
        self.cancel_btn.clicked.connect(self.reject)
        bottom_bar.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("Save && Apply Settings")
        self.save_btn.setToolTip(
            "Save settings persistently to /storage and LBU [Hotkey: Ctrl+S or Enter]"
        )
        self.save_btn.setShortcut("Ctrl+S")
        self.save_btn.setDefault(True)
        self.save_btn.setStyleSheet(
            "background-color: #1F6FEB; color: #FFFFFF; font-weight: bold;"
        )
        self.save_btn.clicked.connect(self._on_save_settings)
        bottom_bar.addWidget(self.save_btn)

        root_layout.addLayout(bottom_bar)

    def _build_audio_tab(self, container: QWidget) -> None:
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.input_device_combo = QComboBox()
        self.input_device_combo.setToolTip(
            "Select USB audio interface for microphone capture"
        )
        self.input_device_combo.currentIndexChanged.connect(
            self._on_input_device_changed
        )
        form.addRow("Input Audio Interface:", self.input_device_combo)

        self.output_device_combo = QComboBox()
        self.output_device_combo.setToolTip(
            "Select audio interface used for reference playback of recorded takes"
        )
        form.addRow("Playback Output Device:", self.output_device_combo)

        self.bit_depth_combo = QComboBox()
        self.bit_depth_combo.setToolTip(
            "Hardware-supported bit depth (defaults to maximum supported by interface)"
        )
        self.bit_depth_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Bit Depth:", self.bit_depth_combo)

        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.setToolTip(
            "Hardware-supported sample rate (defaults to maximum supported by interface)"
        )
        self.sample_rate_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Sample Rate:", self.sample_rate_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(
            "Normal Mode (Standard Mono / Stereo Recording)", False
        )
        self.mode_combo.addItem(
            "Studio Mode (Stereo: Left = Mic, Right = Tone + Talkback)", True
        )
        self.mode_combo.setToolTip(
            "Choose between standard recording or Studio Mode with Right-channel Talkback notes"
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Recording Mode:", self.mode_combo)

        self.channels_combo = QComboBox()
        self.channels_combo.addItem("Mono (1 Channel)", 1)
        self.channels_combo.addItem("Stereo (2 Channels)", 2)
        self.channels_combo.setToolTip(
            "Select Mono or Stereo output file (Studio Mode automatically uses Stereo)"
        )
        self.channels_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Recording Channels:", self.channels_combo)

        self.input_channel_combo = QComboBox()
        self.input_channel_combo.setToolTip(
            "Which hardware microphone input channel to capture for Mono / Studio Left track"
        )
        form.addRow("Mic Input Channel:", self.input_channel_combo)

        self.tone_freq_combo = QComboBox()
        for freq_hz, freq_label in TALKBACK_TONE_FREQ_OPTIONS:
            self.tone_freq_combo.addItem(freq_label, float(freq_hz))
        self.tone_freq_combo.setToolTip(
            "Select frequency for Studio Mode talkback press and release marker tones "
            "(lower tones like 330 Hz or 440 Hz are warmer and easier on the ear)"
        )
        self.tone_freq_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Talkback Tone Frequency:", self.tone_freq_combo)

        tone_dur_row = QHBoxLayout()
        tone_dur_row.setContentsMargins(0, 0, 0, 0)
        tone_dur_row.setSpacing(10)

        press_lbl = QLabel("Press:")
        tone_dur_row.addWidget(press_lbl)

        self.press_tone_duration_combo = QComboBox()
        for dur in TALKBACK_TONE_DURATION_OPTIONS:
            self.press_tone_duration_combo.addItem(
                self._format_duration_option(dur, default_sec=1.0), float(dur)
            )
        self.press_tone_duration_combo.setToolTip(
            "Duration of the marker tone on the Right channel when the Talkback button is pressed (Default: 1.0s)"
        )
        self.press_tone_duration_combo.currentIndexChanged.connect(self._on_mode_changed)
        tone_dur_row.addWidget(self.press_tone_duration_combo, stretch=1)

        release_lbl = QLabel("Release:")
        tone_dur_row.addWidget(release_lbl)

        self.release_tone_duration_combo = QComboBox()
        for dur in TALKBACK_TONE_DURATION_OPTIONS:
            self.release_tone_duration_combo.addItem(
                self._format_duration_option(dur, default_sec=0.5), float(dur)
            )
        self.release_tone_duration_combo.setToolTip(
            "Duration of the marker tone on the Right channel when Talkback mode is concluded/released (Default: 0.5s)"
        )
        self.release_tone_duration_combo.currentIndexChanged.connect(self._on_mode_changed)
        tone_dur_row.addWidget(self.release_tone_duration_combo, stretch=1)

        form.addRow("Talkback Tone Duration:", tone_dur_row)

        self.osk_combo = QComboBox()
        self.osk_combo.addItem(
            "Enabled (Touchscreen Mode — Show on-screen keyboard for text fields)",
            True,
        )
        self.osk_combo.addItem(
            "Disabled (Hardware Keyboard Mode)",
            False,
        )
        self.osk_combo.setToolTip(
            "Enable or disable the On-Screen Touch Keyboard when editing the Project Prefix, "
            "Samba user credentials, or any other text input field"
        )
        self.osk_combo.currentIndexChanged.connect(self._on_osk_setting_changed)
        form.addRow("On-Screen Keyboard:", self.osk_combo)

        layout.addLayout(form)

        self.mode_hint_label = QLabel()
        self.mode_hint_label.setWordWrap(True)
        self.mode_hint_label.setStyleSheet(
            "background-color: #0D1014; color: #8B949E; padding: 10px; border-radius: 6px; border: 1px solid #21262D;"
        )
        layout.addWidget(self.mode_hint_label)

        self.storage_info_label = QLabel()
        self.storage_info_label.setWordWrap(True)
        layout.addWidget(self.storage_info_label)
        layout.addStretch(1)

    def _build_samba_tab(self, container: QWidget) -> None:
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        info = QLabel(
            "Manage authenticated users for the /storage Samba network share.\n"
            "All files in /storage are masked (rw-rw-rw-) so all authorized users have "
            "full read, write, and delete access for file retrieval and cleanup."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #8B949E;")
        layout.addWidget(info)

        content_row = QHBoxLayout()
        content_row.setSpacing(16)

        # Left column: Existing users list
        left_box = QGroupBox("Authorized Samba Users")
        left_layout = QVBoxLayout(left_box)
        self.samba_users_list = QListWidget()
        self.samba_users_list.currentTextChanged.connect(self._on_samba_user_selected)
        left_layout.addWidget(self.samba_users_list, stretch=1)

        self.remove_user_btn = QPushButton("Remove Selected User")
        self.remove_user_btn.setToolTip(
            "Revoke Samba share access for the selected user [Hotkey: Del]"
        )
        self.remove_user_btn.setShortcut("Delete")
        self.remove_user_btn.setStyleSheet(
            "background-color: #8E1523; color: #FFFFFF;"
        )
        self.remove_user_btn.clicked.connect(self._on_remove_samba_user)
        left_layout.addWidget(self.remove_user_btn)
        content_row.addWidget(left_box, stretch=1)

        # Right column: Add / Change password form
        right_box = QGroupBox("Add User / Change Password")
        right_layout = QVBoxLayout(right_box)
        form = QFormLayout()
        form.setSpacing(10)

        self.samba_username_edit = QLineEdit()
        self.samba_username_edit.setPlaceholderText("e.g. studio_engineer")
        form.addRow("Username:", self.samba_username_edit)

        self.samba_password_edit = QLineEdit()
        self.samba_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.samba_password_edit.setPlaceholderText("Minimum 4 characters")
        form.addRow("Password:", self.samba_password_edit)

        self.samba_password_confirm_edit = QLineEdit()
        self.samba_password_confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.samba_password_confirm_edit.setPlaceholderText("Confirm password")
        form.addRow("Confirm:", self.samba_password_confirm_edit)

        right_layout.addLayout(form)

        self.samba_osk_btn = QPushButton("⌨ Open Touch Keyboard")
        self.samba_osk_btn.setToolTip(
            "Open the On-Screen Touch Keyboard to enter Samba Username and Password"
        )
        self.samba_osk_btn.clicked.connect(self._on_open_samba_osk)
        right_layout.addWidget(self.samba_osk_btn)

        btn_row = QHBoxLayout()
        self.add_user_btn = QPushButton("Add New User")
        self.add_user_btn.setToolTip(
            "Create a new Samba user with the entered username and password [Hotkey: Alt+A]"
        )
        self.add_user_btn.setShortcut("Alt+A")
        self.add_user_btn.setStyleSheet(
            "background-color: #1F883D; color: #FFFFFF; font-weight: bold;"
        )
        self.add_user_btn.clicked.connect(self._on_add_samba_user)
        btn_row.addWidget(self.add_user_btn)

        self.change_pw_btn = QPushButton("Update Password")
        self.change_pw_btn.setToolTip(
            "Change password for the entered or selected Samba user [Hotkey: Alt+P]"
        )
        self.change_pw_btn.setShortcut("Alt+P")
        self.change_pw_btn.clicked.connect(self._on_change_samba_password)
        btn_row.addWidget(self.change_pw_btn)

        right_layout.addLayout(btn_row)

        self.samba_status_label = QLabel("")
        self.samba_status_label.setWordWrap(True)
        self.samba_status_label.setStyleSheet("color: #58A6FF; margin-top: 8px;")
        right_layout.addWidget(self.samba_status_label)
        right_layout.addStretch(1)

        content_row.addWidget(right_box, stretch=1)
        layout.addLayout(content_row, stretch=1)

    # -------------------------------------------------------------------------
    # Audio Hardware & Settings Logic
    # -------------------------------------------------------------------------
    def _populate_devices_and_settings(self, force_probe: bool = False) -> None:
        settings = self.config_manager.settings
        self._capture_devices = self.audio_engine.get_capture_devices(force_probe=force_probe)
        self._playback_devices = self.audio_engine.get_playback_devices(force_probe=False)
        self._device_map = {d.device_id: d for d in self._capture_devices}

        self.input_device_combo.blockSignals(True)
        self.input_device_combo.clear()

        if not self._capture_devices:
            self.input_device_combo.addItem(
                "No Audio Capture Interface Detected", ""
            )
        else:
            for dev in self._capture_devices:
                self.input_device_combo.addItem(dev.display_name, dev.device_id)

        # Select saved input device or default to first USB capture device
        selected_idx = 0
        if settings.input_device:
            idx = self.input_device_combo.findData(settings.input_device)
            if idx >= 0:
                selected_idx = idx
            else:
                # Saved device is disconnected; show it as unavailable so user sees why
                self.input_device_combo.addItem(
                    f"[Disconnected] {settings.input_device}", settings.input_device
                )
                selected_idx = self.input_device_combo.count() - 1
        self.input_device_combo.setCurrentIndex(selected_idx)
        self.input_device_combo.blockSignals(False)

        # Output playback devices
        self.output_device_combo.clear()
        self.output_device_combo.addItem(
            "Same as Input USB Audio Interface (Default)", "same"
        )
        for pdev in self._playback_devices:
            self.output_device_combo.addItem(pdev.display_name, pdev.device_id)
        out_idx = self.output_device_combo.findData(settings.output_device)
        self.output_device_combo.setCurrentIndex(out_idx if out_idx >= 0 else 0)

        # Mode & Channels
        mode_idx = self.mode_combo.findData(bool(settings.studio_mode))
        self.mode_combo.setCurrentIndex(mode_idx if mode_idx >= 0 else 0)

        ch_idx = self.channels_combo.findData(settings.channels)
        self.channels_combo.setCurrentIndex(ch_idx if ch_idx >= 0 else 0)

        self._select_or_add_float_combo(
            self.tone_freq_combo,
            float(settings.talkback_tone_hz),
            lambda v: f"{v:g} Hz (Custom)",
        )
        self._select_or_add_float_combo(
            self.press_tone_duration_combo,
            float(settings.talkback_tone_duration_sec),
            lambda v: self._format_duration_option(v, default_sec=1.0),
        )
        self._select_or_add_float_combo(
            self.release_tone_duration_combo,
            float(settings.talkback_release_tone_duration_sec),
            lambda v: self._format_duration_option(v, default_sec=0.5),
        )

        osk_idx = self.osk_combo.findData(bool(settings.on_screen_keyboard))
        self.osk_combo.setCurrentIndex(osk_idx if osk_idx >= 0 else 0)
        self.samba_osk_btn.setVisible(self._is_osk_enabled())

        # Populate bit depth & sample rate for active input device
        self._update_capabilities_for_current_device(use_saved_values=True)
        self._update_mode_ui_state()

    @staticmethod
    def _format_duration_option(sec: float, default_sec: float) -> str:
        val = max(0.0, float(sec))
        if val <= 0.0:
            return "Off (0.0s — Disabled)"
        num_str = f"{val:.1f}" if val == round(val) else f"{val:g}"
        unit = "second" if abs(val - 1.0) < 1e-6 else "seconds"
        suffix = " (Default)" if abs(val - default_sec) < 1e-6 else ""
        return f"{num_str} {unit}{suffix}"

    @staticmethod
    def _select_or_add_float_combo(
        combo: QComboBox, target_val: float, label_fn
    ) -> None:
        combo.blockSignals(True)
        try:
            for idx in range(combo.count()):
                data = combo.itemData(idx)
                if data is not None and abs(float(data) - float(target_val)) < 1e-3:
                    combo.setCurrentIndex(idx)
                    return
            combo.addItem(label_fn(float(target_val)), float(target_val))
            combo.setCurrentIndex(combo.count() - 1)
        finally:
            combo.blockSignals(False)

    def _register_osk_fields(self) -> None:
        self.osk_controller.register_line_edit(
            self.samba_username_edit,
            title="Samba Username",
            hint="Letters, numbers, underscores, or hyphens (max 31 chars)",
            group="samba_credentials",
        )
        self.osk_controller.register_line_edit(
            self.samba_password_edit,
            title="Samba Password",
            hint="Minimum 4 characters",
            group="samba_credentials",
        )
        self.osk_controller.register_line_edit(
            self.samba_password_confirm_edit,
            title="Confirm Samba Password",
            hint="Re-enter password to confirm",
            group="samba_credentials",
        )
        self.osk_controller.attach_to_container(self)

    def _is_osk_enabled(self) -> bool:
        if hasattr(self, "osk_combo") and self.osk_combo.count() > 0:
            return bool(self.osk_combo.currentData())
        return bool(self.config_manager.settings.on_screen_keyboard)

    def _on_osk_setting_changed(self, _index: int) -> None:
        if hasattr(self, "samba_osk_btn"):
            self.samba_osk_btn.setVisible(self._is_osk_enabled())

    def _on_open_samba_osk(self) -> None:
        if not self.samba_username_edit.text().strip():
            target = self.samba_username_edit
        elif not self.samba_password_edit.text():
            target = self.samba_password_edit
        else:
            target = self.samba_password_confirm_edit
        self.osk_controller.open_for_line_edit(target)

    def _on_rescan_devices(self) -> None:
        self._populate_devices_and_settings(force_probe=True)

    def _on_exit_to_console(self) -> None:
        reply = QMessageBox.question(
            self,
            "Exit to Linux Console",
            "Close Alpine Recording Station and return to the Linux console (tty1) for troubleshooting?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.exit_to_console_requested = True
            self.reject()

    def _on_input_device_changed(self, _index: int) -> None:
        if self._initializing:
            return
        # When user selects a hardware interface, default to its maximum bit depth & sample rate
        # unless previously saved in known_device_defaults
        self._update_capabilities_for_current_device(use_saved_values=False)

    def _update_capabilities_for_current_device(self, use_saved_values: bool) -> None:
        settings = self.config_manager.settings
        dev_id = str(self.input_device_combo.currentData() or "")
        dev = self._device_map.get(dev_id)

        self.bit_depth_combo.blockSignals(True)
        self.sample_rate_combo.blockSignals(True)
        try:
            self.bit_depth_combo.clear()
            self.sample_rate_combo.clear()
            self.input_channel_combo.clear()

            if dev is None:
                # Disconnected or no hardware available
                for bd in (16, 24, 32):
                    self.bit_depth_combo.addItem(f"{bd}-bit PCM", bd)
                for sr in (44100, 48000, 96000):
                    self.sample_rate_combo.addItem(f"{sr:,} Hz", sr)
                self.input_channel_combo.addItem("Channel 1 (Default)", 0)
                return

            # Populate supported bit depths (ascending order, defaulting to maximum)
            for bd in sorted(dev.supported_bit_depths):
                self.bit_depth_combo.addItem(f"{bd}-bit PCM", bd)

            # Populate supported sample rates (ascending order, defaulting to maximum)
            for sr in sorted(dev.supported_sample_rates):
                self.sample_rate_combo.addItem(f"{sr:,} Hz", sr)

            # Populate input channels
            max_in_ch = max(1, dev.max_channels)
            for ch_i in range(max_in_ch):
                label = f"Input Channel {ch_i + 1}" + (" (Default)" if ch_i == 0 else "")
                self.input_channel_combo.addItem(label, ch_i)

            # Determine target bit depth & sample rate (default to hardware MAX)
            target_bd = dev.max_bit_depth
            target_sr = dev.max_sample_rate
            target_in_ch = settings.input_channel

            saved_per_dev = settings.known_device_defaults.get(dev_id, {})
            if use_saved_values and settings.bit_depth in dev.supported_bit_depths:
                target_bd = settings.bit_depth
            elif saved_per_dev.get("bit_depth") in dev.supported_bit_depths:
                target_bd = int(saved_per_dev["bit_depth"])

            if use_saved_values and settings.sample_rate in dev.supported_sample_rates:
                target_sr = settings.sample_rate
            elif saved_per_dev.get("sample_rate") in dev.supported_sample_rates:
                target_sr = int(saved_per_dev["sample_rate"])

            bd_idx = self.bit_depth_combo.findData(target_bd)
            self.bit_depth_combo.setCurrentIndex(
                bd_idx if bd_idx >= 0 else self.bit_depth_combo.count() - 1
            )

            sr_idx = self.sample_rate_combo.findData(target_sr)
            self.sample_rate_combo.setCurrentIndex(
                sr_idx if sr_idx >= 0 else self.sample_rate_combo.count() - 1
            )

            in_ch_idx = self.input_channel_combo.findData(target_in_ch)
            self.input_channel_combo.setCurrentIndex(in_ch_idx if in_ch_idx >= 0 else 0)
        finally:
            self.bit_depth_combo.blockSignals(False)
            self.sample_rate_combo.blockSignals(False)

        self._update_storage_preview()

    def _on_mode_changed(self, _index: int) -> None:
        self._update_mode_ui_state()

    def _update_mode_ui_state(self) -> None:
        studio = bool(self.mode_combo.currentData())
        freq_val = float(self.tone_freq_combo.currentData() or 1000.0)
        press_val = float(
            self.press_tone_duration_combo.currentData()
            if self.press_tone_duration_combo.currentData() is not None
            else 1.0
        )
        rel_val = float(
            self.release_tone_duration_combo.currentData()
            if self.release_tone_duration_combo.currentData() is not None
            else 0.5
        )
        freq_str = f"{freq_val:g} Hz"
        press_str = f"{press_val:g}-second" if press_val > 0 else "disabled (0s)"
        rel_str = (
            f"emits a {rel_val:g}-second release tone ({freq_str}) on the Right channel when concluded"
            if rel_val > 0
            else "concludes without a release tone (0s)"
        )

        if studio:
            # Studio mode defaults to and enforces Stereo (Left=Mic, Right=Talkback+Tone)
            stereo_idx = self.channels_combo.findData(2)
            if stereo_idx >= 0:
                self.channels_combo.setCurrentIndex(stereo_idx)
            self.channels_combo.setEnabled(False)
            self.mode_hint_label.setText(
                "Studio Mode Active: Output WAV is Stereo (2-channel). "
                "Microphone is recorded to the Left channel while the Right channel remains silent. "
                f"Holding the Talkback button mutes the Left channel, plays a {press_str} marker tone ({freq_str}) "
                f"into the headset and on the Right channel, records spoken notes on the Right channel while held, and {rel_str} (and into the headset)."
            )
        else:
            self.channels_combo.setEnabled(True)
            self.mode_hint_label.setText(
                "Normal Mode Active: Records microphone directly in either Mono (1-channel) "
                "or Stereo (2-channel) at the selected bit depth and sample rate."
            )

        self._update_storage_preview()

    def _update_storage_preview(self) -> None:
        if not hasattr(self, "storage_info_label"):
            return
        bd = int(self.bit_depth_combo.currentData() or 24)
        sr = int(self.sample_rate_combo.currentData() or 48000)
        studio = bool(self.mode_combo.currentData())
        ch = 2 if studio else int(self.channels_combo.currentData() or 1)

        st = self.config_manager.get_storage_status(
            sample_rate=sr,
            bit_depth=bd,
            channels=ch,
            studio_mode=studio,
        )
        ch_str = "Studio Stereo (2ch)" if studio else ("Stereo (2ch)" if ch >= 2 else "Mono (1ch)")
        if not st.available:
            self.storage_info_label.setText(
                f"Storage ({st.storage_path}): Unavailable"
            )
            self.storage_info_label.setStyleSheet(
                "background-color: #0D1014; color: #6E7681; padding: 8px 10px; "
                "border-radius: 6px; border: 1px solid #21262D; font-family: monospace;"
            )
            return

        prefix_badge = ""
        if st.level == "full":
            fg, bg, border = "#FF6B7A", "#240D11", "#F85149"
            prefix_badge = "⚠ DRIVE FULL — "
        elif st.level == "critical":
            fg, bg, border = "#FF6B7A", "#200E12", "#F85149"
            prefix_badge = "⚠ CRITICAL LOW SPACE — "
        elif st.level == "warning":
            fg, bg, border = "#FFDF5D", "#19150B", "#D29922"
            prefix_badge = "⚠ LOW DISK SPACE — "
        else:
            fg, bg, border = "#58A6FF", "#0D1014", "#1F6FEB"

        self.storage_info_label.setText(
            f"{prefix_badge}/storage Utilization: {st.used_percent:.1f}% used "
            f"({st.formatted_free} free of {st.formatted_total})  •  "
            f"Rate: {st.mb_per_minute:.1f} MB/min ({bd}-bit / {sr:,} Hz / {ch_str})  •  "
            f"Time Remaining: {st.formatted_remaining}"
        )
        self.storage_info_label.setStyleSheet(
            f"background-color: {bg}; color: {fg}; padding: 8px 10px; "
            f"border-radius: 6px; border: 1px solid {border}; font-family: monospace; font-weight: bold;"
        )

    def _on_save_settings(self) -> None:
        s = self.config_manager.settings
        dev_id = str(self.input_device_combo.currentData() or "")
        s.input_device = dev_id
        s.output_device = str(self.output_device_combo.currentData() or "same")
        s.bit_depth = int(self.bit_depth_combo.currentData() or 16)
        s.sample_rate = int(self.sample_rate_combo.currentData() or 48000)
        s.studio_mode = bool(self.mode_combo.currentData())
        s.channels = 2 if s.studio_mode else int(self.channels_combo.currentData() or 1)
        s.input_channel = int(self.input_channel_combo.currentData() or 0)
        s.talkback_tone_hz = float(
            self.tone_freq_combo.currentData()
            if self.tone_freq_combo.currentData() is not None
            else 1000.0
        )
        s.talkback_tone_duration_sec = float(
            self.press_tone_duration_combo.currentData()
            if self.press_tone_duration_combo.currentData() is not None
            else 1.0
        )
        s.talkback_release_tone_duration_sec = float(
            self.release_tone_duration_combo.currentData()
            if self.release_tone_duration_combo.currentData() is not None
            else 0.5
        )
        s.on_screen_keyboard = self._is_osk_enabled()

        if dev_id:
            s.known_device_defaults[dev_id] = {
                "bit_depth": s.bit_depth,
                "sample_rate": s.sample_rate,
            }

        self.config_manager.save(commit_lbu=True)
        self.accept()

    # -------------------------------------------------------------------------
    # Samba User Administration Logic
    # -------------------------------------------------------------------------
    def _refresh_samba_users(self) -> None:
        self.samba_users_list.clear()
        for user in self.samba_manager.list_users():
            self.samba_users_list.addItem(user)

    def _on_samba_user_selected(self, username: str) -> None:
        if username:
            self.samba_username_edit.setText(username)

    def _on_add_samba_user(self) -> None:
        uname = self.samba_username_edit.text().strip()
        pw = self.samba_password_edit.text()
        pw2 = self.samba_password_confirm_edit.text()

        if pw != pw2:
            self._set_samba_status("Passwords do not match.", error=True)
            return

        ok, msg = self.samba_manager.add_user(uname, pw)
        self._set_samba_status(msg, error=not ok)
        if ok:
            self.samba_password_edit.clear()
            self.samba_password_confirm_edit.clear()
            self._refresh_samba_users()

    def _on_change_samba_password(self) -> None:
        uname = self.samba_username_edit.text().strip()
        pw = self.samba_password_edit.text()
        pw2 = self.samba_password_confirm_edit.text()

        if pw != pw2:
            self._set_samba_status("Passwords do not match.", error=True)
            return

        ok, msg = self.samba_manager.change_password(uname, pw)
        self._set_samba_status(msg, error=not ok)
        if ok:
            self.samba_password_edit.clear()
            self.samba_password_confirm_edit.clear()

    def _on_remove_samba_user(self) -> None:
        item = self.samba_users_list.currentItem()
        uname = item.text() if item else self.samba_username_edit.text().strip()
        if not uname:
            self._set_samba_status("Select a user to remove first.", error=True)
            return

        reply = QMessageBox.question(
            self,
            "Confirm Remove Samba User",
            f"Remove Samba share access for user '{uname}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        ok, msg = self.samba_manager.remove_user(uname)
        self._set_samba_status(msg, error=not ok)
        if ok:
            self.samba_username_edit.clear()
            self._refresh_samba_users()

    def _set_samba_status(self, message: str, error: bool = False) -> None:
        color = "#F85149" if error else "#3FB950"
        self.samba_status_label.setStyleSheet(
            f"color: {color}; font-weight: bold; margin-top: 8px;"
        )
        self.samba_status_label.setText(message)
