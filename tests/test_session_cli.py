"""Pure-stdlib unit tests for Remote Producer CLI session retrieval and live control."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Tuple

from app.config_manager import ConfigManager
from app.session_cli import (
    SESSION_STATE_FILENAME,
    SessionControlServer,
    build_session_urls,
    main as session_cli_main,
    read_session_state_file,
    render_qr_terminal,
    resolve_runtime_dir,
    write_session_state_file,
)


class _FakeProducerBackend:
    """In-memory RemoteProducerServer stand-in for stdlib-only IPC/CLI tests."""

    def __init__(self, runtime_dir: Path) -> None:
        self.server_running = False
        self.remote_producer_enabled = False
        self.port = 8443
        self.hosts = ["192.168.1.50", "10.0.0.12"]
        self.session_id = "ARS-1A2B"
        self.session_passcode = "482910"
        self.invite_token = "tok_initial_12345"
        self.project_name = "StudioDemo"
        self.connected_clients = 2
        self._rotate_seq = 0
        self.banned_ips = [{"ip": "203.0.113.99", "remaining_seconds": 300, "ban_count": 1}]
        self.control_server = SessionControlServer(
            runtime_dir=runtime_dir,
            state_provider=self.get_state,
            action_handler=self.handle_action,
        )

    def get_state(self, host_override: str | None = None) -> Dict[str, Any]:
        primary = host_override or self.hosts[0]
        session_url, login_url = build_session_urls(
            primary, self.port, self.session_id, self.invite_token
        )
        return {
            "pid": os.getpid(),
            "updated_at": time.time(),
            "session_created_at": time.time() - 10.0,
            "server_running": self.server_running,
            "remote_producer_enabled": self.remote_producer_enabled,
            "port": self.port,
            "hosts": list(self.hosts),
            "primary_host": primary,
            "session_id": self.session_id,
            "session_passcode": self.session_passcode,
            "invite_token": self.invite_token,
            "session_url": session_url,
            "login_url": login_url,
            "project_name": self.project_name,
            "connected_clients": self.connected_clients,
            "talent_mic_live": True,
            "ssl_mode": "self-signed",
            "ssl_fingerprint_sha256": "AB:CD:EF:12:34:56:78:90:AA:BB:CC:DD",
            "banned_ip_count": len(self.banned_ips),
            "banned_ips": list(self.banned_ips),
        }

    def handle_action(
        self, action: str, req: Dict[str, Any]
    ) -> Tuple[bool, str, Dict[str, Any]]:
        if action == "status":
            return (True, "Active studio session retrieved.", self.get_state())
        if action == "rotate":
            self._rotate_seq += 1
            self.session_id = f"ARS-ROT{self._rotate_seq}"
            self.session_passcode = f"{700000 + self._rotate_seq:06d}"
            self.invite_token = f"tok_rotated_{self._rotate_seq}"
            return (True, f"Session rotated to {self.session_id}.", self.get_state())
        if action == "enable":
            if req.get("port") is not None:
                self.port = int(req["port"])
            if req.get("rotate"):
                self._rotate_seq += 1
                self.session_id = f"ARS-ROT{self._rotate_seq}"
                self.session_passcode = f"{700000 + self._rotate_seq:06d}"
                self.invite_token = f"tok_rotated_{self._rotate_seq}"
            self.server_running = True
            self.remote_producer_enabled = True
            return (
                True,
                f"Remote Producer HTTPS server active on port {self.port}.",
                self.get_state(),
            )
        if action == "disable":
            self.server_running = False
            self.remote_producer_enabled = False
            return (True, "Remote Producer HTTPS server stopped.", self.get_state())
        if action == "unban":
            cleared = len(self.banned_ips)
            self.banned_ips = []
            return (True, f"Cleared {cleared} Fail2Ban IP lockout(s).", self.get_state())
        return (False, f"Unsupported action: {action}", self.get_state())


def _run_cli(argv: list[str]) -> Tuple[int, str, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = session_cli_main(argv)
    return (rc, out_buf.getvalue(), err_buf.getvalue())


class TestSessionCli(unittest.TestCase):
    def test_resolve_runtime_dir_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            explicit = base / "explicit_rt"
            env_rt = base / "env_rt"
            cfg_dir = base / "custom_etc"

            old_env = os.environ.get("ALPINE_RECORDER_RUNTIME_DIR")
            try:
                os.environ["ALPINE_RECORDER_RUNTIME_DIR"] = str(env_rt)
                self.assertEqual(
                    resolve_runtime_dir(etc_config_dir=cfg_dir, runtime_dir=explicit),
                    explicit,
                )
                self.assertEqual(resolve_runtime_dir(etc_config_dir=cfg_dir), env_rt)

                del os.environ["ALPINE_RECORDER_RUNTIME_DIR"]
                self.assertEqual(
                    resolve_runtime_dir(etc_config_dir=cfg_dir),
                    cfg_dir / "runtime",
                )
            finally:
                if old_env is None:
                    os.environ.pop("ALPINE_RECORDER_RUNTIME_DIR", None)
                else:
                    os.environ["ALPINE_RECORDER_RUNTIME_DIR"] = old_env

    def test_terminal_qr_renderer_unicode_and_ansi(self) -> None:
        url = "https://192.168.1.50:8443/?session=ARS-1A2B&token=tok_initial_12345"
        plain = render_qr_terminal(url, border=2, use_ansi=False)
        self.assertNotIn("\033[", plain)
        lines_plain = plain.splitlines()
        self.assertGreaterEqual(len(lines_plain), 12)
        self.assertTrue(any(ch in plain for ch in ("█", "▀", "▄")))

        ansi = render_qr_terminal(url, border=2, use_ansi=True)
        for line in ansi.splitlines():
            self.assertTrue(line.startswith("\033[30;47m"))
            self.assertTrue(line.endswith("\033[0m"))

    def test_live_control_server_cli_queries_actions_and_security(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rt_dir = base / "runtime"
            cfg_dir = base / "etc"
            stg_dir = base / "storage"
            backend = _FakeProducerBackend(rt_dir)
            self.assertTrue(backend.control_server.start(persistent=True))
            try:
                state_path = rt_dir / SESSION_STATE_FILENAME
                self.assertTrue(state_path.is_file())
                raw = read_session_state_file(state_path)
                self.assertIsNotNone(raw)
                assert raw is not None
                self.assertIn("control_secret", raw)
                self.assertEqual(raw["control_host"], "127.0.0.1")
                self.assertGreater(int(raw["control_port"]), 0)

                # 1. Unauthorized control socket request is rejected
                with socket.create_connection(("127.0.0.1", backend.control_server.port), timeout=2.0) as sock:
                    sock.sendall((json.dumps({"action": "status", "secret": "wrong-secret"}) + "\n").encode("utf-8"))
                    reply = json.loads(sock.recv(4096).decode("utf-8").splitlines()[0])
                self.assertFalse(reply["ok"])
                self.assertIn("Unauthorized", reply["error"])

                # 2. Summary while disabled does not leak PIN or invite token
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "--summary"])
                self.assertEqual(rc, 0, err)
                self.assertIn("DISABLED", out)
                self.assertIn("ARS-1A2B", out)
                self.assertNotIn("482910", out)
                self.assertNotIn("tok_initial_12345", out)

                # 3. Single-field --url while disabled warns on stderr and prints URL
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "--url"])
                self.assertEqual(rc, 0)
                self.assertIn("currently disabled", err)
                self.assertEqual(
                    out.strip(),
                    "https://192.168.1.50:8443/?session=ARS-1A2B&token=tok_initial_12345",
                )

                # 4. Live enable on custom port 8444
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "enable", "--port", "8444", "--no-qr"])
                self.assertEqual(rc, 0, err)
                self.assertTrue(backend.server_running)
                self.assertEqual(backend.port, 8444)
                self.assertIn("ACTIVE (HTTPS listening on port 8444)", out)
                self.assertIn("482910", out)
                self.assertNotIn("█", out)

                # 5. Human output with server active includes terminal QR by default
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "status"])
                self.assertEqual(rc, 0, err)
                self.assertIn("ARS-1A2B", out)
                self.assertIn("482910", out)
                self.assertIn("tok_initial_12345", out)
                self.assertIn("Locked-Out IPs:     1 — 203.0.113.99 (300s)", out)
                self.assertTrue(any(ch in out for ch in ("█", "▀", "▄")))

                # 6. Summary while active shows URL + session ID, no PIN/token
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "--summary"])
                self.assertEqual(rc, 0, err)
                self.assertIn("ACTIVE on https://192.168.1.50:8444/", out)
                self.assertNotIn("482910", out)
                self.assertNotIn("tok_initial_12345", out)

                # 7. --json strips control_* fields
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "--json"])
                self.assertEqual(rc, 0, err)
                parsed = json.loads(out)
                self.assertTrue(parsed["ok"])
                self.assertTrue(parsed["kiosk_running"])
                self.assertTrue(parsed["server_running"])
                self.assertEqual(parsed["session_id"], "ARS-1A2B")
                self.assertEqual(parsed["session_passcode"], "482910")
                self.assertEqual(parsed["invite_token"], "tok_initial_12345")
                self.assertNotIn("control_secret", parsed)
                self.assertNotIn("control_host", parsed)
                self.assertNotIn("control_port", parsed)

                # 8. Single-field flags & positional commands + --host overrides (IPv4 and IPv6)
                for argv, expected in (
                    (["--runtime-dir", str(rt_dir), "--id"], "ARS-1A2B"),
                    (["--runtime-dir", str(rt_dir), "id"], "ARS-1A2B"),
                    (["--runtime-dir", str(rt_dir), "--pin"], "482910"),
                    (["--runtime-dir", str(rt_dir), "pin"], "482910"),
                    (["--runtime-dir", str(rt_dir), "--key"], "tok_initial_12345"),
                    (["--runtime-dir", str(rt_dir), "key"], "tok_initial_12345"),
                    (["--runtime-dir", str(rt_dir), "--login-url", "--host", "10.20.30.40"], "https://10.20.30.40:8444/"),
                    (["--runtime-dir", str(rt_dir), "url", "--host", "::1"], "https://[::1]:8444/?session=ARS-1A2B&token=tok_initial_12345"),
                ):
                    rc, out, err = _run_cli(argv)
                    self.assertEqual(rc, 0, f"{argv}: {err}")
                    self.assertEqual(err, "")
                    self.assertEqual(out.strip(), expected)

                # 9. qr subcommand
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "qr"])
                self.assertEqual(rc, 0, err)
                self.assertIn("Invite URL: https://192.168.1.50:8444/", out)
                self.assertTrue(any(ch in out for ch in ("█", "▀", "▄")))

                # 10. Rotate session live
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "rotate", "--json"])
                self.assertEqual(rc, 0, err)
                rot_data = json.loads(out)
                self.assertEqual(rot_data["session_id"], "ARS-ROT1")
                self.assertEqual(rot_data["session_passcode"], "700001")
                self.assertEqual(rot_data["invite_token"], "tok_rotated_1")
                self.assertEqual(rot_data["banned_ip_count"], 1)

                # 10b. Unban locked-out IPs live
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "unban", "--json"])
                self.assertEqual(rc, 0, err)
                unban_data = json.loads(out)
                self.assertTrue(unban_data["ok"])
                self.assertEqual(unban_data["action"], "unban")
                self.assertEqual(unban_data["banned_ip_count"], 0)
                self.assertEqual(len(backend.banned_ips), 0)

                # 11. Disable server live
                rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "disable", "--json"])
                self.assertEqual(rc, 0, err)
                dis_data = json.loads(out)
                self.assertTrue(dis_data["ok"])
                self.assertFalse(dis_data["server_running"])
                self.assertFalse(backend.server_running)
                self.assertTrue(state_path.is_file())
            finally:
                backend.control_server.stop()

            self.assertFalse((rt_dir / SESSION_STATE_FILENAME).exists())

            # 12. Conflicting --enable --disable returns exit code 2
            rc, _out, err = _run_cli(
                [
                    "--runtime-dir", str(rt_dir),
                    "--config-dir", str(cfg_dir),
                    "--storage-dir", str(stg_dir),
                    "--enable", "--disable",
                ]
            )
            self.assertEqual(rc, 2)
            self.assertIn("Cannot specify both", err)

    def test_stale_pid_cleanup_and_offline_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rt_dir = base / "runtime"
            cfg_dir = base / "etc"
            stg_dir = base / "storage"
            state_path = rt_dir / SESSION_STATE_FILENAME

            # Spawn and wait for a short-lived Python subprocess to guarantee a dead PID
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            proc.wait(timeout=5)
            dead_pid = int(proc.pid)

            write_session_state_file(
                state_path,
                {
                    "pid": dead_pid,
                    "control_host": "127.0.0.1",
                    "control_port": 1,
                    "control_secret": "stale-secret",
                    "session_id": "ARS-DEAD",
                },
            )
            self.assertTrue(state_path.is_file())

            rc, out, err = _run_cli(["--runtime-dir", str(rt_dir), "status"])
            self.assertEqual(rc, 1)
            self.assertIn("not currently running", err)
            self.assertFalse(state_path.exists())

            # Offline enable persists settings and reports exit code 1 (kiosk not running)
            rc, out, err = _run_cli(
                [
                    "--runtime-dir", str(rt_dir),
                    "--config-dir", str(cfg_dir),
                    "--storage-dir", str(stg_dir),
                    "enable",
                    "--port", "9443",
                    "--json",
                ]
            )
            self.assertEqual(rc, 1)
            en_payload = json.loads(out)
            self.assertFalse(en_payload["ok"])
            self.assertFalse(en_payload["kiosk_running"])
            self.assertTrue(en_payload["remote_producer_enabled"])
            self.assertEqual(en_payload["port"], 9443)

            cm_verify = ConfigManager(storage_dir=str(stg_dir), etc_config_dir=str(cfg_dir))
            self.assertTrue(cm_verify.settings.remote_producer_enabled)
            self.assertEqual(cm_verify.settings.remote_producer_port, 9443)

            # Offline disable persists settings and exits 0
            rc, out, err = _run_cli(
                [
                    "--runtime-dir", str(rt_dir),
                    "--config-dir", str(cfg_dir),
                    "--storage-dir", str(stg_dir),
                    "disable",
                    "--json",
                ]
            )
            self.assertEqual(rc, 0, err)
            dis_payload = json.loads(out)
            self.assertTrue(dis_payload["ok"])
            self.assertFalse(dis_payload["kiosk_running"])
            self.assertFalse(dis_payload["remote_producer_enabled"])

            cm_verify2 = ConfigManager(storage_dir=str(stg_dir), etc_config_dir=str(cfg_dir))
            self.assertFalse(cm_verify2.settings.remote_producer_enabled)

    def test_scripts_lf_endings_and_session_cli_wiring(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        for rel in (
            "install.sh",
            "config/alpine-recorder",
            "config/alpine-recorder.initd",
            "config/xinitrc",
        ):
            raw = (repo_root / rel).read_bytes()
            self.assertNotIn(b"\r\n", raw, f"CRLF line endings detected in {rel}")

        install_text = (repo_root / "install.sh").read_text(encoding="utf-8")
        self.assertIn("app/session_cli.py", install_text)
        self.assertIn('lbu include "${APP_DEST}/app/session_cli.py"', install_text)
        self.assertIn('lbu exclude "${ETC_CONFIG_DIR}/runtime"', install_text)

        launcher_text = (repo_root / "config" / "alpine-recorder").read_text(encoding="utf-8")
        self.assertIn("session|web-session|session-key)", launcher_text)
        self.assertIn("-m app.session_cli", launcher_text)
        self.assertIn("--summary", launcher_text)

        initd_text = (repo_root / "config" / "alpine-recorder.initd").read_text(encoding="utf-8")
        self.assertIn("web_session.json", initd_text)


if __name__ == "__main__":
    unittest.main()
