"""Configuration persistence, LBU synchronization, and recording file management."""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_DIR = "/storage"
DEFAULT_ETC_CONFIG_DIR = "/etc/alpine-recorder"


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
    talkback_tone_duration_sec: float = 0.5
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
    def _can_use_directory(path: Path) -> bool:
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".write_test"
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

        # Ensure /storage has permissive rwx permissions for Samba sharing
        try:
            os.chmod(self.storage_dir, 0o777)
        except OSError:
            pass

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
