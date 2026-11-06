"""Configuration persistence, LBU synchronization, and recording file management."""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import posixpath
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_DIR = "/storage"
DEFAULT_ETC_CONFIG_DIR = "/etc/alpine-recorder"

STORAGE_WARN_USED_PERCENT: float = 80.0
STORAGE_CRITICAL_USED_PERCENT: float = 90.0
STORAGE_WARN_REMAINING_SEC: float = 1800.0      # 30 minutes
STORAGE_CRITICAL_REMAINING_SEC: float = 600.0   # 10 minutes

TALKBACK_TONE_FREQ_OPTIONS: tuple[tuple[float, str], ...] = (
    (330.0, "330 Hz (E4 — Warm Low, Very Easy on Ear)"),
    (440.0, "440 Hz (A4 Concert Pitch — Easy on Ear)"),
    (528.0, "528 Hz (C5 — Soft Musical Chime)"),
    (660.0, "660 Hz (E5 — Gentle Mid Marker)"),
    (800.0, "800 Hz (Soft Studio Marker)"),
    (1000.0, "1000 Hz / 1 kHz (Standard Broadcast Reference)"),
    (1200.0, "1200 Hz / 1.2 kHz (Bright Marker)"),
)

TALKBACK_TONE_DURATION_OPTIONS: tuple[float, ...] = (
    0.0,
    0.25,
    0.5,
    0.75,
    1.0,
    1.5,
    2.0,
    3.0,
)


def sanitize_project_name(name: str) -> str:
    """Sanitize a project name so it is safe for filenames on Linux and Windows/Samba.

    Replaces characters invalid in SMB/Windows/POSIX filenames with underscores,
    and strips leading/trailing whitespace. Defaults to 'Project' if empty.
    """
    cleaned = name.strip()
    # Replace characters not allowed in Windows/SMB or Linux filenames
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
    # Collapse consecutive spaces into a single underscore or keep clean characters
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned if cleaned else "Project"


def generate_recording_filename(
    project_name: str, dt: Optional[datetime.datetime] = None
) -> str:
    """Generate a WAV filename with format: <ProjectName>_yyyy-mm-dd_HH.MM.SS_TZ.wav."""
    prefix = sanitize_project_name(project_name)
    if dt is None:
        dt = datetime.datetime.now().astimezone()
    elif dt.tzinfo is None:
        dt = dt.astimezone()

    tz_name = dt.strftime("%Z") or "LOCAL"
    # Clean TZ name for Windows/Samba safety (e.g. 'UTC+00:00' -> 'UTC+0000')
    tz_name = re.sub(r'[<>:"/\\|?*\s]', "", tz_name) or "LOCAL"
    timestamp = dt.strftime("%Y-%m-%d_%H.%M.%S")
    return f"{prefix}_{timestamp}_{tz_name}.wav"


@dataclass
class RecorderSettings:
    """Persistent settings for the Alpine Recording Station."""

    input_device: str = ""  # ALSA device id e.g. 'hw:1,0' or '' for auto-select USB
    output_device: str = "same"  # 'same' (use input USB device) or specific ALSA id
    sample_rate: int = 0  # 0 means auto-select maximum hardware sample rate
    bit_depth: int = 0  # 0 means auto-select maximum hardware bit depth
    channels: int = 1  # 1 = Mono, 2 = Stereo (in Normal Mode)
    input_channel: int = 0  # 0-indexed input channel on multi-channel interface
    studio_mode: bool = False  # True = Studio Mode (Stereo output, L=Mic, R=Talkback+Tone)
    project_name: str = "Session"
    on_screen_keyboard: bool = True  # True = Show touch on-screen keyboard for text inputs
    talkback_tone_hz: float = 1000.0
    talkback_tone_duration_sec: float = 1.0
    talkback_release_tone_duration_sec: float = 0.5
    storage_dir: str = DEFAULT_STORAGE_DIR
    known_device_defaults: dict = field(default_factory=dict)

    @property
    def effective_channels(self) -> int:
        """Return effective output WAV channel count (Studio Mode forces 2 channels)."""
        if self.studio_mode:
            return 2
        return 2 if self.channels >= 2 else 1


