"""
Remote Producer (Guided VO) HTTPS & Low-Latency WebSocket Server
================================================================
Provides a stripped-down, dark-studio-themed web console for a remote
producer to:
  1. Authenticate against a session-bound credential (via scannable QR code
     link or Session ID + 6-digit Session Passcode).
  2. Listen live with low latency to the talent's microphone performance
     (respecting the talent's Mic Send / Cough privacy control).
  3. Control their own Mic Send (Privacy / Cough switch + momentary Cough
     mute) so the producer's microphone is never unexpectedly hot.
  4. Use the momentary Talkback button (or hold [T] / [Space]) to prompt
     the talent in real time; when the talent is recording in Studio Mode,
     the remote producer's voice is recorded onto the Right (Talkback)
     channel between the configured press/release marker tones.

Served over HTTPS/WSS by default using SslCertificateManager (auto-generated
self-signed X.509 v3 certificate out-of-the-box, easily replaceable via
/storage/ssl or custom certificate paths).
"""

import base64
import hashlib
import hmac
import http.cookies
import json
import logging
import math
import secrets
import socket
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from app.audio_engine import AudioEngine, resample_mono_signal
from app.config_manager import ConfigManager
from app.network_info import get_lan_ip_addresses
from app.qr_code import encode_qr_matrix, qr_matrix_to_svg
from app.ssl_manager import CertificateInfo, SslCertificateManager

logger = logging.getLogger("AlpineRecorder.RemoteProducer")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
STREAM_SAMPLE_RATE = 24000  # 24 kHz low-latency wideband voice stream

# Binary WebSocket frame headers
FRAME_TALENT_AUDIO = 0x01     # Server -> Producer: [0x01, flags, sample_rate_div100_le16, int16_pcm...]
FRAME_PRODUCER_AUDIO = 0x02   # Producer -> Server: [0x02, flags, sample_rate_div100_le16, int16_pcm...]


@dataclass
class ProducerSession:
    """
    Session-tied authentication state. Rotating the session generates a new
    session_id, passcode, invite_token, and HMAC secret, immediately
    invalidating any previous cookies, QR codes, or active WebSocket streams.
    """
    session_id: str = field(default_factory=lambda: f"ARS-{secrets.token_hex(2).upper()}")
    session_passcode: str = field(default_factory=lambda: f"{secrets.randbelow(900000) + 100000:06d}")
    invite_token: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    hmac_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    created_at: float = field(default_factory=time.time)

    def rotate(self) -> None:
        self.session_id = f"ARS-{secrets.token_hex(2).upper()}"
        self.session_passcode = f"{secrets.randbelow(900000) + 100000:06d}"
        self.invite_token = secrets.token_urlsafe(18)
        self.hmac_secret = secrets.token_bytes(32)
        self.created_at = time.time()

    def create_auth_token(self, ttl_seconds: int = 43200) -> str:
        exp = int(time.time()) + ttl_seconds
        nonce = secrets.token_hex(8)
        payload = f"{self.session_id}:{exp}:{nonce}"
        sig = hmac.new(self.hmac_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def verify_auth_token(self, token: str) -> bool:
        if not token or ":" not in token:
            return False
        parts = token.split(":")
        if len(parts) != 4:
            return False
        sid, exp_str, nonce, provided_sig = parts
        if not hmac.compare_digest(sid, self.session_id):
            return False
        try:
            exp = int(exp_str)
        except ValueError:
            return False
        if time.time() > exp:
            return False
        payload = f"{sid}:{exp_str}:{nonce}"
        expected_sig = hmac.new(self.hmac_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided_sig, expected_sig)

    def verify_credentials(self, passcode: str = "", invite_token: str = "") -> bool:
        clean_passcode = (passcode or "").strip().replace(" ", "").replace("-", "")
        clean_invite = (invite_token or "").strip()
        if clean_invite and hmac.compare_digest(clean_invite, self.invite_token):
            return True
        if clean_passcode and hmac.compare_digest(clean_passcode, self.session_passcode):
            return True
        return False


class WebSocketConnection:
    """Minimal, thread-safe RFC 6455 WebSocket connection wrapper."""

    def __init__(self, sock: socket.socket, client_address: Tuple[str, int], session_id: str):
        self.sock = sock
        self.client_address = client_address
        self.session_id = session_id
        self.client_id = secrets.token_hex(4)
        self.connected_at = time.time()
        self.send_lock = threading.Lock()
        self.closed = False
        # Producer client mic & talkback state
        self.mic_send_enabled = False  # Off by default so producer mic is not hot
        self.cough_muted = False
        self.talkback_active = False
        self.listen_enabled = True

    def send_text(self, text: str) -> bool:
        data = text.encode("utf-8")
        return self._send_frame(0x1, data)

    def send_binary(self, payload: bytes) -> bool:
        return self._send_frame(0x2, payload)

    def send_pong(self, payload: bytes = b"") -> bool:
        return self._send_frame(0xA, payload[:125])

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def _send_frame(self, opcode: int, payload: bytes) -> bool:
        if self.closed:
            return False
        length = len(payload)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))

        with self.send_lock:
            try:
                self.sock.sendall(bytes(header) + payload)
                return True
            except Exception:
                self.closed = True
                return False


def _read_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            continue
        except Exception:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def read_ws_frame(sock: socket.socket, max_payload: int = 262144) -> Optional[Tuple[int, bytes]]:
    """Read a single RFC 6455 client-to-server frame."""
    hdr = _read_exact(sock, 2)
    if not hdr:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    if length == 126:
        ext = _read_exact(sock, 2)
        if not ext:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _read_exact(sock, 8)
        if not ext:
            return None
        length = struct.unpack("!Q", ext)[0]

    if length > max_payload:
        return None

    mask_key = b""
    if masked:
        mask_key = _read_exact(sock, 4)
        if not mask_key:
            return None

    payload = _read_exact(sock, length) if length > 0 else b""
    if payload is None:
        return None

    if masked and payload:
        unmasked = bytearray(length)
        for i in range(length):
            unmasked[i] = payload[i] ^ mask_key[i & 3]
        payload = bytes(unmasked)

    return opcode, payload


