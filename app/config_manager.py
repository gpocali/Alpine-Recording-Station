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

PROTECTED_STORAGE_DIRS: frozenset[str] = frozenset({
    ".alpine-recorder",
    "lost+found",
    "ssl",
})

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

CLOCK_TIME_FORMAT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("12h", "12-Hour (e.g. 08:05:30 PM — Default)"),
    ("24h", "24-Hour (e.g. 20:05:30)"),
)

CLOCK_DATE_FORMAT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("YYYY-MM-DD", "YYYY-MM-DD (ISO / International — e.g. 2026-11-08)"),
    ("MM/DD/YYYY", "MM/DD/YYYY (Month/Day/Year — e.g. 11/08/2026)"),
    ("DD/MM/YYYY", "DD/MM/YYYY (Day/Month/Year — e.g. 08/11/2026)"),
    ("DD.MM.YYYY", "DD.MM.YYYY (Day.Month.Year — e.g. 08.11.2026)"),
    ("DD MMM YYYY", "DD MMM YYYY (e.g. 08 Nov 2026)"),
    ("MMM DD, YYYY", "MMM DD, YYYY (e.g. Nov 08, 2026)"),
    ("none", "Time Only (Hide Date)"),
)

_MONTH_ABBR: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def format_clock_display(
    time_format: str = "12h",
    date_format: str = "YYYY-MM-DD",
    dt: Optional[datetime.datetime] = None,
) -> str:
    """Format current local date and time for the studio clock according to user settings."""
    if dt is None:
        dt = datetime.datetime.now().astimezone()
    elif dt.tzinfo is None:
        dt = dt.astimezone()

    tf = str(time_format or "12h").strip().lower()
    if tf == "24h":
        time_str = f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    else:
        hour_12 = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        time_str = f"{hour_12:02d}:{dt.minute:02d}:{dt.second:02d} {ampm}"

    df = str(date_format if date_format is not None else "YYYY-MM-DD").strip()
    if df.lower() == "none" or not df:
        date_str = ""
    elif df == "MM/DD/YYYY":
        date_str = f"{dt.month:02d}/{dt.day:02d}/{dt.year:04d}"
    elif df == "DD/MM/YYYY":
        date_str = f"{dt.day:02d}/{dt.month:02d}/{dt.year:04d}"
    elif df == "DD.MM.YYYY":
        date_str = f"{dt.day:02d}.{dt.month:02d}.{dt.year:04d}"
    elif df == "DD MMM YYYY":
        m_abbr = _MONTH_ABBR[max(0, min(11, dt.month - 1))]
        date_str = f"{dt.day:02d} {m_abbr} {dt.year:04d}"
    elif df == "MMM DD, YYYY":
        m_abbr = _MONTH_ABBR[max(0, min(11, dt.month - 1))]
        date_str = f"{m_abbr} {dt.day:02d}, {dt.year:04d}"
    else:
        date_str = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"

    if date_str:
        return f"{date_str}  •  {time_str}"
    return time_str


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


_TAKE_NUMBER_RE = re.compile(
    r"(?:^|[_\-\s])take[_\-\s]*(\d+)(?:[_\-\s.]|$)", re.IGNORECASE
)


def extract_take_number(
    filename: str, project_name: Optional[str] = None
) -> Optional[int]:
    """Extract an integer take number from a recording filename, if present.

    If ``project_name`` is provided, a matching ``<ProjectName>_`` prefix is
    stripped before searching so project names such as ``Take2`` are not
    mistaken for a take number.
    """
    stem = (filename or "").strip()
    if stem.lower().endswith(".wav"):
        stem = stem[:-4]
    if not stem:
        return None

    target = stem
    if project_name is not None:
        prefix_clean = sanitize_project_name(project_name)
        expected_prefix = f"{prefix_clean}_"
        if target.startswith(expected_prefix):
            target = target[len(expected_prefix):]

    match = _TAKE_NUMBER_RE.search(target)
    if not match:
        return None
    try:
        val = int(match.group(1))
        return val if val > 0 else None
    except ValueError:
        return None


