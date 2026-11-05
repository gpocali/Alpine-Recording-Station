"""Samba share configuration and user administration (add, remove, change password) with LBU persistence."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

from app.config_manager import ConfigManager

logger = logging.getLogger(__name__)

RESERVED_USERNAMES = {
    "root",
    "bin",
    "daemon",
    "adm",
    "lp",
    "sync",
    "shutdown",
    "halt",
    "mail",
    "news",
    "uucp",
    "operator",
    "man",
    "postmaster",
    "cron",
    "ftp",
    "sshd",
    "at",
    "squid",
    "xfs",
    "games",
    "cyrus",
    "vpopmail",
    "ntp",
    "smmsp",
    "guest",
    "nobody",
}

USERNAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,30}$")


class SambaManager:
    """Administrates Samba share users and passwords on Alpine Linux."""

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager
        self.users_registry_path = (
            self.config_manager.etc_config_dir / "samba_users.json"
        )
        self.pdbedit_bin = shutil.which("pdbedit")
        self.smbpasswd_bin = shutil.which("smbpasswd")
        self.adduser_bin = shutil.which("adduser")
        self.deluser_bin = shutil.which("deluser")

    @property
    def is_system_samba_available(self) -> bool:
        return bool(self.pdbedit_bin and self.smbpasswd_bin)

    @staticmethod
    def validate_username(username: str) -> Tuple[bool, str]:
        """Validate a Samba username for safety and POSIX compliance."""
        uname = username.strip()
        if not uname:
            return False, "Username cannot be empty."
        if uname.lower() in RESERVED_USERNAMES:
            return False, f"'{uname}' is a reserved system account name."
        if not USERNAME_RE.match(uname):
            return (
                False,
                "Username must start with a letter or underscore and contain only letters, numbers, underscores, or hyphens (max 31 chars).",
            )
        return True, ""

    @staticmethod
    def validate_password(password: str) -> Tuple[bool, str]:
        if not password or len(password) < 4:
            return False, "Password must be at least 4 characters long."
        if "\n" in password or "\r" in password:
            return False, "Password cannot contain newline characters."
        return True, ""

    def list_users(self) -> List[str]:
        """Return sorted list of configured Samba usernames."""
        if self.is_system_samba_available and self.pdbedit_bin:
            try:
                res = subprocess.run(
                    [self.pdbedit_bin, "-L"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if res.returncode == 0:
                    users: List[str] = []
                    for line in res.stdout.splitlines():
                        if ":" in line:
                            uname = line.split(":", 1)[0].strip()
                            if uname and uname.lower() not in RESERVED_USERNAMES:
                                users.append(uname)
                    return sorted(set(users))
            except Exception as exc:
                logger.warning("Failed to list users via pdbedit: %s", exc)

        # Fallback to local registry (used in dev/test environments without Samba)
        return sorted(self._load_fallback_registry())

    def add_user(self, username: str, password: str) -> Tuple[bool, str]:
        """Create a Samba user (and underlying Alpine system account if needed)."""
        uname = username.strip()
        valid_u, msg_u = self.validate_username(uname)
        if not valid_u:
            return False, msg_u

        valid_p, msg_p = self.validate_password(password)
        if not valid_p:
            return False, msg_p

        if uname in self.list_users():
            return False, f"User '{uname}' already exists. Use 'Change Password' instead."

        if self.is_system_samba_available and self.smbpasswd_bin:
            try:
                # 1. Ensure group sambashare exists
                addgroup_bin = shutil.which("addgroup")
                if addgroup_bin:
                    subprocess.run(
                        [addgroup_bin, "-S", "sambashare"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )

                # 2. Ensure Linux system account exists (no login shell, no home dir)
                if self.adduser_bin:
                    subprocess.run(
                        [
                            self.adduser_bin,
                            "-D",
                            "-H",
                            "-G",
                            "sambashare",
                            "-s",
                            "/sbin/nologin",
                            uname,
                        ],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                if addgroup_bin:
                    subprocess.run(
                        [addgroup_bin, uname, "sambashare"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )

                # 3. Add user to Samba passdb non-interactively
                proc = subprocess.run(
                    [self.smbpasswd_bin, "-a", "-s", uname],
                    input=f"{password}\n{password}\n",
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout).strip()
                    return False, f"smbpasswd failed: {err or 'Unknown error'}"

                # 4. Enable Samba account
                subprocess.run(
                    [self.smbpasswd_bin, "-e", uname],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

                self._save_to_fallback_registry_add(uname)
                self.config_manager.trigger_lbu_commit_async()
                return True, f"Samba user '{uname}' added and saved to LBU."
            except Exception as exc:
                logger.exception("Error adding Samba user %s", uname)
                return False, f"Error adding user: {exc}"

        # Fallback dev mode
        self._save_to_fallback_registry_add(uname)
        return True, f"Samba user '{uname}' added (simulated mode)."

    def change_password(self, username: str, new_password: str) -> Tuple[bool, str]:
        """Change the password for an existing Samba user."""
        uname = username.strip()
        valid_p, msg_p = self.validate_password(new_password)
        if not valid_p:
            return False, msg_p

        if uname not in self.list_users():
            return False, f"User '{uname}' does not exist."

        if self.is_system_samba_available and self.smbpasswd_bin:
            try:
                proc = subprocess.run(
                    [self.smbpasswd_bin, "-s", uname],
                    input=f"{new_password}\n{new_password}\n",
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout).strip()
                    return False, f"Failed to change password: {err}"

                self.config_manager.trigger_lbu_commit_async()
                return True, f"Password updated for '{uname}' and saved to LBU."
            except Exception as exc:
                return False, f"Error updating password: {exc}"

        return True, f"Password updated for '{uname}' (simulated mode)."

    def remove_user(self, username: str) -> Tuple[bool, str]:
        """Remove a user from Samba share access."""
        uname = username.strip()
        if not uname:
            return False, "No user selected."
        if uname.lower() in RESERVED_USERNAMES:
            return False, f"Cannot remove system account '{uname}'."

        if self.is_system_samba_available and self.smbpasswd_bin:
            try:
                proc = subprocess.run(
                    [self.smbpasswd_bin, "-x", uname],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout).strip()
                    return False, f"Failed to remove Samba user: {err}"

                if self.deluser_bin:
                    subprocess.run(
                        [self.deluser_bin, uname],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )

                self._save_to_fallback_registry_remove(uname)
                self.config_manager.trigger_lbu_commit_async()
                return True, f"Samba user '{uname}' removed and committed to LBU."
            except Exception as exc:
                return False, f"Error removing user: {exc}"

        self._save_to_fallback_registry_remove(uname)
        return True, f"Samba user '{uname}' removed (simulated mode)."

    def _load_fallback_registry(self) -> List[str]:
        if not self.users_registry_path.is_file():
            return []
        try:
            data = json.loads(self.users_registry_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:
            pass
        return []

    def _save_to_fallback_registry_add(self, username: str) -> None:
        users = set(self._load_fallback_registry())
        users.add(username)
        self._write_fallback_registry(sorted(users))

    def _save_to_fallback_registry_remove(self, username: str) -> None:
        users = set(self._load_fallback_registry())
        users.discard(username)
        self._write_fallback_registry(sorted(users))

    def _write_fallback_registry(self, users: List[str]) -> None:
        try:
            self.users_registry_path.parent.mkdir(parents=True, exist_ok=True)
            self.users_registry_path.write_text(
                json.dumps(users, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
