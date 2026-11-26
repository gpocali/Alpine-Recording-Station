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
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

from app.audio_engine import AudioEngine, linear_to_dbfs, resample_mono_signal
from app.config_manager import MAX_PROJECT_NAME_LENGTH, ConfigManager, sanitize_project_name
from app.network_info import get_lan_ip_addresses
from app.prompter_engine import sanitize_script_html
from app.qr_code import encode_qr_matrix, qr_matrix_to_svg
from app.session_cli import SessionControlServer, build_session_urls, resolve_runtime_dir
from app.ssl_manager import CertificateInfo, SslCertificateManager
from app.web_auth import WebUserAccount, WebUserManager

logger = logging.getLogger("AlpineRecorder.RemoteProducer")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
STREAM_SAMPLE_RATE = 24000  # 24 kHz low-latency wideband voice stream
MAX_HTTP_CONNECTIONS = 32
MAX_WS_CLIENTS = 10
MAX_HTTP_POST_BYTES = 16384
MAX_SCRIPT_POST_BYTES = 524288
HTTP_SOCKET_TIMEOUT_SEC = 10.0
WS_IDLE_TIMEOUT_SEC = 35.0

# Binary WebSocket frame headers
FRAME_TALENT_AUDIO = 0x01     # Server -> Producer: [0x01, flags, sample_rate_div100_le16, int16_pcm...]
FRAME_PRODUCER_AUDIO = 0x02   # Producer -> Server: [0x02, flags, sample_rate_div100_le16, int16_pcm...]


def normalize_ip_str(raw: str) -> str:
    """Normalize an IPv4/IPv6 address string (unwrapping IPv4-mapped IPv6), or return ''."""
    candidate = (raw or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("["):
        end_bracket = candidate.find("]")
        if end_bracket == -1:
            return ""
        candidate = candidate[1:end_bracket]
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        if candidate.count(":") == 1 and "." in candidate:
            host_part = candidate.split(":", 1)[0]
            try:
                addr = ipaddress.ip_address(host_part)
            except ValueError:
                return ""
        else:
            return ""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.compressed


def parse_trusted_proxies(spec: str) -> Tuple[ipaddress._BaseNetwork, ...]:
    """Parse a comma/whitespace-separated list of trusted proxy IPs or CIDR subnets."""
    networks: List[ipaddress._BaseNetwork] = []
    for token in re.split(r"[\s,]+", (spec or "").strip()):
        tok = token.strip()
        if not tok:
            continue
        if tok.startswith("[") and tok.endswith("]"):
            tok = tok[1:-1]
        if "%" in tok and "/" not in tok:
            tok = tok.split("%", 1)[0]
        try:
            net = ipaddress.ip_network(tok, strict=False)
            if (
                isinstance(net, ipaddress.IPv6Network)
                and net.prefixlen == 128
                and net.network_address.ipv4_mapped is not None
            ):
                net = ipaddress.ip_network(net.network_address.ipv4_mapped)
            networks.append(net)
        except ValueError:
            logger.warning("Ignoring invalid trusted proxy entry %r", tok)
    return tuple(networks)


def is_ip_trusted(ip_str: str, trusted_networks: Tuple[ipaddress._BaseNetwork, ...]) -> bool:
    """Return True if ``ip_str`` belongs to any network in ``trusted_networks``."""
    if not trusted_networks:
        return False
    norm = normalize_ip_str(ip_str)
    if not norm:
        return False
    try:
        addr = ipaddress.ip_address(norm)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in trusted_networks)


def resolve_client_ip(
    peer_ip: str,
    x_forwarded_for: str = "",
    x_real_ip: str = "",
    trusted_networks: Tuple[ipaddress._BaseNetwork, ...] = (),
) -> str:
    """Resolve the effective client IP for rate limiting, honoring forwarding headers only from trusted proxies."""
    peer = normalize_ip_str(peer_ip) or (peer_ip or "unknown").strip() or "unknown"
    if not is_ip_trusted(peer, trusted_networks):
        return peer

    if x_forwarded_for:
        chain: List[str] = []
        for part in x_forwarded_for.split(","):
            norm = normalize_ip_str(part)
            if norm:
                chain.append(norm)
        if chain:
            for candidate in reversed(chain):
                if not is_ip_trusted(candidate, trusted_networks):
                    return candidate
            return chain[0]

    if x_real_ip:
        real = normalize_ip_str(x_real_ip)
        if real:
            return real

    return peer


@dataclass
class _IpAttemptRecord:
    failures: Deque[float] = field(default_factory=deque)
    banned_until: float = 0.0
    ban_count: int = 0
    last_seen: float = 0.0