def generate_recording_filename(
    project_name: str,
    dt: Optional[datetime.datetime] = None,
    take_number: Optional[int] = None,
) -> str:
    """Generate a WAV filename with format: <ProjectName>[_TakeNNN]_yyyy-mm-dd_HH.MM.SS_TZ.wav."""
    prefix = sanitize_project_name(project_name)
    if dt is None:
        dt = datetime.datetime.now().astimezone()
    elif dt.tzinfo is None:
        dt = dt.astimezone()

    tz_name = dt.strftime("%Z") or "LOCAL"
    # Clean TZ name for Windows/Samba safety (e.g. 'UTC+00:00' -> 'UTC+0000')
    tz_name = re.sub(r'[<>:"/\\|?*\s]', "", tz_name) or "LOCAL"
    timestamp = dt.strftime("%Y-%m-%d_%H.%M.%S")
    if take_number is not None and int(take_number) > 0:
        return f"{prefix}_Take{int(take_number):03d}_{timestamp}_{tz_name}.wav"
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
    clock_time_format: str = "12h"  # '12h' (Default) or '24h'
    clock_date_format: str = "YYYY-MM-DD"  # Regionally agnostic date format or 'none'
    talkback_tone_hz: float = 1000.0
    talkback_tone_duration_sec: float = 1.0
    talkback_release_tone_duration_sec: float = 0.5
    remote_producer_enabled: bool = False
    remote_producer_port: int = 8443
    remote_producer_send_talent_mic: bool = True
    remote_producer_allow_project_rename: bool = False
    ssl_cert_path: str = ""
    ssl_key_path: str = ""
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
    project_name: str = ""

    def __post_init__(self) -> None:
        if not self.project_name and self.filepath is not None:
            parent_name = self.filepath.parent.name
            if parent_name and parent_name not in ("", "storage", "/"):
                self.project_name = parent_name
            else:
                self.project_name = "Root"

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

    @property
    def formatted_modified(self) -> str:
        try:
            dt = datetime.datetime.fromtimestamp(self.modified_time).astimezone()
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""