class _SecureProducerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class RemoteProducerServer:
    """
    HTTPS + WebSocket Server for Guided VO Remote Producer sessions.
    """

    def __init__(
        self,
        audio_engine: AudioEngine,
        config_manager: ConfigManager,
        ssl_manager: Optional[SslCertificateManager] = None,
        status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        host: str = "0.0.0.0",
        port: Optional[int] = None,
    ):
        self.audio_engine = audio_engine
        self.config_manager = config_manager
        self.ssl_manager = ssl_manager or SslCertificateManager(
            config_manager=config_manager,
            cert_dir=Path(config_manager.etc_config_dir) / "ssl",
            drop_dir=Path(config_manager.storage_dir) / "ssl",
        )
        self.status_provider = status_provider
        self.host = host
        self._configured_port = (
            port if port is not None else int(getattr(config_manager.settings, "remote_producer_port", 8443))
        )

        self.session = ProducerSession()
        self._httpd: Optional[_SecureProducerHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._broadcast_thread: Optional[threading.Thread] = None
        self._running = threading.Event()

        self._clients_lock = threading.Lock()
        self._clients: Dict[str, WebSocketConnection] = {}
        self._cert_info: Optional[CertificateInfo] = None

    @property
    def is_running(self) -> bool:
        return self._running.is_set() and self._httpd is not None

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_port)
        return int(self._configured_port)

    @property
    def connected_client_count(self) -> int:
        with self._clients_lock:
            return sum(1 for c in self._clients.values() if not c.closed)

    @property
    def certificate_info(self) -> CertificateInfo:
        if self._cert_info is None:
            custom_cert = getattr(self.config_manager.settings, "ssl_cert_path", "") or None
            custom_key = getattr(self.config_manager.settings, "ssl_key_path", "") or None
            self._cert_info = self.ssl_manager.get_certificate_info(custom_cert, custom_key)
        return self._cert_info

    def get_primary_host(self) -> str:
        ips = get_lan_ip_addresses()
        return ips[0] if ips else "127.0.0.1"

    def get_session_url(self, include_invite_token: bool = True, host_override: Optional[str] = None) -> str:
        host = host_override or self.get_primary_host()
        base = f"https://{host}:{self.port}/"
        if include_invite_token:
            query = urllib.parse.urlencode({
                "session": self.session.session_id,
                "token": self.session.invite_token,
            })
            return f"{base}?{query}"
        return base

    def get_qr_svg(self, include_invite_token: bool = True, host_override: Optional[str] = None) -> str:
        url = self.get_session_url(include_invite_token=include_invite_token, host_override=host_override)
        matrix = encode_qr_matrix(url)
        return qr_matrix_to_svg(matrix, border=3)

    def get_qr_matrix(self, include_invite_token: bool = True, host_override: Optional[str] = None) -> List[List[bool]]:
        url = self.get_session_url(include_invite_token=include_invite_token, host_override=host_override)
        return encode_qr_matrix(url)

    def rotate_session(self) -> ProducerSession:
        """
        Rotate session ID, passcode, and invite token, disconnecting any
        existing remote producer clients and releasing remote talkback.
        """
        self.session.rotate()
        self._disconnect_all_clients()
        self.audio_engine.set_remote_talkback(False)
        logger.info("Rotated remote producer session -> %s", self.session.session_id)
        return self.session

    def reload_ssl_certificates(self) -> CertificateInfo:
        """Reload SSL certificates (and restart listener if running)."""
        custom_cert = getattr(self.config_manager.settings, "ssl_cert_path", "") or None
        custom_key = getattr(self.config_manager.settings, "ssl_key_path", "") or None
        self._cert_info = self.ssl_manager.get_certificate_info(custom_cert, custom_key)
        if self.is_running:
            port = self._configured_port
            self.stop()
            self.start(port=port)
        return self._cert_info

    def start(self, port: Optional[int] = None) -> bool:
        if port is not None:
            self._configured_port = int(port)
        if self.is_running:
            return True

        try:
            custom_cert = getattr(self.config_manager.settings, "ssl_cert_path", "") or None
            custom_key = getattr(self.config_manager.settings, "ssl_key_path", "") or None
            ssl_ctx = self.ssl_manager.create_ssl_context(custom_cert, custom_key)
            self._cert_info = self.ssl_manager.get_certificate_info(custom_cert, custom_key)

            server_ref = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, fmt: str, *args: Any) -> None:
                    logger.debug("RemoteProducerHTTP: " + fmt, *args)

                def do_GET(self) -> None:
                    server_ref._handle_http_get(self)

                def do_POST(self) -> None:
                    server_ref._handle_http_post(self)

            httpd = _SecureProducerHTTPServer((self.host, self._configured_port), Handler)
            httpd.socket = ssl_ctx.wrap_socket(httpd.socket, server_side=True)
            self._httpd = httpd
            self._running.set()

            self.audio_engine.register_live_audio_listener(self._on_live_talent_audio)

            self._server_thread = threading.Thread(
                target=self._serve_forever, daemon=True, name="RemoteProducerHTTPS"
            )
            self._server_thread.start()

            self._broadcast_thread = threading.Thread(
                target=self._state_broadcast_loop, daemon=True, name="RemoteProducerStateBroadcast"
            )
            self._broadcast_thread.start()

            logger.info(
                "Remote Producer HTTPS server started on https://%s:%d (session %s)",
                self.get_primary_host(),
                self.port,
                self.session.session_id,
            )
            return True
        except Exception as exc:
            logger.error("Failed to start Remote Producer HTTPS server: %s", exc)
            self._running.clear()
            self._httpd = None
            return False

    def stop(self) -> None:
        self._running.clear()
        self.audio_engine.unregister_live_audio_listener(self._on_live_talent_audio)
        self.audio_engine.set_remote_talkback(False)
        self._disconnect_all_clients()

        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=1.5)
        self._server_thread = None

    def _serve_forever(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        try:
            httpd.serve_forever(poll_interval=0.2)
        except Exception as exc:
            if self._running.is_set():
                logger.debug("HTTPS server loop exited: %s", exc)

    def _disconnect_all_clients(self) -> None:
        with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()

    # ------------------------------------------------------------------
    # Audio & State Streaming
    # ------------------------------------------------------------------
    def _on_live_talent_audio(self, mono_samples: np.ndarray, sample_rate: int, is_muted: bool) -> None:
        """
        Receive live talent input block from AudioEngine, downsample to
        24 kHz Int16LE, and broadcast to listening remote producer clients.
        """
        if not self._running.is_set():
            return
        with self._clients_lock:
            listeners = [
                c for c in self._clients.values()
                if not c.closed and c.listen_enabled
            ]
        if not listeners:
            return

        if is_muted or mono_samples.size == 0:
            n_target = max(1, int(round(len(mono_samples) * STREAM_SAMPLE_RATE / max(1, sample_rate))))
            pcm_int16 = np.zeros(n_target, dtype="<i2")
        else:
            resampled = resample_mono_signal(mono_samples, sample_rate, STREAM_SAMPLE_RATE)
            clipped = np.clip(resampled, -1.0, 1.0)
            pcm_int16 = (clipped * 32767.0).astype("<i2")

        flags = 0x01 if is_muted else 0x00
        if self.audio_engine.talkback_active:
            flags |= 0x02
        header = struct.pack("<BBH", FRAME_TALENT_AUDIO, flags, STREAM_SAMPLE_RATE // 100)
        packet = header + pcm_int16.tobytes()

        for client in listeners:
            client.send_binary(packet)

    def build_status_snapshot(self) -> Dict[str, Any]:
        """Build the stripped-down status snapshot sent to the remote producer UI."""
        vu = self.audio_engine.get_vu_readings()
        settings = self.config_manager.settings

        extra: Dict[str, Any] = {}
        if self.status_provider is not None:
            try:
                extra = self.status_provider() or {}
            except Exception:
                extra = {}

        if self.audio_engine.is_recording:
            if self.audio_engine.is_record_paused:
                state_code = "PAUSED"
            else:
                state_code = "RECORDING"
        elif self.audio_engine.is_monitoring:
            state_code = "MONITORING"
        else:
            state_code = "STOPPED"

        rec_path = self.audio_engine.current_recording_path
        active_take = extra.get("active_take_name", rec_path.name if rec_path else "")

        if "storage_remaining" not in extra:
            try:
                st = self.config_manager.get_storage_status()
                storage_rem = st.formatted_remaining
                storage_lvl = st.level
            except Exception:
                storage_rem = "--"
                storage_lvl = "ok"
        else:
            storage_rem = str(extra.get("storage_remaining", "--"))
            storage_lvl = str(extra.get("storage_level", "ok"))

        return {
            "type": "state",
            "session_id": self.session.session_id,
            "project_name": extra.get("project_name", settings.project_name or "Session"),
            "state": state_code,
            "is_recording": bool(self.audio_engine.is_recording),
            "is_record_paused": bool(self.audio_engine.is_record_paused),
            "elapsed_seconds": round(float(self.audio_engine.elapsed_seconds), 2),
            "studio_mode": bool(settings.studio_mode),
            "sample_rate": int(settings.sample_rate),
            "bit_depth": int(settings.bit_depth),
            "channels": int(settings.effective_channels),
            "talkback_active": bool(self.audio_engine.talkback_active),
            "remote_talkback_active": bool(self.audio_engine.remote_talkback_active),
            "local_talkback_active": bool(self.audio_engine.local_talkback_active),
            "talent_mic_live": bool(self.audio_engine.is_talent_mic_live_to_producer),
            "talent_mic_send_enabled": bool(self.audio_engine.talent_mic_send_enabled),
            "talent_cough_muted": bool(self.audio_engine.talent_cough_muted),
            "connected_producers": int(self.connected_client_count),
            "take_count": int(extra.get("take_count", 0)),
            "active_take_name": str(active_take),
            "storage_remaining": storage_rem,
            "storage_level": storage_lvl,
            "vu": {
                "rms_db": round(float(vu.rms_db), 1),
                "peak_db": round(float(vu.peak_db), 1),
                "peak_hold_db": round(float(vu.peak_hold_db), 1),
                "clipped": bool(vu.clipped),
                "channel_peaks": [round(float(x), 1) for x in (vu.channel_peaks or (vu.peak_db,))],
                "channel_rms": [round(float(x), 1) for x in (vu.channel_rms or (vu.rms_db,))],
                "channel_peak_holds": [round(float(x), 1) for x in (vu.channel_peak_holds or (vu.peak_hold_db,))],
            },
        }

    def _state_broadcast_loop(self) -> None:
        while self._running.is_set():
            time.sleep(0.08)  # ~12.5 Hz telemetry update for smooth VU & timer
            with self._clients_lock:
                clients = [c for c in self._clients.values() if not c.closed]
            if not clients:
                continue
            try:
                payload = json.dumps(self.build_status_snapshot())
            except Exception:
                continue
            dead: List[str] = []
            for client in clients:
                if not client.send_text(payload):
                    dead.append(client.client_id)
            if dead:
                self._cleanup_dead_clients(dead)

    def _cleanup_dead_clients(self, client_ids: List[str]) -> None:
        with self._clients_lock:
            for cid in client_ids:
                client = self._clients.pop(cid, None)
                if client:
                    client.close()
        self._recompute_remote_talkback_state()

    def _recompute_remote_talkback_state(self) -> None:
        with self._clients_lock:
            any_active = any(
                (not c.closed) and c.talkback_active and c.mic_send_enabled and (not c.cough_muted)
                for c in self._clients.values()
            )
        self.audio_engine.set_remote_talkback(any_active)

    # ------------------------------------------------------------------
    # HTTP & Authentication Handlers
    # ------------------------------------------------------------------
    def _set_security_headers(
        self,
        handler: BaseHTTPRequestHandler,
        content_type: str,
        csp_nonce: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        handler.send_header("Content-Type", content_type)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
        handler.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=(self)")
        handler.send_header("Cache-Control", "no-store, max-age=0")
        if csp_nonce:
            csp = (
                "default-src 'self'; "
                f"script-src 'nonce-{csp_nonce}' 'strict-dynamic'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "connect-src 'self' wss: https:; "
                "media-src 'self' blob:; "
                "object-src 'none'; "
                "base-uri 'self'; "
                "frame-ancestors 'none'"
            )
            handler.send_header("Content-Security-Policy", csp)
        if extra_headers:
            for k, v in extra_headers.items():
                handler.send_header(k, v)

    def _extract_auth_token(self, handler: BaseHTTPRequestHandler, query_params: Dict[str, List[str]]) -> str:
        cookie_hdr = handler.headers.get("Cookie", "")
        if cookie_hdr:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(cookie_hdr)
                for name in ("__Host-ars_session", "ars_session"):
                    if name in jar:
                        val = jar[name].value
                        if val:
                            return val
            except Exception:
                pass
        auth_hdr = handler.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            return auth_hdr[7:].strip()
        if "auth" in query_params and query_params["auth"]:
            return query_params["auth"][0]
        return ""

    def _is_request_authenticated(
        self, handler: BaseHTTPRequestHandler, query_params: Dict[str, List[str]]
    ) -> bool:
        token = self._extract_auth_token(handler, query_params)
        if token and self.session.verify_auth_token(token):
            return True
        return False

    def _handle_http_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        path = parsed.path or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/ws/producer":
            self._handle_websocket_upgrade(handler, query)
            return

        if path == "/api/status":
            if not self._is_request_authenticated(handler, query):
                self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"authenticated": False, "session_id": self.session.session_id})
                return
            snap = self.build_status_snapshot()
            snap["authenticated"] = True
            self._send_json(handler, HTTPStatus.OK, snap)
            return

        if path == "/api/qr.svg":
            if not self._is_request_authenticated(handler, query):
                self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
                return
            svg_bytes = self.get_qr_svg(include_invite_token=True).encode("utf-8")
            handler.send_response(HTTPStatus.OK)
            self._set_security_headers(handler, "image/svg+xml; charset=utf-8")
            handler.send_header("Content-Length", str(len(svg_bytes)))
            handler.end_headers()
            handler.wfile.write(svg_bytes)
            return

        if path == "/" or path == "/index.html":
            # Check if URL carries a valid session invite token (from QR scan or link)
            invite = (query.get("token") or [""])[0]
            set_cookie_header: Optional[str] = None
            authenticated = self._is_request_authenticated(handler, query)

            if not authenticated and invite and self.session.verify_credentials(invite_token=invite):
                auth_token = self.session.create_auth_token()
                set_cookie_header = (
                    f"__Host-ars_session={auth_token}; SameSite=Lax; HttpOnly; Secure; Path=/; Max-Age=43200"
                )
                authenticated = True

            nonce = secrets.token_urlsafe(16)
            html = self._render_producer_html(
                csp_nonce=nonce,
                authenticated=authenticated,
                prefill_token=invite if not authenticated else "",
            )
            body = html.encode("utf-8")
            handler.send_response(HTTPStatus.OK)
            extra = {"Set-Cookie": set_cookie_header} if set_cookie_header else None
            self._set_security_headers(handler, "text/html; charset=utf-8", csp_nonce=nonce, extra_headers=extra)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return

        self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_http_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        path = parsed.path or "/"

        content_len = int(handler.headers.get("Content-Length", "0") or "0")
        raw_body = handler.rfile.read(min(content_len, 16384)) if content_len > 0 else b""

        if path == "/api/login":
            data: Dict[str, Any] = {}
            ctype = handler.headers.get("Content-Type", "")
            if "application/json" in ctype:
                try:
                    data = json.loads(raw_body.decode("utf-8"))
                except Exception:
                    data = {}
            else:
                form = urllib.parse.parse_qs(raw_body.decode("utf-8", errors="ignore"))
                data = {k: v[0] for k, v in form.items() if v}

            passcode = str(data.get("password") or data.get("passcode") or "")
            invite_token = str(data.get("invite_token") or data.get("token") or "")
            submitted_session = str(data.get("session_id") or data.get("username") or "").strip()

            if submitted_session and submitted_session.upper() != self.session.session_id.upper():
                self._send_json(
                    handler,
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "Session ID does not match the active studio session."},
                )
                return

            if self.session.verify_credentials(passcode=passcode, invite_token=invite_token):
                auth_token = self.session.create_auth_token()
                cookie_val = (
                    f"__Host-ars_session={auth_token}; SameSite=Lax; HttpOnly; Secure; Path=/; Max-Age=43200"
                )
                self._send_json(
                    handler,
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "session_id": self.session.session_id,
                        "auth_token": auth_token,
                    },
                    extra_headers={"Set-Cookie": cookie_val},
                )
            else:
                self._send_json(
                    handler,
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "Invalid session passcode or expired invite token."},
                )
            return

        if path == "/api/logout":
            expired_cookie = (
                "__Host-ars_session=; SameSite=Lax; HttpOnly; Secure; Path=/; Max-Age=0"
            )
            self._send_json(
                handler,
                HTTPStatus.OK,
                {"ok": True},
                extra_headers={
                    "Set-Cookie": expired_cookie,
                    "Clear-Site-Data": '"cookies", "storage", "cache"',
                },
            )
            return

        self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _send_json(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        payload: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        raw = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        self._set_security_headers(handler, "application/json; charset=utf-8", extra_headers=extra_headers)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    # ------------------------------------------------------------------
    # RFC 6455 WebSocket Upgrade & Full-Duplex Session Loop
    # ------------------------------------------------------------------
    def _handle_websocket_upgrade(
        self, handler: BaseHTTPRequestHandler, query: Dict[str, List[str]]
    ) -> None:
        if not self._is_request_authenticated(handler, query):
            self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"error": "Session authentication required"})
            return

        ws_key = handler.headers.get("Sec-WebSocket-Key", "").strip()
        upgrade = handler.headers.get("Upgrade", "").lower()
        if not ws_key or "websocket" not in upgrade:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "Expected WebSocket upgrade"})
            return

        accept_raw = hashlib.sha1((ws_key + WS_GUID).encode("ascii")).digest()
        accept_key = base64.b64encode(accept_raw).decode("ascii")

        handler.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept_key)
        handler.end_headers()

        sock = handler.connection
        sock.settimeout(1.0)
        ws_conn = WebSocketConnection(sock, handler.client_address, self.session.session_id)

        with self._clients_lock:
            self._clients[ws_conn.client_id] = ws_conn

        logger.info("Remote producer connected (%s, client=%s)", handler.client_address[0], ws_conn.client_id)

        try:
            # Send initial state snapshot immediately
            ws_conn.send_text(json.dumps(self.build_status_snapshot()))

            while self._running.is_set() and not ws_conn.closed:
                # Verify session was not rotated
                if ws_conn.session_id != self.session.session_id:
                    ws_conn.send_text(json.dumps({"type": "session_revoked"}))
                    break

                frame = read_ws_frame(sock)
                if frame is None:
                    break
                opcode, payload = frame

                if opcode == 0x8:  # Close
                    break
                elif opcode == 0x9:  # Ping
                    ws_conn.send_pong(payload)
                elif opcode == 0x1:  # Text JSON message
                    self._handle_ws_json(ws_conn, payload)
                elif opcode == 0x2:  # Binary audio frame from producer
                    self._handle_ws_binary(ws_conn, payload)
        except Exception as exc:
            logger.debug("WebSocket session ended (%s): %s", ws_conn.client_id, exc)
        finally:
            with self._clients_lock:
                self._clients.pop(ws_conn.client_id, None)
            ws_conn.close()
            self._recompute_remote_talkback_state()
            logger.info("Remote producer disconnected (client=%s)", ws_conn.client_id)

    def _handle_ws_json(self, ws_conn: WebSocketConnection, payload: bytes) -> None:
        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        mtype = msg.get("type")

        if mtype == "mic_state":
            ws_conn.mic_send_enabled = bool(msg.get("send_enabled", False))
            ws_conn.cough_muted = bool(msg.get("cough_muted", False))
            if not ws_conn.mic_send_enabled or ws_conn.cough_muted:
                ws_conn.talkback_active = False
            self._recompute_remote_talkback_state()
            ws_conn.send_text(json.dumps(self.build_status_snapshot()))

        elif mtype == "talkback":
            requested = bool(msg.get("active", False))
            # Only allow talkback when the producer's Mic Send switch is armed and not cough-muted
            if requested and ws_conn.mic_send_enabled and not ws_conn.cough_muted:
                ws_conn.talkback_active = True
            else:
                ws_conn.talkback_active = False
            self._recompute_remote_talkback_state()
            ws_conn.send_text(json.dumps(self.build_status_snapshot()))

        elif mtype == "listen_state":
            ws_conn.listen_enabled = bool(msg.get("enabled", True))

        elif mtype == "ping":
            ws_conn.send_text(json.dumps({"type": "pong", "ts": msg.get("ts", 0)}))

    def _handle_ws_binary(self, ws_conn: WebSocketConnection, payload: bytes) -> None:
        """
        Handle incoming binary PCM frame from remote producer mic:
        Header: [0x02, flags, sample_rate_div100_le16] + Int16LE mono samples.
        """
        if len(payload) <= 4:
            return
        if not (ws_conn.mic_send_enabled and not ws_conn.cough_muted and ws_conn.talkback_active):
            return
        frame_type, _flags, sr_div100 = struct.unpack("<BBH", payload[:4])
        if frame_type != FRAME_PRODUCER_AUDIO:
            return
        in_sr = max(8000, min(96000, int(sr_div100) * 100))
        pcm_bytes = payload[4:]
        n_samples = len(pcm_bytes) // 2
        if n_samples <= 0:
            return
        pcm_int16 = np.frombuffer(pcm_bytes[: n_samples * 2], dtype="<i2")
        float_samples = pcm_int16.astype(np.float32) / 32768.0
        self.audio_engine.push_remote_talkback_audio(float_samples, in_sr)

    # ------------------------------------------------------------------
    # Stripped-Down Producer Console Single-Page HTML
    # ------------------------------------------------------------------
    def _render_producer_html(self, csp_nonce: str, authenticated: bool, prefill_token: str = "") -> str:
        session_id = self.session.session_id
        project_name = self.config_manager.settings.project_name or "Session"
        auth_str = "true" if authenticated else "false"
        safe_prefill = html_escape(prefill_token)
        safe_session = html_escape(session_id)
        safe_project = html_escape(project_name)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alpine Recording Station — Remote Producer Console</title>