class LoginRateLimiter:
    """Fail2Ban-style thread-safe sliding-window login attempt limiter with escalating IP bans."""

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        ban_seconds: float = 900.0,
        max_ban_seconds: Optional[float] = None,
        max_tracked_ips: int = 4096,
        global_max_failures: int = 40,
        global_window_seconds: float = 60.0,
        global_cooldown_seconds: float = 60.0,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock_fn
        self._records: Dict[str, _IpAttemptRecord] = {}
        self._global_failures: Deque[float] = deque()
        self._global_cooldown_until: float = 0.0

        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(10.0, float(window_seconds))
        self.ban_seconds = max(10.0, float(ban_seconds))
        cap = 7200.0 if max_ban_seconds is None else float(max_ban_seconds)
        self.max_ban_seconds = max(self.ban_seconds, cap)
        self.max_tracked_ips = max(16, int(max_tracked_ips))
        self.global_max_failures = max(5, int(global_max_failures))
        self.global_window_seconds = max(5.0, float(global_window_seconds))
        self.global_cooldown_seconds = max(5.0, float(global_cooldown_seconds))

    def configure(
        self,
        max_attempts: Optional[int] = None,
        window_seconds: Optional[float] = None,
        ban_seconds: Optional[float] = None,
        max_ban_seconds: Optional[float] = None,
    ) -> None:
        """Update rate-limiter thresholds in place while preserving active bans."""
        with self._lock:
            if max_attempts is not None:
                self.max_attempts = max(1, int(max_attempts))
            if window_seconds is not None:
                self.window_seconds = max(10.0, float(window_seconds))
            if ban_seconds is not None:
                self.ban_seconds = max(10.0, float(ban_seconds))
            if max_ban_seconds is not None:
                self.max_ban_seconds = max(self.ban_seconds, float(max_ban_seconds))
            else:
                self.max_ban_seconds = max(self.ban_seconds, self.max_ban_seconds)

    def _prune_stale_locked(self, now: float) -> None:
        g_cutoff = now - self.global_window_seconds
        while self._global_failures and self._global_failures[0] < g_cutoff:
            self._global_failures.popleft()

        w_cutoff = now - self.window_seconds
        escalation_ttl = max(self.ban_seconds * 4.0, 3600.0)
        stale_ips: List[str] = []
        for ip, rec in self._records.items():
            while rec.failures and rec.failures[0] < w_cutoff:
                rec.failures.popleft()
            if rec.banned_until <= now:
                if not rec.failures and (
                    rec.ban_count <= 0 or (now - rec.last_seen) > escalation_ttl
                ):
                    stale_ips.append(ip)
        for ip in stale_ips:
            self._records.pop(ip, None)

    def check_allowed(self, ip: str) -> Tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)`` for ``ip``."""
        ip_key = (ip or "unknown").strip() or "unknown"
        with self._lock:
            now = self._clock()
            rec = self._records.get(ip_key)
            ip_rem = max(0.0, rec.banned_until - now) if rec is not None else 0.0
            global_rem = max(0.0, self._global_cooldown_until - now)
            wait_sec = max(ip_rem, global_rem)
            if wait_sec > 0.0:
                return (False, max(1, int(math.ceil(wait_sec))))
            return (True, 0)

    def record_failure(self, ip: str) -> Tuple[bool, int, int]:
        """Record a failed login attempt for ``ip``.

        Returns ``(is_banned, retry_after_seconds, attempts_remaining)``.
        """
        ip_key = (ip or "unknown").strip() or "unknown"
        with self._lock:
            now = self._clock()
            self._prune_stale_locked(now)

            self._global_failures.append(now)
            if len(self._global_failures) >= self.global_max_failures:
                self._global_cooldown_until = max(
                    self._global_cooldown_until, now + self.global_cooldown_seconds
                )
                self._global_failures.clear()
                logger.warning(
                    "Global login failure threshold reached (%d failures); activating %ds cooldown",
                    self.global_max_failures,
                    int(self.global_cooldown_seconds),
                )

            rec = self._records.get(ip_key)
            if rec is not None and rec.banned_until > now:
                rec.last_seen = now
                rem = max(
                    rec.banned_until - now,
                    max(0.0, self._global_cooldown_until - now),
                )
                return (True, max(1, int(math.ceil(rem))), 0)

            if rec is None:
                if len(self._records) >= self.max_tracked_ips:
                    unbanned = [k for k, v in self._records.items() if v.banned_until <= now]
                    if unbanned:
                        victim = min(unbanned, key=lambda k: self._records[k].last_seen)
                    else:
                        victim = min(self._records.keys(), key=lambda k: self._records[k].banned_until)
                    self._records.pop(victim, None)
                rec = _IpAttemptRecord()
                self._records[ip_key] = rec

            escalation_ttl = max(self.ban_seconds * 4.0, 3600.0)
            if rec.last_seen > 0.0 and (now - rec.last_seen) > escalation_ttl and rec.banned_until <= now:
                rec.ban_count = 0

            rec.failures.append(now)
            rec.last_seen = now

            if len(rec.failures) >= self.max_attempts:
                rec.ban_count += 1
                duration = min(
                    self.max_ban_seconds,
                    self.ban_seconds * (2 ** (rec.ban_count - 1)),
                )
                rec.banned_until = now + duration
                rec.failures.clear()
                logger.warning(
                    "Fail2Ban lockout triggered for IP %s for %ds (repeat ban #%d)",
                    ip_key,
                    int(math.ceil(duration)),
                    rec.ban_count,
                )
                rem = max(duration, max(0.0, self._global_cooldown_until - now))
                return (True, max(1, int(math.ceil(rem))), 0)

            if self._global_cooldown_until > now:
                g_rem = self._global_cooldown_until - now
                return (True, max(1, int(math.ceil(g_rem))), 0)

            remaining = max(0, self.max_attempts - len(rec.failures))
            return (False, 0, remaining)

    def record_success(self, ip: str) -> None:
        """Clear prior failed-attempt history for ``ip`` after successful authentication."""
        ip_key = (ip or "unknown").strip() or "unknown"
        with self._lock:
            now = self._clock()
            rec = self._records.get(ip_key)
            if rec is not None and rec.banned_until <= now:
                self._records.pop(ip_key, None)

    def clear_all(self) -> int:
        """Clear all tracked failures, active IP bans, and global cooldown. Return active bans cleared."""
        with self._lock:
            now = self._clock()
            active = sum(1 for rec in self._records.values() if rec.banned_until > now)
            if self._global_cooldown_until > now and active == 0:
                active = 1
            self._records.clear()
            self._global_failures.clear()
            self._global_cooldown_until = 0.0
            return active

    def unban_ip(self, ip: str) -> bool:
        """Remove a specific IP from the lockout/tracking table. Return True if it was actively banned."""
        ip_key = normalize_ip_str(ip) or (ip or "").strip()
        if not ip_key:
            return False
        with self._lock:
            now = self._clock()
            rec = self._records.pop(ip_key, None)
            return bool(rec is not None and rec.banned_until > now)

    def get_banned_summary(self) -> List[Dict[str, Any]]:
        """Return active IP bans sorted by longest remaining lockout."""
        with self._lock:
            now = self._clock()
            self._prune_stale_locked(now)
            out: List[Dict[str, Any]] = []
            for ip, rec in self._records.items():
                if rec.banned_until > now:
                    out.append({
                        "ip": ip,
                        "remaining_seconds": max(1, int(math.ceil(rec.banned_until - now))),
                        "ban_count": int(rec.ban_count),
                    })
            out.sort(key=lambda item: (-int(item["remaining_seconds"]), str(item["ip"])))
            return out


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


@dataclass(frozen=True)
class AuthPrincipal:
    """Authenticated Web UI caller identity (temporary session guest or standing local login)."""

    kind: str  # "session" or "local"
    username: str
    role: str  # "guest" or "admin"/"producer"
    session_id: str

    @property
    def is_local(self) -> bool:
        return self.kind == "local"


class WebSocketConnection:
    """Minimal, thread-safe RFC 6455 WebSocket connection wrapper."""

    def __init__(
        self,
        sock: socket.socket,
        client_address: Tuple[str, int],
        session_id: str,
        auth_kind: str = "session",
        username: str = "",
    ):
        self.sock = sock
        self.client_address = client_address
        self.session_id = session_id
        self.auth_kind = auth_kind
        self.username = username
        self.client_id = secrets.token_hex(4)
        self.connected_at = time.time()
        self.send_lock = threading.Lock()
        self.closed = False
        # Producer client talkback & monitor state (latched when idle, push-to-talk when recording)
        self.mic_send_enabled = True
        self.cough_muted = False
        self.talkback_active = False
        self.talkback_latched = False
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


def _read_exact(
    sock: socket.socket, n: int, max_idle_seconds: Optional[float] = None
) -> Optional[bytes]:
    buf = bytearray()
    deadline = (
        (time.monotonic() + float(max_idle_seconds))
        if max_idle_seconds is not None and max_idle_seconds > 0
        else None
    )
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (socket.timeout, TimeoutError):
            if deadline is not None and time.monotonic() >= deadline:
                return None
            continue
        except ssl.SSLError as exc:
            if "timed out" in str(exc).lower():
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                continue
            return None
        except Exception:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
        if deadline is not None:
            deadline = time.monotonic() + float(max_idle_seconds)  # type: ignore[arg-type]
    return bytes(buf)


def read_ws_frame(
    sock: socket.socket,
    max_payload: int = 262144,
    *,
    require_mask: bool = False,
    max_idle_seconds: Optional[float] = None,
) -> Optional[Tuple[int, bytes]]:
    """Read a single RFC 6455 WebSocket frame."""
    hdr = _read_exact(sock, 2, max_idle_seconds=max_idle_seconds)
    if not hdr:
        return None
    b1, b2 = hdr[0], hdr[1]
    if (b1 & 0x70) != 0:
        # No RSV extensions are negotiated
        return None
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    if require_mask and not masked:
        return None

    if length == 126:
        ext = _read_exact(sock, 2, max_idle_seconds=max_idle_seconds)
        if not ext:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _read_exact(sock, 8, max_idle_seconds=max_idle_seconds)
        if not ext:
            return None
        length = struct.unpack("!Q", ext)[0]

    if opcode >= 0x8 and (length > 125 or not fin):
        return None

    if length > max_payload:
        return None

    mask_key = b""
    if masked:
        mask_key = _read_exact(sock, 4, max_idle_seconds=max_idle_seconds)
        if not mask_key:
            return None

    payload = (
        _read_exact(sock, length, max_idle_seconds=max_idle_seconds)
        if length > 0
        else b""
    )
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

    def __init__(self, *args: Any, max_connections: int = MAX_HTTP_CONNECTIONS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._conn_sem = threading.BoundedSemaphore(max(4, int(max_connections)))

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._conn_sem.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._conn_sem.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conn_sem.release()

    def finish_request(self, request: Any, client_address: Any) -> None:
        try:
            request.settimeout(HTTP_SOCKET_TIMEOUT_SEC)
        except OSError:
            pass
        if hasattr(request, "do_handshake"):
            try:
                request.do_handshake()
            except (OSError, ValueError):
                return
        super().finish_request(request, client_address)

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.debug("RemoteProducerHTTPS request error from %s", client_address, exc_info=True)


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
        on_project_name_changed: Optional[Callable[[str], None]] = None,
        on_script_updated: Optional[Callable[[str, str], None]] = None,
        web_user_manager: Optional[WebUserManager] = None,
        host: str = "0.0.0.0",
        port: Optional[int] = None,
        runtime_dir: Optional[Path | str] = None,
    ):
        self.audio_engine = audio_engine
        self.config_manager = config_manager
        self.ssl_manager = ssl_manager or SslCertificateManager(
            config_manager=config_manager,
            cert_dir=Path(config_manager.etc_config_dir) / "ssl",
            drop_dir=Path(config_manager.storage_dir) / "ssl",
        )
        self.status_provider = status_provider
        self.on_project_name_changed = on_project_name_changed
        self.on_script_updated = on_script_updated
        self.web_user_manager = web_user_manager or WebUserManager(
            etc_config_dir=config_manager.etc_config_dir,
            storage_dir=config_manager.storage_dir,
            on_persist_lbu=config_manager.trigger_lbu_commit_async,
        )
        self.host = host
        self._configured_port = (
            port if port is not None else int(getattr(config_manager.settings, "remote_producer_port", 8443))
        )

        self._session_lock = threading.RLock()
        self.session = ProducerSession()
        self._httpd: Optional[_SecureProducerHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._broadcast_thread: Optional[threading.Thread] = None
        self._running = threading.Event()

        s = self.config_manager.settings
        self.rate_limiter = LoginRateLimiter(
            max_attempts=int(getattr(s, "remote_producer_max_login_attempts", 5) or 5),
            window_seconds=float(getattr(s, "remote_producer_lockout_window_sec", 300) or 300),
            ban_seconds=float(getattr(s, "remote_producer_ban_duration_sec", 900) or 900),
        )
        self._trusted_networks: Tuple[ipaddress._BaseNetwork, ...] = parse_trusted_proxies(
            str(getattr(s, "remote_producer_trusted_proxies", "127.0.0.1,::1") or "127.0.0.1,::1")
        )

        self._clients_lock = threading.Lock()
        self._clients: Dict[str, WebSocketConnection] = {}
        self._cert_info: Optional[CertificateInfo] = None
        self._tb_peak_hold_db: float = -60.0
        resolved_rt_dir = resolve_runtime_dir(
            etc_config_dir=self.config_manager.etc_config_dir,
            runtime_dir=runtime_dir,
        )
        self._control_server = SessionControlServer(
            runtime_dir=resolved_rt_dir,
            state_provider=self.get_session_info,
            action_handler=self._handle_control_action,
        )
        self.audio_engine.register_talkback_unlatch_listener(self._on_engine_talkback_unlatched)
        self.audio_engine.register_talkback_state_listener(self._on_engine_talkback_state_changed)

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
        return qr_matrix_to_svg(matrix, border=4, dark_color="#000000")

    def get_qr_matrix(self, include_invite_token: bool = True, host_override: Optional[str] = None) -> List[List[bool]]:
        url = self.get_session_url(include_invite_token=include_invite_token, host_override=host_override)
        return encode_qr_matrix(url)

    def get_session_info(self, host_override: Optional[str] = None) -> Dict[str, Any]:
        """Build a live metadata & credential snapshot for the CLI control plane."""
        with self._session_lock:
            lan_ips = get_lan_ip_addresses()
            hosts = list(lan_ips) if lan_ips else ["127.0.0.1"]
            primary = str(host_override).strip() if host_override else (lan_ips[0] if lan_ips else "127.0.0.1")
            active_port = int(self.port)
            sid = self.session.session_id
            pin = self.session.session_passcode
            token = self.session.invite_token
            created_at = float(self.session.created_at)
            session_url, login_url = build_session_urls(primary, active_port, sid, token)
            cert_info = self._cert_info
            banned_summary = self.rate_limiter.get_banned_summary()
            return {
                "pid": os.getpid(),
                "updated_at": time.time(),
                "session_created_at": created_at,
                "server_running": bool(self.is_running),
                "remote_producer_enabled": bool(
                    getattr(self.config_manager.settings, "remote_producer_enabled", False)
                ),
                "port": active_port,
                "hosts": hosts,
                "primary_host": primary,
                "session_id": sid,
                "session_passcode": pin,
                "invite_token": token,
                "session_url": session_url,
                "login_url": login_url,
                "project_name": str(self.config_manager.settings.project_name or "Session"),
                "connected_clients": int(self.connected_client_count),
                "talent_mic_live": bool(self.audio_engine.is_talent_mic_live_to_producer),
                "ssl_mode": cert_info.mode if cert_info is not None else "",
                "ssl_fingerprint_sha256": cert_info.fingerprint_sha256 if cert_info is not None else "",
                "banned_ip_count": len(banned_summary),
                "banned_ips": banned_summary,
                "max_login_attempts": int(self.rate_limiter.max_attempts),
                "ban_duration_sec": int(self.rate_limiter.ban_seconds),
                "trusted_proxies": str(
                    getattr(self.config_manager.settings, "remote_producer_trusted_proxies", "127.0.0.1,::1")
                ),
            }

    def reload_security_settings(self) -> None:
        """Reload Fail2Ban rate-limiter thresholds and trusted proxy CIDR networks from ConfigManager."""
        s = self.config_manager.settings
        self.rate_limiter.configure(
            max_attempts=int(getattr(s, "remote_producer_max_login_attempts", 5) or 5),
            window_seconds=float(getattr(s, "remote_producer_lockout_window_sec", 300) or 300),
            ban_seconds=float(getattr(s, "remote_producer_ban_duration_sec", 900) or 900),
        )
        self._trusted_networks = parse_trusted_proxies(
            str(getattr(s, "remote_producer_trusted_proxies", "127.0.0.1,::1") or "127.0.0.1,::1")
        )
        self._control_server.sync_state()

    def clear_login_bans(self, ip: Optional[str] = None) -> int:
        """Clear all active login bans (or a single IP if provided)."""
        if ip and str(ip).strip():
            cleared = 1 if self.rate_limiter.unban_ip(str(ip)) else 0
        else:
            cleared = self.rate_limiter.clear_all()
        self._control_server.sync_state()
        return cleared

    def _resolve_client_ip(self, handler: BaseHTTPRequestHandler) -> str:
        peer = str(handler.client_address[0]) if handler.client_address else ""
        xff = handler.headers.get("X-Forwarded-For", "")
        xri = handler.headers.get("X-Real-IP", "")
        return resolve_client_ip(peer, xff, xri, self._trusted_networks)

    @staticmethod
    def _normalize_host_port(netloc: str, default_scheme: str = "https") -> Tuple[str, Optional[int]]:
        clean = (netloc or "").strip()
        if not clean:
            return ("", None)
        try:
            parsed = urllib.parse.urlsplit(f"{default_scheme}://{clean}")
            host = (parsed.hostname or "").strip().lower()
            port = parsed.port
            if port is None:
                port = 443 if default_scheme.lower() == "https" else 80
            return (host, port)
        except Exception:
            return (clean.lower(), None)

    def _is_same_origin_request(
        self, handler: BaseHTTPRequestHandler, *, is_navigation: bool = False
    ) -> bool:
        """Validate Origin and Fetch Metadata headers to block CSWSH and cross-site CSRF."""
        if not is_navigation:
            sec_site = (handler.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if sec_site and sec_site not in ("same-origin", "none"):
                return False

        origin = (handler.headers.get("Origin") or "").strip()
        if not origin:
            return True
        if origin.lower() == "null":
            return False

        try:
            parsed_origin = urllib.parse.urlsplit(origin)
        except Exception:
            return False

        orig_scheme = (parsed_origin.scheme or "").lower()
        if orig_scheme not in ("https", "http") or not parsed_origin.netloc:
            return False

        peer_ip = str(handler.client_address[0]) if handler.client_address else ""
        peer_trusted = is_ip_trusted(peer_ip, self._trusted_networks)

        expected_scheme = "https"
        if peer_trusted:
            xf_proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
            if xf_proto in ("http", "https"):
                expected_scheme = xf_proto
        if orig_scheme != expected_scheme:
            return False

        orig_hp = self._normalize_host_port(parsed_origin.netloc, default_scheme=orig_scheme)
        if not orig_hp[0]:
            return False

        candidate_hosts: List[str] = []
        host_hdr = (handler.headers.get("Host") or "").strip()
        if host_hdr:
            candidate_hosts.append(host_hdr)
        if peer_trusted:
            xf_host = (handler.headers.get("X-Forwarded-Host") or "").strip()
            for item in xf_host.split(","):
                item_clean = item.strip()
                if item_clean:
                    candidate_hosts.append(item_clean)

        if not candidate_hosts:
            return False

        for cand in candidate_hosts:
            cand_hp = self._normalize_host_port(cand, default_scheme=expected_scheme)
            if not cand_hp[0]:
                continue
            if orig_hp == cand_hp:
                return True
            if peer_trusted and orig_hp[0] == cand_hp[0]:
                return True

        return False

    def start_control_server(self, persistent: bool = True) -> bool:
        """Start the local loopback control server and sync ``web_session.json``."""
        return self._control_server.start(persistent=persistent)

    def stop_control_server(self) -> None:
        """Stop the local loopback control server and clean up ``web_session.json``."""
        self._control_server.persistent = False
        self._control_server.stop()

    def shutdown(self) -> None:
        """Fully shut down both the HTTPS server and the CLI control server."""
        self._control_server.persistent = False
        self.stop()
        self._control_server.stop()

    def _handle_control_action(
        self, action: str, req: Dict[str, Any]
    ) -> Tuple[bool, str, Dict[str, Any]]:
        if action == "status":
            return (True, "Active studio session retrieved.", self.get_session_info())

        if action == "rotate":
            self.rotate_session()
            return (True, f"Session rotated to {self.session.session_id}.", self.get_session_info())

        if action == "enable":
            req_port = req.get("port")
            if req_port is not None:
                try:
                    target_port = int(req_port)
                except (TypeError, ValueError):
                    return (False, "Invalid port number.", self.get_session_info())
                if not (1024 <= target_port <= 65535):
                    return (False, "Port must be between 1024 and 65535.", self.get_session_info())
            else:
                if self._configured_port == 0:
                    target_port = 0
                else:
                    target_port = int(
                        getattr(self.config_manager.settings, "remote_producer_port", self._configured_port)
                        or 8443
                    )

            if bool(req.get("rotate", False)):
                self.rotate_session()

            if self.is_running and self.port != target_port:
                self.stop()

            ok = self.start(port=target_port)
            if ok:
                self.config_manager.settings.remote_producer_enabled = True
                self.config_manager.settings.remote_producer_port = int(self.port)
                self.config_manager.save(commit_lbu=True)
                return (
                    True,
                    f"Remote Producer HTTPS server active on port {self.port}.",
                    self.get_session_info(),
                )
            return (
                False,
                f"Could not start Remote Producer HTTPS server on port {target_port}.",
                self.get_session_info(),
            )

        if action == "disable":
            self.config_manager.settings.remote_producer_enabled = False
            self.config_manager.save(commit_lbu=True)
            self.stop()
            return (True, "Remote Producer HTTPS server stopped.", self.get_session_info())

        if action == "unban":
            cleared = self.clear_login_bans(req.get("ip"))
            return (
                True,
                f"Cleared {cleared} active login lockout(s).",
                self.get_session_info(),
            )

        if action == "reload_users":
            self.web_user_manager.load()
            # Disconnect any local WebSocket clients whose user was removed
            with self._clients_lock:
                to_drop = [
                    c
                    for c in self._clients.values()
                    if c.auth_kind == "local"
                    and self.web_user_manager.get_user(c.username) is None
                ]
                for c in to_drop:
                    self._clients.pop(c.client_id, None)
            for client in to_drop:
                client.close()
            if to_drop:
                self._recompute_remote_talkback_state()
            return (True, "Reloaded local Web UI accounts.", self.get_session_info())

        return (False, f"Unsupported action: {action}", self.get_session_info())

    def rotate_session(self) -> ProducerSession:
        """
        Rotate session ID, passcode, and invite token, disconnecting any
        temporary session-based clients while keeping standing local logins active.
        """
        with self._session_lock:
            self.session.rotate()
            sid = self.session.session_id
        self._disconnect_session_clients()
        self._recompute_remote_talkback_state()
        self.broadcast_state_snapshot()
        self._control_server.sync_state()
        logger.info("Rotated remote producer session -> %s", sid)
        return self.session

    def _disconnect_session_clients(self) -> None:
        with self._clients_lock:
            to_close = [c for c in self._clients.values() if c.auth_kind != "local"]
            for c in to_close:
                self._clients.pop(c.client_id, None)
        for client in to_close:
            client.close()

    def reload_ssl_certificates(self) -> CertificateInfo:
        """Reload SSL certificates (and restart listener if running)."""
        custom_cert = getattr(self.config_manager.settings, "ssl_cert_path", "") or None
        custom_key = getattr(self.config_manager.settings, "ssl_key_path", "") or None
        self._cert_info = self.ssl_manager.get_certificate_info(custom_cert, custom_key)
        if self.is_running:
            port = self._configured_port
            self.stop()
            self.start(port=port)
        else:
            self._control_server.sync_state()
        return self._cert_info

    def start(self, port: Optional[int] = None) -> bool:
        if port is not None:
            self._configured_port = int(port)
        if self.is_running:
            self._control_server.start(persistent=self._control_server.persistent)
            self._control_server.sync_state()
            return True

        try:
            custom_cert = getattr(self.config_manager.settings, "ssl_cert_path", "") or None
            custom_key = getattr(self.config_manager.settings, "ssl_key_path", "") or None
            ssl_ctx = self.ssl_manager.create_ssl_context(custom_cert, custom_key)
            self._cert_info = self.ssl_manager.get_certificate_info(custom_cert, custom_key)

            server_ref = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"
                timeout = HTTP_SOCKET_TIMEOUT_SEC

                def log_message(self, fmt: str, *args: Any) -> None:
                    logger.debug("RemoteProducerHTTP: " + fmt, *args)

                def do_GET(self) -> None:
                    server_ref._handle_http_get(self)

                def do_POST(self) -> None:
                    server_ref._handle_http_post(self)

            httpd = _SecureProducerHTTPServer((self.host, self._configured_port), Handler)
            httpd.socket = ssl_ctx.wrap_socket(
                httpd.socket,
                server_side=True,
                do_handshake_on_connect=False,
            )
            self._httpd = httpd
            self._running.set()

            self.audio_engine.register_live_audio_listener(self._on_live_talent_audio)
            self.audio_engine.register_talkback_unlatch_listener(self._on_engine_talkback_unlatched)
            self.audio_engine.register_talkback_state_listener(self._on_engine_talkback_state_changed)

            self._server_thread = threading.Thread(
                target=self._serve_forever, daemon=True, name="RemoteProducerHTTPS"
            )
            self._server_thread.start()

            self._broadcast_thread = threading.Thread(
                target=self._state_broadcast_loop, daemon=True, name="RemoteProducerStateBroadcast"
            )
            self._broadcast_thread.start()

            self._control_server.start(persistent=self._control_server.persistent)
            self._control_server.sync_state()

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
        self.audio_engine.set_remote_talkback(False, latched=False)
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

        if self._broadcast_thread and self._broadcast_thread.is_alive():
            self._broadcast_thread.join(timeout=0.5)
        self._broadcast_thread = None

        if self._control_server.persistent:
            self._control_server.sync_state()
        else:
            self._control_server.stop()

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
        if self.audio_engine.is_playing:
            flags |= 0x04
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
        elif self.audio_engine.is_playing:
            state_code = "PLAYBACK_PAUSED" if self.audio_engine.is_paused else "PLAYING"
        elif self.audio_engine.is_monitoring:
            state_code = "MONITORING"
        else:
            state_code = "STOPPED"

        rec_path = self.audio_engine.current_recording_path
        active_take = extra.get("active_take_name", rec_path.name if rec_path else "")

        play_path = self.audio_engine.playing_filepath
        playing_take_name = str(extra.get("playing_take_name", play_path.name if play_path else ""))
        is_playing = bool(self.audio_engine.is_playing)
        is_play_paused = bool(self.audio_engine.is_paused)
        play_position_sec = round(float(self.audio_engine.play_position_sec), 2)
        play_duration_sec = round(float(self.audio_engine.play_duration_sec), 2)

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

        talent_live = bool(self.audio_engine.is_talent_mic_live_to_producer)
        tb_active = bool(self.audio_engine.talkback_active)
        local_tb = bool(self.audio_engine.local_talkback_active)

        if not vu.active or vu.peak_hold_level <= 1e-5:
            self._tb_peak_hold_db = -60.0

        if is_playing:
            eff_ch = 2 if settings.studio_mode else max(1, min(2, int(settings.effective_channels)))
            raw_peaks = list(vu.channel_peaks[:eff_ch]) if vu.channel_peaks else [vu.peak_level]
            raw_rms = list(vu.channel_rms[:eff_ch]) if vu.channel_rms else [vu.rms_level]
            raw_holds = list(vu.channel_peak_holds[:eff_ch]) if vu.channel_peak_holds else [vu.peak_hold_level]
            while len(raw_peaks) < eff_ch:
                raw_peaks.append(raw_peaks[0] if raw_peaks else 0.0)
            while len(raw_rms) < eff_ch:
                raw_rms.append(raw_rms[0] if raw_rms else 0.0)
            while len(raw_holds) < eff_ch:
                raw_holds.append(raw_holds[0] if raw_holds else 0.0)
            ch_peaks_db = [round(float(linear_to_dbfs(x)), 1) for x in raw_peaks]
            ch_rms_db = [round(float(linear_to_dbfs(x)), 1) for x in raw_rms]
            ch_holds_db = [round(float(linear_to_dbfs(x)), 1) for x in raw_holds]
            overall_peak_db = round(float(vu.peak_db), 1)
            overall_rms_db = round(float(vu.rms_db), 1)
            overall_hold_db = round(float(vu.peak_hold_db), 1)
        elif settings.studio_mode:
            # Channel 0: VO (L) — Talent VO Track (muted during Talkback or when Talent Mic is muted/cough-muted)
            if not tb_active and talent_live:
                vo_peak_db = round(float(linear_to_dbfs(vu.peak_level)), 1)
                vo_rms_db = round(float(linear_to_dbfs(vu.rms_level)), 1)
            else:
                vo_peak_db = -60.0
                vo_rms_db = -60.0
            vo_hold_db = (
                round(float(linear_to_dbfs(vu.peak_hold_level)), 1)
                if talent_live
                else -60.0
            )

            # Channel 1: TB (R) — Talkback Track (only active while Talkback is engaged)
            if tb_active:
                rem_peak, rem_rms = self.audio_engine.get_remote_talkback_levels()
                loc_peak = vu.peak_level if (local_tb or talent_live) else 0.0
                loc_rms = vu.rms_level if (local_tb or talent_live) else 0.0
                tb_peak_lin = min(1.0, loc_peak + rem_peak)
                tb_rms_lin = min(1.0, math.sqrt(loc_rms * loc_rms + rem_rms * rem_rms))
                tb_peak_db = round(float(linear_to_dbfs(tb_peak_lin)), 1)
                tb_rms_db = round(float(linear_to_dbfs(tb_rms_lin)), 1)
                if tb_peak_db > self._tb_peak_hold_db:
                    self._tb_peak_hold_db = tb_peak_db
            else:
                tb_peak_db = -60.0
                tb_rms_db = -60.0
            tb_hold_db = round(float(self._tb_peak_hold_db), 1)

            ch_peaks_db = [vo_peak_db, tb_peak_db]
            ch_rms_db = [vo_rms_db, tb_rms_db]
            ch_holds_db = [vo_hold_db, tb_hold_db]
            overall_peak_db = tb_peak_db if tb_active else vo_peak_db
            overall_rms_db = tb_rms_db if tb_active else vo_rms_db
            overall_hold_db = max(vo_hold_db, tb_hold_db)
        else:
            eff_ch = max(1, min(2, int(settings.effective_channels)))
            rem_peak, rem_rms = (
                self.audio_engine.get_remote_talkback_levels()
                if tb_active
                else (0.0, 0.0)
            )
            if talent_live or local_tb:
                raw_peaks = list(vu.channel_peaks[:eff_ch]) if vu.channel_peaks else [vu.peak_level]
                raw_rms = list(vu.channel_rms[:eff_ch]) if vu.channel_rms else [vu.rms_level]
                raw_holds = list(vu.channel_peak_holds[:eff_ch]) if vu.channel_peak_holds else [vu.peak_hold_level]
                while len(raw_peaks) < eff_ch:
                    raw_peaks.append(raw_peaks[0] if raw_peaks else 0.0)
                while len(raw_rms) < eff_ch:
                    raw_rms.append(raw_rms[0] if raw_rms else 0.0)
                while len(raw_holds) < eff_ch:
                    raw_holds.append(raw_holds[0] if raw_holds else 0.0)
            else:
                raw_peaks = [0.0] * eff_ch
                raw_rms = [0.0] * eff_ch
                raw_holds = [0.0] * eff_ch

            if tb_active:
                raw_peaks = [min(1.0, p + rem_peak) for p in raw_peaks]
                raw_rms = [
                    min(1.0, math.sqrt(r * r + rem_rms * rem_rms)) for r in raw_rms
                ]
                raw_holds = [max(h, p) for h, p in zip(raw_holds, raw_peaks)]

            if talent_live or tb_active:
                ch_peaks_db = [round(float(linear_to_dbfs(x)), 1) for x in raw_peaks]
                ch_rms_db = [round(float(linear_to_dbfs(x)), 1) for x in raw_rms]
                ch_holds_db = [round(float(linear_to_dbfs(x)), 1) for x in raw_holds]
                overall_peak_db = max(ch_peaks_db) if ch_peaks_db else -60.0
                overall_rms_db = max(ch_rms_db) if ch_rms_db else -60.0
                overall_hold_db = max(ch_holds_db) if ch_holds_db else -60.0
                if tb_active and overall_peak_db > self._tb_peak_hold_db:
                    self._tb_peak_hold_db = overall_peak_db
                    tb_hold_val = round(float(self._tb_peak_hold_db), 1)
                    overall_hold_db = max(overall_hold_db, tb_hold_val)
                    ch_holds_db = [max(h, tb_hold_val) for h in ch_holds_db]
            else:
                ch_peaks_db = [-60.0] * eff_ch
                ch_rms_db = [-60.0] * eff_ch
                ch_holds_db = [-60.0] * eff_ch
                overall_peak_db = -60.0
                overall_rms_db = -60.0
                overall_hold_db = -60.0

        return {
            "type": "state",
            "session_id": self.session.session_id,
            "project_name": extra.get("project_name", settings.project_name or "Session"),
            "state": state_code,
            "is_recording": bool(self.audio_engine.is_recording),
            "is_record_paused": bool(self.audio_engine.is_record_paused),
            "is_auto_paused": bool(self.audio_engine.is_auto_paused),
            "mute_auto_pause_timeout_sec": float(
                getattr(settings, "mute_auto_pause_timeout_sec", 30.0)
            ),
            "auto_pause_session_timeout_sec": float(
                getattr(settings, "auto_pause_session_timeout_sec", 1800.0)
            ),
            "is_playing": is_playing,
            "is_play_paused": is_play_paused,
            "play_position_sec": play_position_sec,
            "play_duration_sec": play_duration_sec,
            "playing_take_name": playing_take_name,
            "elapsed_seconds": round(float(self.audio_engine.elapsed_seconds), 2),
            "studio_mode": bool(settings.studio_mode),
            "sample_rate": int(settings.sample_rate),
            "bit_depth": int(settings.bit_depth),
            "channels": int(settings.effective_channels),
            "talkback_tone_hz": float(settings.talkback_tone_hz),
            "talkback_tone_duration_sec": float(settings.talkback_tone_duration_sec),
            "talkback_release_tone_duration_sec": float(settings.talkback_release_tone_duration_sec),
            "talkback_active": tb_active,
            "talkback_latched": bool(self.audio_engine.talkback_latched),
            "remote_talkback_active": bool(self.audio_engine.remote_talkback_active),
            "remote_talkback_latched": bool(self.audio_engine.remote_talkback_latched),
            "local_talkback_active": local_tb,
            "local_talkback_latched": bool(self.audio_engine.local_talkback_latched),
            "talent_mic_live": talent_live,
            "talent_mic_send_enabled": bool(self.audio_engine.talent_mic_send_enabled),
            "talent_cough_muted": bool(self.audio_engine.talent_cough_muted),
            "allow_project_rename": bool(
                getattr(settings, "remote_producer_allow_project_rename", False)
            ),
            "connected_producers": int(self.connected_client_count),
            "take_count": int(extra.get("take_count", 0)),
            "active_take_name": str(active_take),
            "storage_remaining": storage_rem,
            "storage_level": storage_lvl,
            "vu": {
                "rms_db": overall_rms_db,
                "peak_db": overall_peak_db,
                "peak_hold_db": overall_hold_db,
                "clipped": bool(vu.clipped and (talent_live or is_playing)),
                "channel_peaks": ch_peaks_db,
                "channel_rms": ch_rms_db,
                "channel_peak_holds": ch_holds_db,
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
        self._control_server.sync_state()

    def _recompute_remote_talkback_state(self) -> None:
        with self._clients_lock:
            any_active = any(
                (not c.closed) and c.talkback_active and c.mic_send_enabled and (not c.cough_muted)
                for c in self._clients.values()
            )
            any_latched = any(
                (not c.closed)
                and c.talkback_active
                and c.talkback_latched
                and c.mic_send_enabled
                and (not c.cough_muted)
                for c in self._clients.values()
            )
        self.audio_engine.set_remote_talkback(any_active, latched=any_latched)

    def _on_engine_talkback_unlatched(self) -> None:
        """
        Called by AudioEngine when recording starts so any latched/active
        remote producer talkback is immediately unlatched and switched to PTT.
        """
        with self._clients_lock:
            clients = [c for c in self._clients.values() if not c.closed]
            for c in self._clients.values():
                c.talkback_active = False
                c.talkback_latched = False
        if not clients:
            return
        try:
            unlatch_msg = json.dumps({"type": "talkback_unlatched", "reason": "recording_started"})
            snap_msg = json.dumps(self.build_status_snapshot())
        except Exception:
            return
        for client in clients:
            try:
                client.send_text(unlatch_msg)
                client.send_text(snap_msg)
            except Exception:
                pass

    def broadcast_state_snapshot(self) -> None:
        """Immediately push an updated status snapshot to all connected producers."""
        with self._clients_lock:
            clients = [c for c in self._clients.values() if not c.closed]
        if not clients:
            return
        try:
            snap_msg = json.dumps(self.build_status_snapshot())
        except Exception:
            return
        for client in clients:
            try:
                client.send_text(snap_msg)
            except Exception:
                pass

    def broadcast_script_update(
        self, project_name: str, html_content: str, mtime: float = 0.0
    ) -> None:
        """Push a live script update to all connected WebSocket clients."""
        with self._clients_lock:
            clients = [c for c in self._clients.values() if not c.closed]
        if not clients:
            return
        try:
            msg = json.dumps(
                {
                    "type": "script_updated",
                    "project_name": sanitize_project_name(project_name),
                    "html": str(html_content or ""),
                    "mtime": float(mtime or time.time()),
                }
            )
        except Exception:
            return
        for client in clients:
            try:
                client.send_text(msg)
            except Exception:
                pass

    def switch_active_project(self, new_name: str, *, require_permission_setting: bool = False) -> Tuple[bool, str]:
        """Switch the active project folder (when idle) and notify the local UI."""
        if require_permission_setting and not bool(
            getattr(self.config_manager.settings, "remote_producer_allow_project_rename", False)
        ):
            return (False, "Project name editing is disabled in studio settings.")
        if self.audio_engine.is_recording or self.audio_engine.is_record_paused:
            return (False, "Cannot change project name while recording is in progress.")
        ok_name, cleaned_or_err = self.config_manager.validate_new_project_name(new_name)
        if not ok_name:
            return (False, cleaned_or_err)

        self.config_manager.settings.project_name = cleaned_or_err
        self.config_manager.save(commit_lbu=True)
        self.config_manager.ensure_project_dir(cleaned_or_err)
        if self.on_project_name_changed is not None:
            try:
                self.on_project_name_changed(cleaned_or_err)
            except Exception as exc:
                logger.warning("on_project_name_changed callback failed: %s", exc)
        self.broadcast_state_snapshot()
        self._control_server.sync_state()
        return (True, cleaned_or_err)

    def update_project_name(self, new_name: str) -> Tuple[bool, str]:
        """Update the active project name on behalf of an authenticated remote producer."""
        return self.switch_active_project(new_name, require_permission_setting=True)

    def _on_engine_talkback_state_changed(self) -> None:
        """Immediately push updated status snapshot to connected producers when Talkback state changes."""
        self.broadcast_state_snapshot()

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
        handler.send_header("Strict-Transport-Security", "max-age=31536000")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        handler.send_header("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
        handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
        handler.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=(self)")
        handler.send_header("Vary", "Origin, Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site")
        handler.send_header("Cache-Control", "no-store, max-age=0")
        if csp_nonce:
            csp = (
                "default-src 'self'; "
                f"script-src 'nonce-{csp_nonce}' 'strict-dynamic'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "connect-src 'self' wss:; "
                "media-src 'self' blob:; "
                "object-src 'none'; "
                "base-uri 'none'; "
                "form-action 'self'; "
                "frame-ancestors 'none'"
            )
            handler.send_header("Content-Security-Policy", csp)
        if extra_headers:
            for k, v in extra_headers.items():
                handler.send_header(k, v)

    @staticmethod
    def _format_duration_human(seconds: int) -> str:
        secs = max(1, int(seconds))
        mins, rem = divmod(secs, 60)
        hrs, mins = divmod(mins, 60)
        if hrs > 0:
            return f"{hrs}h {mins}m {rem}s"
        if mins > 0:
            return f"{mins}m {rem}s"
        return f"{rem}s"

    def _send_rate_limited(
        self,
        handler: BaseHTTPRequestHandler,
        retry_after: int,
        as_html: bool = False,
        prefill_session: str = "",
        message: Optional[str] = None,
    ) -> None:
        wait_sec = max(1, int(retry_after))
        msg = message or (
            f"Too many failed login attempts. Try again in {self._format_duration_human(wait_sec)}."
        )
        if as_html:
            nonce = secrets.token_urlsafe(16)
            html = self._render_producer_html(
                csp_nonce=nonce,
                authenticated=False,
                prefill_session=prefill_session,
                prefill_token="",
                initial_error=msg,
                initial_retry_after=wait_sec,
            )
            body = html.encode("utf-8")
            handler.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self._set_security_headers(
                handler,
                "text/html; charset=utf-8",
                csp_nonce=nonce,
                extra_headers={"Retry-After": str(wait_sec)},
            )
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return

        self._send_json(
            handler,
            HTTPStatus.TOO_MANY_REQUESTS,
            {"ok": False, "error": msg, "retry_after": wait_sec},
            extra_headers={"Retry-After": str(wait_sec)},
        )

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

    def _authenticate_request(
        self, handler: BaseHTTPRequestHandler, query_params: Dict[str, List[str]]
    ) -> Optional[AuthPrincipal]:
        token = self._extract_auth_token(handler, query_params)
        if not token:
            return None
        if self.session.verify_auth_token(token):
            return AuthPrincipal(
                kind="session",
                username=self.session.session_id,
                role="guest",
                session_id=self.session.session_id,
            )
        local_user = self.web_user_manager.verify_auth_token(token)
        if local_user is not None:
            return AuthPrincipal(
                kind="local",
                username=local_user.username,
                role=local_user.role,
                session_id=self.session.session_id,
            )
        return None

    def _is_request_authenticated(
        self, handler: BaseHTTPRequestHandler, query_params: Dict[str, List[str]]
    ) -> bool:
        return self._authenticate_request(handler, query_params) is not None

    def _handle_http_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        path = parsed.path or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/ws/producer":
            self._handle_websocket_upgrade(handler, query)
            return

        if path == "/api/status":
            if not self._is_same_origin_request(handler):
                self._send_json(handler, HTTPStatus.FORBIDDEN, {"error": "Cross-origin request rejected"})
                return
            principal = self._authenticate_request(handler, query)
            if principal is None:
                self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"authenticated": False})
                return
            snap = self.build_status_snapshot()
            snap["authenticated"] = True
            snap["auth_kind"] = principal.kind
            snap["auth_user"] = principal.username
            snap["auth_role"] = principal.role
            self._send_json(handler, HTTPStatus.OK, snap)
            return

        if path == "/api/projects":
            if not self._is_same_origin_request(handler):
                self._send_json(handler, HTTPStatus.FORBIDDEN, {"error": "Cross-origin request rejected"})
                return
            principal = self._authenticate_request(handler, query)
            if principal is None:
                self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Authentication required"})
                return
            if not principal.is_local:
                self._send_json(
                    handler,
                    HTTPStatus.FORBIDDEN,
                    {"ok": False, "error": "Standing local account login required to browse all projects."},
                )
                return
            active_clean = sanitize_project_name(self.config_manager.settings.project_name)
            folders = self.config_manager.list_project_folders()
            items = []
            seen_active = False
            for f in folders:
                if f.project_name == active_clean:
                    seen_active = True
                items.append(
                    {
                        "project_name": f.project_name,
                        "recording_count": int(f.recording_count),
                        "other_file_count": int(f.other_file_count),
                        "total_size_bytes": int(f.total_size_bytes),
                        "formatted_size": f.formatted_size,
                        "is_active_project": bool(f.is_active_project),
                        "has_script": bool(self.config_manager.has_project_script(f.project_name)),
                    }
                )
            if not seen_active:
                items.insert(
                    0,
                    {
                        "project_name": active_clean,
                        "recording_count": 0,
                        "other_file_count": 0,
                        "total_size_bytes": 0,
                        "formatted_size": "0 B",
                        "is_active_project": True,
                        "has_script": bool(self.config_manager.has_project_script(active_clean)),
                    },
                )
            self._send_json(
                handler,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "active_project": active_clean,
                    "is_recording": bool(self.audio_engine.is_recording or self.audio_engine.is_record_paused),
                    "projects": items,
                },
            )
            return

        if path == "/api/script":
            if not self._is_same_origin_request(handler):
                self._send_json(handler, HTTPStatus.FORBIDDEN, {"error": "Cross-origin request rejected"})
                return
            principal = self._authenticate_request(handler, query)
            if principal is None:
                self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Authentication required"})
                return
            active_clean = sanitize_project_name(self.config_manager.settings.project_name)
            requested_proj = (query.get("project") or [""])[0].strip()
            if requested_proj:
                if not principal.is_local and sanitize_project_name(requested_proj) != active_clean:
                    self._send_json(
                        handler,
                        HTTPStatus.FORBIDDEN,
                        {"ok": False, "error": "Session guests can only access the active project script."},
                    )
                    return
                ok_name, target_proj = self.config_manager.validate_new_project_name(requested_proj)
                if not ok_name:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "error": target_proj})
                    return
            else:
                target_proj = active_clean
            html_content, mtime = self.config_manager.load_project_script(target_proj)
            self._send_json(
                handler,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "project_name": target_proj,
                    "html": sanitize_script_html(html_content),
                    "mtime": mtime,
                },
            )
            return

        if path == "/api/qr.svg":
            if not self._is_same_origin_request(handler):
                self._send_json(handler, HTTPStatus.FORBIDDEN, {"error": "Cross-origin request rejected"})
                return
            client_ip = self._resolve_client_ip(handler)
            allowed, retry_after = self.rate_limiter.check_allowed(client_ip)
            if not allowed:
                self._send_rate_limited(handler, retry_after)
                return
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
            invite = (query.get("token") or [""])[0]
            prefill_session = (query.get("session") or [""])[0].strip()[:32]
            set_cookie_header: Optional[str] = None
            principal = self._authenticate_request(handler, query)
            authenticated = principal is not None
            client_ip = self._resolve_client_ip(handler)
            allowed, retry_after = self.rate_limiter.check_allowed(client_ip)

            initial_error = ""
            initial_retry_after = 0
            clean_prefill_token = ""

            if not authenticated and invite:
                if not allowed:
                    self._send_rate_limited(
                        handler,
                        retry_after,
                        as_html=True,
                        prefill_session=prefill_session,
                    )
                    return
                if self.session.verify_credentials(invite_token=invite):
                    auth_token = self.session.create_auth_token()
                    set_cookie_header = (
                        f"__Host-ars_session={auth_token}; SameSite=Lax; HttpOnly; Secure; Path=/; Max-Age=43200"
                    )
                    self.rate_limiter.record_success(client_ip)
                    authenticated = True
                    principal = AuthPrincipal(
                        kind="session",
                        username=self.session.session_id,
                        role="guest",
                        session_id=self.session.session_id,
                    )
                else:
                    banned, ban_retry, _rem = self.rate_limiter.record_failure(client_ip)
                    if banned:
                        self._send_rate_limited(
                            handler,
                            ban_retry,
                            as_html=True,
                            prefill_session=prefill_session,
                        )
                        return
                    initial_error = "Invalid Session ID, passcode, or invite token."
            elif not authenticated and not allowed:
                initial_retry_after = retry_after
                initial_error = (
                    f"Too many failed login attempts. Try again in "
                    f"{self._format_duration_human(retry_after)}."
                )

            nonce = secrets.token_urlsafe(16)
            html = self._render_producer_html(
                csp_nonce=nonce,
                authenticated=authenticated,
                auth_kind=principal.kind if principal is not None else "",
                auth_user=principal.username if principal is not None else "",
                prefill_session=prefill_session if not authenticated else "",
                prefill_token=clean_prefill_token,
                initial_error=initial_error,
                initial_retry_after=initial_retry_after,
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

    def _parse_post_json(self, handler: BaseHTTPRequestHandler, raw_body: bytes) -> Dict[str, Any]:
        ctype = handler.headers.get("Content-Type", "")
        if "application/json" in ctype:
            try:
                parsed_json = json.loads(raw_body.decode("utf-8"))
                if isinstance(parsed_json, dict):
                    return parsed_json
            except Exception:
                return {}
            return {}
        form = urllib.parse.parse_qs(raw_body.decode("utf-8", errors="ignore"))
        return {k: v[0] for k, v in form.items() if v}

    def _handle_http_post(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._is_same_origin_request(handler):
            self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error": "Cross-origin request rejected"})
            return

        parsed = urllib.parse.urlsplit(handler.path)
        path = parsed.path or "/"
        query = urllib.parse.parse_qs(parsed.query)

        raw_cl = (handler.headers.get("Content-Length") or "0").strip()
        try:
            content_len = int(raw_cl) if raw_cl else 0
        except ValueError:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length header"})
            return
        if content_len < 0:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length header"})
            return
        max_bytes = MAX_SCRIPT_POST_BYTES if path == "/api/script" else MAX_HTTP_POST_BYTES
        if content_len > max_bytes:
            self._send_json(handler, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request payload too large"})
            return

        raw_body = handler.rfile.read(content_len) if content_len > 0 else b""

        if path == "/api/login":
            client_ip = self._resolve_client_ip(handler)
            allowed, retry_after = self.rate_limiter.check_allowed(client_ip)
            if not allowed:
                self._send_rate_limited(handler, retry_after)
                return

            data = self._parse_post_json(handler, raw_body)
            login_mode = str(data.get("login_mode") or "").strip().lower()
            passcode = str(data.get("password") or data.get("passcode") or "")
            invite_token = str(data.get("invite_token") or data.get("token") or "")
            submitted_id = str(data.get("session_id") or data.get("username") or "").strip()

            # 1. Check defined local account login (explicit local mode or non-session username)
            local_user: Optional[WebUserAccount] = None
            if login_mode == "local" or (
                submitted_id
                and passcode
                and not invite_token.strip()
                and not submitted_id.upper().startswith("ARS-")
            ):
                local_user = self.web_user_manager.verify_credentials(submitted_id, passcode)

            if local_user is not None:
                self.rate_limiter.record_success(client_ip)
                auth_token = self.web_user_manager.create_auth_token(local_user)
                cookie_val = (
                    f"__Host-ars_session={auth_token}; SameSite=Lax; HttpOnly; Secure; Path=/; Max-Age=604800"
                )
                self._send_json(
                    handler,
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "auth_kind": "local",
                        "username": local_user.username,
                        "role": local_user.role,
                        "session_id": self.session.session_id,
                        "auth_token": auth_token,
                    },
                    extra_headers={"Set-Cookie": cookie_val},
                )
                return

            # 2. Check temporary session login (Session ID + 6-digit PIN or invite token)
            if login_mode != "local":
                expected_sid = self.session.session_id.upper().encode("utf-8")
                provided_sid = submitted_id.upper().encode("utf-8")
                sid_matches = bool(submitted_id) and hmac.compare_digest(provided_sid, expected_sid)
                sid_conflict = bool(submitted_id) and not sid_matches

                invite_clean = invite_token.strip()
                pass_clean = passcode.strip()
                invite_ok = bool(invite_clean) and self.session.verify_credentials(invite_token=invite_clean) and not sid_conflict
                pin_ok = bool(pass_clean) and self.session.verify_credentials(passcode=pass_clean) and sid_matches

                if pin_ok or invite_ok:
                    self.rate_limiter.record_success(client_ip)
                    auth_token = self.session.create_auth_token()
                    cookie_val = (
                        f"__Host-ars_session={auth_token}; SameSite=Lax; HttpOnly; Secure; Path=/; Max-Age=43200"
                    )
                    self._send_json(
                        handler,
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "auth_kind": "session",
                            "username": self.session.session_id,
                            "role": "guest",
                            "session_id": self.session.session_id,
                            "auth_token": auth_token,
                        },
                        extra_headers={"Set-Cookie": cookie_val},
                    )
                    return

            banned, ban_retry, remaining = self.rate_limiter.record_failure(client_ip)
            if banned:
                self._send_rate_limited(handler, ban_retry)
            else:
                err_msg = (
                    "Invalid username or password."
                    if login_mode == "local"
                    else "Invalid Session ID, passcode, or invite token."
                )
                self._send_json(
                    handler,
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "ok": False,
                        "error": err_msg,
                        "attempts_remaining": remaining,
                    },
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

        if path == "/api/projects":
            principal = self._authenticate_request(handler, query)
            if principal is None:
                self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Authentication required"})
                return
            if not principal.is_local:
                self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error": "Local account login required."})
                return
            data = self._parse_post_json(handler, raw_body)
            proj_name = str(data.get("project_name") or "").strip()
            set_active = bool(data.get("set_active", False))
            ok, detail = self.config_manager.create_project_folder(proj_name)
            if not ok:
                self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "error": detail})
                return
            if set_active and not (self.audio_engine.is_recording or self.audio_engine.is_record_paused):
                self.switch_active_project(detail, require_permission_setting=False)
            self._send_json(
                handler,
                HTTPStatus.OK,
                {"ok": True, "project_name": detail, "active_project": sanitize_project_name(self.config_manager.settings.project_name)},
            )
            return

        if path == "/api/projects/select":
            principal = self._authenticate_request(handler, query)
            if principal is None:
                self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Authentication required"})
                return
            if not principal.is_local:
                self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error": "Local account login required."})
                return
            data = self._parse_post_json(handler, raw_body)
            proj_name = str(data.get("project_name") or "").strip()
            ok, detail = self.switch_active_project(proj_name, require_permission_setting=False)
            if not ok:
                self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "error": detail})
                return
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "project_name": detail})
            return

        if path == "/api/projects/rename":
            principal = self._authenticate_request(handler, query)
            if principal is None:
                self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Authentication required"})
                return
            if not principal.is_local:
                self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error": "Local account login required."})
                return
            if self.audio_engine.is_recording or self.audio_engine.is_record_paused:
                self._send_json(
                    handler,
                    HTTPStatus.CONFLICT,
                    {"ok": False, "error": "Cannot rename projects while recording is in progress."},
                )
                return
            data = self._parse_post_json(handler, raw_body)
            old_name = str(data.get("old_name") or "").strip()
            new_name = str(data.get("new_name") or "").strip()
            was_active = sanitize_project_name(self.config_manager.settings.project_name) == old_name
            ok, detail = self.config_manager.rename_project_folder(old_name, new_name)
            if not ok:
                self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "error": detail})
                return
            if was_active and self.on_project_name_changed is not None:
                try:
                    self.on_project_name_changed(detail)
                except Exception as exc:
                    logger.warning("on_project_name_changed callback failed: %s", exc)
            self.broadcast_state_snapshot()
            self._control_server.sync_state()
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "project_name": detail})
            return

        if path == "/api/script":
            principal = self._authenticate_request(handler, query)
            if principal is None:
                self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Authentication required"})
                return
            data = self._parse_post_json(handler, raw_body)
            active_clean = sanitize_project_name(self.config_manager.settings.project_name)
            requested_proj = str(data.get("project_name") or "").strip()
            if requested_proj:
                if not principal.is_local and sanitize_project_name(requested_proj) != active_clean:
                    self._send_json(
                        handler,
                        HTTPStatus.FORBIDDEN,
                        {"ok": False, "error": "Session guests can only edit the active project script."},
                    )
                    return
                ok_name, target_proj = self.config_manager.validate_new_project_name(requested_proj)
                if not ok_name:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "error": target_proj})
                    return
            else:
                target_proj = active_clean

            raw_html = str(data.get("html") or "")
            sanitized = sanitize_script_html(raw_html)
            ok, msg, _saved_path, mtime = self.config_manager.save_project_script(
                sanitized, project_name=target_proj
            )
            if not ok:
                self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": msg})
                return
            if self.on_script_updated is not None:
                try:
                    self.on_script_updated(target_proj, sanitized)
                except Exception as exc:
                    logger.warning("on_script_updated callback failed: %s", exc)
            self.broadcast_script_update(target_proj, sanitized, mtime=mtime)
            self._send_json(
                handler,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "project_name": target_proj,
                    "html": sanitized,
                    "mtime": mtime,
                    "message": msg,
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
        if not self._is_same_origin_request(handler):
            self._send_json(handler, HTTPStatus.FORBIDDEN, {"error": "Cross-origin WebSocket connection rejected"})
            return

        client_ip = self._resolve_client_ip(handler)
        allowed, retry_after = self.rate_limiter.check_allowed(client_ip)
        if not allowed:
            self._send_rate_limited(handler, retry_after)
            return

        principal = self._authenticate_request(handler, query)
        if principal is None:
            self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"error": "Session authentication required"})
            return

        if self.connected_client_count >= MAX_WS_CLIENTS:
            self._send_json(handler, HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Maximum concurrent producer connections reached"})
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
        ws_conn = WebSocketConnection(
            sock,
            handler.client_address,
            self.session.session_id,
            auth_kind=principal.kind,
            username=principal.username,
        )

        with self._clients_lock:
            self._clients[ws_conn.client_id] = ws_conn

        self._control_server.sync_state()
        logger.info(
            "Remote producer connected (%s, client=%s, auth=%s)",
            client_ip,
            ws_conn.client_id,
            principal.kind,
        )

        try:
            # Send initial state snapshot immediately
            ws_conn.send_text(json.dumps(self.build_status_snapshot()))

            while self._running.is_set() and not ws_conn.closed:
                # Verify temporary session was not rotated (standing local accounts persist)
                if ws_conn.auth_kind != "local" and ws_conn.session_id != self.session.session_id:
                    ws_conn.send_text(json.dumps({"type": "session_revoked"}))
                    break

                frame = read_ws_frame(
                    sock,
                    max_payload=131072,
                    require_mask=True,
                    max_idle_seconds=WS_IDLE_TIMEOUT_SEC,
                )
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
            self._control_server.sync_state()
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
                ws_conn.talkback_latched = False
            self._recompute_remote_talkback_state()
            ws_conn.send_text(json.dumps(self.build_status_snapshot()))

        elif mtype == "talkback":
            requested = bool(msg.get("active", False))
            # Only allow talkback when the producer's Mic Send switch is armed and not cough-muted
            if requested and ws_conn.mic_send_enabled and not ws_conn.cough_muted:
                ws_conn.talkback_active = True
                if self.audio_engine.is_recording:
                    ws_conn.talkback_latched = False
                else:
                    ws_conn.talkback_latched = bool(msg.get("latched", True))
            else:
                ws_conn.talkback_active = False
                ws_conn.talkback_latched = False
            self._recompute_remote_talkback_state()
            ws_conn.send_text(json.dumps(self.build_status_snapshot()))

        elif mtype == "listen_state":
            ws_conn.listen_enabled = bool(msg.get("enabled", True))

        elif mtype == "set_project_name":
            ok, detail = self.update_project_name(str(msg.get("project_name", "")))
            ws_conn.send_text(
                json.dumps(
                    {
                        "type": "project_rename_result",
                        "ok": ok,
                        "project_name": detail if ok else (self.config_manager.settings.project_name or "Session"),
                        "error": "" if ok else detail,
                    }
                )
            )

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
    def _render_producer_html(
        self,
        csp_nonce: str,
        authenticated: bool,
        auth_kind: str = "",
        auth_user: str = "",
        prefill_session: str = "",
        prefill_token: str = "",
        initial_error: str = "",
        initial_retry_after: int = 0,
    ) -> str:
        session_id = self.session.session_id
        project_name = self.config_manager.settings.project_name or "Session"
        auth_str = "true" if authenticated else "false"
        has_local_users_str = "true" if self.web_user_manager.has_users() else "false"
        safe_prefill = html_escape(prefill_token)
        safe_prefill_session = html_escape(prefill_session) if (not authenticated and prefill_session) else ""
        safe_hdr_session = html_escape(session_id) if authenticated else "Auth Required"
        safe_hdr_project = html_escape(project_name) if authenticated else "--"
        safe_js_project = json.dumps(project_name if authenticated else "").replace("</", "<\\/")
        safe_js_auth_kind = json.dumps(auth_kind if authenticated else "").replace("</", "<\\/")
        safe_js_auth_user = json.dumps(auth_user if authenticated else "").replace("</", "<\\/")
        safe_js_initial_error = json.dumps(initial_error or "").replace("</", "<\\/")
        safe_js_retry_after = max(0, int(initial_retry_after or 0))

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
.btn-subtle:disabled {{
  opacity: 0.45;
  cursor: not-allowed;
}}
.project-edit-bar {{
  background: var(--bg-card);
  border: 1px solid #388BFD;
  border-radius: 10px;
  padding: 12px 16px;
  margin-bottom: 14px;
  display: none;
  flex-direction: column;
  gap: 8px;
}}
.project-edit-row {{
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}}
.project-edit-row label {{
  font-size: 12px;
  font-weight: 700;
  color: #C9D1D9;
  letter-spacing: 0.4px;
}}
.project-edit-row .text-input {{
  flex: 1 1 220px;
  padding: 8px 10px;
  font-size: 14px;
}}
.btn-save-inline {{
  background: #1F6FEB;
  border: 1px solid #388BFD;
  color: #FFFFFF;
  border-radius: 6px;
  padding: 8px 14px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}}
.btn-save-inline:disabled {{
  opacity: 0.45;
  cursor: not-allowed;
}}
.project-edit-feedback {{
  font-size: 12px;
  color: #FFA198;
  display: none;
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
.btn-primary:disabled {{
  background: #21262D;
  border-color: #30363D;
  color: #8B949E;
  cursor: not-allowed;
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
.studio-deck.deck-playing {{
  background: #0E1B2B;
  border-color: var(--brand-color);
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
.state-banner.banner-playing {{
  background: #0D2744;
  border-color: #58A6FF;
  color: #79C0FF;
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
.timer-display.timer-playing {{
  color: #79C0FF;
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
/* Talkback Button */
.btn-talkback {{
  width: 100%;
  min-height: 64px;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 18px;
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
  transition: background-color 0.1s ease, border-color 0.1s ease, color 0.1s ease;
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
@keyframes muteFlashRed {{
  0%, 49% {{
    background-color: #D72638;
    color: #FFFFFF;
    border-color: #FF7B72;
  }}
  50%, 100% {{
    background-color: #490B11;
    color: #FFA198;
    border-color: #D72638;
  }}
}}
.pill-muted {{
  color: #FFFFFF;
  background-color: #D72638;
  border: 1px solid #FF7B72;
  border-radius: 5px;
  padding: 2px 8px;
  font-weight: 700;
  animation: muteFlashRed 0.8s steps(1, end) infinite;
}}
.pill-live {{
  color: #7EE787;
  font-weight: 700;
}}
/* Login Mode Switcher & Console Nav Tabs */
.login-mode-tabs, .nav-tabs {{
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--border-subtle);
  padding-bottom: 8px;
  flex-wrap: wrap;
}}
.tab-btn {{
  background: #21262D;
  border: 1px solid #30363D;
  color: #C9D1D9;
  border-radius: 7px;
  padding: 7px 14px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}}
.tab-btn:hover {{
  border-color: #58A6FF;
}}
.tab-btn.active {{
  background: #1F6FEB;
  border-color: #388BFD;
  color: #FFFFFF;
}}
/* Rich-Text Script Editor */
.rte-toolbar {{
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 8px 10px;
  background: #0D1014;
  border: 1px solid #30363D;
  border-radius: 8px 8px 0 0;
}}
.rte-btn {{
  background: #21262D;
  border: 1px solid #30363D;
  color: #E6EDF3;
  border-radius: 5px;
  padding: 5px 9px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}}
.rte-btn:hover {{
  background: #292E36;
  border-color: #58A6FF;
}}
.rte-sep {{
  width: 1px;
  height: 20px;
  background: #30363D;
  margin: 0 2px;
}}
.rte-editor {{
  min-height: 340px;
  max-height: 60vh;
  overflow-y: auto;
  background: #090C10;
  color: #F0F6FC;
  border: 1px solid #30363D;
  border-top: none;
  border-radius: 0 0 8px 8px;
  padding: 18px 22px;
  font-size: 18px;
  line-height: 1.65;
  outline: none;
}}
.rte-editor:focus {{
  border-color: #58A6FF;
}}
.script-actions-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 12px;
  flex-wrap: wrap;
}}
/* Projects Table */
.projects-table {{
  width: 100%;
  border-collapse: collapse;
  margin-top: 12px;
  font-size: 13px;
}}
.projects-table th, .projects-table td {{
  text-align: left;
  padding: 10px 10px;
  border-bottom: 1px solid #21262D;
}}
.projects-table th {{
  color: var(--text-secondary);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.7px;
}}
.proj-actions {{
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}}
.select-input {{
  background: var(--bg-deck);
  border: 1px solid #30363D;
  border-radius: 6px;
  color: var(--text-primary);
  padding: 6px 10px;
  font-size: 13px;
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
    <span class="badge" id="hdr-session">Session: {safe_hdr_session}</span>
    <span class="badge" id="hdr-project">Project: {safe_hdr_project}</span>
    <button type="button" class="btn-subtle" id="btn-edit-project" style="display:none;" title="Update active Project Name">✎ Project</button>
    <span class="badge badge-conn-warn" id="hdr-conn">● Connecting...</span>
    <button type="button" class="btn-subtle" id="btn-logout" style="display:none;">Sign Out</button>
  </div>
</header>

<main class="main-wrap">
  <!-- SESSION-TIED & LOCAL ACCOUNT LOGIN VIEW -->
  <section class="login-card" id="login-view" style="display:none;">
    <h1 id="login-heading">Join Guided VO Session</h1>
    <div class="login-mode-tabs" role="tablist" aria-label="Login method">
      <button type="button" class="tab-btn active" id="btn-mode-session" role="tab" aria-selected="true">Session PIN</button>
      <button type="button" class="tab-btn" id="btn-mode-local" role="tab" aria-selected="false">Local Account</button>
    </div>
    <p id="login-desc">Authenticate with the Studio Session ID and 6-digit Session Passcode displayed on the studio's Remote Producer setup screen, or scan the session QR code.</p>
    <div class="login-error" id="login-error" role="alert"></div>
    <form id="login-form" action="/api/login" method="post">
      <input type="hidden" id="login-mode-input" name="login_mode" value="session">
      <div class="form-field">
        <label for="session-id-input" id="lbl-login-id">STUDIO SESSION ID</label>
        <input
          type="text"
          id="session-id-input"
          name="username"
          class="text-input"
          autocomplete="username"
          autocorrect="off"
          autocapitalize="off"
          spellcheck="false"
          enterkeyhint="next"
          maxlength="32"
          value="{safe_prefill_session}"
          placeholder="e.g. ARS-1A2B"
          required
        >
      </div>
      <div class="form-field">
        <label for="session-passcode" id="lbl-login-pass">SESSION PASSCODE (6-DIGIT PIN)</label>
        <div class="input-row">
          <input
            type="password"
            id="session-passcode"
            name="password"
            class="text-input"
            autocomplete="current-password"
            inputmode="numeric"
            enterkeyhint="done"
            maxlength="128"
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

  <!-- PRODUCER CONSOLE, SCRIPT EDITOR & PROJECTS VIEW -->
  <section id="console-view" style="display:none;">
    <nav class="nav-tabs" role="tablist" aria-label="Producer console sections">
      <button type="button" class="tab-btn active" id="nav-tab-console" role="tab" aria-selected="true" aria-controls="panel-console">◉ Studio Console</button>
      <button type="button" class="tab-btn" id="nav-tab-script" role="tab" aria-selected="false" aria-controls="panel-script">▤ Script &amp; Prompter</button>
      <button type="button" class="tab-btn" id="nav-tab-projects" role="tab" aria-selected="false" aria-controls="panel-projects" style="display:none;">◈ Projects</button>
    </nav>

    <div id="panel-console" role="tabpanel">
    <!-- Optional Remote Project Rename Bar (shown only when enabled in studio settings) -->
    <div class="project-edit-bar" id="project-edit-bar">
      <div class="project-edit-row">
        <label for="project-name-input">PROJECT NAME</label>
        <input type="text" id="project-name-input" class="text-input" placeholder="Enter Project Name..." maxlength="{MAX_PROJECT_NAME_LENGTH}">
        <button type="button" class="btn-save-inline" id="btn-save-project">Save Project</button>
        <button type="button" class="btn-subtle" id="btn-cancel-project">Cancel</button>
      </div>
      <div class="project-edit-feedback" id="project-edit-feedback" role="alert"></div>
    </div>

    <!-- Studio Deck Readout -->
    <div class="studio-deck" id="studio-deck">
      <div class="deck-top">
        <div>
          <div class="state-banner" id="state-banner">● STANDBY — LIVE INPUT MONITOR</div>
          <div style="margin-top:8px; font-size:12px; color:#8B949E;" id="mode-summary">
            Mode: Studio Split Mode (2-Ch Dual Track: L = VO Talent, R = Producer &amp; Talent Talkback)
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

      <!-- Studio Talkback Prompt Card (Latch when idle, PTT when recording) -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">2. DIRECT TALENT — STUDIO TALKBACK</span>
          <span class="badge badge-conn-ok" id="talkback-mode-badge">LATCH / TOGGLE (IDLE)</span>
        </div>
        <button
          type="button"
          class="btn-talkback"
          id="btn-talkback"
          aria-pressed="false"
          aria-describedby="talkback-hint"
        >
          ◉ TALKBACK — CLICK OR PRESS [T / SPACE]
        </button>
        <div style="margin-top:8px; font-size:12px; color:#8B949E;" id="talkback-hint">
          Idle: Click or press [T] / [Space] to latch Talkback ON or OFF (auto-unlatches to PTT when recording starts)
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:14px; padding:12px 18px;">
      <div class="status-Strip">
        <span>Mix-Minus Active: You hear the Talent Mic; Talent headphones hear your Talkback. In Studio Split Mode, Track R mixes Producer + Talent Mic with marker tones while Track L (VO) is muted; in Normal Mode, Talkback mixes onto the main channel(s).</span>
        <span id="rtt-readout">Link Latency: -- ms</span>
      </div>
    </div>
    </div><!-- /panel-console -->

    <!-- SCRIPT & TELEPROMPTER EDITOR PANEL -->
    <div id="panel-script" role="tabpanel" style="display:none;">
      <div class="card">
        <div class="card-header">
          <span class="card-title">PROJECT SCRIPT &amp; LIVE TELEPROMPTER SYNC</span>
          <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
            <label for="script-project-select" style="font-size:12px; color:#8B949E;">Project:</label>
            <select id="script-project-select" class="select-input"></select>
            <button type="button" class="btn-subtle" id="btn-reload-script">↻ Reload</button>
          </div>
        </div>
        <div class="rte-toolbar" role="toolbar" aria-label="Script formatting">
          <button type="button" class="rte-btn" data-cmd="bold" title="Bold"><b>B</b></button>
          <button type="button" class="rte-btn" data-cmd="italic" title="Italic"><i>I</i></button>
          <button type="button" class="rte-btn" data-cmd="underline" title="Underline"><u>U</u></button>
          <span class="rte-sep"></span>
          <button type="button" class="rte-btn" data-block="H2" title="Section Heading">H2</button>
          <button type="button" class="rte-btn" data-block="P" title="Paragraph">P</button>
          <button type="button" class="rte-btn" data-cmd="insertUnorderedList" title="Bullet List">• List</button>
          <span class="rte-sep"></span>
          <button type="button" class="rte-btn" data-color="#F0F6FC" title="Default White Text">White</button>
          <button type="button" class="rte-btn" data-color="#FFDF5D" style="color:#FFDF5D;" title="Amber Emphasis">Amber</button>
          <button type="button" class="rte-btn" data-color="#79C0FF" style="color:#79C0FF;" title="Cyan Note">Cyan</button>
          <button type="button" class="rte-btn" data-color="#7EE787" style="color:#7EE787;" title="Green Cue">Green</button>
          <button type="button" class="rte-btn" data-color="#FF7B72" style="color:#FF7B72;" title="Red Warning">Red</button>
          <span class="rte-sep"></span>
          <button type="button" class="rte-btn" data-insert="(pause)" title="Insert non-spoken (cue) excluded from AI lookahead">(Cue)</button>
          <button type="button" class="rte-btn" data-insert="[direction]" title="Insert non-spoken [note] excluded from AI lookahead">[Note]</button>
          <button type="button" class="rte-btn" data-insert="{{SFX}}" title="Insert non-spoken {{SFX}} excluded from AI lookahead">{{SFX}}</button>
          <span class="rte-sep"></span>
          <button type="button" class="rte-btn" data-cmd="removeFormat" title="Clear Formatting">Clear</button>
        </div>
        <div id="script-editor" class="rte-editor" contenteditable="true" spellcheck="true" aria-label="Rich text teleprompter script editor"></div>
        <div class="script-actions-row">
          <span id="script-status-msg" style="font-size:12px; color:#8B949E;">Saved to /storage/&lt;Project&gt;/script.html. Text in (...), [...], and {{...}} is excluded from Whisper AI lookahead. Edits preserve talent reading position.</span>
          <button type="button" class="btn-save-inline" id="btn-save-script">Save &amp; Sync to Prompter</button>
        </div>
      </div>
    </div>

    <!-- STANDING LOCAL ACCOUNT PROJECTS MANAGER PANEL -->
    <div id="panel-projects" role="tabpanel" style="display:none;">
      <div class="card">
        <div class="card-header">
          <span class="card-title">STUDIO PROJECTS &amp; SCRIPTS (STANDING LOCAL ACCESS)</span>
          <button type="button" class="btn-subtle" id="btn-refresh-projects">↻ Refresh Projects</button>
        </div>
        <div class="project-edit-row" style="margin-bottom:10px;">
          <label for="new-project-name">NEW PROJECT</label>
          <input type="text" id="new-project-name" class="text-input" placeholder="Enter new project name..." maxlength="{MAX_PROJECT_NAME_LENGTH}">
          <button type="button" class="btn-save-inline" id="btn-create-project">Create Project</button>
          <button type="button" class="btn-subtle" id="btn-create-activate-project">Create &amp; Set Active</button>
        </div>
        <div class="project-edit-feedback" id="projects-feedback" role="alert"></div>
        <table class="projects-table" aria-label="Available studio projects">
          <thead>
            <tr>
              <th>Project Name</th>
              <th>Takes</th>
              <th>Size</th>
              <th>Script</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="projects-tbody"></tbody>
        </table>
      </div>
    </div>
  </section>
</main>

<script nonce="{csp_nonce}">
(function() {{
  "use strict";

  let isAuthenticated = {auth_str};
  let authKind = {safe_js_auth_kind};
  let authUser = {safe_js_auth_user};
  const hasLocalUsers = {has_local_users_str};
  const initialError = {safe_js_initial_error};
  const initialRetryAfter = {safe_js_retry_after};
  let lockoutTimer = null;
  let lockoutUntilMs = 0;
  let activeNavTab = "console";
  let editingScriptProject = "";
  let cachedProjects = [];

  // Immediately strip sensitive URL query parameters (e.g. ?token= or ?session=) from browser bar/history
  if (window.location.search && window.history && window.history.replaceState) {{
    window.history.replaceState({{}}, document.title, window.location.pathname || "/");
  }}

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

  // Producer Mic capture (Producer -> Talent Talkback: latch when idle, push-to-talk when recording)
  let micStream = null;
  let micSourceNode = null;
  let micProcessorNode = null;
  let talkbackPressed = false;
  let talkbackDesired = false;
  let talkbackLatched = false;
  let talkbackHoldActive = false;
  let talkbackPressTs = 0;
  let lastTalkbackActionTs = 0;
  let latestIsRecording = false;
  let latestIsRecordPaused = false;
  let latestIsAutoPaused = false;

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
  let latestToneHz = 1000.0;
  let latestPressToneSec = 1.0;
  let latestReleaseToneSec = 0.5;
  let latestProjectName = {safe_js_project};
  let latestAllowProjectRename = false;
  let lastObservedLocalTbActive = null;
  let activeToneOsc = null;
  let activeToneGain = null;

  const loginView = document.getElementById("login-view");
  const consoleView = document.getElementById("console-view");
  const loginForm = document.getElementById("login-form");
  const loginError = document.getElementById("login-error");
  const loginModeInput = document.getElementById("login-mode-input");
  const btnModeSession = document.getElementById("btn-mode-session");
  const btnModeLocal = document.getElementById("btn-mode-local");
  const loginHeading = document.getElementById("login-heading");
  const loginDesc = document.getElementById("login-desc");
  const lblLoginId = document.getElementById("lbl-login-id");
  const lblLoginPass = document.getElementById("lbl-login-pass");
  const sessionInput = document.getElementById("session-id-input");
  const passInput = document.getElementById("session-passcode");
  const btnTogglePass = document.getElementById("btn-toggle-pass");
  const btnLoginSubmit = document.getElementById("btn-login-submit");
  const btnLogout = document.getElementById("btn-logout");

  const navTabConsole = document.getElementById("nav-tab-console");
  const navTabScript = document.getElementById("nav-tab-script");
  const navTabProjects = document.getElementById("nav-tab-projects");
  const panelConsole = document.getElementById("panel-console");
  const panelScript = document.getElementById("panel-script");
  const panelProjects = document.getElementById("panel-projects");
  const scriptProjectSelect = document.getElementById("script-project-select");
  const btnReloadScript = document.getElementById("btn-reload-script");
  const scriptEditor = document.getElementById("script-editor");
  const btnSaveScript = document.getElementById("btn-save-script");
  const scriptStatusMsg = document.getElementById("script-status-msg");
  const newProjectNameInput = document.getElementById("new-project-name");
  const btnCreateProject = document.getElementById("btn-create-project");
  const btnCreateActivateProject = document.getElementById("btn-create-activate-project");
  const btnRefreshProjects = document.getElementById("btn-refresh-projects");
  const projectsFeedback = document.getElementById("projects-feedback");
  const projectsTbody = document.getElementById("projects-tbody");

  const hdrConn = document.getElementById("hdr-conn");
  const hdrSession = document.getElementById("hdr-session");
  const hdrProject = document.getElementById("hdr-project");
  const btnEditProject = document.getElementById("btn-edit-project");
  const projectEditBar = document.getElementById("project-edit-bar");
  const projectNameInput = document.getElementById("project-name-input");
  const btnSaveProject = document.getElementById("btn-save-project");
  const btnCancelProject = document.getElementById("btn-cancel-project");
  const projectEditFeedback = document.getElementById("project-edit-feedback");

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

  const btnTalkback = document.getElementById("btn-talkback");
  const talkbackModeBadge = document.getElementById("talkback-mode-badge");
  const talkbackHint = document.getElementById("talkback-hint");
  const rttReadout = document.getElementById("rtt-readout");

  function formatLockoutDuration(totalSec) {{
    const sec = Math.max(1, Math.floor(Number(totalSec) || 1));
    const hrs = Math.floor(sec / 3600);
    const mins = Math.floor((sec % 3600) / 60);
    const rem = sec % 60;
    if (hrs > 0) return hrs + "h " + mins + "m " + rem + "s";
    if (mins > 0) return mins + "m " + rem + "s";
    return rem + "s";
  }}

  function stopLockoutCountdown() {{
    if (lockoutTimer) {{
      clearInterval(lockoutTimer);
      lockoutTimer = null;
    }}
    lockoutUntilMs = 0;
    btnLoginSubmit.disabled = false;
  }}

  function startLockoutCountdown(seconds, baseMsg) {{
    stopLockoutCountdown();
    const secs = Math.max(1, Number(seconds) || 60);
    lockoutUntilMs = Date.now() + secs * 1000;
    btnLoginSubmit.disabled = true;

    const raw = (baseMsg || "Too many failed login attempts").trim();
    const marker = raw.toLowerCase().indexOf("try again in");
    let prefix = marker >= 0 ? raw.slice(0, marker).trim() : raw;
    while (prefix.endsWith(".")) {{
      prefix = prefix.slice(0, -1).trim();
    }}
    if (!prefix) {{
      prefix = "Too many failed login attempts";
    }}

    function tick() {{
      const rem = Math.max(0, Math.ceil((lockoutUntilMs - Date.now()) / 1000));
      if (rem <= 0) {{
        stopLockoutCountdown();
        loginError.textContent = "Lockout expired. Verify your Session ID and Passcode before trying again.";
        loginError.style.display = "block";
        return;
      }}
      loginError.textContent = prefix + ". Try again in " + formatLockoutDuration(rem) + ".";
      loginError.style.display = "block";
    }}
    tick();
    lockoutTimer = setInterval(tick, 1000);
  }}

  function setLoginMode(mode) {{
    const isLocal = mode === "local";
    loginModeInput.value = isLocal ? "local" : "session";
    btnModeSession.className = isLocal ? "tab-btn" : "tab-btn active";
    btnModeSession.setAttribute("aria-selected", isLocal ? "false" : "true");
    btnModeLocal.className = isLocal ? "tab-btn active" : "tab-btn";
    btnModeLocal.setAttribute("aria-selected", isLocal ? "true" : "false");
    if (isLocal) {{
      loginHeading.textContent = "Local Studio Account Login";
      loginDesc.textContent = "Sign in with a defined local Web UI account for standing access to all studio projects, script editing, and live producer monitoring.";
      lblLoginId.textContent = "USERNAME";
      sessionInput.placeholder = "Enter local username";
      lblLoginPass.textContent = "PASSWORD";
      passInput.placeholder = "Enter account password";
      passInput.inputMode = "text";
      btnLoginSubmit.textContent = "Sign In to Studio";
    }} else {{
      loginHeading.textContent = "Join Guided VO Session";
      loginDesc.textContent = "Authenticate with the Studio Session ID and 6-digit Session Passcode displayed on the studio's Remote Producer setup screen, or scan the session QR code.";
      lblLoginId.textContent = "STUDIO SESSION ID";
      sessionInput.placeholder = "e.g. ARS-1A2B";
      lblLoginPass.textContent = "SESSION PASSCODE (6-DIGIT PIN)";
      passInput.placeholder = "Enter 6-digit passcode";
      passInput.inputMode = "numeric";
      btnLoginSubmit.textContent = "Connect to Studio Session";
    }}
  }}
  btnModeSession.addEventListener("click", function() {{ setLoginMode("session"); }});
  btnModeLocal.addEventListener("click", function() {{ setLoginMode("local"); }});

  function switchNavTab(tabName) {{
    activeNavTab = tabName;
    const isConsole = tabName === "console";
    const isScript = tabName === "script";
    const isProjects = tabName === "projects";
    panelConsole.style.display = isConsole ? "block" : "none";
    panelScript.style.display = isScript ? "block" : "none";
    panelProjects.style.display = isProjects ? "block" : "none";
    navTabConsole.className = isConsole ? "tab-btn active" : "tab-btn";
    navTabConsole.setAttribute("aria-selected", isConsole ? "true" : "false");
    navTabScript.className = isScript ? "tab-btn active" : "tab-btn";
    navTabScript.setAttribute("aria-selected", isScript ? "true" : "false");
    navTabProjects.className = isProjects ? "tab-btn active" : "tab-btn";
    navTabProjects.setAttribute("aria-selected", isProjects ? "true" : "false");
    if (isScript) {{
      loadScriptForProject(editingScriptProject || latestProjectName || "");
    }} else if (isProjects && authKind === "local") {{
      refreshProjectsList();
    }}
  }}
  navTabConsole.addEventListener("click", function() {{ switchNavTab("console"); }});
  navTabScript.addEventListener("click", function() {{ switchNavTab("script"); }});
  navTabProjects.addEventListener("click", function() {{ switchNavTab("projects"); }});

  function showView(authenticated) {{
    isAuthenticated = authenticated;
    if (authenticated) {{
      stopLockoutCountdown();
      loginError.style.display = "none";
      loginView.style.display = "none";
      consoleView.style.display = "block";
      btnLogout.style.display = "inline-block";
      navTabProjects.style.display = (authKind === "local") ? "inline-block" : "none";
      if (authKind === "local") {{
        refreshProjectsList();
      }}
      populateScriptProjectSelect();
      connectWebSocket();
    }} else {{
      authKind = "";
      authUser = "";
      loginView.style.display = "block";
      consoleView.style.display = "none";
      btnLogout.style.display = "none";
      navTabProjects.style.display = "none";
      hdrSession.textContent = "Session: Auth Required";
      hdrProject.textContent = "Project: --";
      hdrConn.textContent = "● Auth Required";
      hdrConn.className = "badge badge-conn-warn";
    }}
  }}

  // Clear stale inline login error as soon as the user edits credentials (unless Fail2Ban lockout is active)
  function clearErrorOnEdit() {{
    if (Date.now() >= lockoutUntilMs && loginError.style.display !== "none") {{
      loginError.style.display = "none";
    }}
  }}
  sessionInput.addEventListener("input", clearErrorOnEdit);
  passInput.addEventListener("input", clearErrorOnEdit);

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
    if (Date.now() < lockoutUntilMs) {{
      return;
    }}
    loginError.style.display = "none";
    btnLoginSubmit.disabled = true;
    const modeVal = loginModeInput.value || "session";
    const sessionVal = sessionInput.value;
    const passVal = passInput.value;
    const inviteVal = document.getElementById("invite-token-input").value;
    try {{
      const resp = await fetch("/api/login", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{
          login_mode: modeVal,
          session_id: sessionVal,
          username: sessionVal,
          password: passVal,
          invite_token: inviteVal
        }})
      }});
      let data = {{}};
      try {{
        data = await resp.json();
      }} catch (_e) {{}}

      if (resp.ok && data.ok) {{
        authKind = data.auth_kind || "session";
        authUser = data.username || "";
        // Clean token from URL bar for security
        if (window.history && window.history.replaceState) {{
          window.history.replaceState({{}}, document.title, "/");
        }}
        showView(true);
      }} else if (resp.status === 429) {{
        const headerRetry = parseInt(resp.headers.get("Retry-After") || "60", 10);
        const retrySec = Number(data.retry_after) || (Number.isFinite(headerRetry) ? headerRetry : 60);
        startLockoutCountdown(retrySec, data.error || "Too many failed login attempts");
      }} else {{
        const remSuffix = (typeof data.attempts_remaining === "number")
          ? (" (" + data.attempts_remaining + " attempt" + (data.attempts_remaining === 1 ? "" : "s") + " left before temporary lockout)")
          : "";
        loginError.textContent = (data.error || "Authentication failed.") + remSuffix;
        loginError.style.display = "block";
        btnLoginSubmit.disabled = false;
      }}
    }} catch (err) {{
      loginError.textContent = "Network error connecting to studio.";
      loginError.style.display = "block";
      btnLoginSubmit.disabled = false;
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

  // Push-to-Talk Talkback Capture (Producer -> Talent)
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
        if (!talkbackPressed) {{
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
    talkbackLatched = false;
    talkbackHoldActive = false;
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
    btnTalkback.setAttribute("aria-pressed", talkbackPressed ? "true" : "false");
    if (latestIsRecording) {{
      if (talkbackModeBadge) {{
        talkbackModeBadge.textContent = "PUSH-TO-TALK";
        talkbackModeBadge.className = talkbackPressed ? "badge badge-conn-warn" : "badge badge-conn-ok";
      }}
      if (talkbackPressed) {{
        btnTalkback.className = "btn-talkback talkback-live";
        btnTalkback.textContent = "⚡ TALKBACK — HOLD TO PROMPT TALENT [Hold T or Space]";
        talkbackHint.textContent = "⚡ Push-to-Talk active — release button or [T] / [Space] to end";
      }} else {{
        btnTalkback.className = "btn-talkback";
        btnTalkback.textContent = "◉ TALKBACK — HOLD TO PROMPT TALENT [Hold T or Space]";
        talkbackHint.textContent = "Recording active: Hold Talkback button or [T] / [Space] for momentary Push-to-Talk";
      }}
    }} else {{
      if (talkbackModeBadge) {{
        talkbackModeBadge.textContent = talkbackLatched ? "LATCHED ON AIR" : "LATCH / TOGGLE (IDLE)";
        talkbackModeBadge.className = talkbackLatched ? "badge badge-conn-warn" : "badge badge-conn-ok";
      }}
      if (talkbackPressed) {{
        btnTalkback.className = "btn-talkback talkback-live";
        btnTalkback.textContent = "⚡ TALKBACK — CLICK OR PRESS [T / SPACE]";
        talkbackHint.textContent = "⚡ Talkback latched ON — click or press [T] / [Space] to unlatch (auto-unlatches when recording starts)";
      }} else {{
        btnTalkback.className = "btn-talkback";
        btnTalkback.textContent = "◉ TALKBACK — CLICK OR PRESS [T / SPACE]";
        talkbackHint.textContent = "Idle: Click or press [T] / [Space] to latch Talkback ON or OFF (auto-unlatches to PTT when recording starts)";
      }}
    }}
  }}

  function playHeadsetTalkbackTone(isPress) {{
    ensureAudioContext();
    if (!audioCtx) {{
      return;
    }}
    if (activeToneOsc) {{
      try {{ activeToneOsc.stop(); }} catch (_e) {{}}
      try {{ activeToneOsc.disconnect(); }} catch (_e) {{}}
      activeToneOsc = null;
    }}
    if (activeToneGain) {{
      try {{ activeToneGain.disconnect(); }} catch (_e) {{}}
      activeToneGain = null;
    }}
    const durSec = Number(isPress ? latestPressToneSec : latestReleaseToneSec);
    const freqHz = Number(latestToneHz) || 1000.0;
    if (!(durSec > 0) || !(freqHz > 0)) {{
      return;
    }}
    const now = audioCtx.currentTime;
    const fadeSec = Math.min(0.005, durSec * 0.25);
    const peakGain = Math.max(0.25, monitorVolume) * 0.5;
    const osc = audioCtx.createOscillator();
    const toneGain = audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(freqHz, now);
    toneGain.gain.setValueAtTime(0.0001, now);
    toneGain.gain.linearRampToValueAtTime(peakGain, now + fadeSec);
    if (durSec > fadeSec * 2) {{
      toneGain.gain.setValueAtTime(peakGain, now + durSec - fadeSec);
    }}
    toneGain.gain.linearRampToValueAtTime(0.0001, now + durSec);
    osc.connect(toneGain);
    toneGain.connect(audioCtx.destination);
    osc.start(now);
    osc.stop(now + durSec);
    activeToneOsc = osc;
    activeToneGain = toneGain;
    osc.onended = function() {{
      if (activeToneOsc === osc) {{
        activeToneOsc = null;
        activeToneGain = null;
      }}
    }};
  }}

  async function setTalkbackActive(active, latchedMode) {{
    const wantLatched = Boolean(latchedMode) && !latestIsRecording;
    talkbackDesired = Boolean(active);
    lastTalkbackActionTs = performance.now();
    if (active) {{
      const ok = await ensureMicCapture();
      if (!ok || !talkbackDesired) {{
        talkbackPressed = false;
        talkbackLatched = false;
        talkbackHoldActive = false;
        refreshMicAndTalkbackUi();
        return;
      }}
      if (talkbackPressed && talkbackLatched === wantLatched) {{
        return;
      }}
      const wasPressed = talkbackPressed;
      talkbackPressed = true;
      talkbackLatched = wantLatched;
      lastTalkbackActionTs = performance.now();
      if (!wasPressed && !lastObservedLocalTbActive) {{
        playHeadsetTalkbackTone(true);
      }}
      sendJson({{ type: "talkback", active: true, latched: talkbackLatched }});
      refreshMicAndTalkbackUi();
    }} else {{
      if (!talkbackPressed && !talkbackLatched) {{
        return;
      }}
      const wasPressed = talkbackPressed;
      talkbackPressed = false;
      talkbackLatched = false;
      talkbackHoldActive = false;
      lastTalkbackActionTs = performance.now();
      if (wasPressed && !lastObservedLocalTbActive) {{
        playHeadsetTalkbackTone(false);
      }}
      sendJson({{ type: "talkback", active: false, latched: false }});
      refreshMicAndTalkbackUi();
    }}
  }}

  function handleTalkbackPress() {{
    if (latestIsRecording && latestIsRecordPaused && !latestIsAutoPaused) {{
      return;
    }}
    talkbackPressTs = performance.now();
    if (!latestIsRecording) {{
      // Idle / Not Recording: click toggles latch; holding acts as momentary PTT
      if (talkbackPressed) {{
        talkbackHoldActive = false;
        setTalkbackActive(false, false);
      }} else {{
        talkbackHoldActive = true;
        setTalkbackActive(true, true);
      }}
    }} else {{
      // Recording: momentary Push-to-Talk (PTT)
      talkbackHoldActive = true;
      setTalkbackActive(true, false);
    }}
  }}

  function handleTalkbackRelease() {{
    const heldMs = performance.now() - talkbackPressTs;
    talkbackHoldActive = false;
    if (!latestIsRecording && talkbackLatched && heldMs < 250) {{
      // Quick click/tap (< 250ms) when idle keeps Talkback latched ON until clicked again
      refreshMicAndTalkbackUi();
      return;
    }}
    if (talkbackPressed || talkbackDesired) {{
      setTalkbackActive(false, false);
    }}
  }}

  btnTalkback.addEventListener("pointerdown", function(ev) {{
    ev.preventDefault();
    try {{ btnTalkback.setPointerCapture(ev.pointerId); }} catch (_e) {{}}
    handleTalkbackPress();
  }});
  btnTalkback.addEventListener("pointerup", function(ev) {{
    ev.preventDefault();
    try {{ btnTalkback.releasePointerCapture(ev.pointerId); }} catch (_e) {{}}
    handleTalkbackRelease();
  }});
  btnTalkback.addEventListener("pointercancel", function() {{
    handleTalkbackRelease();
  }});
  btnTalkback.addEventListener("pointerleave", function() {{
    if (latestIsRecording && talkbackPressed) {{
      handleTalkbackRelease();
    }}
  }});
  window.addEventListener("pointerup", function() {{
    if (talkbackHoldActive) {{
      handleTalkbackRelease();
    }}
  }});
  window.addEventListener("blur", function() {{
    if (talkbackHoldActive) {{
      handleTalkbackRelease();
    }}
  }});

  // Keyboard shortcuts: [T] or [Space] to toggle Talkback latch when idle, or hold for PTT when recording
  window.addEventListener("keydown", function(ev) {{
    if (!isAuthenticated) return;
    const tag = (ev.target && ev.target.tagName) ? ev.target.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || tag === "select" || (ev.target && ev.target.isContentEditable)) return;

    if ((ev.code === "KeyT" || ev.code === "Space") && !ev.repeat) {{
      ev.preventDefault();
      handleTalkbackPress();
    }}
  }});

  window.addEventListener("keyup", function(ev) {{
    if (!isAuthenticated) return;
    const tag = (ev.target && ev.target.tagName) ? ev.target.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || tag === "select" || (ev.target && ev.target.isContentEditable)) return;
    if (ev.code === "KeyT" || ev.code === "Space") {{
      handleTalkbackRelease();
    }}
  }});

  function openProjectEditBar() {{
    if (!latestAllowProjectRename || latestIsRecording) return;
    projectEditFeedback.style.display = "none";
    projectEditFeedback.textContent = "";
    projectNameInput.value = latestProjectName || "";
    projectEditBar.style.display = "flex";
    projectNameInput.focus();
    projectNameInput.select();
  }}

  function closeProjectEditBar() {{
    projectEditBar.style.display = "none";
    projectEditFeedback.style.display = "none";
    projectEditFeedback.textContent = "";
  }}

  function submitProjectRename() {{
    if (!latestAllowProjectRename || latestIsRecording) return;
    const nextName = (projectNameInput.value || "").trim();
    if (!nextName) {{
      projectEditFeedback.textContent = "Please enter a valid project name.";
      projectEditFeedback.style.display = "block";
      return;
    }}
    sendJson({{ type: "set_project_name", project_name: nextName }});
  }}

  btnEditProject.addEventListener("click", function() {{
    if (projectEditBar.style.display === "flex") {{
      closeProjectEditBar();
    }} else {{
      openProjectEditBar();
    }}
  }});
  btnCancelProject.addEventListener("click", closeProjectEditBar);
  btnSaveProject.addEventListener("click", submitProjectRename);
  projectNameInput.addEventListener("keydown", function(ev) {{
    if (ev.key === "Enter") {{
      ev.preventDefault();
      submitProjectRename();
    }} else if (ev.key === "Escape") {{
      ev.preventDefault();
      closeProjectEditBar();
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
        send_enabled: true,
        cough_muted: false
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
        reconnectTimer = setTimeout(async function() {{
          try {{
            const statusResp = await fetch("/api/status", {{ cache: "no-store" }});
            if (statusResp.status === 401 || statusResp.status === 403 || statusResp.status === 429) {{
              stopMicCapture();
              showView(false);
              if (statusResp.status === 429) {{
                const ra = parseInt(statusResp.headers.get("Retry-After") || "60", 10);
                startLockoutCountdown(Number.isFinite(ra) ? ra : 60, "Too many failed login attempts");
              }}
              return;
            }}
          }} catch (_e) {{}}
          connectWebSocket();
        }}, 1500);
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
    if (msg.type === "talkback_unlatched") {{
      const wasActive = talkbackPressed || Boolean(lastObservedLocalTbActive);
      talkbackPressed = false;
      talkbackDesired = false;
      talkbackLatched = false;
      talkbackHoldActive = false;
      lastTalkbackActionTs = performance.now();
      if (wasActive) {{
        playHeadsetTalkbackTone(false);
      }}
      lastObservedLocalTbActive = false;
      latestIsRecording = true;
      refreshMicAndTalkbackUi();
      return;
    }}
    if (msg.type === "script_updated") {{
      if (msg.project_name && msg.project_name === editingScriptProject && document.activeElement !== scriptEditor) {{
        scriptEditor.innerHTML = msg.html || "";
        scriptStatusMsg.textContent = "✓ Script updated from studio (" + msg.project_name + "/script.html).";
        scriptStatusMsg.style.color = "#7EE787";
      }}
      return;
    }}
    if (msg.type === "project_rename_result") {{
      if (msg.ok) {{
        latestProjectName = msg.project_name || latestProjectName;
        hdrProject.textContent = "Project: " + (latestProjectName || "Default");
        closeProjectEditBar();
      }} else {{
        projectEditFeedback.textContent = msg.error || "Could not update project name.";
        projectEditFeedback.style.display = "block";
      }}
      return;
    }}
    if (msg.type !== "state") {{
      return;
    }}

    const wasRecording = latestIsRecording;
    latestIsRecording = Boolean(msg.is_recording);
    latestIsRecordPaused = Boolean(msg.is_record_paused);
    latestIsAutoPaused = Boolean(msg.is_auto_paused);
    if (!wasRecording && latestIsRecording) {{
      // Recording just started: unlatch Talkback and switch to momentary PTT
      if (talkbackLatched || (talkbackPressed && !talkbackHoldActive)) {{
        talkbackPressed = false;
        talkbackDesired = false;
        talkbackLatched = false;
        talkbackHoldActive = false;
        lastTalkbackActionTs = performance.now();
        sendJson({{ type: "talkback", active: false, latched: false }});
      }}
    }} else if (
      talkbackPressed &&
      !talkbackHoldActive &&
      !msg.remote_talkback_active &&
      (performance.now() - lastTalkbackActionTs) > 600
    ) {{
      talkbackPressed = false;
      talkbackDesired = false;
      talkbackLatched = false;
    }}
    refreshMicAndTalkbackUi();

    hdrSession.textContent = "Session: " + (msg.session_id || "--");
    latestProjectName = msg.project_name || "Default";
    hdrProject.textContent = "Project: " + latestProjectName;
    latestAllowProjectRename = Boolean(msg.allow_project_rename);
    btnEditProject.style.display = latestAllowProjectRename ? "inline-block" : "none";
    btnEditProject.disabled = latestIsRecording || latestIsRecordPaused;
    btnEditProject.title = (latestIsRecording || latestIsRecordPaused)
      ? "Project name cannot be changed while recording"
      : "Update active Project Name";
    btnSaveProject.disabled = latestIsRecording || latestIsRecordPaused;
    if (!latestAllowProjectRename && projectEditBar.style.display === "flex") {{
      closeProjectEditBar();
    }}

    latestStudioMode = Boolean(msg.studio_mode);
    latestChannels = Number(msg.channels || 2);
    if (msg.talkback_tone_hz !== undefined) {{
      latestToneHz = Number(msg.talkback_tone_hz);
    }}
    if (msg.talkback_tone_duration_sec !== undefined) {{
      latestPressToneSec = Number(msg.talkback_tone_duration_sec);
    }}
    if (msg.talkback_release_tone_duration_sec !== undefined) {{
      latestReleaseToneSec = Number(msg.talkback_release_tone_duration_sec);
    }}

    const nowLocalTbActive = Boolean(msg.local_talkback_active);
    if (
      lastObservedLocalTbActive !== null &&
      nowLocalTbActive !== lastObservedLocalTbActive &&
      !talkbackPressed
    ) {{
      playHeadsetTalkbackTone(nowLocalTbActive);
    }}
    lastObservedLocalTbActive = nowLocalTbActive;

    if (latestStudioMode) {{
      modeSummary.textContent =
        "Mode: Studio Split Mode (Track L = Talent VO, Track R = Producer & Talent Talkback) @ " +
        (msg.sample_rate || 48000) + " Hz / " + (msg.bit_depth || 24) + "-bit";
    }} else {{
      modeSummary.textContent =
        "Mode: Standard (" + latestChannels + "-Ch) @ " +
        (msg.sample_rate || 48000) + " Hz / " + (msg.bit_depth || 24) + "-bit";
    }}

    if (msg.is_playing) {{
      timerDisplay.textContent = formatElapsed(msg.play_position_sec) + " / " + formatElapsed(msg.play_duration_sec);
    }} else {{
      timerDisplay.textContent = formatElapsed(msg.elapsed_seconds);
    }}

    if (msg.talkback_active) {{
      studioDeck.className = "studio-deck deck-talkback";
      stateBanner.className = "state-banner banner-talkback";
      const who = (msg.remote_talkback_active && msg.local_talkback_active)
        ? "PRODUCER & TALENT TALKBACK ON AIR"
        : (msg.remote_talkback_active
          ? (msg.remote_talkback_latched ? "PRODUCER TALKBACK LATCHED" : "PRODUCER TALKBACK ON AIR")
          : (msg.local_talkback_latched ? "TALENT TALKBACK LATCHED" : "TALENT TALKBACK ACTIVE"));
      const recTarget = latestStudioMode
        ? " — RECORDING TO TALKBACK TRACK (R)"
        : " — RECORDING TO MAIN CHANNEL(S)";
      stateBanner.textContent = "⚡ " + who + (msg.is_recording ? recTarget : "");
      timerDisplay.className = msg.is_recording ? "timer-display timer-rec" : "timer-display";
    }} else if (msg.is_recording && msg.is_record_paused) {{
      studioDeck.className = "studio-deck deck-paused";
      stateBanner.className = "state-banner banner-paused";
      stateBanner.textContent = msg.is_auto_paused
        ? "❚❚ RECORDING AUTO-PAUSED (MIC MUTED — TALKBACK OR UNMUTE TO RESUME)"
        : "❚❚ RECORDING PAUSED";
      timerDisplay.className = "timer-display timer-paused";
    }} else if (msg.is_recording) {{
      studioDeck.className = "studio-deck deck-recording";
      stateBanner.className = "state-banner banner-rec";
      stateBanner.textContent = "● RECORDING IN PROGRESS";
      timerDisplay.className = "timer-display timer-rec";
    }} else if (msg.is_playing && msg.is_play_paused) {{
      studioDeck.className = "studio-deck deck-paused";
      stateBanner.className = "state-banner banner-paused";
      const playClip = msg.playing_take_name ? (": " + msg.playing_take_name) : "";
      stateBanner.textContent = "❚❚ PLAYBACK PAUSED" + playClip;
      timerDisplay.className = "timer-display timer-paused";
    }} else if (msg.is_playing) {{
      studioDeck.className = "studio-deck deck-playing";
      stateBanner.className = "state-banner banner-playing";
      const playClip = msg.playing_take_name ? (": " + msg.playing_take_name) : "";
      stateBanner.textContent = "▶ PLAYING RECORDED TAKE" + playClip;
      timerDisplay.className = "timer-display timer-playing";
    }} else {{
      studioDeck.className = "studio-deck";
      stateBanner.className = "state-banner";
      stateBanner.textContent = "● STANDBY — LIVE INPUT MONITOR";
      timerDisplay.className = "timer-display";
    }}

    if (msg.is_playing) {{
      talentFeedBadge.textContent = "▶ Reference Playback Stream: LIVE";
      talentFeedBadge.className = "pill-live";
    }} else if (msg.talent_mic_live) {{
      talentFeedBadge.textContent = "◉ Talent Mic Feed: LIVE";
      talentFeedBadge.className = "pill-live";
    }} else if (msg.talent_cough_muted) {{
      talentFeedBadge.textContent = "✕ Talent Mic Feed: COUGH MUTE ACTIVE";
      talentFeedBadge.className = "pill-muted";
    }} else {{
      talentFeedBadge.textContent = "○ Talent Mic Feed: MUTED FOR PRIVACY";
      talentFeedBadge.className = "pill-muted";
    }}

    const activeTake = msg.active_take_name
      ? (" (" + msg.active_take_name + ")")
      : (msg.playing_take_name ? (" (Reviewing: " + msg.playing_take_name + ")") : "");
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
    if (nextPlayTime < now || nextPlayTime > now + 0.35) {{
      nextPlayTime = now + 0.025;
    }}
    src.start(nextPlayTime);
    nextPlayTime += audioBuf.duration;
  }}

  // 60-Segment LED VU & Peak Hold Canvas Renderer (Matching Talent UI)
  function dbToNorm(db) {{
    const num = Number(db);
    const validDb = Number.isFinite(num) ? num : -60.0;
    const clamped = Math.max(-60.0, Math.min(0.0, validDb));
    return (clamped + 60.0) / 60.0;
  }}

  function drawVuMeter() {{
    const ctx = vuCanvas.getContext("2d");
    const w = vuCanvas.width;
    const h = vuCanvas.height;
    ctx.fillStyle = "#0D1014";
    ctx.fillRect(0, 0, w, h);

    const labels = latestStudioMode ? ["VO (L)", "TB (R)"] : (latestChannels >= 2 ? ["CH 1", "CH 2"] : ["MONO"]);
    const peaks = Array.isArray(latestVu.channel_peaks) ? latestVu.channel_peaks : [-60.0, -60.0];
    const rmsList = Array.isArray(latestVu.channel_rms) ? latestVu.channel_rms : [-60.0, -60.0];
    const holds = Array.isArray(latestVu.channel_peak_holds) ? latestVu.channel_peak_holds : [-60.0, -60.0];

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

      const chPeakDb = peaks[chIdx] !== undefined ? peaks[chIdx] : -60.0;
      const chRmsDb = rmsList[chIdx] !== undefined ? rmsList[chIdx] : -60.0;
      const chHoldDb = holds[chIdx] !== undefined ? holds[chIdx] : -60.0;

      const peakNorm = dbToNorm(chPeakDb);
      const rmsNorm = dbToNorm(chRmsDb);
      const holdNorm = dbToNorm(chHoldDb);

      const activePeakSegs = Math.round(peakNorm * numSegments);
      const activeRmsSegs = Math.round(rmsNorm * numSegments);
      const holdSeg = Math.min(numSegments - 1, Math.round(holdNorm * (numSegments - 1)));

      for (let s = 0; s < numSegments; s++) {{
        const segDb = -60.0 + ((s + 1) / numSegments) * 60.0;
        let rmsColor = "#3FB950";
        let peakColor = "#2EA043";
        let dimColor = "#112618";
        if (segDb >= -6.0) {{
          rmsColor = "#FF4444";
          peakColor = "#F85149";
          dimColor = "#2D1215";
        }} else if (segDb >= -18.0) {{
          rmsColor = "#F0B72F";
          peakColor = "#D29922";
          dimColor = "#2B2311";
        }}

        const x = leftPad + s * (segW + segGap);
        if (s < activeRmsSegs) {{
          ctx.fillStyle = rmsColor;
        }} else if (s < activePeakSegs) {{
          ctx.fillStyle = peakColor;
        }} else {{
          ctx.fillStyle = dimColor;
        }}
        ctx.fillRect(x, rowY, segW, rowH);

        if (s === holdSeg && holdNorm > 0.005) {{
          ctx.fillStyle = chHoldDb >= -3.0 ? "#FF5555" : (chHoldDb >= -18.0 ? "#FFDF5D" : "#58A6FF");
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

  // ------------------------------------------------------------------
  // Rich-Text Script Editor & Standing Projects Management
  // ------------------------------------------------------------------
  function populateScriptProjectSelect() {{
    const currentVal = editingScriptProject || latestProjectName || "Session";
    scriptProjectSelect.innerHTML = "";
    const names = [];
    if (authKind === "local" && cachedProjects.length > 0) {{
      cachedProjects.forEach(function(p) {{
        if (p && p.project_name && names.indexOf(p.project_name) === -1) {{
          names.push(p.project_name);
        }}
      }});
    }}
    if (latestProjectName && names.indexOf(latestProjectName) === -1) {{
      names.unshift(latestProjectName);
    }}
    if (names.length === 0) {{
      names.push(currentVal);
    }}
    names.forEach(function(name) {{
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name + (name === latestProjectName ? " (Active)" : "");
      scriptProjectSelect.appendChild(opt);
    }});
    if (names.indexOf(currentVal) !== -1) {{
      scriptProjectSelect.value = currentVal;
    }} else {{
      scriptProjectSelect.value = names[0];
    }}
    editingScriptProject = scriptProjectSelect.value;
    scriptProjectSelect.disabled = (authKind !== "local");
  }}

  async function loadScriptForProject(projName) {{
    const target = (projName || latestProjectName || "").trim();
    if (!target) return;
    editingScriptProject = target;
    populateScriptProjectSelect();
    scriptStatusMsg.textContent = "Loading script for " + target + "...";
    scriptStatusMsg.style.color = "#8B949E";
    try {{
      const resp = await fetch("/api/script?project=" + encodeURIComponent(target), {{ cache: "no-store" }});
      const data = await resp.json();
      if (resp.ok && data.ok) {{
        editingScriptProject = data.project_name || target;
        scriptEditor.innerHTML = data.html || "";
        scriptStatusMsg.textContent = "Loaded /storage/" + editingScriptProject + "/script.html";
        scriptStatusMsg.style.color = "#8B949E";
      }} else {{
        scriptStatusMsg.textContent = data.error || "Could not load script.";
        scriptStatusMsg.style.color = "#FFA198";
      }}
    }} catch (_e) {{
      scriptStatusMsg.textContent = "Network error loading script.";
      scriptStatusMsg.style.color = "#FFA198";
    }}
  }}

  async function saveCurrentScript() {{
    const target = (scriptProjectSelect.value || editingScriptProject || latestProjectName || "").trim();
    if (!target) return;
    btnSaveScript.disabled = true;
    scriptStatusMsg.textContent = "Saving & syncing to Prompter...";
    scriptStatusMsg.style.color = "#79C0FF";
    try {{
      const resp = await fetch("/api/script", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{
          project_name: target,
          html: scriptEditor.innerHTML
        }})
      }});
      const data = await resp.json();
      if (resp.ok && data.ok) {{
        scriptStatusMsg.textContent = "✓ Saved to /storage/" + data.project_name + "/script.html & synced live to Prompter.";
        scriptStatusMsg.style.color = "#7EE787";
        if (authKind === "local") {{
          refreshProjectsList();
        }}
      }} else {{
        scriptStatusMsg.textContent = data.error || "Failed to save script.";
        scriptStatusMsg.style.color = "#FFA198";
      }}
    }} catch (_e) {{
      scriptStatusMsg.textContent = "Network error saving script.";
      scriptStatusMsg.style.color = "#FFA198";
    }} finally {{
      btnSaveScript.disabled = false;
    }}
  }}

  document.querySelectorAll(".rte-btn").forEach(function(btn) {{
    btn.addEventListener("click", function() {{
      scriptEditor.focus();
      const cmd = btn.getAttribute("data-cmd");
      const block = btn.getAttribute("data-block");
      const color = btn.getAttribute("data-color");
      const ins = btn.getAttribute("data-insert");
      if (cmd) {{
        document.execCommand(cmd, false, null);
      }} else if (block) {{
        document.execCommand("formatBlock", false, block);
      }} else if (color) {{
        document.execCommand("foreColor", false, color);
      }} else if (ins) {{
        document.execCommand("insertText", false, " " + ins + " ");
      }}
    }});
  }});

  scriptProjectSelect.addEventListener("change", function() {{
    loadScriptForProject(scriptProjectSelect.value);
  }});
  btnReloadScript.addEventListener("click", function() {{
    loadScriptForProject(scriptProjectSelect.value || editingScriptProject || latestProjectName);
  }});
  btnSaveScript.addEventListener("click", saveCurrentScript);

  function showProjectsFeedback(msg, isError) {{
    if (!msg) {{
      projectsFeedback.style.display = "none";
      projectsFeedback.textContent = "";
      return;
    }}
    projectsFeedback.textContent = msg;
    projectsFeedback.style.color = isError ? "#FFA198" : "#7EE787";
    projectsFeedback.style.display = "block";
  }}

  async function refreshProjectsList() {{
    if (authKind !== "local") return;
    try {{
      const resp = await fetch("/api/projects", {{ cache: "no-store" }});
      const data = await resp.json();
      if (!resp.ok || !data.ok) {{
        showProjectsFeedback(data.error || "Could not load projects.", true);
        return;
      }}
      cachedProjects = Array.isArray(data.projects) ? data.projects : [];
      populateScriptProjectSelect();
      renderProjectsTable(cachedProjects, Boolean(data.is_recording));
    }} catch (_e) {{
      showProjectsFeedback("Network error loading projects.", true);
    }}
  }}

  function renderProjectsTable(projects, isRecording) {{
    projectsTbody.innerHTML = "";
    projects.forEach(function(proj) {{
      const tr = document.createElement("tr");

      const tdName = document.createElement("td");
      tdName.style.fontWeight = "700";
      tdName.textContent = proj.project_name + (proj.is_active_project ? "  ★ ACTIVE" : "");
      if (proj.is_active_project) {{
        tdName.style.color = "#7EE787";
      }}
      tr.appendChild(tdName);

      const tdTakes = document.createElement("td");
      tdTakes.textContent = String(proj.recording_count || 0);
      tr.appendChild(tdTakes);

      const tdSize = document.createElement("td");
      tdSize.textContent = proj.formatted_size || "0 B";
      tr.appendChild(tdSize);

      const tdScript = document.createElement("td");
      tdScript.textContent = proj.has_script ? "✓ Script" : "—";
      tdScript.style.color = proj.has_script ? "#79C0FF" : "#6E7681";
      tr.appendChild(tdScript);

      const tdAct = document.createElement("td");
      const wrap = document.createElement("div");
      wrap.className = "proj-actions";

      const btnEdit = document.createElement("button");
      btnEdit.type = "button";
      btnEdit.className = "btn-subtle";
      btnEdit.textContent = "▤ Edit Script";
      btnEdit.addEventListener("click", function() {{
        editingScriptProject = proj.project_name;
        switchNavTab("script");
      }});
      wrap.appendChild(btnEdit);

      if (!proj.is_active_project) {{
        const btnAct = document.createElement("button");
        btnAct.type = "button";
        btnAct.className = "btn-subtle";
        btnAct.textContent = "Set Active";
        btnAct.disabled = isRecording;
        btnAct.addEventListener("click", async function() {{
          const r = await fetch("/api/projects/select", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ project_name: proj.project_name }})
          }});
          const d = await r.json();
          if (r.ok && d.ok) {{
            showProjectsFeedback("Active project switched to '" + d.project_name + "'.", false);
            refreshProjectsList();
          }} else {{
            showProjectsFeedback(d.error || "Could not switch project.", true);
          }}
        }});
        wrap.appendChild(btnAct);
      }}

      const btnRen = document.createElement("button");
      btnRen.type = "button";
      btnRen.className = "btn-subtle";
      btnRen.textContent = "✎ Rename";
      btnRen.disabled = isRecording;
      btnRen.addEventListener("click", async function() {{
        const nextName = window.prompt("Rename project '" + proj.project_name + "' to:", proj.project_name);
        if (!nextName || nextName.trim() === proj.project_name) return;
        const r = await fetch("/api/projects/rename", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ old_name: proj.project_name, new_name: nextName.trim() }})
        }});
        const d = await r.json();
        if (r.ok && d.ok) {{
          showProjectsFeedback("Renamed project to '" + d.project_name + "'.", false);
          refreshProjectsList();
        }} else {{
          showProjectsFeedback(d.error || "Could not rename project.", true);
        }}
      }});
      wrap.appendChild(btnRen);

      tdAct.appendChild(wrap);
      tr.appendChild(tdAct);
      projectsTbody.appendChild(tr);
    }});
  }}

  async function createProjectFromBar(setActive) {{
    const name = (newProjectNameInput.value || "").trim();
    if (!name) {{
      showProjectsFeedback("Please enter a project name.", true);
      return;
    }}
    try {{
      const r = await fetch("/api/projects", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ project_name: name, set_active: Boolean(setActive) }})
      }});
      const d = await r.json();
      if (r.ok && d.ok) {{
        newProjectNameInput.value = "";
        showProjectsFeedback("Created project '" + d.project_name + "'.", false);
        refreshProjectsList();
      }} else {{
        showProjectsFeedback(d.error || "Could not create project.", true);
      }}
    }} catch (_e) {{
      showProjectsFeedback("Network error creating project.", true);
    }}
  }}

  btnCreateProject.addEventListener("click", function() {{ createProjectFromBar(false); }});
  btnCreateActivateProject.addEventListener("click", function() {{ createProjectFromBar(true); }});
  btnRefreshProjects.addEventListener("click", refreshProjectsList);

  showView(isAuthenticated);
  if (!isAuthenticated) {{
    if (initialRetryAfter > 0) {{
      startLockoutCountdown(initialRetryAfter, initialError);
    }} else if (initialError) {{
      loginError.textContent = initialError;
      loginError.style.display = "block";
    }}
  }}
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