@dataclass
class ProjectFolderSummary:
    """Metadata summary for a project directory inside /storage."""

    project_name: str
    folder_path: Path
    recording_count: int = 0
    other_file_count: int = 0
    total_size_bytes: int = 0
    latest_modified_time: float = 0.0
    is_active_project: bool = False

    @property
    def total_item_count(self) -> int:
        return self.recording_count + self.other_file_count

    @property
    def is_empty(self) -> bool:
        return self.recording_count == 0 and self.other_file_count == 0

    @property
    def formatted_size(self) -> str:
        return format_storage_bytes(self.total_size_bytes)


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
                for inc_path in ("/etc/alpine-recorder", "/etc/alpine-recorder/ssl"):
                    if Path(inc_path).exists():
                        subprocess.run(
                            [lbu_bin, "include", inc_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                            check=False,
                        )
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

    @staticmethod
    def _fsync_directory(dir_path: Path) -> None:
        """Flush directory entry metadata to disk on POSIX filesystems so Samba reflects changes immediately."""
        if os.name != "posix" or not hasattr(os, "fsync"):
            return
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            fd = os.open(str(dir_path), flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def get_project_dir(self, project_name: Optional[str] = None) -> Path:
        """Return the project subfolder path inside /storage for the given or active project."""
        prefix_clean = sanitize_project_name(
            project_name if project_name is not None else self.settings.project_name
        )
        return self.storage_dir / prefix_clean

    def get_next_take_number(self, project_name: Optional[str] = None) -> int:
        """Return the next 1-based take number for the given or active project.

        Starts at 1 for a new/empty project folder. When resuming an existing
        project folder, returns ``highest_existing_take + 1``.
        """
        proj_label = (
            project_name if project_name is not None else self.settings.project_name
        )
        proj_dir = self.get_project_dir(proj_label)
        if not proj_dir.is_dir():
            return 1

        max_take = 0
        try:
            for item in proj_dir.iterdir():
                try:
                    if item.is_symlink() or not item.is_file():
                        continue
                except OSError:
                    continue
                if not item.name.lower().endswith(".wav"):
                    continue
                take_num = extract_take_number(item.name, project_name=proj_label)
                if take_num is not None and take_num > max_take:
                    max_take = take_num
        except OSError:
            pass
        return max_take + 1

    def ensure_project_dir(self, project_name: Optional[str] = None) -> Path:
        """Ensure the project subfolder exists inside /storage with proper Samba permissions."""
        proj_dir = self.get_project_dir(project_name)
        try:
            proj_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create project directory %s: %s", proj_dir, exc)
        self.enforce_storage_permissions()
        self._fsync_directory(proj_dir)
        self._fsync_directory(self.storage_dir)
        return proj_dir

    def rename_recording(
        self, filepath: Path | str, new_name: str
    ) -> Tuple[bool, str, Optional[Path]]:
        """Rename a completed WAV recording within its project folder.

        Args:
            filepath: Existing path to the WAV file.
            new_name: Desired new filename (with or without .wav extension).

        Returns:
            Tuple of (success, message, new_filepath).
        """
        src_path = Path(filepath)
        if not src_path.is_file():
            return (False, f"Recording file not found: {src_path.name}", None)

        raw_name = (new_name or "").strip()
        if raw_name.lower().endswith(".wav"):
            raw_name = raw_name[:-4].strip()

        # Sanitize characters invalid in Linux or Windows/Samba filenames
        cleaned_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw_name)
        cleaned_stem = re.sub(r"\s+", "_", cleaned_stem).strip("._")
        if not cleaned_stem:
            return (False, "Please enter a valid recording filename.", None)

        target_filename = f"{cleaned_stem}.wav"
        dst_path = src_path.parent / target_filename

        if dst_path == src_path:
            return (True, f"Recording name unchanged ({target_filename}).", src_path)

        if dst_path.exists():
            return (
                False,
                f"A file named '{target_filename}' already exists in {src_path.parent.name}/.",
                None,
            )

        try:
            src_path.rename(dst_path)
            self.enforce_storage_permissions()
            self._fsync_directory(dst_path.parent)
            return (True, f"Renamed to {target_filename}", dst_path)
        except OSError as exc:
            logger.warning("Failed to rename %s to %s: %s", src_path, dst_path, exc)
            return (False, f"Could not rename file: {exc}", None)

    def list_project_recordings(
        self, project_name: Optional[str] = None
    ) -> List[RecordingEntry]:
        """List all finished WAV files in the project's subfolder (/storage/<ProjectName>/).

        Also includes any legacy WAV files directly in /storage matching <ProjectName>_*.wav.
        """
        self.enforce_storage_permissions()
        prefix_clean = sanitize_project_name(
            project_name if project_name is not None else self.settings.project_name
        )
        expected_prefix = f"{prefix_clean}_"
        proj_dir = self.storage_dir / prefix_clean

        entries: List[RecordingEntry] = []
        seen_paths: set[Path] = set()

        # 1. Scan project subfolder (/storage/<ProjectName>/) for all WAV files (including renamed ones)
        if proj_dir.is_dir():
            try:
                for item in proj_dir.iterdir():
                    if not item.is_file():
                        continue
                    if not item.name.lower().endswith(".wav"):
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
                        seen_paths.add(item.resolve())
                    except OSError:
                        continue
            except OSError:
                pass

        # 2. Also include any legacy root-level WAV files in /storage matching <ProjectName>_*.wav
        if self.storage_dir.is_dir():
            try:
                for item in self.storage_dir.iterdir():
                    if not item.is_file():
                        continue
                    if not item.name.lower().endswith(".wav"):
                        continue
                    if not item.name.startswith(expected_prefix):
                        continue
                    try:
                        resolved = item.resolve()
                        if resolved in seen_paths:
                            continue
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
                        seen_paths.add(resolved)
                    except OSError:
                        continue
            except OSError:
                pass

        # Newest recordings first
        entries.sort(key=lambda e: e.modified_time, reverse=True)
        return entries

    @staticmethod
    def is_protected_storage_name(name: str) -> bool:
        """Return True if a folder/file name inside /storage is reserved for system use."""
        cleaned = (name or "").strip()
        if not cleaned or cleaned.startswith(".") or cleaned in PROTECTED_STORAGE_DIRS:
            return True
        return False

    def _is_valid_user_path_in_storage(self, target: Path) -> bool:
        """Verify target path is strictly inside /storage and not inside a protected system directory."""
        try:
            storage_resolved = self.storage_dir.resolve()
            target_resolved = target.resolve()
            rel = target_resolved.relative_to(storage_resolved)
        except Exception:
            return False
        parts = rel.parts
        if not parts:
            return False
        if self.is_protected_storage_name(parts[0]):
            return False
        return True

    def list_project_folders(self) -> List[ProjectFolderSummary]:
        """List all project directories in /storage with recording counts and total disk usage."""
        self.enforce_storage_permissions()
        active_clean = sanitize_project_name(self.settings.project_name)
        summaries: List[ProjectFolderSummary] = []

        if not self.storage_dir.is_dir():
            return summaries

        try:
            for entry in self.storage_dir.iterdir():
                try:
                    if entry.is_symlink() or not entry.is_dir():
                        continue
                except OSError:
                    continue
                if self.is_protected_storage_name(entry.name):
                    continue

                rec_count = 0
                other_count = 0
                total_bytes = 0
                latest_mtime = 0.0
                try:
                    latest_mtime = entry.stat().st_mtime
                except OSError:
                    pass

                stack: List[Path] = [entry]
                while stack:
                    curr = stack.pop()
                    try:
                        with os.scandir(curr) as it:
                            for sub in it:
                                try:
                                    if sub.is_symlink():
                                        continue
                                    if sub.is_dir(follow_symlinks=False):
                                        stack.append(Path(sub.path))
                                    elif sub.is_file(follow_symlinks=False):
                                        st = sub.stat(follow_symlinks=False)
                                        total_bytes += max(0, int(st.st_size))
                                        if st.st_mtime > latest_mtime:
                                            latest_mtime = st.st_mtime
                                        if sub.name.lower().endswith(".wav"):
                                            rec_count += 1
                                        else:
                                            other_count += 1
                                except OSError:
                                    continue
                    except OSError:
                        continue

                summaries.append(
                    ProjectFolderSummary(
                        project_name=entry.name,
                        folder_path=entry,
                        recording_count=rec_count,
                        other_file_count=other_count,
                        total_size_bytes=total_bytes,
                        latest_modified_time=latest_mtime,
                        is_active_project=(entry.name == active_clean),
                    )
                )
        except OSError:
            pass

        summaries.sort(
            key=lambda s: (not s.is_active_project, s.project_name.lower())
        )
        return summaries

    def list_all_recordings(
        self, project_filter: Optional[str] = None
    ) -> List[RecordingEntry]:
        """List completed WAV recordings across all project folders in /storage (or filtered by project)."""
        self.enforce_storage_permissions()
        entries: List[RecordingEntry] = []
        seen_paths: set[Path] = set()

        if not self.storage_dir.is_dir():
            return entries

        filter_clean = (project_filter or "").strip()
        if filter_clean in ("", "*"):
            filter_clean = ""

        # 1. Scan all non-protected project subfolders in /storage
        try:
            for proj_dir in self.storage_dir.iterdir():
                try:
                    if proj_dir.is_symlink() or not proj_dir.is_dir():
                        continue
                except OSError:
                    continue
                if self.is_protected_storage_name(proj_dir.name):
                    continue
                if filter_clean and proj_dir.name != filter_clean:
                    continue

                try:
                    for item in proj_dir.iterdir():
                        if not item.is_file() or item.is_symlink():
                            continue
                        if not item.name.lower().endswith(".wav"):
                            continue
                        try:
                            resolved = item.resolve()
                            if resolved in seen_paths:
                                continue
                            st = item.stat()
                            duration, sr, ch, bits = self._probe_wav_header(
                                item, st.st_size
                            )
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
                                    project_name=proj_dir.name,
                                )
                            )
                            seen_paths.add(resolved)
                        except OSError:
                            continue
                except OSError:
                    continue
        except OSError:
            pass

        # 2. Also include any root-level WAV files directly in /storage
        if not filter_clean or filter_clean == "Root":
            try:
                for item in self.storage_dir.iterdir():
                    if not item.is_file() or item.is_symlink():
                        continue
                    if not item.name.lower().endswith(".wav"):
                        continue
                    try:
                        resolved = item.resolve()
                        if resolved in seen_paths:
                            continue
                        st = item.stat()
                        duration, sr, ch, bits = self._probe_wav_header(
                            item, st.st_size
                        )
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
                                project_name="Root",
                            )
                        )
                        seen_paths.add(resolved)
                    except OSError:
                        continue
            except OSError:
                pass

        entries.sort(key=lambda e: e.modified_time, reverse=True)
        return entries

    def delete_recording(
        self, filepath: Path | str
    ) -> Tuple[bool, str, int]:
        """Delete a single WAV recording file inside /storage and return (ok, message, freed_bytes)."""
        src_path = Path(filepath)
        if not src_path.exists() or not src_path.is_file():
            return (False, f"Recording file not found: {src_path.name}", 0)

        if not self._is_valid_user_path_in_storage(src_path):
            return (
                False,
                "Cannot delete files outside of /storage project directories.",
                0,
            )

        try:
            freed_bytes = max(0, int(src_path.stat().st_size))
        except OSError:
            freed_bytes = 0

        try:
            try:
                os.chmod(src_path, 0o666)
            except OSError:
                pass
            parent_dir = src_path.parent
            src_path.unlink()
            self._fsync_directory(parent_dir)
            return (
                True,
                f"Deleted '{src_path.name}' ({format_storage_bytes(freed_bytes)} freed).",
                freed_bytes,
            )
        except OSError as exc:
            logger.warning("Failed to delete recording %s: %s", src_path, exc)
            return (False, f"Could not delete file: {exc}", 0)

    def _resolve_user_project_dir(
        self, project_name_or_path: Path | str
    ) -> Tuple[Optional[Path], str]:
        """Resolve and validate a project directory path inside /storage."""
        if isinstance(project_name_or_path, Path):
            candidate = project_name_or_path
        else:
            raw = str(project_name_or_path or "").strip()
            if not raw:
                return (None, "No project folder specified.")
            p_obj = Path(raw)
            if p_obj.is_absolute():
                candidate = p_obj
            else:
                if "/" in raw or "\\" in raw or raw in (".", ".."):
                    return (None, f"Invalid project folder name: {raw}")
                candidate = self.storage_dir / raw

        if self.is_protected_storage_name(candidate.name):
            return (
                None,
                f"Folder '{candidate.name}' is a protected system directory and cannot be modified.",
            )

        if not candidate.exists() or not candidate.is_dir():
            return (None, f"Project folder not found: {candidate.name}")

        if not self._is_valid_user_path_in_storage(candidate):
            return (
                None,
                "Project folder must be located directly inside /storage.",
            )

        try:
            if candidate.resolve().parent != self.storage_dir.resolve():
                return (
                    None,
                    "Target must be a top-level project folder inside /storage.",
                )
        except OSError:
            return (None, "Could not resolve project folder path.")

        return (candidate, "")

    @staticmethod
    def _measure_dir_contents(dir_path: Path) -> Tuple[int, int]:
        """Return (total_files, total_bytes) recursively inside dir_path."""
        file_count = 0
        byte_count = 0
        stack: List[Path] = [dir_path]
        while stack:
            curr = stack.pop()
            try:
                with os.scandir(curr) as it:
                    for sub in it:
                        try:
                            if sub.is_symlink():
                                continue
                            if sub.is_dir(follow_symlinks=False):
                                stack.append(Path(sub.path))
                            elif sub.is_file(follow_symlinks=False):
                                st = sub.stat(follow_symlinks=False)
                                file_count += 1
                                byte_count += max(0, int(st.st_size))
                        except OSError:
                            continue
            except OSError:
                continue
        return (file_count, byte_count)

    @staticmethod
    def _chmod_writable_recursive(root_path: Path) -> None:
        """Ensure all files/dirs under root_path are writable before removal."""
        for dirpath, dirnames, filenames in os.walk(root_path):
            dp = Path(dirpath)
            try:
                os.chmod(dp, 0o777)
            except OSError:
                pass
            for dname in dirnames:
                try:
                    os.chmod(dp / dname, 0o777)
                except OSError:
                    pass
            for fname in filenames:
                try:
                    os.chmod(dp / fname, 0o666)
                except OSError:
                    pass

    def cleanup_empty_project_folders(self) -> Tuple[bool, str, int]:
        """Remove all empty project folders (containing 0 files and 0 subdirectories) in /storage."""
        removed_names: List[str] = []
        if not self.storage_dir.is_dir():
            return (True, "No empty project folders to clean up.", 0)

        for summary in self.list_project_folders():
            if not summary.is_empty:
                continue
            folder = summary.folder_path
            try:
                # Only remove if directory has no entries at all
                if any(folder.iterdir()):
                    continue
                try:
                    os.chmod(folder, 0o777)
                except OSError:
                    pass
                folder.rmdir()
                removed_names.append(summary.project_name)
            except OSError:
                continue

        count = len(removed_names)
        if count == 0:
            return (True, "No empty project folders found.", 0)
        self._fsync_directory(self.storage_dir)
        joined = ", ".join(f"{n}/" for n in removed_names)
        return (
            True,
            f"Cleaned up {count} empty project folder(s): {joined}",
            count,
        )

    def cleanup_project_folder(
        self,
        project_name_or_path: Path | str,
        confirm_recursive: bool = False,
    ) -> Tuple[bool, str, int]:
        """Delete all recordings and files inside a project folder while keeping the folder itself.

        Requires confirm_recursive=True as a safety guard against accidental recursive cleanup.
        Returns (success, message, freed_bytes).
        """
        proj_dir, err = self._resolve_user_project_dir(project_name_or_path)
        if proj_dir is None:
            return (False, err, 0)

        if not confirm_recursive:
            return (
                False,
                "Extra confirmation is required before cleaning up all files in a project folder.",
                0,
            )

        file_count, freed_bytes = self._measure_dir_contents(proj_dir)
        if file_count == 0:
            return (
                True,
                f"Project folder '{proj_dir.name}/' is already empty (0 B freed).",
                0,
            )

        self._chmod_writable_recursive(proj_dir)
        try:
            for item in list(proj_dir.iterdir()):
                if item.is_symlink() or item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
            self.enforce_storage_permissions()
            self._fsync_directory(proj_dir)
            self._fsync_directory(self.storage_dir)
            return (
                True,
                f"Cleaned up '{proj_dir.name}/' — deleted {file_count} file(s) ({format_storage_bytes(freed_bytes)} freed).",
                freed_bytes,
            )
        except OSError as exc:
            logger.warning("Failed to clean up project folder %s: %s", proj_dir, exc)
            return (False, f"Could not clean up project folder: {exc}", 0)

    def delete_project_folder(
        self,
        project_name_or_path: Path | str,
        confirm_recursive: bool = False,
    ) -> Tuple[bool, str, int]:
        """Recursively delete a project folder and all of its contents from /storage.

        Requires confirm_recursive=True as a safety guard against accidental recursive deletion.
        Returns (success, message, freed_bytes).
        """
        proj_dir, err = self._resolve_user_project_dir(project_name_or_path)
        if proj_dir is None:
            return (False, err, 0)

        if not confirm_recursive:
            return (
                False,
                "Extra recursive deletion confirmation is required before deleting a project folder.",
                0,
            )

        file_count, freed_bytes = self._measure_dir_contents(proj_dir)
        self._chmod_writable_recursive(proj_dir)
        try:
            shutil.rmtree(proj_dir)
            self.enforce_storage_permissions()
            self._fsync_directory(self.storage_dir)
            return (
                True,
                f"Deleted project folder '{proj_dir.name}/' and {file_count} file(s) ({format_storage_bytes(freed_bytes)} freed).",
                freed_bytes,
            )
        except OSError as exc:
            logger.warning("Failed to delete project folder %s: %s", proj_dir, exc)
            return (False, f"Could not delete project folder: {exc}", 0)

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