<style>
:root {{
  color-scheme: dark;
  --brand-color: #58A6FF;
  --accent-rec: #DA3633;
  --accent-pause: #D29922;
  --accent-talkback: #D29922;
  --accent-ok: #2EA043;
  --bg-main: #111418;
  --bg-card: #161B22;
  --bg-deck: #0D1014;
  --border-subtle: #262C36;
  --text-primary: #E6EDF3;
  --text-secondary: #8B949E;
  --text-muted: #6E7681;
  accent-color: var(--brand-color);
}}
* {{
  box-sizing: border-box;
}}
body {{
  margin: 0;
  padding: 0;
  background-color: var(--bg-main);
  color: var(--text-primary);
  font-family: "DejaVu Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}}
.visually-hidden {{
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
  border: 0;
}}
/* Top Header Bar */
.top-bar {{
  background: var(--bg-card);
  border-bottom: 1px solid var(--border-subtle);
  padding: 12px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}}
.brand-group {{
  display: flex;
  align-items: center;
  gap: 12px;
}}
.brand-title {{
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.4px;
  color: #F0F6FC;
}}
.role-pill {{
  background: #1B2738;
  border: 1px solid #388BFD;
  color: #79C0FF;
  font-size: 11px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 999px;
  letter-spacing: 0.6px;
}}
.header-meta {{
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}}
.badge {{
  background: #21262D;
  border: 1px solid #30363D;
  color: #C9D1D9;
  font-size: 12px;
  font-weight: 600;
  padding: 5px 11px;
  border-radius: 6px;
}}
.badge-conn-ok {{
  background: #0F2D1E;
  border-color: #2EA043;
  color: #7EE787;
}}
.badge-conn-warn {{
  background: #341A04;
  border-color: #D29922;
  color: #F2CC60;
}}
.btn-subtle {{
  background: #21262D;
  border: 1px solid #30363D;
  color: #C9D1D9;
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}}
.btn-subtle:hover {{
  background: #292E36;
  border-color: #58A6FF;
}}
/* Main Container */
.main-wrap {{
  max-width: 980px;
  width: 100%;
  margin: 0 auto;
  padding: 18px 20px 28px;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 16px;
}}
/* Login View */
.login-card {{
  max-width: 440px;
  width: 100%;
  margin: 48px auto;
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 26px 28px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
}}
.login-card h1 {{
  margin: 0 0 6px;
  font-size: 19px;
  color: #F0F6FC;
}}
.login-card p {{
  margin: 0 0 18px;
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.45;
}}
.form-field {{
  margin-bottom: 16px;
}}
.form-field label {{
  display: block;
  font-size: 12px;
  font-weight: 700;
  color: #C9D1D9;
  margin-bottom: 6px;
  letter-spacing: 0.4px;
}}
.input-row {{
  display: flex;
  gap: 8px;
}}
.text-input {{
  width: 100%;
  background: var(--bg-deck);
  border: 1px solid #30363D;
  border-radius: 8px;
  padding: 10px 12px;
  color: var(--text-primary);
  font-size: 15px;
  font-family: "DejaVu Sans Mono", monospace;
}}
.text-input:focus {{
  outline: 2px solid var(--brand-color);
  border-color: transparent;
}}
.btn-primary {{
  width: 100%;
  background: #1F6FEB;
  border: 1px solid #388BFD;
  color: #FFFFFF;
  border-radius: 8px;
  padding: 12px 16px;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
}}
.btn-primary:hover {{
  background: #388BFD;
}}
.login-error {{
  background: #3D1114;
  border: 1px solid #F85149;
  color: #FFA198;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 13px;
  margin-bottom: 14px;
  display: none;
}}
/* Studio Deck Card */
.studio-deck {{
  background: var(--bg-deck);
  border: 2px solid var(--border-subtle);
  border-radius: 12px;
  padding: 18px 22px;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}}
.studio-deck.deck-recording {{
  background: #1A0F12;
  border-color: var(--accent-rec);
}}
.studio-deck.deck-paused {{
  background: #1B160D;
  border-color: var(--accent-pause);
}}
.studio-deck.deck-talkback {{
  background: #1E170B;
  border-color: var(--accent-talkback);
}}
.deck-top {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}}
.state-banner {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.7px;
  background: #161B22;
  border: 1px solid #30363D;
  color: #8B949E;
}}
.state-banner.banner-rec {{
  background: #3D1114;
  border-color: #F85149;
  color: #FFA198;
}}
.state-banner.banner-paused {{
  background: #341A04;
  border-color: #D29922;
  color: #F2CC60;
}}
.state-banner.banner-talkback {{
  background: #3B2300;
  border-color: #F0883E;
  color: #FFDF5D;
}}
.timer-display {{
  font-family: "DejaVu Sans Mono", "Consolas", monospace;
  font-size: 44px;
  font-weight: 700;
  letter-spacing: 2px;
  color: #8B949E;
}}
.timer-display.timer-rec {{
  color: #FF7B72;
}}
.timer-display.timer-paused {{
  color: #F2CC60;
}}
.deck-sub-row {{
  margin-top: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--text-secondary);
}}
/* VU Meter Card */
.card {{
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  padding: 16px 18px;
}}
.card-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  gap: 10px;
  flex-wrap: wrap;
}}
.card-title {{
  font-size: 11px;
  font-weight: 700;
  color: var(--text-secondary);
  letter-spacing: 1px;
  text-transform: uppercase;
}}
.vu-readout {{
  font-family: "DejaVu Sans Mono", monospace;
  font-size: 12px;
  font-weight: 700;
  color: #C9D1D9;
}}
.vu-canvas {{
  width: 100%;
  height: 92px;
  display: block;
  background: #0D1014;
  border-radius: 6px;
  border: 1px solid #21262D;
}}
/* Controls Grid */
.controls-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
  gap: 16px;
}}
.control-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
}}
/* Brand-Consistent Toggle & Checkbox Styling + Fallbacks */
.switch-label {{
  display: inline-flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  user-select: none;
}}
.switch-input {{
  accent-color: var(--brand-color);
  width: 18px;
  height: 18px;
  cursor: pointer;
}}
@supports not (accent-color: var(--brand-color)) {{
  .switch-label input[type="checkbox"] + span::before {{
    content: "";
    display: inline-block;
    width: 16px;
    height: 16px;
    border: 2px solid #58A6FF;
    border-radius: 4px;
    margin-right: 8px;
    vertical-align: middle;
  }}
  .switch-label input[type="checkbox"]:checked + span::after {{
    content: "✓";
    color: #58A6FF;
    font-weight: 700;
    margin-left: -18px;
    margin-right: 6px;
  }}
}}
/* Range Slider with Brand Progress */
input[type="range"].brand-slider {{
  accent-color: var(--brand-color);
  width: 100%;
  cursor: pointer;
  --progress: 85%;
}}
@supports not (accent-color: var(--brand-color)) {{
  input[type="range"].brand-slider {{
    -webkit-appearance: none;
    appearance: none;
    height: 6px;
    border-radius: 4px;
    background: linear-gradient(to right, var(--brand-color) var(--progress), #30363D var(--progress));
  }}
}}
/* Privacy / Cough & Talkback Buttons */
.btn-mic-gate {{
  width: 100%;
  padding: 12px 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  border: 2px solid #30363D;
  background: #21262D;
  color: #C9D1D9;
  transition: all 0.12s ease;
}}
.btn-mic-gate.armed {{
  background: #0F2D1E;
  border-color: #2EA043;
  color: #7EE787;
}}
.btn-cough {{
  width: 100%;
  margin-top: 10px;
  padding: 11px 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  border: 2px solid #388BFD;
  background: #161F2C;
  color: #79C0FF;
  user-select: none;
  touch-action: none;
}}
.btn-cough.cough-active {{
  background: #3D1114;
  border-color: #F85149;
  color: #FFA198;
}}
.btn-talkback {{
  width: 100%;
  padding: 20px 18px;
  border-radius: 10px;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.6px;
  cursor: pointer;
  border: 2px solid #9E6A03;
  background: #261A05;
  color: #F2CC60;
  user-select: none;
  touch-action: none;
  transition: all 0.1s ease;
}}
.btn-talkback:disabled {{
  background: #161B22;
  border-color: #30363D;
  color: #6E7681;
  cursor: not-allowed;
}}
.btn-talkback.talkback-live {{
  background: #D29922;
  border-color: #FFDF5D;
  color: #0D1014;
  box-shadow: 0 0 24px rgba(210, 153, 34, 0.45);
}}
.status-Strip {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--text-secondary);
}}
.pill-muted {{
  color: #FFA198;
  font-weight: 700;
}}
.pill-live {{
  color: #7EE787;
  font-weight: 700;
}}
</style>
</head>
<body>

