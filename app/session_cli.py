"""Headless CLI and loopback control server for Remote Producer web session keys.

Provides:
  - ``SessionControlServer``: Lightweight loopback-only TCP JSON control server
    and private runtime state writer (``web_session.json``) embedded inside
    ``RemoteProducerServer``.
  - ``alpine-recorder session ...`` CLI entrypoint (``python3 -m app.session_cli``)
    that queries or controls the running workstation without importing PyQt6,
    NumPy, or audio/X11 libraries.
"""

from __future__ import annotations

import argparse
import ctypes
import hmac
import json
import logging
import os
import secrets
import socket
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config_manager import DEFAULT_ETC_CONFIG_DIR, ConfigManager
from app.qr_code import encode_qr_matrix

logger = logging.getLogger("AlpineRecorder.SessionCLI")

SESSION_STATE_FILENAME = "web_session.json"
DEFAULT_RUNTIME_SYSTEM_DIR = "/run/alpine-recorder"
_MAX_CONTROL_REQUEST_BYTES = 8192
_CONTROL_SOCKET_TIMEOUT_SEC = 2.0


def resolve_runtime_dir(
    etc_config_dir: Optional[Path | str] = None,
    runtime_dir: Optional[Path | str] = None,
) -> Path:
    """Resolve the private runtime directory for ``web_session.json``.

    Precedence:
      1. Explicit ``runtime_dir`` argument
      2. ``ALPINE_RECORDER_RUNTIME_DIR`` environment variable
      3. ``/run/alpine-recorder`` on POSIX when using the default system config dir
         and ``/run`` (or ``/run/alpine-recorder``) is accessible
      4. ``<etc_config_dir>/runtime``
    """
    if runtime_dir is not None and str(runtime_dir).strip():
        return Path(str(runtime_dir).strip())

    env_rt = os.environ.get("ALPINE_RECORDER_RUNTIME_DIR", "").strip()
    if env_rt:
        return Path(env_rt)

    cfg_dir = Path(
        etc_config_dir
        or os.environ.get("ALPINE_RECORDER_CONFIG_DIR")
        or DEFAULT_ETC_CONFIG_DIR
    )
    if os.name == "posix" and cfg_dir == Path(DEFAULT_ETC_CONFIG_DIR):
        sys_rt = Path(DEFAULT_RUNTIME_SYSTEM_DIR)
        run_root = Path("/run")
        if (
            (sys_rt.is_dir() and (os.access(sys_rt, os.W_OK) or (sys_rt / SESSION_STATE_FILENAME).is_file()))
            or (run_root.is_dir() and os.access(run_root, os.W_OK))
        ):
            return sys_rt

    return cfg_dir / "runtime"