@dataclass
class RecordingEntry:
    """Metadata for a completed WAV recording in /storage."""

    filepath: Path
    filename: str
    size_bytes: int
    modified_time: float
    duration_seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    bit_depth: int = 0

    @property
    def formatted_duration(self) -> str:
        total_sec = int(round(self.duration_seconds))
        mins, secs = divmod(total_sec, 60)
        hrs, mins = divmod(mins, 60)
        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"

    @property
    def formatted_size(self) -> str:
        mb = self.size_bytes / (1024 * 1024)
        if mb >= 1.0:
            return f"{mb:.1f} MB"
        kb = self.size_bytes / 1024
        return f"{kb:.1f} KB"


def compute_wav_byte_rate(
    sample_rate: int,
    bit_depth: int,
    channels: int = 1,
    studio_mode: bool = False,
) -> int:
    """Compute uncompressed PCM WAV byte rate (bytes per second) for given settings."""
    sr = int(sample_rate) if int(sample_rate) > 0 else 48000
    bd = int(bit_depth) if int(bit_depth) in (16, 24, 32) else 24
    eff_channels = 2 if (studio_mode or int(channels) >= 2) else 1
    bytes_per_sample = bd // 8
    return sr * eff_channels * bytes_per_sample


def format_storage_bytes(num_bytes: int) -> str:
    """Format a byte count into a human-readable capacity string (TB, GB, MB, KB, B)."""
    val = max(0, int(num_bytes))
    tb = 1024 ** 4
    gb = 1024 ** 3
    mb = 1024 ** 2
    kb = 1024
    if val >= tb:
        return f"{val / tb:.2f} TB"
    if val >= gb:
        gb_val = val / gb
        return f"{gb_val:.1f} GB" if gb_val >= 10.0 else f"{gb_val:.2f} GB"
    if val >= mb:
        return f"{val / mb:.1f} MB"
    if val >= kb:
        return f"{val / kb:.1f} KB"
    return f"{val} B"


def format_remaining_time(seconds: float) -> str:
    """Format remaining recording duration in seconds as 'HHh MMm SSs' (or 'HHH+h MMm')."""
    total_sec = max(0, int(math.floor(max(0.0, float(seconds)))))
    mins, secs = divmod(total_sec, 60)
    hrs, mins = divmod(mins, 60)
    if hrs >= 100:
        return f"{hrs:,}h {mins:02d}m"
    return f"{hrs:02d}h {mins:02d}m {secs:02d}s"


