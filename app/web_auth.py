"""Local Web UI user account management and standing HMAC authentication tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("AlpineRecorder.WebAuth")

WEB_USERS_FILENAME = "web_users.json"
PBKDF2_ITERATIONS = 260_000
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128
_USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{1,31}$")
_RESERVED_USERNAMES = frozenset({"root", "admin_system", "session", "guest", "anonymous"})


def validate_web_username(username: str) -> Tuple[bool, str]:
    clean = (username or "").strip()
    if not clean:
        return (False, "Username cannot be empty.")
    if not _USERNAME_RE.match(clean):
        return (
            False,
            "Username must be 2–32 characters, start with a letter, and contain only letters, digits, '.', '_', or '-'.",
        )
    if clean.lower() in _RESERVED_USERNAMES or clean.upper().startswith("ARS-"):
        return (False, f"Username '{clean}' is reserved.")
    return (True, clean)


def validate_web_password(password: str) -> Tuple[bool, str]:
    pw = str(password or "")
    if len(pw) < MIN_PASSWORD_LENGTH:
        return (False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if len(pw) > MAX_PASSWORD_LENGTH:
        return (False, f"Password must be {MAX_PASSWORD_LENGTH} characters or fewer.")
    return (True, "")


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with a random 16-byte salt."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    dk_b64 = base64.urlsafe_b64encode(dk).decode("ascii")
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt_b64}${dk_b64}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored PBKDF2-HMAC-SHA256 hash in constant time."""
    if not password or not stored_hash:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = base64.urlsafe_b64decode(parts[2].encode("ascii"))
        expected_dk = base64.urlsafe_b64decode(parts[3].encode("ascii"))
    except Exception:
        return False
    if iterations < 10_000 or not salt or not expected_dk:
        return False
    actual_dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(actual_dk, expected_dk)


