"""
Remote Producer (Guided VO) Setup, QR Code & SSL Dialog
=======================================================
Provides the local studio talent with:
  - A scannable QR code and copyable HTTPS link for the active session
  - Session ID & 6-digit Session Passcode (PIN) display
  - One-click Session Rotation (revokes prior tokens/cookies immediately)
  - Talent Mic Send (Privacy / Cough) control
  - SSL Certificate status, easy replacement from /storage/ssl or custom
    paths, and self-signed certificate regeneration
"""

from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QGuiApplication, QPainter
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.audio_engine import AudioEngine
from app.config_manager import ConfigManager
from app.network_info import get_lan_ip_addresses
from app.qr_code import encode_qr_matrix
from app.remote_producer import RemoteProducerServer
from app.ui.on_screen_keyboard import OnScreenKeyboardController
from app.ui.styles import (
    build_main_stylesheet,
    compute_scale_factor,
    configure_combo_box_popup,
)


class QrCodeWidget(QWidget):
    """Renders a crisp ISO/IEC 18004 QR code matrix using QPainter."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._matrix: List[List[bool]] = []
        self.setMinimumSize(188, 188)
        self.setMaximumSize(220, 220)

    def set_url(self, url: str) -> None:
        if not url:
            self._matrix = []
        else:
            try:
                self._matrix = encode_qr_matrix(url)
            except Exception:
                self._matrix = []
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        w = self.width()
        h = self.height()
        painter.fillRect(0, 0, w, h, QColor("#FFFFFF"))

        if not self._matrix:
            painter.setPen(QColor("#111418"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "QR Offline")
            painter.end()
            return

        size = len(self._matrix)
        border = 3
        total_modules = size + border * 2
        side = min(w, h)
        scale = max(1, side // total_modules)
        qr_px = total_modules * scale
        ox = (w - qr_px) // 2
        oy = (h - qr_px) // 2

        dark = QColor("#0D1014")
        for r, row in enumerate(self._matrix):
            for c, val in enumerate(row):
                if val:
                    painter.fillRect(
                        ox + (c + border) * scale,
                        oy + (r + border) * scale,
                        scale,
                        scale,
                        dark,
                    )
        painter.end()


class RemoteProducerDialog(QDialog):
    """
    Modal dialog for managing the Guided VO Remote Producer session,
    QR code / invite link, talent mic privacy, and SSL certificates.
    """

    def __init__(
        self,
        remote_server: RemoteProducerServer,
        audio_engine: AudioEngine,
        config_manager: ConfigManager,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.remote_server = remote_server
        self.audio_engine = audio_engine
        self.config_manager = config_manager

        self.setWindowTitle("Remote Producer — Guided VO Session & SSL Setup")
        self.setModal(True)
        self.setMinimumSize(860, 560)
        if parent is not None and parent.width() >= 480:
            pw, ph = parent.width(), parent.height()
            self._scale = compute_scale_factor(pw, ph)
            self.resize(max(860, int(pw * 0.82)), max(560, int(ph * 0.84)))
        else:
            self._scale = 1.0
            self.resize(900, 620)

        self.osk_controller = OnScreenKeyboardController(
            is_enabled=lambda: bool(self.config_manager.settings.on_screen_keyboard),
            parent=self,
        )
        self._build_ui()
        self.setStyleSheet(build_main_stylesheet(self._scale))
        for combo in self.findChildren(QComboBox):
            configure_combo_box_popup(combo, self._scale)
        self.osk_controller.attach_to_container(self)
        self._refresh_all()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(500)
        self._poll_timer.timeout.connect(self._refresh_live_status)
        self._poll_timer.start()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # Header Row
        hdr_row = QHBoxLayout()
        title = QLabel("◎ Remote Producer (Guided VO) Session Setup")
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #F0F6FC;")
        hdr_row.addWidget(title)
        hdr_row.addStretch(1)

        self.status_badge = QLabel("● Server Offline")
        self.status_badge.setStyleSheet(
            "background-color: #21262D; color: #C9D1D9; border: 1px solid #30363D; "
            "border-radius: 6px; padding: 5px 12px; font-size: 12px; font-weight: 700;"
        )
        hdr_row.addWidget(self.status_badge)
        root.addLayout(hdr_row)

        # Main Two-Column Grid
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        # LEFT CARD: QR Code, Session Link & Session Credentials
        session_card = QFrame()
        session_card.setObjectName("CardFrame")
        s_layout = QVBoxLayout(session_card)
        s_layout.setContentsMargins(14, 12, 14, 12)
        s_layout.setSpacing(8)

        s_hdr = QLabel("1. SESSION ACCESS, QR CODE && AUTHENTICATION")
        s_hdr.setObjectName("SectionHeader")
        s_layout.addWidget(s_hdr)

        # Server toggle + Host IP + Port row
        srv_row = QHBoxLayout()
        self.chk_enable_server = QCheckBox("Enable Remote Producer HTTPS Server")
        self.chk_enable_server.setChecked(
            bool(self.remote_server.is_running or self.config_manager.settings.remote_producer_enabled)
        )
        self.chk_enable_server.toggled.connect(self._on_toggle_server)
        srv_row.addWidget(self.chk_enable_server)

        srv_row.addStretch(1)
        srv_row.addWidget(QLabel("Port:"))
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1024, 65535)
        self.spin_port.setValue(int(self.config_manager.settings.remote_producer_port or 8443))
        self.spin_port.valueChanged.connect(self._on_port_changed)
        srv_row.addWidget(self.spin_port)
        s_layout.addLayout(srv_row)

        # QR + Credentials side-by-side
        qr_row = QHBoxLayout()
        qr_row.setSpacing(14)

        self.qr_widget = QrCodeWidget(self)
        qr_row.addWidget(self.qr_widget, stretch=0)

        cred_col = QVBoxLayout()
        cred_col.setSpacing(6)

        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Host IP:"))
        self.host_combo = QComboBox()
        ips = get_lan_ip_addresses()
        for ip in ips:
            self.host_combo.addItem(ip, ip)
        if "127.0.0.1" not in ips:
            self.host_combo.addItem("127.0.0.1 (Local)", "127.0.0.1")
        self.host_combo.currentIndexChanged.connect(self._refresh_qr_and_link)
        host_row.addWidget(self.host_combo, stretch=1)
        cred_col.addLayout(host_row)

        self.lbl_session_id = QLabel("Session ID: --")
        self.lbl_session_id.setStyleSheet(
            "font-family: 'DejaVu Sans Mono', monospace; font-size: 14px; font-weight: 700; color: #79C0FF;"
        )
        cred_col.addWidget(self.lbl_session_id)

        self.lbl_passcode = QLabel("Session Passcode (PIN): ------")
        self.lbl_passcode.setStyleSheet(
            "font-family: 'DejaVu Sans Mono', monospace; font-size: 16px; font-weight: 700; "
            "color: #7EE787; background-color: #0D1014; border: 1px solid #2EA043; "
            "border-radius: 6px; padding: 6px 10px;"
        )
        cred_col.addWidget(self.lbl_passcode)

        self.chk_embed_token = QCheckBox("Include one-step invite token in QR && Link")
        self.chk_embed_token.setChecked(True)
        self.chk_embed_token.toggled.connect(self._refresh_qr_and_link)
        cred_col.addWidget(self.chk_embed_token)

        self.btn_rotate_session = QPushButton("↻ New Session && Rotate Passcode")
        self.btn_rotate_session.setMinimumHeight(36)
        self.btn_rotate_session.clicked.connect(self._on_rotate_session)
        cred_col.addWidget(self.btn_rotate_session)

        cred_col.addStretch(1)
        qr_row.addLayout(cred_col, stretch=1)
        s_layout.addLayout(qr_row)

        # Session URL + Copy Button
        link_lbl = QLabel("Producer Session URL (HTTPS):")
        link_lbl.setStyleSheet("font-size: 12px; color: #8B949E; font-weight: 600;")
        s_layout.addWidget(link_lbl)

        link_row = QHBoxLayout()
        self.link_edit = QLineEdit()
        self.link_edit.setReadOnly(True)
        self.link_edit.setStyleSheet("font-family: 'DejaVu Sans Mono', monospace; font-size: 12px;")
        link_row.addWidget(self.link_edit, stretch=1)

        self.btn_copy_link = QPushButton("Copy Link")
        self.btn_copy_link.setMinimumHeight(34)
        self.btn_copy_link.clicked.connect(self._on_copy_link)
        link_row.addWidget(self.btn_copy_link)
        s_layout.addLayout(link_row)

        grid.addWidget(session_card, 0, 0)

        # RIGHT COLUMN: Talent Privacy / Cough + SSL Certificates
        right_col = QVBoxLayout()
        right_col.setSpacing(10)

        # Privacy & Cough Card
        privacy_card = QFrame()
        privacy_card.setObjectName("CardFrame")
        p_layout = QVBoxLayout(privacy_card)
        p_layout.setContentsMargins(14, 12, 14, 12)
        p_layout.setSpacing(8)

        p_hdr = QLabel("2. TALENT MIC PRIVACY && COUGH CONTROL")
        p_hdr.setObjectName("SectionHeader")
        p_layout.addWidget(p_hdr)

        self.chk_send_talent_mic = QCheckBox(
            "Send Talent Microphone Live to Remote Producer"
        )
        self.chk_send_talent_mic.setChecked(bool(self.audio_engine.talent_mic_send_enabled))
        self.chk_send_talent_mic.toggled.connect(self._on_toggle_talent_mic_send)
        p_layout.addWidget(self.chk_send_talent_mic)

        self.lbl_privacy_status = QLabel("")
        self.lbl_privacy_status.setWordWrap(True)
        self.lbl_privacy_status.setStyleSheet("font-size: 12px; color: #8B949E;")
        p_layout.addWidget(self.lbl_privacy_status)

        right_col.addWidget(privacy_card)

        # SSL Certificate Management Card
        ssl_card = QFrame()
        ssl_card.setObjectName("CardFrame")
        ssl_layout = QVBoxLayout(ssl_card)
        ssl_layout.setContentsMargins(14, 12, 14, 12)
        ssl_layout.setSpacing(8)

        ssl_hdr = QLabel("3. SSL / TLS CERTIFICATE MANAGEMENT")
        ssl_hdr.setObjectName("SectionHeader")
        ssl_layout.addWidget(ssl_hdr)

        self.lbl_cert_summary = QLabel("Checking SSL certificate...")
        self.lbl_cert_summary.setWordWrap(True)
        self.lbl_cert_summary.setStyleSheet(
            "background-color: #0D1014; border: 1px solid #262C36; border-radius: 6px; "
            "padding: 8px 10px; font-family: 'DejaVu Sans Mono', monospace; font-size: 11px; color: #C9D1D9;"
        )
        ssl_layout.addWidget(self.lbl_cert_summary)

        ssl_btn_row = QHBoxLayout()
        self.btn_import_drop = QPushButton("Import Issued Cert (/storage/ssl)")
        self.btn_import_drop.setMinimumHeight(34)
        self.btn_import_drop.clicked.connect(self._on_import_drop_ssl)
        ssl_btn_row.addWidget(self.btn_import_drop)

        self.btn_regen_self_signed = QPushButton("↺ Regenerate Self-Signed Cert")
        self.btn_regen_self_signed.setMinimumHeight(34)
        self.btn_regen_self_signed.clicked.connect(self._on_regenerate_self_signed)
        ssl_btn_row.addWidget(self.btn_regen_self_signed)
        ssl_layout.addLayout(ssl_btn_row)

        cert_form = QFormLayout()
        cert_form.setSpacing(6)
        self.cert_path_edit = QLineEdit(self.config_manager.settings.ssl_cert_path or "")
        self.cert_path_edit.setPlaceholderText("Optional custom fullchain/cert.pem path")
        self.osk_controller.register_line_edit(
            self.cert_path_edit, title="Custom SSL Certificate Path (.pem / .crt)"
        )
        cert_form.addRow("Cert Path:", self.cert_path_edit)

        self.key_path_edit = QLineEdit(self.config_manager.settings.ssl_key_path or "")
        self.key_path_edit.setPlaceholderText("Optional custom privkey/key.pem path")
        self.osk_controller.register_line_edit(
            self.key_path_edit, title="Custom SSL Private Key Path (.key / .pem)"
        )
        cert_form.addRow("Key Path:", self.key_path_edit)
        ssl_layout.addLayout(cert_form)

        self.btn_apply_custom_ssl = QPushButton("Install && Apply Custom Cert Paths")
        self.btn_apply_custom_ssl.setMinimumHeight(34)
        self.btn_apply_custom_ssl.clicked.connect(self._on_apply_custom_ssl)
        ssl_layout.addWidget(self.btn_apply_custom_ssl)

        right_col.addWidget(ssl_card)
        grid.addLayout(right_col, 0, 1)
        grid.setColumnStretch(0, 11)
        grid.setColumnStretch(1, 10)
        root.addLayout(grid, stretch=1)

        # Feedback Banner + Close Button
        self.feedback_label = QLabel(
            "Tip: Drop issued server.crt && server.key into the Samba share folder 'ssl/' (/storage/ssl) "
            "and click 'Import Issued Cert' to replace the default self-signed certificate."
        )
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setStyleSheet("font-size: 12px; color: #58A6FF;")
        root.addWidget(self.feedback_label)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        close_btn = QPushButton("Done && Close")
        close_btn.setObjectName("PrimaryDialogButton")
        close_btn.setMinimumHeight(40)
        close_btn.setMinimumWidth(160)
        close_btn.clicked.connect(self.accept)
        bottom_row.addWidget(close_btn)
        root.addLayout(bottom_row)

    # ------------------------------------------------------------------
    # Refresh & Actions
    # ------------------------------------------------------------------
    def _refresh_all(self) -> None:
        self._refresh_qr_and_link()
        self._refresh_ssl_info()
        self._refresh_live_status()

    def _refresh_qr_and_link(self) -> None:
        host = self.host_combo.currentData() or self.remote_server.get_primary_host()
        include_token = self.chk_embed_token.isChecked()
        url = self.remote_server.get_session_url(
            include_invite_token=include_token, host_override=str(host)
        )
        self.link_edit.setText(url)
        self.qr_widget.set_url(url)
        self.lbl_session_id.setText(f"Session ID: {self.remote_server.session.session_id}")
        self.lbl_passcode.setText(
            f"Session Passcode (PIN): {self.remote_server.session.session_passcode}"
        )

    def _refresh_ssl_info(self) -> None:
        info = self.remote_server.certificate_info
        mode_label = "Self-Signed (Default)" if info.mode == "self-signed" else "Issued / Custom Certificate"
        valid_str = "✓ Valid" if info.valid else f"⚠ {info.error or 'Invalid'}"
        sans = ", ".join(info.san_entries[:4]) if info.san_entries else "localhost"
        lines = [
            f"Status: {valid_str}  |  Mode: {mode_label}",
            f"Subject: {info.subject or 'CN=Alpine-Recording-Station'}",
            f"SANs: {sans}",
            f"Expires: {info.not_after or 'N/A'}",
            f"SHA-256: {info.fingerprint_sha256[:35]}..." if info.fingerprint_sha256 else "SHA-256: --",
        ]
        self.lbl_cert_summary.setText("\n".join(lines))

    def _refresh_live_status(self) -> None:
        if self.remote_server.is_running:
            count = self.remote_server.connected_client_count
            if self.audio_engine.remote_talkback_active:
                self.status_badge.setText(
                    f"⚡ Producer Talkback ON AIR ({count} Connected)"
                )
                self.status_badge.setStyleSheet(
                    "background-color: #3B2300; color: #FFDF5D; border: 1px solid #F0883E; "
                    "border-radius: 6px; padding: 5px 12px; font-size: 12px; font-weight: 700;"
                )
            elif count > 0:
                self.status_badge.setText(
                    f"● HTTPS Active (:{self.remote_server.port}) — {count} Producer(s) Connected"
                )
                self.status_badge.setStyleSheet(
                    "background-color: #0F2D1E; color: #7EE787; border: 1px solid #2EA043; "
                    "border-radius: 6px; padding: 5px 12px; font-size: 12px; font-weight: 700;"
                )
            else:
                self.status_badge.setText(
                    f"◎ HTTPS Ready on Port {self.remote_server.port} — Waiting for Producer"
                )
                self.status_badge.setStyleSheet(
                    "background-color: #161F2C; color: #79C0FF; border: 1px solid #388BFD; "
                    "border-radius: 6px; padding: 5px 12px; font-size: 12px; font-weight: 700;"
                )
        else:
            self.status_badge.setText("○ Server Disabled (Independent Studio Mode)")
            self.status_badge.setStyleSheet(
                "background-color: #21262D; color: #8B949E; border: 1px solid #30363D; "
                "border-radius: 6px; padding: 5px 12px; font-size: 12px; font-weight: 700;"
            )

        if self.audio_engine.is_talent_mic_live_to_producer:
            self.lbl_privacy_status.setText(
                "◉ Status: Talent Mic is LIVE to connected remote producers. "
                "You can also mute or hold-to-cough anytime from the main Studio Deck [B]."
            )
            self.lbl_privacy_status.setStyleSheet("font-size: 12px; color: #7EE787;")
        else:
            self.lbl_privacy_status.setText(
                "○ Status: Talent Mic is MUTED for privacy / cough. Remote producers receive "
                "silence while your local recording continues uninterrupted."
            )
            self.lbl_privacy_status.setStyleSheet("font-size: 12px; color: #FFA198;")

    def _on_toggle_server(self, checked: bool) -> None:
        port = int(self.spin_port.value())
        self.config_manager.settings.remote_producer_enabled = bool(checked)
        self.config_manager.settings.remote_producer_port = port
        self.config_manager.save(commit_lbu=True)
        if checked:
            ok = self.remote_server.start(port=port)
            if ok:
                self.feedback_label.setText(
                    f"✓ Remote Producer HTTPS server started on port {self.remote_server.port}."
                )
            else:
                self.feedback_label.setText(
                    "⚠ Could not bind HTTPS server port. Check if another process is using that port."
                )
        else:
            self.remote_server.stop()
            self.feedback_label.setText(
                "Remote Producer server stopped. Station is operating independently."
            )
        self._refresh_all()

    def _on_port_changed(self, value: int) -> None:
        self.config_manager.settings.remote_producer_port = int(value)
        self.config_manager.save(commit_lbu=True)
        if self.remote_server.is_running:
            self.remote_server.stop()
            self.remote_server.start(port=int(value))
        self._refresh_all()

    def _on_rotate_session(self) -> None:
        self.remote_server.rotate_session()
        self._refresh_qr_and_link()
        self.feedback_label.setText(
            f"✓ Session rotated to {self.remote_server.session.session_id}. "
            "Previous QR codes, links, and connected sessions have been revoked."
        )

    def _on_copy_link(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.link_edit.text())
            self.feedback_label.setText("✓ Session HTTPS link copied to clipboard.")

    def _on_toggle_talent_mic_send(self, checked: bool) -> None:
        self.audio_engine.set_talent_mic_send_enabled(bool(checked))
        self.config_manager.settings.remote_producer_send_talent_mic = bool(checked)
        self.config_manager.save(commit_lbu=True)
        self._refresh_live_status()

    def _on_import_drop_ssl(self) -> None:
        ok, msg = self.remote_server.ssl_manager.import_from_drop_dir()
        if ok:
            self.config_manager.settings.ssl_cert_path = ""
            self.config_manager.settings.ssl_key_path = ""
            self.config_manager.save(commit_lbu=True)
            self.cert_path_edit.setText("")
            self.key_path_edit.setText("")
            self.remote_server.reload_ssl_certificates()
            self.feedback_label.setText(f"✓ {msg}")
        else:
            self.feedback_label.setText(f"⚠ {msg}")
        self._refresh_ssl_info()

    def _on_regenerate_self_signed(self) -> None:
        ok, msg = self.remote_server.ssl_manager.ensure_certificates(force_regenerate=True)
        if ok:
            self.config_manager.settings.ssl_cert_path = ""
            self.config_manager.settings.ssl_key_path = ""
            self.config_manager.save(commit_lbu=True)
            self.cert_path_edit.setText("")
            self.key_path_edit.setText("")
            self.remote_server.reload_ssl_certificates()
            self.feedback_label.setText(f"✓ {msg}")
        else:
            self.feedback_label.setText(f"⚠ {msg}")
        self._refresh_ssl_info()

    def _on_apply_custom_ssl(self) -> None:
        cpath = self.cert_path_edit.text().strip()
        kpath = self.key_path_edit.text().strip()
        if not cpath or not kpath:
            self.feedback_label.setText(
                "⚠ Enter both a certificate path and private key path, or use Import (/storage/ssl)."
            )
            return
        ok, msg = self.remote_server.ssl_manager.install_custom_certificate(
            Path(cpath), Path(kpath)
        )
        if ok:
            self.config_manager.settings.ssl_cert_path = ""
            self.config_manager.settings.ssl_key_path = ""
            self.config_manager.save(commit_lbu=True)
            self.remote_server.reload_ssl_certificates()
            self.feedback_label.setText(f"✓ {msg}")
        else:
            self.feedback_label.setText(f"⚠ {msg}")
        self._refresh_ssl_info()