def format_remaining_hms(seconds: float) -> str:
    """Format remaining recording duration in seconds as 'HH:MM:SS'."""
    total_sec = max(0, int(math.floor(max(0.0, float(seconds)))))
    mins, secs = divmod(total_sec, 60)
    hrs, mins = divmod(mins, 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


@dataclass
class StorageStatus:
    """Telemetry snapshot for /storage drive utilization and recording time remaining."""

    storage_path: Path
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    used_percent: float = 0.0
    sample_rate: int = 48000
    bit_depth: int = 24
    channels: int = 1
    studio_mode: bool = False
    byte_rate: int = 0
    remaining_seconds: float = 0.0
    level: str = "ok"  # "ok", "warning", "critical", "full", or "offline"
    available: bool = True
    filesystem_device: str = ""
    mount_point: str = ""
    filesystem_type: str = ""
    is_temporary_fs: bool = False

    @property
    def effective_channels(self) -> int:
        return 2 if (self.studio_mode or self.channels >= 2) else 1

    @property
    def drive_summary(self) -> str:
        """Return human-readable mount/filesystem descriptor for /storage."""
        if self.filesystem_device and self.filesystem_type:
            dev_short = self.filesystem_device
            if self.is_temporary_fs:
                return f"{self.storage_path} [RAM {self.filesystem_type}]"
            return f"{self.storage_path} ({dev_short} • {self.filesystem_type})"
        return str(self.storage_path)

    @property
    def formatted_remaining(self) -> str:
        return format_remaining_time(self.remaining_seconds)

    @property
    def formatted_remaining_hms(self) -> str:
        return format_remaining_hms(self.remaining_seconds)

    @property
    def formatted_free(self) -> str:
        return format_storage_bytes(self.free_bytes)

    @property
    def formatted_used(self) -> str:
        return format_storage_bytes(self.used_bytes)

    @property
    def formatted_total(self) -> str:
        return format_storage_bytes(self.total_bytes)

    @property
    def mb_per_minute(self) -> float:
        if self.byte_rate <= 0:
            return 0.0
        return (self.byte_rate * 60.0) / (1024.0 * 1024.0)

    @property
    def is_warning(self) -> bool:
        return self.level == "warning"

    @property
    def is_critical(self) -> bool:
        return self.level in ("critical", "full")

    @property
    def status_badge_text(self) -> str:
        if not self.available or self.level == "offline":
            return "OFFLINE"
        if self.level == "full":
            return "DRIVE FULL"
        if self.level == "critical":
            return "CRITICAL LOW SPACE"
        if self.level == "warning":
            return "LOW SPACE"
        return "OK"


TEMP_FILESYSTEM_TYPES: tuple[str, ...] = (
    "tmpfs",
    "ramfs",
    "rootfs",
    "devtmpfs",
)


def parse_proc_mounts(mounts_text: str) -> List[Tuple[str, str, str]]:
    """Parse /proc/mounts content into a list of (device, mount_point, fs_type)."""
    entries: List[Tuple[str, str, str]] = []
    for raw_line in mounts_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        dev = parts[0].replace("\\040", " ")
        mnt = parts[1].replace("\\040", " ")
        fstype = parts[2]
        entries.append((dev, mnt, fstype))
    return entries


def resolve_mount_for_path(
    target_path: Path | str, mounts: List[Tuple[str, str, str]]
) -> Tuple[Path, str, str, str, bool]:
    """Identify the active mounted filesystem backing target_path from /proc/mounts."""
    raw_str = str(target_path)
    if os.name != "nt":
        try:
            real_str = os.path.realpath(raw_str)
        except OSError:
            real_str = raw_str
    else:
        # On Windows (used for cross-platform unit tests), preserve POSIX root paths
        p_obj = Path(target_path)
        if not p_obj.drive:
            real_str = posixpath.normpath(raw_str.replace("\\", "/"))
        else:
            real_str = raw_str.replace("\\", "/")
    real_path = Path(real_str)

    if not mounts:
        return (real_path, "", "", "", False)

    norm_target = real_str.rstrip("/") or "/"

    # Find longest matching ancestor mountpoint; iterate in order so later mounts win on ties
    best_dev = ""
    best_mnt = ""
    best_fs = ""
    best_len = -1

    for dev, mnt, fstype in mounts:
        if fstype == "autofs":
            continue
        norm_mnt = mnt.rstrip("/") or "/"
        if norm_target == norm_mnt or (
            norm_mnt == "/" and norm_target.startswith("/")
        ) or norm_target.startswith(norm_mnt + "/"):
            mnt_len = 0 if norm_mnt == "/" else len(norm_mnt)
            if mnt_len >= best_len:
                best_len = mnt_len
                best_dev = dev
                best_mnt = norm_mnt
                best_fs = fstype

    is_temp = best_fs.lower() in TEMP_FILESYSTEM_TYPES

    # If /storage itself is on root tmpfs, check if a persistent drive is mounted directly inside /storage
    if is_temp and norm_target != "/":
        prefix = norm_target + "/"
        for dev, mnt, fstype in mounts:
            norm_mnt = mnt.rstrip("/")
            if (
                norm_mnt.startswith(prefix)
                and fstype.lower() not in TEMP_FILESYSTEM_TYPES
                and fstype != "autofs"
            ):
                return (Path(norm_mnt), dev, norm_mnt, fstype, False)

    return (real_path, best_dev, best_mnt, best_fs, is_temp)


class ConfigManager:
    """Manages loading and saving settings across persistent storage and LBU."""

    def __init__(
        self,
        storage_dir: Optional[str] = None,
        etc_config_dir: Optional[str] = None,
    ) -> None:
        env_storage = os.environ.get("ALPINE_RECORDER_STORAGE")
        env_config = os.environ.get("ALPINE_RECORDER_CONFIG_DIR")

        self.storage_dir = Path(storage_dir or env_storage or DEFAULT_STORAGE_DIR)
        self.etc_config_dir = Path(
            etc_config_dir or env_config or DEFAULT_ETC_CONFIG_DIR
        )
        self._mount_attempted: bool = False

        # Ensure /storage persistent partition is mounted if configured in /etc/fstab
        self._ensure_storage_mounted()

        # Fallback for non-root or non-Alpine test runs
        if not self._can_use_directory(self.storage_dir):
            fallback_base = Path.home() / ".alpine-recorder-dev"
            self.storage_dir = fallback_base / "storage"
        if not self._can_use_directory(self.etc_config_dir):
            fallback_base = Path.home() / ".alpine-recorder-dev"
            self.etc_config_dir = fallback_base / "etc"

        self.storage_config_path = (
            self.storage_dir / ".alpine-recorder" / "settings.json"
        )
        self.etc_config_path = self.etc_config_dir / "settings.json"

        self.ensure_directories()
        self.settings: RecorderSettings = self.load()

    @staticmethod
    def _read_proc_mounts() -> List[Tuple[str, str, str]]:
        """Read mounted filesystems from /proc/mounts on Linux."""
        proc_mounts = Path("/proc/mounts")
        if not proc_mounts.is_file():
            return []
        try:
            return parse_proc_mounts(
                proc_mounts.read_text(encoding="utf-8", errors="ignore")
            )
        except OSError:
            return []

    def _ensure_storage_mounted(self, force: bool = False) -> None:
        """If /storage is unmounted on a diskless RAM root (tmpfs), mount it from /etc/fstab."""
        if os.name != "posix":
            return
        if self._mount_attempted and not force:
            return

        target = self.storage_dir
        try:
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

        mounts = self._read_proc_mounts()
        if not mounts:
            return

        _probe, _dev, mnt_pt, _fs, is_temp = resolve_mount_for_path(target, mounts)
        norm_target = os.path.realpath(str(target)).rstrip("/") or "/"
        if mnt_pt == norm_target and not is_temp:
            self._mount_attempted = True
            return

        self._mount_attempted = True
        mount_bin = shutil.which("mount")
        if not mount_bin:
            return

        try:
            subprocess.run(
                [mount_bin, str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            mounts_after = self._read_proc_mounts()
            _p2, _d2, mnt_pt2, _f2, is_temp2 = resolve_mount_for_path(
                target, mounts_after
            )
            if mnt_pt2 != norm_target or is_temp2:
                subprocess.run(
                    [mount_bin, "-a"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
        except Exception as exc:
            logger.debug("Mount check for %s skipped: %s", target, exc)

    @staticmethod
    def _can_use_directory(path: Path) -> bool:
        try:
            if not path.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            real_dir = Path(os.path.realpath(str(path)))
            if not real_dir.is_dir():
                real_dir.mkdir(parents=True, exist_ok=True)
            if os.access(real_dir, os.W_OK):
                return True
            test_file = real_dir / ".write_test"
            test_file.touch(exist_ok=True)
            test_file.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def ensure_directories(self) -> None:
        """Ensure storage and configuration directories exist with proper permissions."""
        for directory in (
            self.storage_dir,
            self.storage_config_path.parent,
            self.etc_config_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Could not create directory %s: %s", directory, exc)

        self.enforce_storage_permissions()

    @staticmethod
    def _resolve_sambashare_gid() -> int:
        """Return numeric GID for the 'sambashare' group on POSIX systems, or -1 if unavailable."""
        try:
            import grp  # type: ignore[import-not-found]

            return int(grp.getgrnam("sambashare").gr_gid)
        except Exception:
            return -1

    def enforce_storage_permissions(self) -> None:
        """Recursively mask permissions and ownership across /storage for Samba share access.

        Ensures /storage and all subdirectories (excluding .alpine-recorder and lost+found)
        have 2777 (or 0777) permissions and root:sambashare ownership, and all files inside
        the shared folder structure have at least 0666 (rw-rw-rw-) permissions.
        """
        if not self.storage_dir.is_dir():
            return

        has_chown = hasattr(os, "chown")
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        target_uid = 0 if is_root else -1
        target_gid = self._resolve_sambashare_gid() if has_chown else -1

        def _mask_dir(dir_path: Path) -> None:
            try:
                st = dir_path.stat()
                if (st.st_mode & 0o2777) != 0o2777:
                    try:
                        os.chmod(dir_path, 0o2777)
                    except OSError:
                        os.chmod(dir_path, (st.st_mode & 0o7777) | 0o777)
                elif (st.st_mode & 0o777) != 0o777:
                    os.chmod(dir_path, (st.st_mode & 0o7777) | 0o777)

                if has_chown and (
                    (target_uid >= 0 and st.st_uid != target_uid)
                    or (target_gid >= 0 and st.st_gid != target_gid)
                ):
                    try:
                        os.chown(dir_path, target_uid, target_gid)
                    except OSError:
                        if target_gid >= 0 and st.st_gid != target_gid:
                            try:
                                os.chown(dir_path, -1, target_gid)
                            except OSError:
                                pass
            except OSError:
                pass

        _mask_dir(self.storage_dir)

        skip_names = {".alpine-recorder", "lost+found"}
        stack: List[Path] = [self.storage_dir]

        while stack:
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        if entry.name in skip_names:
                            continue
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                dir_p = Path(entry.path)
                                _mask_dir(dir_p)
                                stack.append(dir_p)
                            elif entry.is_file(follow_symlinks=False):
                                st = entry.stat(follow_symlinks=False)
                                if (st.st_mode & 0o666) != 0o666:
                                    try:
                                        os.chmod(
                                            entry.path, (st.st_mode & 0o777) | 0o666
                                        )
                                    except OSError:
                                        pass
                                if has_chown and (
                                    (target_uid >= 0 and st.st_uid != target_uid)
                                    or (target_gid >= 0 and st.st_gid != target_gid)
                                ):
                                    try:
                                        os.chown(entry.path, target_uid, target_gid)
                                    except OSError:
                                        if target_gid >= 0 and st.st_gid != target_gid:
                                            try:
                                                os.chown(entry.path, -1, target_gid)
                                            except OSError:
                                                pass
                        except OSError:
                            continue
            except OSError:
                continue

    def load(self) -> RecorderSettings:
        """Load settings from the freshest valid configuration file."""
        candidates: List[Path] = []
        for path in (self.storage_config_path, self.etc_config_path):
            if path.is_file():
                candidates.append(path)

        # Sort by modification time descending so the latest saved settings win
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        for path in candidates:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    valid_keys = {
                        f.name for f in RecorderSettings.__dataclass_fields__.values()
                    }
                    filtered = {k: v for k, v in raw.items() if k in valid_keys}
                    filtered["storage_dir"] = str(self.storage_dir)
                    settings = RecorderSettings(**filtered)
                    return settings
            except Exception as exc:
                logger.warning("Failed to load settings from %s: %s", path, exc)

        default_settings = RecorderSettings(storage_dir=str(self.storage_dir))
        return default_settings

    def save(self, commit_lbu: bool = False) -> None:
        """Save current settings to both /storage and /etc/alpine-recorder."""
        self.settings.storage_dir = str(self.storage_dir)
        payload = json.dumps(asdict(self.settings), indent=2, sort_keys=True)

        for path in (self.storage_config_path, self.etc_config_path):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_suffix(".json.tmp")
                tmp_path.write_text(payload, encoding="utf-8")
                tmp_path.replace(path)
            except OSError as exc:
                logger.warning("Failed to write settings to %s: %s", path, exc)

        if commit_lbu:
            self.trigger_lbu_commit_async()

    @staticmethod
    def trigger_lbu_commit_async() -> None:
        """Run `lbu commit -d` in a background thread if running on Alpine with LBU."""
        lbu_bin = shutil.which("lbu")
        if not lbu_bin:
            return

        def _worker() -> None:
            try:
                subprocess.run(
                    [lbu_bin, "exclude", "/var/lib/samba/private/msg.sock"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                subprocess.run(
                    [lbu_bin, "commit", "-d"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
            except Exception as exc:
                logger.debug("lbu commit skipped or failed: %s", exc)

        threading.Thread(target=_worker, daemon=True).start()

    def list_project_recordings(
        self, project_name: Optional[str] = None
    ) -> List[RecordingEntry]:
        """List all finished WAV files in /storage matching the project name prefix."""
        self.enforce_storage_permissions()
        prefix_clean = sanitize_project_name(
            project_name if project_name is not None else self.settings.project_name
        )
        expected_prefix = f"{prefix_clean}_"

        entries: List[RecordingEntry] = []
        if not self.storage_dir.is_dir():
            return entries

        for item in self.storage_dir.iterdir():
            if not item.is_file():
                continue
            if not item.name.lower().endswith(".wav"):
                continue
            if not item.name.startswith(expected_prefix):
                continue

            try:
                st = item.stat()
                duration, sr, ch, bits = self._probe_wav_header(item, st.st_size)
                entries.append(
                    RecordingEntry(
                        filepath=item,
                        filename=item.name,
                        size_bytes=st.st_size,
                        modified_time=st.st_mtime,
                        duration_seconds=duration,
                        sample_rate=sr,
                        channels=ch,
                        bit_depth=bits,
                    )
                )
            except OSError:
                continue

        # Newest recordings first
        entries.sort(key=lambda e: e.modified_time, reverse=True)
        return entries

    def get_storage_status(
        self,
        sample_rate: Optional[int] = None,
        bit_depth: Optional[int] = None,
        channels: Optional[int] = None,
        studio_mode: Optional[bool] = None,
        disk_usage_fn: Optional[Callable[[Path], Tuple[int, int, int]]] = None,
    ) -> StorageStatus:
        """Query /storage disk utilization and calculate recording time remaining for current or given settings."""
        sr_raw = sample_rate if sample_rate is not None else self.settings.sample_rate
        bd_raw = bit_depth if bit_depth is not None else self.settings.bit_depth
        st_mode = bool(
            studio_mode if studio_mode is not None else self.settings.studio_mode
        )
        ch_raw = channels if channels is not None else self.settings.channels

        sr = int(sr_raw) if int(sr_raw) > 0 else 48000
        bd = int(bd_raw) if int(bd_raw) in (16, 24, 32) else 24
        eff_ch = 2 if (st_mode or int(ch_raw) >= 2) else 1
        byte_rate = compute_wav_byte_rate(sr, bd, eff_ch, st_mode)

        probe_path = self.storage_dir
        fs_device = ""
        mount_point = ""
        fs_type = ""
        is_temp_fs = False

        try:
            while not probe_path.exists() and probe_path.parent != probe_path:
                probe_path = probe_path.parent

            if disk_usage_fn is None and os.name == "posix":
                mounts = self._read_proc_mounts()
                if mounts:
                    resolved_probe, fs_device, mount_point, fs_type, is_temp_fs = (
                        resolve_mount_for_path(probe_path, mounts)
                    )
                    if is_temp_fs and not self._mount_attempted:
                        self._ensure_storage_mounted(force=True)
                        mounts = self._read_proc_mounts()
                        resolved_probe, fs_device, mount_point, fs_type, is_temp_fs = (
                            resolve_mount_for_path(probe_path, mounts)
                        )
                    if resolved_probe.exists():
                        probe_path = resolved_probe
                else:
                    probe_path = Path(os.path.realpath(str(probe_path)))
            elif disk_usage_fn is None:
                probe_path = Path(os.path.realpath(str(probe_path)))

            usage_fn = disk_usage_fn if disk_usage_fn is not None else shutil.disk_usage
            usage = usage_fn(probe_path)
            total_bytes = max(0, int(usage[0]))
            raw_used = max(0, int(usage[1]))
            free_bytes = max(0, int(usage[2]))
        except Exception as exc:
            logger.debug("Could not query disk usage for %s: %s", self.storage_dir, exc)
            return StorageStatus(
                storage_path=self.storage_dir,
                sample_rate=sr,
                bit_depth=bd,
                channels=eff_ch,
                studio_mode=st_mode,
                byte_rate=byte_rate,
                level="offline",
                available=False,
            )

        if total_bytes <= 0:
            return StorageStatus(
                storage_path=self.storage_dir,
                sample_rate=sr,
                bit_depth=bd,
                channels=eff_ch,
                studio_mode=st_mode,
                byte_rate=byte_rate,
                level="offline",
                available=False,
            )

        # Ensure used_bytes accounts for unavailable space so 0 free bytes == 100.0% used
        used_bytes = min(total_bytes, max(raw_used, total_bytes - free_bytes))
        used_percent = min(100.0, max(0.0, (used_bytes / total_bytes) * 100.0))

        if byte_rate > 0 and free_bytes > 44:
            remaining_seconds = float(free_bytes) / float(byte_rate)
        else:
            remaining_seconds = 0.0

        if free_bytes <= 44 or remaining_seconds < 1.0 or used_percent >= 99.95:
            level = "full"
        elif (
            used_percent >= STORAGE_CRITICAL_USED_PERCENT
            or remaining_seconds <= STORAGE_CRITICAL_REMAINING_SEC
        ):
            level = "critical"
        elif (
            used_percent >= STORAGE_WARN_USED_PERCENT
            or remaining_seconds <= STORAGE_WARN_REMAINING_SEC
        ):
            level = "warning"
        else:
            level = "ok"

        return StorageStatus(
            storage_path=self.storage_dir,
            total_bytes=total_bytes,
            used_bytes=used_bytes,
            free_bytes=free_bytes,
            used_percent=used_percent,
            sample_rate=sr,
            bit_depth=bd,
            channels=eff_ch,
            studio_mode=st_mode,
            byte_rate=byte_rate,
            remaining_seconds=remaining_seconds,
            level=level,
            available=True,
            filesystem_device=fs_device,
            mount_point=mount_point,
            filesystem_type=fs_type,
            is_temporary_fs=is_temp_fs,
        )

    @staticmethod
    def _probe_wav_header(filepath: Path, file_size: int) -> tuple[float, int, int, int]:
        """Quickly parse standard RIFF WAV header to extract duration, rate, channels, bit depth."""
        try:
            with filepath.open("rb") as f:
                header = f.read(44)
                if len(header) < 44 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
                    return (0.0, 0, 0, 0)
                channels = int.from_bytes(header[22:24], "little")
                sample_rate = int.from_bytes(header[24:28], "little")
                byte_rate = int.from_bytes(header[28:32], "little")
                bits_per_sample = int.from_bytes(header[34:36], "little")
                data_bytes = max(0, file_size - 44)
                duration = (data_bytes / byte_rate) if byte_rate > 0 else 0.0
                return (duration, sample_rate, channels, bits_per_sample)
        except Exception:
            return (0.0, 0, 0, 0)