<header class="top-bar">
  <div class="brand-group">
    <span class="brand-title">◉ ALPINE RECORDING STATION</span>
    <span class="role-pill">REMOTE PRODUCER — GUIDED VO</span>
  </div>
  <div class="header-meta">
    <span class="badge" id="hdr-session">Session: {safe_session}</span>
    <span class="badge" id="hdr-project">Project: {safe_project}</span>
    <span class="badge badge-conn-warn" id="hdr-conn">● Connecting...</span>
    <button type="button" class="btn-subtle" id="btn-logout" style="display:none;">Sign Out</button>
  </div>
</header>

<main class="main-wrap">
  <!-- SESSION-TIED LOGIN VIEW -->
  <section class="login-card" id="login-view" style="display:none;">
    <h1>Join Guided VO Session</h1>
    <p>Authenticate with the 6-digit Session Passcode displayed on the studio's Remote Producer setup screen, or scan the session QR code.</p>
    <div class="login-error" id="login-error" role="alert"></div>
    <form id="login-form" action="/api/login" method="post">
      <div class="form-field">
        <label for="session-id-input">STUDIO SESSION ID</label>
        <input
          type="text"
          id="session-id-input"
          name="username"
          class="text-input"
          autocomplete="username"
          value="{safe_session}"
          readonly
        >
      </div>
      <div class="form-field">
        <label for="session-passcode">SESSION PASSCODE (6-DIGIT PIN)</label>
        <div class="input-row">
          <input
            type="password"
            id="session-passcode"
            name="password"
            class="text-input"
            autocomplete="current-password"
            inputmode="numeric"
            placeholder="Enter 6-digit passcode"
            required
          >
          <button
            type="button"
            class="btn-subtle"
            id="btn-toggle-pass"
            aria-label="Show session passcode"
            aria-pressed="false"
          >Show</button>
        </div>
      </div>
      <input type="hidden" id="invite-token-input" name="invite_token" value="{safe_prefill}">
      <button type="submit" class="btn-primary" id="btn-login-submit">Connect to Studio Session</button>
    </form>
  </section>

  <!-- STRIPPED-DOWN PRODUCER CONSOLE VIEW -->
  <section id="console-view" style="display:none;">
    <!-- Studio Deck Readout -->
    <div class="studio-deck" id="studio-deck">
      <div class="deck-top">
        <div>
          <div class="state-banner" id="state-banner">● STANDBY — LIVE INPUT MONITOR</div>
          <div style="margin-top:8px; font-size:12px; color:#8B949E;" id="mode-summary">
            Mode: Studio Mode (2-Ch Dual Track: L = VO Talent, R = Producer &amp; Talent Talkback)
          </div>
        </div>
        <div class="timer-display" id="timer-display">00:00:00.0</div>
      </div>
      <div class="deck-sub-row">
        <span id="talent-feed-badge" class="pill-live">◉ Talent Mic Feed: LIVE</span>
        <span id="take-summary">Session Takes: 0</span>
        <span id="storage-summary">Storage Remaining: --</span>
      </div>
    </div>

    <!-- Live VU & Peak Hold Meter -->
    <div class="card" style="margin-top:14px;">
      <div class="card-header">
        <span class="card-title">LIVE STUDIO LEVEL METER (60 dB DIGITAL SCALE)</span>
        <span class="vu-readout" id="vu-readout">PEAK: -60.0 dBFS | HOLD: -60.0 dBFS | RMS: -60.0 dBFS</span>
      </div>
      <canvas class="vu-canvas" id="vu-canvas" width="880" height="92"></canvas>
    </div>

    <!-- Producer Audio & Talkback Controls -->
    <div class="controls-grid" style="margin-top:14px;">
      <!-- Listen to Talent Card -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">1. LISTEN TO TALENT PERFORMANCE</span>
          <span class="badge" id="stream-rate-badge">24.0 kHz PCM</span>
        </div>
        <div class="control-row">
          <label class="switch-label" for="chk-listen-talent">
            <input type="checkbox" id="chk-listen-talent" class="switch-input" role="switch" checked>
            <span>Monitor Talent Audio Live</span>
          </label>
        </div>
        <div class="control-row" style="margin-top:14px;">
          <label for="monitor-volume" style="font-size:12px; color:#8B949E; min-width:110px;">Monitor Volume</label>
          <input type="range" id="monitor-volume" class="brand-slider" min="0" max="150" value="100">
          <span id="monitor-volume-val" style="font-size:12px; font-family:monospace; min-width:44px; text-align:right;">100%</span>
        </div>
      </div>

      <!-- Producer Mic Privacy & Cough Switch Card -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">2. PRODUCER MIC PRIVACY &amp; COUGH GATE</span>
          <span class="badge" id="mic-privacy-badge">MIC LOCKED (PRIVATE)</span>
        </div>
        <button type="button" class="btn-mic-gate" id="btn-mic-arm" aria-pressed="false">
          ○ Send Producer Mic: OFF (Private — Click to Arm)
        </button>
        <button type="button" class="btn-cough" id="btn-cough" disabled>
          Hold to Cough / Privacy Mute [Hold C or Click M]
        </button>
      </div>
    </div>

    <!-- Momentary Talkback Prompt Deck -->
    <div class="card" style="margin-top:14px;">
      <div class="card-header">
        <span class="card-title">3. DIRECT TALENT — STUDIO TALKBACK</span>
        <span style="font-size:12px; color:#8B949E;" id="talkback-hint">
          Arm "Send Producer Mic" above to enable Talkback
        </span>
      </div>
      <button type="button" class="btn-talkback" id="btn-talkback" disabled>
        ◉ TALKBACK — HOLD TO PROMPT TALENT [Hold T or Space]
      </button>
      <div class="status-Strip" style="margin-top:10px;">
        <span>Mix-Minus Active: You hear the Talent Mic; Talent headphones hear your Talkback. In Studio Mode, Track R mixes Producer + Talent Mic with marker tones while Track L (VO) is muted.</span>
        <span id="rtt-readout">Link Latency: -- ms</span>
      </div>
    </div>
  </section>