@dataclass
class WebUserAccount:
    """A defined local login account with standing Web UI access."""

    username: str
    password_hash: str
    role: str = "admin"  # 'admin' = full standing project & script access
    token_version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WebUserManager:
    """Manages persistent local Web UI login accounts and standing HMAC tokens."""

    def __init__(
        self,
        etc_config_dir: Path | str,
        storage_dir: Path | str,
        on_persist_lbu: Optional[Callable[[], None]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.etc_path = Path(etc_config_dir) / WEB_USERS_FILENAME
        self.storage_path = Path(storage_dir) / ".alpine-recorder" / WEB_USERS_FILENAME
        self._on_persist_lbu = on_persist_lbu

        self._auth_secret: bytes = secrets.token_bytes(32)
        self._users: Dict[str, WebUserAccount] = {}  # keyed by username.lower()
        self._dummy_hash: str = hash_password(secrets.token_urlsafe(16))
        self.load()

    def load(self) -> None:
        """Load local Web UI accounts from the freshest persisted store."""
        with self._lock:
            candidates = [p for p in (self.storage_path, self.etc_path) if p.is_file()]
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for path in candidates:
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        continue
                    secret_hex = str(raw.get("auth_secret_hex") or "").strip()
                    if len(secret_hex) >= 64:
                        try:
                            self._auth_secret = bytes.fromhex(secret_hex)
                        except ValueError:
                            pass
                    loaded_users: Dict[str, WebUserAccount] = {}
                    raw_users = raw.get("users") or []
                    if isinstance(raw_users, list):
                        for item in raw_users:
                            if not isinstance(item, dict):
                                continue
                            uname = str(item.get("username") or "").strip()
                            phash = str(item.get("password_hash") or "").strip()
                            if not uname or not phash:
                                continue
                            acct = WebUserAccount(
                                username=uname,
                                password_hash=phash,
                                role=str(item.get("role") or "admin"),
                                token_version=max(1, int(item.get("token_version") or 1)),
                                created_at=float(item.get("created_at") or 0.0),
                                updated_at=float(item.get("updated_at") or 0.0),
                            )
                            loaded_users[uname.lower()] = acct
                    self._users = loaded_users
                    return
                except Exception as exc:
                    logger.warning("Failed to load web users from %s: %s", path, exc)

    def _save_locked(self, commit_lbu: bool = True) -> None:
        payload = json.dumps(
            {
                "version": 1,
                "auth_secret_hex": self._auth_secret.hex(),
                "users": [asdict(u) for u in sorted(self._users.values(), key=lambda x: x.username.lower())],
            },
            indent=2,
            sort_keys=True,
        )
        for path in (self.storage_path, self.etc_path):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(payload, encoding="utf-8")
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                tmp.replace(path)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            except OSError as exc:
                logger.warning("Failed to save web users to %s: %s", path, exc)

        if commit_lbu and self._on_persist_lbu is not None:
            try:
                self._on_persist_lbu()
            except Exception:
                pass

    def has_users(self) -> bool:
        with self._lock:
            return len(self._users) > 0

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                u.to_public_dict()
                for u in sorted(self._users.values(), key=lambda x: x.username.lower())
            ]

    def get_user(self, username: str) -> Optional[WebUserAccount]:
        key = (username or "").strip().lower()
        if not key:
            return None
        with self._lock:
            return self._users.get(key)

    def add_user(
        self, username: str, password: str, role: str = "admin"
    ) -> Tuple[bool, str]:
        ok_u, res_u = validate_web_username(username)
        if not ok_u:
            return (False, res_u)
        ok_p, err_p = validate_web_password(password)
        if not ok_p:
            return (False, err_p)
        clean_role = "admin" if str(role or "admin").strip().lower() != "producer" else "producer"
        now = time.time()
        with self._lock:
            key = res_u.lower()
            if key in self._users:
                return (False, f"Local Web UI user '{res_u}' already exists.")
            self._users[key] = WebUserAccount(
                username=res_u,
                password_hash=hash_password(password),
                role=clean_role,
                token_version=1,
                created_at=now,
                updated_at=now,
            )
            self._save_locked(commit_lbu=True)
        return (True, f"Added local Web UI login '{res_u}'.")

    def change_password(self, username: str, new_password: str) -> Tuple[bool, str]:
        ok_p, err_p = validate_web_password(new_password)
        if not ok_p:
            return (False, err_p)
        key = (username or "").strip().lower()
        with self._lock:
            acct = self._users.get(key)
            if acct is None:
                return (False, f"User '{username}' not found.")
            acct.password_hash = hash_password(new_password)
            acct.token_version += 1
            acct.updated_at = time.time()
            self._save_locked(commit_lbu=True)
            uname = acct.username
        return (True, f"Updated password for '{uname}' (previous sessions revoked).")

    def remove_user(self, username: str) -> Tuple[bool, str]:
        key = (username or "").strip().lower()
        with self._lock:
            acct = self._users.pop(key, None)
            if acct is None:
                return (False, f"User '{username}' not found.")
            self._save_locked(commit_lbu=True)
            uname = acct.username
        return (True, f"Removed local Web UI login '{uname}'.")

    def verify_credentials(self, username: str, password: str) -> Optional[WebUserAccount]:
        """Verify username & password with constant-time comparison even for unknown users."""
        key = (username or "").strip().lower()
        pw = str(password or "")
        with self._lock:
            acct = self._users.get(key)
            target_hash = acct.password_hash if acct is not None else self._dummy_hash
        if not verify_password(pw, target_hash):
            return None
        return acct

    def create_auth_token(
        self, user: WebUserAccount, ttl_seconds: int = 86400 * 7
    ) -> str:
        """Create an HMAC-signed standing token for a local user account."""
        exp = int(time.time()) + max(300, int(ttl_seconds))
        nonce = secrets.token_hex(8)
        payload = f"LOCAL:{user.username}:{user.token_version}:{exp}:{nonce}"
        with self._lock:
            secret = self._auth_secret
        sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def verify_auth_token(self, token: str) -> Optional[WebUserAccount]:
        """Verify a standing local user token and confirm user & token_version are still valid."""
        if not token or not token.startswith("LOCAL:"):
            return None
        parts = token.split(":")
        if len(parts) != 6:
            return None
        prefix, uname, ver_str, exp_str, nonce, provided_sig = parts
        if prefix != "LOCAL" or not uname or not nonce:
            return None
        try:
            ver = int(ver_str)
            exp = int(exp_str)
        except ValueError:
            return None
        if time.time() > exp:
            return None
        payload = f"{prefix}:{uname}:{ver_str}:{exp_str}:{nonce}"
        with self._lock:
            expected_sig = hmac.new(
                self._auth_secret, payload.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(provided_sig, expected_sig):
                return None
            acct = self._users.get(uname.lower())
            if acct is None or acct.token_version != ver:
                return None
            return acct


# ---------------------------------------------------------------------------
# Headless CLI: alpine-recorder user [list|add|passwd|remove]
# ---------------------------------------------------------------------------

def _notify_live_server_reload(
    config_dir: Optional[str],
    runtime_dir: Optional[str],
) -> bool:
    """Ask a running RemoteProducerServer (if any) to reload web_users.json live."""
    try:
        from app.session_cli import (
            SESSION_STATE_FILENAME,
            resolve_runtime_dir,
            send_control_command,
        )

        rt_dir = resolve_runtime_dir(
            etc_config_dir=config_dir,
            runtime_dir=runtime_dir,
        )
        state_path = rt_dir / SESSION_STATE_FILENAME
        if not state_path.is_file():
            return False
        ok, _msg, _state = send_control_command(
            state_path=state_path,
            action="reload_users",
            timeout=1.5,
        )
        return bool(ok)
    except Exception:
        return False


def _read_cli_password(args: Any, prompt_label: str = "Password") -> Tuple[bool, str]:
    import getpass
    import sys

    if getattr(args, "password", None) is not None:
        return (True, str(args.password))
    if getattr(args, "password_stdin", False):
        line = sys.stdin.readline()
        return (True, line.rstrip("\r\n"))
    pw1 = getpass.getpass(f"{prompt_label}: ")
    pw2 = getpass.getpass(f"Confirm {prompt_label}: ")
    if pw1 != pw2:
        return (False, "Passwords do not match.")
    return (True, pw1)


def run_user_cli(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint for managing defined local Web UI logins."""
    import argparse
    import sys

    from app.config_manager import (
        DEFAULT_ETC_CONFIG_DIR,
        DEFAULT_STORAGE_DIR,
        ConfigManager,
    )

    parser = argparse.ArgumentParser(
        prog="alpine-recorder user",
        description="Manage local Web UI login accounts with standing project & script access.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "ls", "add", "passwd", "password", "chpasswd", "remove", "rm", "delete", "del"],
        help="Action to perform: list, add, passwd, or remove.",
    )
    parser.add_argument(
        "username",
        nargs="?",
        default=None,
        help="Local Web UI username (required for add, passwd, remove).",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Account password (if omitted, prompts interactively without echoing).",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read password from a single line of stdin.",
    )
    parser.add_argument(
        "--role",
        default="admin",
        choices=["admin", "producer"],
        help="Account role (default: admin).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON.",
    )
    parser.add_argument("--config-dir", default=None, help="Override config directory.")
    parser.add_argument("--storage-dir", default=None, help="Override storage directory.")
    parser.add_argument("--runtime-dir", default=None, help="Override runtime state directory.")

    args = parser.parse_args(argv)
    etc_dir = args.config_dir or os.environ.get("ALPINE_RECORDER_CONFIG_DIR") or DEFAULT_ETC_CONFIG_DIR
    storage_dir = args.storage_dir or os.environ.get("ALPINE_RECORDER_STORAGE") or DEFAULT_STORAGE_DIR

    mgr = WebUserManager(
        etc_config_dir=etc_dir,
        storage_dir=storage_dir,
        on_persist_lbu=ConfigManager.trigger_lbu_commit_async,
    )

    act = args.action.lower()

    if act in ("list", "ls"):
        users = mgr.list_users()
        if args.json:
            print(json.dumps({"ok": True, "users": users}, indent=2, sort_keys=True))
        elif not users:
            print("No local Web UI accounts defined.")
            print("Add one with: alpine-recorder user add <username>")
        else:
            print("Defined Local Web UI Accounts:")
            for u in users:
                print(f"  - {u['username']}  (role: {u.get('role', 'admin')})")
        return 0

    if not args.username:
        msg = f"Username is required for '{act}'. Usage: alpine-recorder user {act} <username>"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            print(f"[FAIL] {msg}", file=sys.stderr)
        return 2

    if act == "add":
        ok_pw, pw_or_err = _read_cli_password(args, "Password")
        if not ok_pw:
            if args.json:
                print(json.dumps({"ok": False, "error": pw_or_err}, indent=2))
            else:
                print(f"[FAIL] {pw_or_err}", file=sys.stderr)
            return 1
        ok, msg = mgr.add_user(args.username, pw_or_err, role=args.role)
    elif act in ("passwd", "password", "chpasswd"):
        ok_pw, pw_or_err = _read_cli_password(args, "New Password")
        if not ok_pw:
            if args.json:
                print(json.dumps({"ok": False, "error": pw_or_err}, indent=2))
            else:
                print(f"[FAIL] {pw_or_err}", file=sys.stderr)
            return 1
        ok, msg = mgr.change_password(args.username, pw_or_err)
    else:
        ok, msg = mgr.remove_user(args.username)

    reloaded = _notify_live_server_reload(args.config_dir, args.runtime_dir) if ok else False

    if args.json:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "action": act,
                    "username": args.username,
                    "message": msg,
                    "live_reloaded": reloaded,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        if ok:
            suffix = " (live server updated)" if reloaded else ""
            print(f"[OK] {msg}{suffix}")
        else:
            print(f"[FAIL] {msg}", file=sys.stderr)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run_user_cli())