def is_pid_alive(pid: int) -> bool:
    """Return True if ``pid`` refers to a live (non-zombie) OS process."""
    if not isinstance(pid, int) or pid <= 0:
        return False

    if os.name == "posix":
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.is_file():
            try:
                stat_text = stat_path.read_text(encoding="utf-8", errors="ignore")
                rparen = stat_text.rfind(")")
                if rparen != -1:
                    after = stat_text[rparen + 1 :].lstrip()
                    if after and after[0] == "Z":
                        return False
            except OSError:
                pass
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return ctypes.GetLastError() == ERROR_ACCESS_DENIED
            try:
                exit_code = ctypes.c_uint32(0)
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                if not ok:
                    return False
                return int(exit_code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False

    return False


def write_session_state_file(state_path: Path, payload: Dict[str, Any]) -> None:
    """Atomically write ``web_session.json`` with private (0700 dir / 0600 file) permissions."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(state_path.parent, 0o700)
        except OSError:
            pass

    raw_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp_path = state_path.parent / f".{state_path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    try:
        fd = os.open(str(tmp_path), flags, 0o600)
        with os.fdopen(fd, "wb") as fp:
            fp.write(raw_bytes)
            fp.flush()
        if os.name == "posix":
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
        os.replace(tmp_path, state_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_session_state_file(state_path: Path) -> Optional[Dict[str, Any]]:
    """Read and parse ``web_session.json`` if it exists and is valid JSON."""
    if not state_path.is_file():
        return None
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def remove_session_state_file(
    state_path: Path,
    expected_secret: Optional[str] = None,
) -> None:
    """Remove ``web_session.json`` unless owned by a different ``control_secret``."""
    if expected_secret and state_path.is_file():
        existing = read_session_state_file(state_path)
        if existing is not None:
            existing_secret = str(existing.get("control_secret") or "")
            if existing_secret and not hmac.compare_digest(existing_secret, expected_secret):
                return
    try:
        state_path.unlink(missing_ok=True)
    except OSError:
        pass


def _format_url_host(host: str) -> str:
    clean = str(host or "").strip()
    if ":" in clean and not clean.startswith("["):
        return f"[{clean}]"
    return clean


def build_session_urls(
    host: str,
    port: int,
    session_id: str,
    invite_token: str,
) -> Tuple[str, str]:
    """Return ``(session_url, login_url)`` for a given host, port, and credentials."""
    url_host = _format_url_host(host)
    login_url = f"https://{url_host}:{int(port)}/"
    query = urllib.parse.urlencode({
        "session": session_id,
        "token": invite_token,
    })
    return (f"{login_url}?{query}", login_url)


def sanitize_public_state(
    state: Dict[str, Any],
    host_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a copy of ``state`` with internal ``control_*`` keys stripped and optional host applied."""
    clean = {
        k: v
        for k, v in state.items()
        if k not in ("control_secret", "control_host", "control_port")
    }
    if host_override is not None and str(host_override).strip():
        host_str = str(host_override).strip()
        port = int(clean.get("port") or 8443)
        sid = str(clean.get("session_id") or "")
        token = str(clean.get("invite_token") or "")
        session_url, login_url = build_session_urls(host_str, port, sid, token)
        clean["primary_host"] = host_str
        clean["login_url"] = login_url
        clean["session_url"] = session_url
    return clean


class SessionControlServer:
    """Authenticated loopback TCP control server for live CLI session queries and actions."""

    def __init__(
        self,
        runtime_dir: Path,
        state_provider: Callable[[Optional[str]], Dict[str, Any]],
        action_handler: Callable[[str, Dict[str, Any]], Tuple[bool, str, Dict[str, Any]]],
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.state_path = self.runtime_dir / SESSION_STATE_FILENAME
        self.state_provider = state_provider
        self.action_handler = action_handler

        self.control_secret: str = secrets.token_hex(32)
        self.persistent: bool = False

        self._lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._port: int = 0

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_running(self) -> bool:
        return self._running.is_set() and self._sock is not None and self._port > 0

    def start(self, persistent: bool = False) -> bool:
        """Start the loopback TCP listener and write ``web_session.json``."""
        with self._lock:
            if persistent:
                self.persistent = True
            if self.is_running:
                self.sync_state()
                return True

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", 0))
                sock.listen(8)
                sock.settimeout(0.4)
                self._sock = sock
                self._port = int(sock.getsockname()[1])
                self._running.set()
                self._thread = threading.Thread(
                    target=self._accept_loop,
                    daemon=True,
                    name="RemoteProducerControl",
                )
                self._thread.start()
            except Exception as exc:
                logger.warning("Failed to start session control server: %s", exc)
                self._running.clear()
                self._sock = None
                self._port = 0
                try:
                    sock.close()
                except OSError:
                    pass
                return False

        self.sync_state()
        return True

    def sync_state(self) -> None:
        """Write the latest session snapshot and control endpoint metadata atomically."""
        with self._lock:
            if not self.is_running:
                return
            snap = dict(self.state_provider(None) or {})
            snap["control_host"] = "127.0.0.1"
            snap["control_port"] = int(self._port)
            snap["control_secret"] = self.control_secret
            try:
                write_session_state_file(self.state_path, snap)
            except OSError as exc:
                logger.warning("Failed to write %s: %s", self.state_path, exc)

    def stop(self) -> None:
        """Stop the loopback listener and remove ``web_session.json`` if owned by this instance."""
        with self._lock:
            self._running.clear()
            sock = self._sock
            self._sock = None
            self._port = 0
            thread = self._thread
            self._thread = None

        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)

        remove_session_state_file(self.state_path, expected_secret=self.control_secret)

    def _accept_loop(self) -> None:
        while self._running.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_client(conn)

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(_CONTROL_SOCKET_TIMEOUT_SEC)
            chunks = bytearray()
            while len(chunks) < _MAX_CONTROL_REQUEST_BYTES:
                data = conn.recv(min(4096, _MAX_CONTROL_REQUEST_BYTES - len(chunks)))
                if not data:
                    break
                chunks.extend(data)
                if b"\n" in data:
                    break

            if len(chunks) >= _MAX_CONTROL_REQUEST_BYTES and b"\n" not in chunks:
                self._send_reply(conn, {"ok": False, "error": "Control request too large."})
                return

            line = bytes(chunks).split(b"\n", 1)[0].strip()
            if not line:
                self._send_reply(conn, {"ok": False, "error": "Empty control request."})
                return

            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:
                self._send_reply(conn, {"ok": False, "error": "Invalid JSON request."})
                return

            if not isinstance(req, dict):
                self._send_reply(conn, {"ok": False, "error": "Control request must be a JSON object."})
                return

            provided_secret = str(req.get("secret") or "")
            if not hmac.compare_digest(provided_secret, self.control_secret):
                self._send_reply(conn, {"ok": False, "error": "Unauthorized control request."})
                return

            action = str(req.get("action") or "status").lower().strip()
            if action not in ("status", "enable", "disable", "rotate", "unban", "reload_users"):
                self._send_reply(conn, {"ok": False, "error": f"Unsupported action: {action}"})
                return

            ok, msg, state = self.action_handler(action, req)
            self.sync_state()

            clean_state = sanitize_public_state(state or {}, host_override=req.get("host"))
            self._send_reply(
                conn,
                {
                    "ok": bool(ok),
                    "action": action,
                    "message": msg if ok else "",
                    "error": "" if ok else msg,
                    "state": clean_state,
                },
            )
        except Exception as exc:
            logger.debug("Session control request handling failed: %s", exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send_reply(conn: socket.socket, payload: Dict[str, Any]) -> None:
        raw = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            conn.sendall(raw)
        except OSError:
            pass


def send_control_command(
    state_path: Path,
    action: str = "status",
    port: Optional[int] = None,
    host: Optional[str] = None,
    rotate: bool = False,
    timeout: float = 2.5,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Query or send a live control command to the running workstation."""
    raw_state = read_session_state_file(state_path)
    if raw_state is None:
        return (False, "No active session state found.", None)

    pid = int(raw_state.get("pid") or 0)
    ctrl_host = str(raw_state.get("control_host") or "127.0.0.1")
    ctrl_port = int(raw_state.get("control_port") or 0)
    ctrl_secret = str(raw_state.get("control_secret") or "")

    if ctrl_port > 0 and ctrl_secret:
        req: Dict[str, Any] = {
            "action": action,
            "secret": ctrl_secret,
        }
        if port is not None:
            req["port"] = int(port)
        if host is not None:
            req["host"] = str(host)
        if rotate:
            req["rotate"] = True

        try:
            with socket.create_connection((ctrl_host, ctrl_port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
                chunks = bytearray()
                while len(chunks) < 65536:
                    data = sock.recv(4096)
                    if not data:
                        break
                    chunks.extend(data)
                    if b"\n" in data:
                        break
                line = bytes(chunks).split(b"\n", 1)[0].strip()
                if line:
                    resp = json.loads(line.decode("utf-8"))
                    if isinstance(resp, dict):
                        ok = bool(resp.get("ok"))
                        msg = str(resp.get("message") if ok else resp.get("error") or "")
                        state = resp.get("state")
                        if isinstance(state, dict):
                            state = sanitize_public_state(state, host_override=host)
                        else:
                            state = None
                        return (ok, msg, state)
        except (OSError, ValueError):
            pass

    if not is_pid_alive(pid):
        remove_session_state_file(state_path, expected_secret=ctrl_secret or None)
        return (False, "Alpine Recording Station is not currently running.", None)

    if action == "status":
        fallback = sanitize_public_state(raw_state, host_override=host)
        return (True, "Read cached session state from running process.", fallback)

    return (
        False,
        "Could not connect to running Alpine Recording Station control socket.",
        None,
    )


def render_qr_terminal(
    text_or_url: str,
    border: int = 2,
    use_ansi: bool = True,
) -> str:
    """Render an ISO/IEC 18004 QR code as compact Unicode half-blocks for terminal display."""
    matrix = encode_qr_matrix(text_or_url)
    size = len(matrix)
    pad = max(1, int(border))
    width = size + pad * 2

    padded: List[List[bool]] = []
    empty_row = [False] * width
    for _ in range(pad):
        padded.append(empty_row)
    for row in matrix:
        padded.append(([False] * pad) + list(row) + ([False] * pad))
    for _ in range(pad):
        padded.append(empty_row)

    lines: List[str] = []
    total_rows = len(padded)
    for r in range(0, total_rows, 2):
        top_row = padded[r]
        bot_row = padded[r + 1] if r + 1 < total_rows else empty_row
        chars: List[str] = []
        for c in range(width):
            t = bool(top_row[c])
            b = bool(bot_row[c])
            if t and b:
                chars.append("█")
            elif t:
                chars.append("▀")
            elif b:
                chars.append("▄")
            else:
                chars.append(" ")
        row_str = "".join(chars)
        if use_ansi:
            lines.append(f"\033[30;47m{row_str}\033[0m")
        else:
            lines.append(row_str)

    return "\n".join(lines)


_VALID_COMMANDS = (
    "status",
    "show",
    "info",
    "enable",
    "start",
    "disable",
    "stop",
    "rotate",
    "new",
    "url",
    "link",
    "pin",
    "passcode",
    "key",
    "token",
    "id",
    "qr",
    "unban",
)


def parse_cli_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="alpine-recorder session",
        description="Inspect and control the live Remote Producer web interface session.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        choices=_VALID_COMMANDS,
        help="Optional subcommand (status, enable, disable, rotate, unban, url, pin, key, id, qr).",
    )
    parser.add_argument("--enable", action="store_true", help="Enable and start the HTTPS web server.")
    parser.add_argument("--disable", action="store_true", help="Stop and disable the HTTPS web server.")
    parser.add_argument("--rotate", action="store_true", help="Generate a new Session ID, PIN, and invite token.")
    parser.add_argument("--unban", action="store_true", help="Clear all temporary Fail2Ban login IP lockouts.")
    parser.add_argument("--port", type=int, default=None, help="HTTPS port when enabling (1024-65535).")
    parser.add_argument("--host", default=None, help="Override IP/hostname shown in session URLs and QR code.")
    parser.add_argument("--url", action="store_true", help="Print only the auto-login invite URL.")
    parser.add_argument("--login-url", action="store_true", help="Print only the manual HTTPS login URL.")
    parser.add_argument("--pin", "--passcode", dest="pin", action="store_true", help="Print only the 6-digit PIN.")
    parser.add_argument("--key", "--token", dest="key", action="store_true", help="Print only the invite token.")
    parser.add_argument("--id", action="store_true", help="Print only the Session ID.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
    parser.add_argument("--summary", action="store_true", help="Output a safe single-line status summary.")
    parser.add_argument("--qr", action="store_true", help="Always render the terminal QR code.")
    parser.add_argument("--no-qr", action="store_true", help="Suppress the terminal QR code.")
    parser.add_argument("--config-dir", default=None, help="Override config directory.")
    parser.add_argument("--storage-dir", default=None, help="Override storage directory.")
    parser.add_argument("--runtime-dir", default=None, help="Override runtime state directory.")
    return parser.parse_args(argv)


def _resolve_existing_state_path(args: argparse.Namespace) -> Path:
    primary_dir = resolve_runtime_dir(
        etc_config_dir=args.config_dir,
        runtime_dir=args.runtime_dir,
    )
    primary_path = primary_dir / SESSION_STATE_FILENAME
    if primary_path.is_file():
        return primary_path

    if (
        args.runtime_dir is None
        and args.config_dir is None
        and not os.environ.get("ALPINE_RECORDER_RUNTIME_DIR")
        and not os.environ.get("ALPINE_RECORDER_CONFIG_DIR")
    ):
        dev_path = Path.home() / ".alpine-recorder-dev" / "etc" / "runtime" / SESSION_STATE_FILENAME
        if dev_path.is_file():
            return dev_path

    return primary_path


def _determine_action(args: argparse.Namespace) -> Tuple[str, bool]:
    cmd = (args.command or "").lower()
    want_enable = bool(args.enable or cmd in ("enable", "start"))
    want_disable = bool(args.disable or cmd in ("disable", "stop"))
    want_rotate = bool(args.rotate or cmd in ("rotate", "new"))
    want_unban = bool(args.unban or cmd == "unban")

    if want_enable and want_disable:
        raise ValueError("Cannot specify both --enable and --disable together.")

    if want_unban:
        return ("unban", False)
    if want_enable:
        return ("enable", want_rotate)
    if want_disable:
        return ("disable", False)
    if want_rotate:
        return ("rotate", True)
    return ("status", False)


def _determine_single_field(args: argparse.Namespace) -> Optional[str]:
    cmd = (args.command or "").lower()
    if args.url or cmd in ("url", "link"):
        return "session_url"
    if args.login_url:
        return "login_url"
    if args.pin or cmd in ("pin", "passcode"):
        return "session_passcode"
    if args.key or cmd in ("key", "token"):
        return "invite_token"
    if args.id or cmd == "id":
        return "session_id"
    return None


def _print_human_session_report(
    state: Dict[str, Any],
    action: str,
    status_msg: str,
    host_override: Optional[str],
    force_qr: bool,
    suppress_qr: bool,
) -> None:
    running = bool(state.get("server_running"))
    port = int(state.get("port") or 8443)
    sid = str(state.get("session_id") or "")
    pin = str(state.get("session_passcode") or "")
    token = str(state.get("invite_token") or "")
    invite_url = str(state.get("session_url") or "")
    login_url = str(state.get("login_url") or "")
    project_name = str(state.get("project_name") or "Session")
    connected = int(state.get("connected_clients") or 0)
    hosts = [str(h) for h in (state.get("hosts") or []) if h]
    primary_host = str(state.get("primary_host") or "")
    ssl_mode = str(state.get("ssl_mode") or "")
    ssl_fp = str(state.get("ssl_fingerprint_sha256") or "")

    print("=============================================================================")
    print(" Alpine Recording Station - Remote Producer Web Session")
    print("=============================================================================")
    if action in ("enable", "disable", "rotate", "unban") and status_msg:
        print(f" Action:             {status_msg}")

    if running:
        print(f" Web Server:         ACTIVE (HTTPS listening on port {port})")
    else:
        print(f" Web Server:         DISABLED (configured port {port})")
    print(f" Project:            {project_name}")
    print(f" Connected Clients:  {connected}")
    banned_list = state.get("banned_ips") or []
    banned_count = int(state.get("banned_ip_count") if state.get("banned_ip_count") is not None else len(banned_list))
    if banned_count > 0:
        formatted = ", ".join(
            f"{entry.get('ip', '?')} ({int(entry.get('remaining_seconds') or entry.get('retry_after_sec') or 0)}s)"
            for entry in banned_list[:5]
            if isinstance(entry, dict)
        )
        suffix = f" — {formatted}" if formatted else ""
        print(f" Locked-Out IPs:     {banned_count}{suffix}")
    else:
        print(" Locked-Out IPs:     0")
    print(f" Session ID:         {sid}")
    print(f" Session PIN:        {pin}")
    print(f" Invite Token:       {token}")
    print(f" Invite URL:         {invite_url}")
    print(f" Manual Login URL:   {login_url}")

    alt_hosts = [h for h in hosts if h != primary_host]
    if alt_hosts and not host_override:
        alt_urls = [build_session_urls(h, port, sid, token)[1] for h in alt_hosts]
        print(f" Alternate Hosts:    {', '.join(alt_urls)}")

    if ssl_mode:
        fp_disp = f" ({ssl_fp[:23]}...)" if ssl_fp else ""
        print(f" SSL Certificate:    {ssl_mode}{fp_disp}")

    show_qr = (running and not suppress_qr) or force_qr
    if show_qr and invite_url:
        use_ansi = bool(
            getattr(sys.stdout, "isatty", lambda: False)()
            and not os.environ.get("NO_COLOR")
        )
        print("")
        print(" Scan QR Code to open & authenticate:")
        print(render_qr_terminal(invite_url, use_ansi=use_ansi))

    if not running and action != "disable":
        print("")
        print("Tip: Run 'alpine-recorder session enable' to start the HTTPS web interface immediately.")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_cli_args(argv)

    if args.port is not None and not (1024 <= int(args.port) <= 65535):
        print("[FAIL] --port must be between 1024 and 65535.", file=sys.stderr)
        return 2

    try:
        action, rotate_flag = _determine_action(args)
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    single_field = _determine_single_field(args)
    state_path = _resolve_existing_state_path(args)

    ok, msg, state = send_control_command(
        state_path=state_path,
        action=action,
        port=args.port,
        host=args.host,
        rotate=rotate_flag,
    )

    if state is not None:
        if not ok:
            if args.json:
                out = dict(state)
                out.update({
                    "ok": False,
                    "kiosk_running": True,
                    "action": action,
                    "error": msg,
                })
                print(json.dumps(out, indent=2, sort_keys=True))
            else:
                print(f"[FAIL] {msg}", file=sys.stderr)
            return 1

        if args.summary:
            if state.get("server_running"):
                print(
                    f"Web Interface: ACTIVE on {state.get('login_url')} "
                    f"(Session {state.get('session_id')}, "
                    f"{int(state.get('connected_clients') or 0)} connected — "
                    f"run 'alpine-recorder session' for PIN/URL/QR)"
                )
            else:
                print(
                    f"Web Interface: DISABLED "
                    f"(Session {state.get('session_id')} ready — "
                    f"run 'alpine-recorder session enable' to start)"
                )
            return 0

        if args.json:
            out = dict(state)
            out.update({
                "ok": True,
                "kiosk_running": True,
                "action": action,
                "message": msg,
            })
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0

        if single_field is not None:
            if not state.get("server_running"):
                print(
                    "[WARN] Remote Producer HTTPS server is currently disabled. "
                    "Run 'alpine-recorder session enable' to start it.",
                    file=sys.stderr,
                )
            print(str(state.get(single_field, "")))
            return 0

        if (args.command or "").lower() == "qr":
            if not state.get("server_running"):
                print(
                    "[WARN] Remote Producer HTTPS server is currently disabled. "
                    "Run 'alpine-recorder session enable' to start it.",
                    file=sys.stderr,
                )
            invite_url = str(state.get("session_url") or "")
            print(f"Invite URL: {invite_url}")
            use_ansi = bool(
                getattr(sys.stdout, "isatty", lambda: False)()
                and not os.environ.get("NO_COLOR")
            )
            print(render_qr_terminal(invite_url, use_ansi=use_ansi))
            return 0

        _print_human_session_report(
            state=state,
            action=action,
            status_msg=msg,
            host_override=args.host,
            force_qr=bool(args.qr),
            suppress_qr=bool(args.no_qr),
        )
        return 0

    # Kiosk workstation is not currently running
    if args.summary:
        return 1

    if action == "disable":
        cm = ConfigManager(
            storage_dir=args.storage_dir,
            etc_config_dir=args.config_dir,
        )
        cm.settings.remote_producer_enabled = False
        cm.save(commit_lbu=True)
        offline_msg = "Remote Producer HTTPS server disabled in saved settings (kiosk is not currently running)."
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "kiosk_running": False,
                        "action": "disable",
                        "remote_producer_enabled": False,
                        "port": int(cm.settings.remote_producer_port),
                        "message": offline_msg,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(offline_msg)
        return 0

    if action == "enable":
        cm = ConfigManager(
            storage_dir=args.storage_dir,
            etc_config_dir=args.config_dir,
        )
        cm.settings.remote_producer_enabled = True
        if args.port is not None:
            cm.settings.remote_producer_port = int(args.port)
        cm.save(commit_lbu=True)
        err_msg = (
            "Remote Producer is now enabled in saved settings, but Alpine Recording Station "
            "is not currently running. Run 'alpine-recorder start' to launch the workstation "
            "and generate a live session."
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "kiosk_running": False,
                        "action": "enable",
                        "remote_producer_enabled": True,
                        "port": int(cm.settings.remote_producer_port),
                        "error": err_msg,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"[INFO] {err_msg}", file=sys.stderr)
        return 1

    if "not currently running" in msg.lower():
        err_msg = f"{msg} Run 'alpine-recorder start' first."
    else:
        err_msg = f"{msg} Alpine Recording Station is not currently running. Run 'alpine-recorder start' first."

    if args.json:
        print(
            json.dumps(
                {
                    "ok": False,
                    "kiosk_running": False,
                    "action": action,
                    "error": err_msg,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"[FAIL] {err_msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