</main>

<script nonce="{csp_nonce}">
(function() {{
  "use strict";

  let isAuthenticated = {auth_str};
  let ws = null;
  let reconnectTimer = null;
  let pingInterval = null;
  let lastPingTs = 0;

  // Audio playback (Talent -> Producer)
  let audioCtx = null;
  let monitorGainNode = null;
  let nextPlayTime = 0;
  let listenEnabled = true;
  let monitorVolume = 1.0;

  // Producer Mic capture (Producer -> Talent Talkback)
  let micStream = null;
  let micSourceNode = null;
  let micProcessorNode = null;
  let micSendEnabled = false; // Default OFF for producer privacy
  let coughMuted = false;
  let talkbackPressed = false;

  // Latest VU state
  let latestVu = {{
    rms_db: -60.0,
    peak_db: -60.0,
    peak_hold_db: -60.0,
    clipped: false,
    channel_peaks: [-60.0, -60.0],
    channel_rms: [-60.0, -60.0],
    channel_peak_holds: [-60.0, -60.0]
  }};
  let latestStudioMode = true;
  let latestChannels = 2;

  const loginView = document.getElementById("login-view");
  const consoleView = document.getElementById("console-view");
  const loginForm = document.getElementById("login-form");
  const loginError = document.getElementById("login-error");
  const passInput = document.getElementById("session-passcode");
  const btnTogglePass = document.getElementById("btn-toggle-pass");
  const btnLogout = document.getElementById("btn-logout");

  const hdrConn = document.getElementById("hdr-conn");
  const hdrSession = document.getElementById("hdr-session");
  const hdrProject = document.getElementById("hdr-project");

  const studioDeck = document.getElementById("studio-deck");
  const stateBanner = document.getElementById("state-banner");
  const modeSummary = document.getElementById("mode-summary");
  const timerDisplay = document.getElementById("timer-display");
  const talentFeedBadge = document.getElementById("talent-feed-badge");
  const takeSummary = document.getElementById("take-summary");
  const storageSummary = document.getElementById("storage-summary");
  const vuReadout = document.getElementById("vu-readout");
  const vuCanvas = document.getElementById("vu-canvas");

  const chkListen = document.getElementById("chk-listen-talent");
  const sliderVolume = document.getElementById("monitor-volume");
  const lblVolume = document.getElementById("monitor-volume-val");

  const btnMicArm = document.getElementById("btn-mic-arm");
  const micPrivacyBadge = document.getElementById("mic-privacy-badge");
  const btnCough = document.getElementById("btn-cough");
  const btnTalkback = document.getElementById("btn-talkback");
  const talkbackHint = document.getElementById("talkback-hint");
  const rttReadout = document.getElementById("rtt-readout");

  function showView(authenticated) {{
    isAuthenticated = authenticated;
    if (authenticated) {{
      loginView.style.display = "none";
      consoleView.style.display = "block";
      btnLogout.style.display = "inline-block";
      connectWebSocket();
    }} else {{
      loginView.style.display = "block";
      consoleView.style.display = "none";
      btnLogout.style.display = "none";
      hdrConn.textContent = "● Auth Required";
      hdrConn.className = "badge badge-conn-warn";
    }}
  }}

  // Password show/hide toggle
  btnTogglePass.addEventListener("click", function() {{
    const showing = passInput.type === "text";
    passInput.type = showing ? "password" : "text";
    btnTogglePass.textContent = showing ? "Show" : "Hide";
    btnTogglePass.setAttribute("aria-pressed", showing ? "false" : "true");
  }});

  // Login submit handler
  loginForm.addEventListener("submit", async function(ev) {{
    ev.preventDefault();
    loginError.style.display = "none";
    const sessionVal = document.getElementById("session-id-input").value;
    const passVal = passInput.value;
    const inviteVal = document.getElementById("invite-token-input").value;
    try {{
      const resp = await fetch("/api/login", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{
          session_id: sessionVal,
          password: passVal,
          invite_token: inviteVal
        }})
      }});
      const data = await resp.json();
      if (resp.ok && data.ok) {{
        // Clean token from URL bar for security
        if (window.history && window.history.replaceState) {{
          window.history.replaceState({{}}, document.title, "/");
        }}
        showView(true);
      }} else {{
        loginError.textContent = data.error || "Authentication failed.";
        loginError.style.display = "block";
      }}
    }} catch (err) {{
      loginError.textContent = "Network error connecting to studio.";
      loginError.style.display = "block";
    }}
  }});

  btnLogout.addEventListener("click", async function() {{
    stopMicCapture();
    if (ws) {{
      ws.close();
      ws = null;
    }}
    try {{
      await fetch("/api/logout", {{ method: "POST" }});
    }} catch (_e) {{}}
    showView(false);
  }});

  // Audio Context & Low-Latency Playback
  function ensureAudioContext() {{
    if (!audioCtx) {{
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx) {{
        audioCtx = new Ctx({{ latencyHint: "interactive", sampleRate: 24000 }});
        monitorGainNode = audioCtx.createGain();
        monitorGainNode.gain.value = monitorVolume;
        monitorGainNode.connect(audioCtx.destination);
      }}
    }}
    if (audioCtx && audioCtx.state === "suspended") {{
      audioCtx.resume();
    }}
  }}

  document.body.addEventListener("pointerdown", ensureAudioContext, {{ passive: true }});

  chkListen.addEventListener("change", function() {{
    listenEnabled = Boolean(chkListen.checked);
    sendJson({{ type: "listen_state", enabled: listenEnabled }});
  }});

  function updateSliderProgress(slider) {{
    const min = parseFloat(slider.min || "0");
    const max = parseFloat(slider.max || "100");
    const val = parseFloat(slider.value || "0");
    const pct = ((val - min) / (max - min)) * 100;
    slider.style.setProperty("--progress", pct.toFixed(1) + "%");
  }}
  updateSliderProgress(sliderVolume);

  sliderVolume.addEventListener("input", function() {{
    updateSliderProgress(sliderVolume);
    const pct = parseInt(sliderVolume.value, 10);
    lblVolume.textContent = pct + "%";
    monitorVolume = pct / 100.0;
    if (monitorGainNode) {{
      monitorGainNode.gain.value = monitorVolume;
    }}
  }});

  // Mic Privacy Gate & Cough Controls
  async function ensureMicCapture() {{
    ensureAudioContext();
    if (micStream) {{
      return true;
    }}
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {{
      talkbackHint.textContent = "Microphone access unavailable in this browser.";
      return false;
    }}
    try {{
      micStream = await navigator.mediaDevices.getUserMedia({{
        audio: {{
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1
        }},
        video: false
      }});
      micSourceNode = audioCtx.createMediaStreamSource(micStream);
      micProcessorNode = audioCtx.createScriptProcessor(1024, 1, 1);
      micProcessorNode.onaudioprocess = function(ev) {{
        if (!micSendEnabled || coughMuted || !talkbackPressed) {{
          return;
        }}
        if (!ws || ws.readyState !== WebSocket.OPEN) {{
          return;
        }}
        const input = ev.inputBuffer.getChannelData(0);
        const srDiv100 = Math.round(ev.inputBuffer.sampleRate / 100);
        const buf = new ArrayBuffer(4 + input.length * 2);
        const view = new DataView(buf);
        view.setUint8(0, 0x02);
        view.setUint8(1, 0x00);
        view.setUint16(2, srDiv100, true);
        let offset = 4;
        for (let i = 0; i < input.length; i++) {{
          let s = Math.max(-1, Math.min(1, input[i]));
          view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
          offset += 2;
        }}
        ws.send(buf);
      }};
      micSourceNode.connect(micProcessorNode);
      micProcessorNode.connect(audioCtx.destination);
      return true;
    }} catch (err) {{
      talkbackHint.textContent = "Microphone permission denied by browser.";
      return false;
    }}
  }}

  function stopMicCapture() {{
    talkbackPressed = false;
    micSendEnabled = false;
    coughMuted = false;
    if (micProcessorNode) {{
      try {{ micProcessorNode.disconnect(); }} catch (_e) {{}}
      micProcessorNode = null;
    }}
    if (micSourceNode) {{
      try {{ micSourceNode.disconnect(); }} catch (_e) {{}}
      micSourceNode = null;
    }}
    if (micStream) {{
      micStream.getTracks().forEach(function(t) {{ t.stop(); }});
      micStream = null;
    }}
    refreshMicAndTalkbackUi();
  }}

  function refreshMicAndTalkbackUi() {{
    const effectivelyLive = micSendEnabled && !coughMuted;
    btnMicArm.setAttribute("aria-pressed", micSendEnabled ? "true" : "false");
    if (micSendEnabled) {{
      btnMicArm.className = "btn-mic-gate armed";
      btnMicArm.textContent = "◉ Send Producer Mic: ARMED (Click to Lock for Privacy)";
      btnCough.disabled = false;
    }} else {{
      btnMicArm.className = "btn-mic-gate";
      btnMicArm.textContent = "○ Send Producer Mic: OFF (Private — Click to Arm)";
      btnCough.disabled = true;
    }}

    if (!micSendEnabled) {{
      micPrivacyBadge.textContent = "MIC LOCKED (PRIVATE)";
      micPrivacyBadge.className = "badge";
      btnTalkback.disabled = true;
      talkbackHint.textContent = "Arm 'Send Producer Mic' above to enable Talkback";
    }} else if (coughMuted) {{
      micPrivacyBadge.textContent = "COUGH MUTED";
      micPrivacyBadge.className = "badge badge-conn-warn";
      btnCough.className = "btn-cough cough-active";
      btnCough.textContent = "✕ COUGH MUTE ACTIVE — Release or Click [M] to Unmute";
      btnTalkback.disabled = true;
      talkbackHint.textContent = "Producer Mic is cough-muted";
    }} else {{
      micPrivacyBadge.textContent = "MIC ARMED (PUSH-TO-TALK)";
      micPrivacyBadge.className = "badge badge-conn-ok";
      btnCough.className = "btn-cough";
      btnCough.textContent = "Hold to Cough / Privacy Mute [Hold C or Press M]";
      btnTalkback.disabled = false;
      talkbackHint.textContent = talkbackPressed
        ? "⚡ TRANSMITTING TO TALENT & TALKBACK TRACK"
        : "Ready — Hold Talkback button or [T] / [Space] to prompt talent";
    }}

    if (talkbackPressed && effectivelyLive) {{
      btnTalkback.className = "btn-talkback talkback-live";
      btnTalkback.textContent = "⚡ TALKBACK ON AIR — PROMPTING TALENT (RELEASE TO END)";
    }} else {{
      btnTalkback.className = "btn-talkback";
      btnTalkback.textContent = "◉ TALKBACK — HOLD TO PROMPT TALENT [Hold T or Space]";
    }}
  }}

  btnMicArm.addEventListener("click", async function() {{
    if (!micSendEnabled) {{
      const ok = await ensureMicCapture();
      if (!ok) {{
        return;
      }}
      micSendEnabled = true;
      coughMuted = false;
    }} else {{
      setTalkbackActive(false);
      stopMicCapture();
    }}
    sendJson({{
      type: "mic_state",
      send_enabled: micSendEnabled,
      cough_muted: coughMuted
    }});
    refreshMicAndTalkbackUi();
  }});

  function setCoughMuted(active) {{
    if (!micSendEnabled) {{
      return;
    }}
    coughMuted = Boolean(active);
    if (coughMuted && talkbackPressed) {{
      setTalkbackActive(false);
    }}
    sendJson({{
      type: "mic_state",
      send_enabled: micSendEnabled,
      cough_muted: coughMuted
    }});
    refreshMicAndTalkbackUi();
  }}

  btnCough.addEventListener("pointerdown", function(ev) {{
    ev.preventDefault();
    setCoughMuted(true);
  }});
  btnCough.addEventListener("pointerup", function(ev) {{
    ev.preventDefault();
    setCoughMuted(false);
  }});
  btnCough.addEventListener("pointerleave", function() {{
    if (coughMuted) {{
      setCoughMuted(false);
    }}
  }});

  function setTalkbackActive(active) {{
    const next = Boolean(active && micSendEnabled && !coughMuted);
    if (talkbackPressed === next) {{
      return;
    }}
    talkbackPressed = next;
    sendJson({{ type: "talkback", active: talkbackPressed }});
    refreshMicAndTalkbackUi();
  }}

  btnTalkback.addEventListener("pointerdown", function(ev) {{
    ev.preventDefault();
    if (!btnTalkback.disabled) {{
      setTalkbackActive(true);
    }}
  }});
  btnTalkback.addEventListener("pointerup", function(ev) {{
    ev.preventDefault();
    setTalkbackActive(false);
  }});
  btnTalkback.addEventListener("pointercancel", function() {{
    setTalkbackActive(false);
  }});
  btnTalkback.addEventListener("pointerleave", function() {{
    if (talkbackPressed) {{
      setTalkbackActive(false);
    }}
  }});

  // Keyboard shortcuts: [T] or [Space] for momentary Talkback, [C] momentary cough, [M] toggle cough
  window.addEventListener("keydown", function(ev) {{
    if (!isAuthenticated) return;
    const tag = (ev.target && ev.target.tagName) ? ev.target.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea") return;

    if ((ev.code === "KeyT" || ev.code === "Space") && !ev.repeat) {{
      if (micSendEnabled && !coughMuted) {{
        ev.preventDefault();
        setTalkbackActive(true);
      }}
    }} else if (ev.code === "KeyC" && !ev.repeat) {{
      ev.preventDefault();
      setCoughMuted(true);
    }} else if (ev.code === "KeyM" && !ev.repeat) {{
      ev.preventDefault();
      setCoughMuted(!coughMuted);
    }}
  }});

  window.addEventListener("keyup", function(ev) {{
    if (!isAuthenticated) return;
    if (ev.code === "KeyT" || ev.code === "Space") {{
      setTalkbackActive(false);
    }} else if (ev.code === "KeyC") {{
      setCoughMuted(false);
    }}
  }});

  // WebSocket Connection & Binary Audio Handling
  function sendJson(obj) {{
    if (ws && ws.readyState === WebSocket.OPEN) {{
      ws.send(JSON.stringify(obj));
    }}
  }}

  function connectWebSocket() {{
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {{
      return;
    }}
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = proto + "//" + window.location.host + "/ws/producer";
    ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";

    ws.onopen = function() {{
      hdrConn.textContent = "● Live Secure Link";
      hdrConn.className = "badge badge-conn-ok";
      sendJson({{
        type: "mic_state",
        send_enabled: micSendEnabled,
        cough_muted: coughMuted
      }});
      sendJson({{ type: "listen_state", enabled: listenEnabled }});

      if (pingInterval) clearInterval(pingInterval);
      pingInterval = setInterval(function() {{
        lastPingTs = performance.now();
        sendJson({{ type: "ping", ts: lastPingTs }});
      }}, 2500);
    }};

    ws.onmessage = function(ev) {{
      if (typeof ev.data === "string") {{
        try {{
          const msg = JSON.parse(ev.data);
          handleServerJson(msg);
        }} catch (_e) {{}}
      }} else if (ev.data instanceof ArrayBuffer) {{
        handleServerBinaryAudio(ev.data);
      }}
    }};

    ws.onclose = function() {{
      hdrConn.textContent = "● Reconnecting...";
      hdrConn.className = "badge badge-conn-warn";
      if (pingInterval) {{
        clearInterval(pingInterval);
        pingInterval = null;
      }}
      if (isAuthenticated) {{
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectWebSocket, 1500);
      }}
    }};
  }}

  function formatElapsed(sec) {{
    const s = Math.max(0, Number(sec) || 0);
    const hrs = Math.floor(s / 3600);
    const mins = Math.floor((s % 3600) / 60);
    const rem = s % 60;
    const whole = Math.floor(rem);
    const tenths = Math.floor((rem - whole) * 10);
    const pad2 = function(n) {{ return n < 10 ? "0" + n : "" + n; }};
    return pad2(hrs) + ":" + pad2(mins) + ":" + pad2(whole) + "." + tenths;
  }}

  function handleServerJson(msg) {{
    if (msg.type === "session_revoked") {{
      stopMicCapture();
      showView(false);
      return;
    }}
    if (msg.type === "pong") {{
      const rtt = Math.max(1, Math.round(performance.now() - Number(msg.ts || lastPingTs)));
      rttReadout.textContent = "Link Latency: " + rtt + " ms";
      return;
    }}
    if (msg.type !== "state") {{
      return;
    }}

    hdrSession.textContent = "Session: " + (msg.session_id || "--");
    hdrProject.textContent = "Project: " + (msg.project_name || "Default");

    latestStudioMode = Boolean(msg.studio_mode);
    latestChannels = Number(msg.channels || 2);

    if (latestStudioMode) {{
      modeSummary.textContent =
        "Mode: Studio Mode (Track L = Talent VO, Track R = Producer & Talent Talkback) @ " +
        (msg.sample_rate || 48000) + " Hz / " + (msg.bit_depth || 24) + "-bit";
    }} else {{
      modeSummary.textContent =
        "Mode: Standard (" + latestChannels + "-Ch) @ " +
        (msg.sample_rate || 48000) + " Hz / " + (msg.bit_depth || 24) + "-bit";
    }}

    timerDisplay.textContent = formatElapsed(msg.elapsed_seconds);

    if (msg.talkback_active) {{
      studioDeck.className = "studio-deck deck-talkback";
      stateBanner.className = "state-banner banner-talkback";
      const who = msg.remote_talkback_active ? "PRODUCER TALKBACK ON AIR" : "TALENT TALKBACK ACTIVE";
      stateBanner.textContent = "⚡ " + who + (msg.is_recording ? " — RECORDING TO TALKBACK TRACK (R)" : "");
      timerDisplay.className = msg.is_recording ? "timer-display timer-rec" : "timer-display";
    }} else if (msg.is_recording && msg.is_record_paused) {{
      studioDeck.className = "studio-deck deck-paused";
      stateBanner.className = "state-banner banner-paused";
      stateBanner.textContent = "❚❚ RECORDING PAUSED";
      timerDisplay.className = "timer-display timer-paused";
    }} else if (msg.is_recording) {{
      studioDeck.className = "studio-deck deck-recording";
      stateBanner.className = "state-banner banner-rec";
      stateBanner.textContent = "● RECORDING IN PROGRESS";
      timerDisplay.className = "timer-display timer-rec";
    }} else {{
      studioDeck.className = "studio-deck";
      stateBanner.className = "state-banner";
      stateBanner.textContent = "● STANDBY — LIVE INPUT MONITOR";
      timerDisplay.className = "timer-display";
    }}

    if (msg.talent_mic_live) {{
      talentFeedBadge.textContent = "◉ Talent Mic Feed: LIVE";
      talentFeedBadge.className = "pill-live";
    }} else {{
      talentFeedBadge.textContent = "○ Talent Mic Feed: MUTED FOR PRIVACY / COUGH";
      talentFeedBadge.className = "pill-muted";
    }}

    const activeTake = msg.active_take_name ? (" (" + msg.active_take_name + ")") : "";
    takeSummary.textContent = "Session Takes: " + (msg.take_count || 0) + activeTake;
    storageSummary.textContent = "Storage Remaining: " + (msg.storage_remaining || "--");

    if (msg.vu) {{
      latestVu = msg.vu;
      const clipTxt = msg.vu.clipped ? " | CLIP!" : "";
      vuReadout.textContent =
        "PEAK: " + Number(msg.vu.peak_db).toFixed(1) + " dBFS | " +
        "HOLD: " + Number(msg.vu.peak_hold_db).toFixed(1) + " dBFS | " +
        "RMS: " + Number(msg.vu.rms_db).toFixed(1) + " dBFS" + clipTxt;
    }}
  }}

  function handleServerBinaryAudio(buf) {{
    if (!listenEnabled || !audioCtx || buf.byteLength <= 4) {{
      return;
    }}
    const view = new DataView(buf);
    const frameType = view.getUint8(0);
    if (frameType !== 0x01) {{
      return;
    }}
    const sr = Math.max(8000, view.getUint16(2, true) * 100);
    const numSamples = (buf.byteLength - 4) >> 1;
    if (numSamples <= 0) {{
      return;
    }}
    const audioBuf = audioCtx.createBuffer(1, numSamples, sr);
    const ch = audioBuf.getChannelData(0);
    let offset = 4;
    for (let i = 0; i < numSamples; i++) {{
      ch[i] = view.getInt16(offset, true) / 32768.0;
      offset += 2;
    }}
    const src = audioCtx.createBufferSource();
    src.buffer = audioBuf;
    src.connect(monitorGainNode);
    const now = audioCtx.currentTime;
    if (nextPlayTime < now || nextPlayTime > now + 0.18) {{
      nextPlayTime = now + 0.02;
    }}
    src.start(nextPlayTime);
    nextPlayTime += audioBuf.duration;
  }}

  // 60-Segment LED VU & Peak Hold Canvas Renderer (Matching Talent UI)
  function dbToNorm(db) {{
    const clamped = Math.max(-60.0, Math.min(0.0, Number(db) || -60.0));
    return (clamped + 60.0) / 60.0;
  }}

  function drawVuMeter() {{
    const ctx = vuCanvas.getContext("2d");
    const w = vuCanvas.width;
    const h = vuCanvas.height;
    ctx.fillStyle = "#0D1014";
    ctx.fillRect(0, 0, w, h);

    const labels = latestStudioMode ? ["VO (L)", "TB (R)"] : (latestChannels >= 2 ? ["CH 1", "CH 2"] : ["MONO"]);
    const peaks = latestVu.channel_peaks || [latestVu.peak_db];
    const rmsList = latestVu.channel_rms || [latestVu.rms_db];
    const holds = latestVu.channel_peak_holds || [latestVu.peak_hold_db];

    const leftPad = 64;
    const rightPad = 24;
    const barW = w - leftPad - rightPad;
    const numSegments = 60;
    const segGap = 2;
    const segW = (barW - (numSegments - 1) * segGap) / numSegments;

    for (let chIdx = 0; chIdx < labels.length; chIdx++) {{
      const rowY = chIdx === 0 ? 14 : 46;
      const rowH = labels.length === 1 ? 38 : 22;

      ctx.fillStyle = "#8B949E";
      ctx.font = "bold 12px monospace";
      ctx.fillText(labels[chIdx], 10, rowY + rowH - 6);

      const peakNorm = dbToNorm(peaks[chIdx] !== undefined ? peaks[chIdx] : latestVu.peak_db);
      const rmsNorm = dbToNorm(rmsList[chIdx] !== undefined ? rmsList[chIdx] : latestVu.rms_db);
      const holdNorm = dbToNorm(holds[chIdx] !== undefined ? holds[chIdx] : latestVu.peak_hold_db);

      const activePeakSegs = Math.round(peakNorm * numSegments);
      const activeRmsSegs = Math.round(rmsNorm * numSegments);
      const holdSeg = Math.min(numSegments - 1, Math.round(holdNorm * (numSegments - 1)));

      for (let s = 0; s < numSegments; s++) {{
        const segDb = -60.0 + (s / numSegments) * 60.0;
        let onColor = "#2EA043";
        let dimColor = "#12261A";
        if (segDb >= -6.0) {{
          onColor = "#F85149";
          dimColor = "#2D1215";
        }} else if (segDb >= -18.0) {{
          onColor = "#D29922";
          dimColor = "#2B2111";
        }}

        const x = leftPad + s * (segW + segGap);
        if (s < activeRmsSegs || s < activePeakSegs) {{
          ctx.fillStyle = onColor;
        }} else {{
          ctx.fillStyle = dimColor;
        }}
        ctx.fillRect(x, rowY, segW, rowH);

        if (s === holdSeg && holdNorm > 0.02) {{
          ctx.fillStyle = "#F0F6FC";
          ctx.fillRect(x, rowY, Math.max(2, segW), rowH);
        }}
      }}
    }}

    // Scale ticks at bottom
    const ticks = [-60, -48, -36, -24, -18, -12, -6, -3, 0];
    ctx.fillStyle = "#6E7681";
    ctx.font = "10px monospace";
    for (let i = 0; i < ticks.length; i++) {{
      const t = ticks[i];
      const tx = leftPad + dbToNorm(t) * barW;
      ctx.fillText(t + "", tx - 8, h - 4);
    }}

    requestAnimationFrame(drawVuMeter);
  }}

  showView(isAuthenticated);
  refreshMicAndTalkbackUi();
  requestAnimationFrame(drawVuMeter);
}})();
</script>
</body>
</html>
"""


def html_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
