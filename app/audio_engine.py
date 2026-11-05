"""ALSA hardware audio discovery, bit-perfect WAV recording, Studio Mode Talkback DSP, and playback."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import math
import os
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

STANDARD_SAMPLE_RATES: Tuple[int, ...] = (
    44100,
    48000,
    88200,
    96000,
    176400,
    192000,
)

STANDARD_BIT_DEPTHS: Tuple[int, ...] = (16, 24, 32)

# ALSA constants from <alsa/pcm.h>
SND_PCM_STREAM_PLAYBACK = 0
SND_PCM_STREAM_CAPTURE = 1
SND_PCM_NONBLOCK = 1

SND_PCM_ACCESS_RW_INTERLEAVED = 3

SND_PCM_FORMAT_S16_LE = 2
SND_PCM_FORMAT_S24_LE = 6      # 24-bit in 4 bytes (LSB padded in upper byte)
SND_PCM_FORMAT_S32_LE = 10     # 32-bit signed little endian
SND_PCM_FORMAT_FLOAT_LE = 14   # 32-bit float
SND_PCM_FORMAT_S24_3LE = 32    # 24-bit packed in 3 bytes


@dataclass
class AudioDeviceCapabilities:
    """Hardware capabilities for a single ALSA or system audio device."""

    device_id: str  # e.g. 'hw:1,0'
    card_index: int
    device_index: int
    name: str
    description: str
    is_usb: bool
    is_capture: bool
    is_playback: bool
    supported_sample_rates: List[int] = field(default_factory=list)
    supported_bit_depths: List[int] = field(default_factory=list)
    min_channels: int = 1
    max_channels: int = 2
    # Maps bit_depth (16, 24, 32) -> preferred ALSA format constant
    alsa_format_map: Dict[int, int] = field(default_factory=dict)

    @property
    def max_sample_rate(self) -> int:
        return max(self.supported_sample_rates) if self.supported_sample_rates else 48000

    @property
    def max_bit_depth(self) -> int:
        return max(self.supported_bit_depths) if self.supported_bit_depths else 16

    @property
    def display_name(self) -> str:
        usb_tag = "[USB] " if self.is_usb else ""
        return f"{usb_tag}{self.name} ({self.device_id})"


class StudioTalkbackProcessor:
    """Sample-accurate DSP router for Normal Mode and Studio Mode with Talkback.

    In Studio Mode:
      - Always outputs 2 channels (Stereo: Left = Ch 0, Right = Ch 1).
      - When Talkback is OFF:
          Left  = Microphone audio (from selected input_channel)
          Right = Silence (0.0)
      - When Talkback is ON (momentary push-to-talk):
          Left  = Silence (0.0)
          Right = First 0.5 seconds is a sine marker tone (e.g. 1000 Hz),
                  followed immediately by Microphone audio on Right channel
                  until Talkback is released.
    """

    def __init__(
        self,
        sample_rate: int,
        studio_mode: bool,
        output_channels: int,
        input_channel: int = 0,
        tone_hz: float = 1000.0,
        tone_duration_sec: float = 0.5,
        tone_amplitude: float = 0.5,  # -6 dBFS
    ) -> None:
        self.sample_rate = max(8000, int(sample_rate))
        self.studio_mode = bool(studio_mode)
        self.output_channels = 2 if self.studio_mode else max(1, min(2, int(output_channels)))
        self.input_channel = max(0, int(input_channel))
        self.tone_hz = float(tone_hz)
        self.tone_duration_sec = float(tone_duration_sec)
        self.tone_amplitude = float(tone_amplitude)

        self._lock = threading.Lock()
        self._talkback_active = False
        self._tone_samples_total = int(round(self.sample_rate * self.tone_duration_sec))
        self._tone_samples_emitted = 0
        self._fade_samples = min(
            int(round(self.sample_rate * 0.005)),  # 5ms click-free fade
            max(1, self._tone_samples_total // 4),
        )

    @property
    def talkback_active(self) -> bool:
        with self._lock:
            return self._talkback_active

    def set_talkback(self, active: bool) -> None:
        """Activate or deactivate momentary Talkback mode."""
        with self._lock:
            if active and not self._talkback_active:
                self._tone_samples_emitted = 0
            self._talkback_active = bool(active)

    def process_block(self, input_frames: np.ndarray) -> np.ndarray:
        """Process a block of normalized float64 samples in range [-1.0, 1.0].

        Args:
            input_frames: Shape (num_frames, hw_channels) float64 array.

        Returns:
            Output array of shape (num_frames, output_channels) in float64 [-1.0, 1.0].
        """
        if input_frames.ndim == 1:
            input_frames = input_frames[:, np.newaxis]

        num_frames, hw_channels = input_frames.shape
        if num_frames == 0:
            return np.zeros((0, self.output_channels), dtype=np.float64)

        mic_ch_idx = min(self.input_channel, hw_channels - 1)
        mic_signal = input_frames[:, mic_ch_idx]

        if not self.studio_mode:
            if self.output_channels == 1:
                return mic_signal[:, np.newaxis].copy()
            # Stereo Normal Mode
            if hw_channels >= 2:
                return input_frames[:, :2].copy()
            return np.column_stack((mic_signal, mic_signal))

        # Studio Mode: always Stereo (Left = Take audio, Right = Talkback tone + notes)
        out = np.zeros((num_frames, 2), dtype=np.float64)

        with self._lock:
            tb_active = self._talkback_active
            tone_pos = self._tone_samples_emitted
            tone_total = self._tone_samples_total

            if not tb_active:
                # Normal Studio state: Mic on Left (0), Silence on Right (1)
                out[:, 0] = mic_signal
                return out

            # Talkback active: Left (0) is muted (zeros).
            # Right (1) gets the 0.5s tone first, then mic_signal.
            tone_remaining = max(0, tone_total - tone_pos)
            tone_count = min(num_frames, tone_remaining)

            if tone_count > 0:
                sample_indices = np.arange(
                    tone_pos, tone_pos + tone_count, dtype=np.float64
                )
                sine_wave = self.tone_amplitude * np.sin(
                    2.0 * math.pi * self.tone_hz * sample_indices / self.sample_rate
                )
                # Apply smooth raised-cosine fade-in and fade-out to avoid clicks
                envelope = np.ones(tone_count, dtype=np.float64)
                if self._fade_samples > 0:
                    fade_in_mask = sample_indices < self._fade_samples
                    if np.any(fade_in_mask):
                        envelope[fade_in_mask] = 0.5 * (
                            1.0
                            - np.cos(
                                math.pi * sample_indices[fade_in_mask] / self._fade_samples
                            )
                        )
                    fade_out_start = tone_total - self._fade_samples
                    fade_out_mask = sample_indices >= fade_out_start
                    if np.any(fade_out_mask):
                        rel = tone_total - sample_indices[fade_out_mask]
                        envelope[fade_out_mask] = 0.5 * (
                            1.0 - np.cos(math.pi * rel / self._fade_samples)
                        )

                out[:tone_count, 1] = sine_wave * envelope
                self._tone_samples_emitted += tone_count

            if tone_count < num_frames:
                out[tone_count:, 1] = mic_signal[tone_count:]

        return out


class WavFileWriter:
    """Streaming RIFF WAVE writer supporting 16-bit, 24-bit, and 32-bit linear PCM."""

    def __init__(
        self,
        filepath: Path,
        sample_rate: int,
        channels: int,
        bit_depth: int,
    ) -> None:
        if bit_depth not in (16, 24, 32):
            raise ValueError(f"Unsupported bit depth: {bit_depth}")
        self.filepath = Path(filepath)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.bit_depth = int(bit_depth)
        self.bytes_per_sample = self.bit_depth // 8
        self.block_align = self.channels * self.bytes_per_sample
        self.byte_rate = self.sample_rate * self.block_align
        self.data_bytes_written = 0
        self.frames_written = 0

        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.filepath.open("wb+")
        self._write_header(0)
        try:
            os.chmod(self.filepath, 0o666)
        except OSError:
            pass

    def _write_header(self, data_size: int) -> None:
        riff_size = 36 + data_size
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            min(riff_size, 0xFFFFFFFF),
            b"WAVE",
            b"fmt ",
            16,  # PCM fmt chunk size
            1,   # AudioFormat = 1 (Linear PCM)
            self.channels,
            self.sample_rate,
            self.byte_rate,
            self.block_align,
            self.bit_depth,
            b"data",
            min(data_size, 0xFFFFFFFF),
        )
        self._file.seek(0)
        self._file.write(header)

    def write_float_frames(self, frames: np.ndarray) -> None:
        """Convert normalized float64 frames in [-1.0, 1.0] to target PCM bytes and write."""
        if frames.size == 0:
            return
        clipped = np.clip(frames, -1.0, 1.0)
        num_frames = clipped.shape[0]
        flat = clipped.reshape(-1)

        if self.bit_depth == 16:
            pcm16 = np.round(flat * 32767.0).astype("<i2")
            raw_bytes = pcm16.tobytes()
        elif self.bit_depth == 24:
            # 24-bit signed integer packed into 3 bytes little-endian (S24_3LE)
            pcm32 = np.round(flat * 8388607.0).astype("<i4")
            raw_u8 = pcm32.view(np.uint8).reshape(-1, 4)[:, :3]
            raw_bytes = raw_u8.tobytes()
        else:  # 32-bit
            pcm32 = np.round(flat * 2147483647.0).astype("<i4")
            raw_bytes = pcm32.tobytes()

        self._file.seek(0, os.SEEK_END)
        self._file.write(raw_bytes)
        self.data_bytes_written += len(raw_bytes)
        self.frames_written += num_frames

    def flush_header(self) -> None:
        """Update RIFF and data chunk sizes on disk."""
        if self._file and not self._file.closed:
            self._write_header(self.data_bytes_written)
            self._file.flush()

    def close(self) -> None:
        if self._file and not self._file.closed:
            try:
                self.flush_header()
            finally:
                self._file.close()
            try:
                os.chmod(self.filepath, 0o666)
            except OSError:
                pass


def read_wav_as_float64(filepath: Path) -> Tuple[np.ndarray, int, int, int]:
    """Read a 16-bit, 24-bit, or 32-bit PCM WAV file into a float64 array (frames, channels)."""
    with Path(filepath).open("rb") as f:
        riff = f.read(12)
        if len(riff) < 12 or riff[0:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise ValueError(f"Invalid WAV file: {filepath}")

        channels = 1
        sample_rate = 48000
        bit_depth = 16
        audio_format = 1
        data_bytes = b""

        while True:
            chunk_hdr = f.read(8)
            if len(chunk_hdr) < 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_hdr)
            if chunk_id == b"fmt ":
                fmt_data = f.read(chunk_size)
                audio_format, channels, sample_rate, _, _, bit_depth = struct.unpack(
                    "<HHIIHH", fmt_data[:16]
                )
            elif chunk_id == b"data":
                data_bytes = f.read(chunk_size)
                break
            else:
                f.seek(chunk_size, os.SEEK_CUR)
            if chunk_size % 2 == 1:
                f.seek(1, os.SEEK_CUR)

    if not data_bytes:
        return np.zeros((0, channels), dtype=np.float64), sample_rate, channels, bit_depth

    if bit_depth == 16:
        samples = np.frombuffer(data_bytes, dtype="<i2").astype(np.float64) / 32768.0
    elif bit_depth == 24:
        usable = (len(data_bytes) // 3) * 3
        raw_u8 = np.frombuffer(data_bytes[:usable], dtype=np.uint8).reshape(-1, 3)
        padded = np.zeros((raw_u8.shape[0], 4), dtype=np.uint8)
        padded[:, 1:4] = raw_u8  # Shift into upper 24 bits for automatic sign extension
        samples = padded.view("<i4").reshape(-1).astype(np.float64) / 2147483648.0
    elif bit_depth == 32 and audio_format == 3:  # IEEE Float
        samples = np.frombuffer(data_bytes, dtype="<f4").astype(np.float64)
    elif bit_depth == 32:
        samples = np.frombuffer(data_bytes, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV bit depth: {bit_depth}")

    num_frames = len(samples) // channels
    frames = samples[: num_frames * channels].reshape(num_frames, channels)
    return frames, sample_rate, channels, bit_depth


class AlsaLibWrapper:
    """Direct ctypes wrapper around libasound.so.2 for hardware probing and PCM streaming."""

    def __init__(self) -> None:
        self.lib: Optional[ctypes.CDLL] = None
        lib_name = ctypes.util.find_library("asound") or "libasound.so.2"
        try:
            self.lib = ctypes.CDLL(lib_name)
            self._setup_signatures()
        except OSError:
            self.lib = None

    @property
    def available(self) -> bool:
        return self.lib is not None and Path("/proc/asound").exists()

    def _setup_signatures(self) -> None:
        assert self.lib is not None
        c_void_p = ctypes.c_void_p
        c_char_p = ctypes.c_char_p
        c_int = ctypes.c_int
        c_uint = ctypes.c_uint
        c_ulong = ctypes.c_ulong
        c_long = ctypes.c_long

        self.lib.snd_pcm_open.argtypes = [ctypes.POINTER(c_void_p), c_char_p, c_int, c_int]
        self.lib.snd_pcm_open.restype = c_int

        self.lib.snd_pcm_close.argtypes = [c_void_p]
        self.lib.snd_pcm_close.restype = c_int

        self.lib.snd_pcm_hw_params_malloc.argtypes = [ctypes.POINTER(c_void_p)]
        self.lib.snd_pcm_hw_params_malloc.restype = c_int

        self.lib.snd_pcm_hw_params_free.argtypes = [c_void_p]
        self.lib.snd_pcm_hw_params_free.restype = None

        self.lib.snd_pcm_hw_params_any.argtypes = [c_void_p, c_void_p]
        self.lib.snd_pcm_hw_params_any.restype = c_int

        self.lib.snd_pcm_hw_params_test_format.argtypes = [c_void_p, c_void_p, c_int]
        self.lib.snd_pcm_hw_params_test_format.restype = c_int

        self.lib.snd_pcm_hw_params_test_rate.argtypes = [c_void_p, c_void_p, c_uint, c_int]
        self.lib.snd_pcm_hw_params_test_rate.restype = c_int

        self.lib.snd_pcm_hw_params_test_channels.argtypes = [c_void_p, c_void_p, c_uint]
        self.lib.snd_pcm_hw_params_test_channels.restype = c_int

        self.lib.snd_pcm_hw_params_set_access.argtypes = [c_void_p, c_void_p, c_int]
        self.lib.snd_pcm_hw_params_set_access.restype = c_int

        self.lib.snd_pcm_hw_params_set_format.argtypes = [c_void_p, c_void_p, c_int]
        self.lib.snd_pcm_hw_params_set_format.restype = c_int

        self.lib.snd_pcm_hw_params_set_channels.argtypes = [c_void_p, c_void_p, c_uint]
        self.lib.snd_pcm_hw_params_set_channels.restype = c_int

        self.lib.snd_pcm_hw_params_set_rate.argtypes = [c_void_p, c_void_p, c_uint, c_int]
        self.lib.snd_pcm_hw_params_set_rate.restype = c_int

        self.lib.snd_pcm_hw_params.argtypes = [c_void_p, c_void_p]
        self.lib.snd_pcm_hw_params.restype = c_int

        self.lib.snd_pcm_prepare.argtypes = [c_void_p]
        self.lib.snd_pcm_prepare.restype = c_int

        self.lib.snd_pcm_recover.argtypes = [c_void_p, c_int, c_int]
        self.lib.snd_pcm_recover.restype = c_int

        self.lib.snd_pcm_readi.argtypes = [c_void_p, c_void_p, c_ulong]
        self.lib.snd_pcm_readi.restype = c_long

        self.lib.snd_pcm_writei.argtypes = [c_void_p, c_void_p, c_ulong]
        self.lib.snd_pcm_writei.restype = c_long

        self.lib.snd_pcm_drain.argtypes = [c_void_p]
        self.lib.snd_pcm_drain.restype = c_int

        self.lib.snd_pcm_drop.argtypes = [c_void_p]
        self.lib.snd_pcm_drop.restype = c_int

    def probe_hw_params(
        self, device_id: str, stream_dir: int
    ) -> Optional[Tuple[List[int], Dict[int, int], int, int]]:
        """Query exact supported rates, bit depths, and channel bounds from ALSA hw device."""
        if not self.lib:
            return None

        pcm = ctypes.c_void_p()
        err = self.lib.snd_pcm_open(
            ctypes.byref(pcm),
            device_id.encode("ascii"),
            stream_dir,
            SND_PCM_NONBLOCK,
        )
        if err < 0 or not pcm:
            return None

        hw_params = ctypes.c_void_p()
        if self.lib.snd_pcm_hw_params_malloc(ctypes.byref(hw_params)) < 0:
            self.lib.snd_pcm_close(pcm)
            return None

        try:
            if self.lib.snd_pcm_hw_params_any(pcm, hw_params) < 0:
                return None

            format_map: Dict[int, int] = {}
            if self.lib.snd_pcm_hw_params_test_format(pcm, hw_params, SND_PCM_FORMAT_S16_LE) == 0:
                format_map[16] = SND_PCM_FORMAT_S16_LE

            # Check 24-bit formats (prefer packed S24_3LE, then S24_LE)
            if self.lib.snd_pcm_hw_params_test_format(pcm, hw_params, SND_PCM_FORMAT_S24_3LE) == 0:
                format_map[24] = SND_PCM_FORMAT_S24_3LE
            elif self.lib.snd_pcm_hw_params_test_format(pcm, hw_params, SND_PCM_FORMAT_S24_LE) == 0:
                format_map[24] = SND_PCM_FORMAT_S24_LE

            # Check 32-bit formats
            if self.lib.snd_pcm_hw_params_test_format(pcm, hw_params, SND_PCM_FORMAT_S32_LE) == 0:
                format_map[32] = SND_PCM_FORMAT_S32_LE
            elif self.lib.snd_pcm_hw_params_test_format(pcm, hw_params, SND_PCM_FORMAT_FLOAT_LE) == 0:
                format_map[32] = SND_PCM_FORMAT_FLOAT_LE

            rates: List[int] = []
            for rate in STANDARD_SAMPLE_RATES:
                if self.lib.snd_pcm_hw_params_test_rate(pcm, hw_params, rate, 0) == 0:
                    rates.append(rate)

            supported_ch: List[int] = []
            for ch in range(1, 33):
                if self.lib.snd_pcm_hw_params_test_channels(pcm, hw_params, ch) == 0:
                    supported_ch.append(ch)

            min_ch = min(supported_ch) if supported_ch else 1
            max_ch = max(supported_ch) if supported_ch else 2
            return (rates, format_map, min_ch, max_ch)
        finally:
            self.lib.snd_pcm_hw_params_free(hw_params)
            self.lib.snd_pcm_close(pcm)


def parse_proc_usb_stream0(stream_text: str) -> Tuple[List[int], Dict[int, int], int, int]:
    """Parse /proc/asound/cardX/stream0 for USB Audio Class hardware capabilities.

    Works even while the ALSA device is open or busy.
    """
    rates: set[int] = set()
    format_map: Dict[int, int] = {}
    channels_found: set[int] = set()

    # Check Capture section if present, else parse full descriptor
    capture_sec = stream_text
    if "Capture:" in stream_text:
        capture_sec = stream_text.split("Capture:", 1)[1]
        if "Playback:" in capture_sec:
            capture_sec = capture_sec.split("Playback:", 1)[0]

    for line in capture_sec.splitlines():
        stripped = line.strip()
        if stripped.startswith("Channels:"):
            for m in re.findall(r"\d+", stripped):
                channels_found.add(int(m))
        elif stripped.startswith("Format:"):
            if "S16_LE" in stripped:
                format_map.setdefault(16, SND_PCM_FORMAT_S16_LE)
            if "S24_3LE" in stripped:
                format_map[24] = SND_PCM_FORMAT_S24_3LE
            elif "S24_LE" in stripped:
                format_map.setdefault(24, SND_PCM_FORMAT_S24_LE)
            if "S32_LE" in stripped:
                format_map[32] = SND_PCM_FORMAT_S32_LE
            elif "FLOAT_LE" in stripped:
                format_map.setdefault(32, SND_PCM_FORMAT_FLOAT_LE)
        elif stripped.startswith("Rates:"):
            if "continuous" in stripped.lower():
                nums = [int(x) for x in re.findall(r"\d+", stripped)]
                if len(nums) >= 2:
                    low, high = nums[0], nums[1]
                    for r in STANDARD_SAMPLE_RATES:
                        if low <= r <= high:
                            rates.add(r)
            else:
                for m in re.findall(r"\d+", stripped):
                    val = int(m)
                    if val in STANDARD_SAMPLE_RATES:
                        rates.add(val)

    min_ch = min(channels_found) if channels_found else 1
    max_ch = max(channels_found) if channels_found else 2
    return (sorted(rates), format_map, min_ch, max_ch)


class AudioEngine:
    """Manages audio hardware discovery, WAV recording, Talkback routing, and reference playback."""

    def __init__(self, simulate_if_empty: bool = False) -> None:
        self.alsa = AlsaLibWrapper()
        self.simulate_if_empty = simulate_if_empty or (
            os.environ.get("ALPINE_RECORDER_SIMULATE_AUDIO", "0") == "1"
        )

        self._record_lock = threading.Lock()
        self._is_recording = False
        self._stop_record_event = threading.Event()
        self._record_thread: Optional[threading.Thread] = None
        self._record_start_monotonic: float = 0.0
        self._current_processor: Optional[StudioTalkbackProcessor] = None
        self._current_recording_path: Optional[Path] = None
        self._peak_level: float = 0.0

        self._play_lock = threading.Lock()
        self._is_playing = False
        self._is_paused = False
        self._stop_play_event = threading.Event()
        self._play_thread: Optional[threading.Thread] = None
        self._play_position_sec: float = 0.0
        self._play_duration_sec: float = 0.0
        self._playing_filepath: Optional[Path] = None

    # -------------------------------------------------------------------------
    # Hardware Enumeration
    # -------------------------------------------------------------------------
    def enumerate_devices(self) -> List[AudioDeviceCapabilities]:
        """Discover available audio devices and query their supported sample rates & bit depths."""
        devices: List[AudioDeviceCapabilities] = []

        proc_asound = Path("/proc/asound")
        if proc_asound.is_dir():
            devices = self._enumerate_alsa_proc(proc_asound)

        if not devices and self.simulate_if_empty:
            devices = [
                AudioDeviceCapabilities(
                    device_id="sim:usb0",
                    card_index=1,
                    device_index=0,
                    name="Simulated USB Studio Audio Interface",
                    description="USB Audio Class 2.0 Interface",
                    is_usb=True,
                    is_capture=True,
                    is_playback=True,
                    supported_sample_rates=[44100, 48000, 88200, 96000, 192000],
                    supported_bit_depths=[16, 24, 32],
                    min_channels=1,
                    max_channels=2,
                    alsa_format_map={
                        16: SND_PCM_FORMAT_S16_LE,
                        24: SND_PCM_FORMAT_S24_3LE,
                        32: SND_PCM_FORMAT_S32_LE,
                    },
                )
            ]

        # Sort USB audio interfaces first
        devices.sort(key=lambda d: (not d.is_usb, d.card_index, d.device_index))
        return devices

    def get_capture_devices(self) -> List[AudioDeviceCapabilities]:
        return [d for d in self.enumerate_devices() if d.is_capture]

    def get_playback_devices(self) -> List[AudioDeviceCapabilities]:
        return [d for d in self.enumerate_devices() if d.is_playback]

    def _enumerate_alsa_proc(self, proc_asound: Path) -> List[AudioDeviceCapabilities]:
        cards_file = proc_asound / "cards"
        pcm_file = proc_asound / "pcm"
        if not cards_file.is_file() or not pcm_file.is_file():
            return []

        card_names: Dict[int, Tuple[str, str, bool]] = {}
        try:
            lines = cards_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            idx = 0
            while idx < len(lines):
                line = lines[idx].strip()
                m = re.match(r"^(\d+)\s+\[([^\]]+)\]:\s*(.+)$", line)
                if m:
                    c_idx = int(m.group(1))
                    short_id = m.group(2).strip()
                    desc = m.group(3).strip()
                    detail = lines[idx + 1].strip() if (idx + 1) < len(lines) else ""
                    card_dir = proc_asound / f"card{c_idx}"
                    is_usb = (
                        (card_dir / "usbbus").exists()
                        or (card_dir / "usbid").exists()
                        or (card_dir / "stream0").exists()
                        or "usb" in desc.lower()
                        or "usb" in detail.lower()
                    )
                    card_names[c_idx] = (desc or short_id, detail, is_usb)
                    idx += 2
                else:
                    idx += 1
        except OSError:
            return []

        devices: List[AudioDeviceCapabilities] = []
        try:
            pcm_lines = pcm_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in pcm_lines:
                # Format: 01-00: USB Audio : USB Audio : playback 1 : capture 1
                m = re.match(r"^(\d+)-(\d+):\s*([^:]+):(.*)$", line.strip())
                if not m:
                    continue
                c_idx = int(m.group(1))
                d_idx = int(m.group(2))
                pcm_name = m.group(3).strip()
                rest = m.group(4).lower()
                is_capture = "capture" in rest
                is_playback = "playback" in rest
                if not is_capture and not is_playback:
                    continue

                card_title, card_detail, is_usb = card_names.get(
                    c_idx, (pcm_name, "", "usb" in pcm_name.lower())
                )
                dev_id = f"hw:{c_idx},{d_idx}"

                rates: List[int] = []
                fmt_map: Dict[int, int] = {}
                min_ch, max_ch = 1, 2

                # 1. Try ALSA libasound hardware probe
                stream_dir = (
                    SND_PCM_STREAM_CAPTURE if is_capture else SND_PCM_STREAM_PLAYBACK
                )
                probed = self.alsa.probe_hw_params(dev_id, stream_dir)
                if probed:
                    rates, fmt_map, min_ch, max_ch = probed

                # 2. Supplement or fallback with /proc/asound/cardX/stream0 for USB devices
                stream0_path = proc_asound / f"card{c_idx}" / f"stream{d_idx}"
                if not stream0_path.is_file():
                    stream0_path = proc_asound / f"card{c_idx}" / "stream0"
                if stream0_path.is_file():
                    try:
                        s_rates, s_fmts, s_min_ch, s_max_ch = parse_proc_usb_stream0(
                            stream0_path.read_text(encoding="utf-8", errors="ignore")
                        )
                        if s_rates:
                            rates = sorted(set(rates) | set(s_rates))
                        if s_fmts:
                            for k, v in s_fmts.items():
                                fmt_map.setdefault(k, v)
                        if not probed:
                            min_ch, max_ch = s_min_ch, s_max_ch
                        else:
                            min_ch = min(min_ch, s_min_ch)
                            max_ch = max(max_ch, s_max_ch)
                    except OSError:
                        pass

                if not rates:
                    rates = [44100, 48000]
                if not fmt_map:
                    fmt_map = {16: SND_PCM_FORMAT_S16_LE}

                bit_depths = sorted(fmt_map.keys())
                devices.append(
                    AudioDeviceCapabilities(
                        device_id=dev_id,
                        card_index=c_idx,
                        device_index=d_idx,
                        name=card_title,
                        description=card_detail or pcm_name,
                        is_usb=is_usb,
                        is_capture=is_capture,
                        is_playback=is_playback,
                        supported_sample_rates=sorted(rates),
                        supported_bit_depths=bit_depths,
                        min_channels=min_ch,
                        max_channels=max_ch,
                        alsa_format_map=fmt_map,
                    )
                )
        except OSError:
            pass

        return devices

    # -------------------------------------------------------------------------
    # Recording State & Control
    # -------------------------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        with self._record_lock:
            return self._is_recording

    @property
    def elapsed_seconds(self) -> float:
        with self._record_lock:
            if not self._is_recording:
                return 0.0
            return max(0.0, time.monotonic() - self._record_start_monotonic)

    @property
    def peak_level(self) -> float:
        return self._peak_level

    @property
    def talkback_active(self) -> bool:
        proc = self._current_processor
        return proc.talkback_active if proc else False

    def set_talkback(self, active: bool) -> None:
        """Set momentary Talkback state during recording."""
        proc = self._current_processor
        if proc is not None:
            proc.set_talkback(active)

    def start_recording(
        self,
        device: AudioDeviceCapabilities,
        output_path: Path,
        sample_rate: int,
        bit_depth: int,
        studio_mode: bool,
        channels: int,
        input_channel: int = 0,
        tone_hz: float = 1000.0,
        tone_duration_sec: float = 0.5,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Start recording from the selected hardware device to a WAV file."""
        self.stop_playback()

        with self._record_lock:
            if self._is_recording:
                return

            output_channels = 2 if studio_mode else max(1, min(2, channels))
            processor = StudioTalkbackProcessor(
                sample_rate=sample_rate,
                studio_mode=studio_mode,
                output_channels=output_channels,
                input_channel=input_channel,
                tone_hz=tone_hz,
                tone_duration_sec=tone_duration_sec,
            )
            self._current_processor = processor
            self._current_recording_path = output_path
            self._stop_record_event.clear()
            self._is_recording = True
            self._record_start_monotonic = time.monotonic()
            self._peak_level = 0.0

            self._record_thread = threading.Thread(
                target=self._record_worker,
                args=(
                    device,
                    output_path,
                    sample_rate,
                    bit_depth,
                    processor,
                    on_error,
                ),
                daemon=True,
            )
            self._record_thread.start()

    def stop_recording(self) -> Optional[Path]:
        """Stop active recording and finalize the WAV file on disk."""
        with self._record_lock:
            if not self._is_recording:
                return None
            self._stop_record_event.set()
            thread = self._record_thread
            recorded_path = self._current_recording_path

        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

        with self._record_lock:
            self._is_recording = False
            self._current_processor = None
            self._peak_level = 0.0

        return recorded_path

    def _record_worker(
        self,
        device: AudioDeviceCapabilities,
        output_path: Path,
        sample_rate: int,
        bit_depth: int,
        processor: StudioTalkbackProcessor,
        on_error: Optional[Callable[[str], None]],
    ) -> None:
        writer: Optional[WavFileWriter] = None
        try:
            writer = WavFileWriter(
                filepath=output_path,
                sample_rate=sample_rate,
                channels=processor.output_channels,
                bit_depth=bit_depth,
            )

            if device.device_id.startswith("sim:") or not self.alsa.available:
                self._record_simulated_loop(sample_rate, processor, writer)
            else:
                self._record_alsa_loop(device, sample_rate, bit_depth, processor, writer)

        except Exception as exc:
            logger.exception("Recording error on %s: %s", device.device_id, exc)
            if on_error:
                on_error(str(exc))
        finally:
            if writer is not None:
                writer.close()
            with self._record_lock:
                self._is_recording = False
                self._peak_level = 0.0

    def _record_simulated_loop(
        self,
        sample_rate: int,
        processor: StudioTalkbackProcessor,
        writer: WavFileWriter,
    ) -> None:
        """Simulated capture loop for testing without physical ALSA hardware."""
        chunk_frames = max(256, sample_rate // 25)  # 40ms blocks
        chunk_duration = chunk_frames / sample_rate
        phase = 0.0
        last_header_flush = time.monotonic()

        while not self._stop_record_event.is_set():
            t0 = time.monotonic()
            t_vec = (np.arange(chunk_frames, dtype=np.float64) + phase) / sample_rate
            phase += chunk_frames
            # Gentle 440 Hz simulated mic signal at -18 dBFS
            sim_mic = 0.125 * np.sin(2.0 * math.pi * 440.0 * t_vec)
            in_block = np.column_stack((sim_mic, sim_mic))

            out_block = processor.process_block(in_block)
            self._peak_level = float(np.max(np.abs(out_block))) if out_block.size else 0.0
            writer.write_float_frames(out_block)

            if t0 - last_header_flush >= 2.0:
                writer.flush_header()
                last_header_flush = t0

            elapsed = time.monotonic() - t0
            sleep_for = max(0.001, chunk_duration - elapsed)
            self._stop_record_event.wait(sleep_for)

    def _record_alsa_loop(
        self,
        device: AudioDeviceCapabilities,
        sample_rate: int,
        bit_depth: int,
        processor: StudioTalkbackProcessor,
        writer: WavFileWriter,
    ) -> None:
        """Capture audio directly from ALSA hardware PCM device."""
        assert self.alsa.lib is not None
        lib = self.alsa.lib

        pcm = ctypes.c_void_p()
        err = lib.snd_pcm_open(
            ctypes.byref(pcm),
            device.device_id.encode("ascii"),
            SND_PCM_STREAM_CAPTURE,
            0,  # Blocking mode
        )
        if err < 0 or not pcm:
            raise RuntimeError(
                f"Cannot open ALSA capture device {device.device_id} (err={err})"
            )

        hw_params = ctypes.c_void_p()
        lib.snd_pcm_hw_params_malloc(ctypes.byref(hw_params))
        try:
            alsa_fmt = device.alsa_format_map.get(bit_depth, SND_PCM_FORMAT_S16_LE)
            hw_channels = max(
                device.min_channels,
                min(device.max_channels, max(2, processor.input_channel + 1)),
            )

            if lib.snd_pcm_hw_params_any(pcm, hw_params) < 0:
                raise RuntimeError("Failed to initialize ALSA hw_params")
            if lib.snd_pcm_hw_params_set_access(pcm, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED) < 0:
                raise RuntimeError("Failed to set ALSA interleaved access")
            if lib.snd_pcm_hw_params_set_format(pcm, hw_params, alsa_fmt) < 0:
                raise RuntimeError(f"Failed to set ALSA format for {bit_depth}-bit")
            if lib.snd_pcm_hw_params_set_channels(pcm, hw_params, hw_channels) < 0:
                hw_channels = device.min_channels
                if lib.snd_pcm_hw_params_set_channels(pcm, hw_params, hw_channels) < 0:
                    raise RuntimeError("Failed to set ALSA channel count")
            if lib.snd_pcm_hw_params_set_rate(pcm, hw_params, sample_rate, 0) < 0:
                raise RuntimeError(f"Failed to set ALSA sample rate {sample_rate} Hz")
            if lib.snd_pcm_hw_params(pcm, hw_params) < 0:
                raise RuntimeError("Failed to apply ALSA hardware parameters")
        finally:
            lib.snd_pcm_hw_params_free(hw_params)

        lib.snd_pcm_prepare(pcm)

        bytes_per_sample = 2
        if alsa_fmt == SND_PCM_FORMAT_S24_3LE:
            bytes_per_sample = 3
        elif alsa_fmt in (SND_PCM_FORMAT_S24_LE, SND_PCM_FORMAT_S32_LE, SND_PCM_FORMAT_FLOAT_LE):
            bytes_per_sample = 4

        period_frames = max(256, min(4096, sample_rate // 25))
        frame_bytes = hw_channels * bytes_per_sample
        buf = ctypes.create_string_buffer(period_frames * frame_bytes)
        last_header_flush = time.monotonic()

        try:
            while not self._stop_record_event.is_set():
                frames_read = lib.snd_pcm_readi(pcm, buf, period_frames)
                if frames_read < 0:
                    recovered = lib.snd_pcm_recover(pcm, int(frames_read), 1)
                    if recovered < 0:
                        raise RuntimeError(f"ALSA capture unrecoverable error: {frames_read}")
                    continue
                if frames_read == 0:
                    continue

                raw_slice = buf.raw[: frames_read * frame_bytes]
                in_float = self._decode_alsa_bytes(
                    raw_slice, int(frames_read), hw_channels, alsa_fmt
                )
                out_float = processor.process_block(in_float)
                self._peak_level = (
                    float(np.max(np.abs(out_float))) if out_float.size else 0.0
                )
                writer.write_float_frames(out_float)

                now = time.monotonic()
                if now - last_header_flush >= 2.0:
                    writer.flush_header()
                    last_header_flush = now
        finally:
            lib.snd_pcm_drop(pcm)
            lib.snd_pcm_close(pcm)

    @staticmethod
    def _decode_alsa_bytes(
        raw_bytes: bytes, num_frames: int, channels: int, alsa_fmt: int
    ) -> np.ndarray:
        """Convert raw ALSA interleaved buffer into normalized float64 [-1.0, 1.0]."""
        if alsa_fmt == SND_PCM_FORMAT_S16_LE:
            arr = np.frombuffer(raw_bytes, dtype="<i2").astype(np.float64) / 32768.0
        elif alsa_fmt == SND_PCM_FORMAT_S24_3LE:
            raw_u8 = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(-1, 3)
            padded = np.zeros((raw_u8.shape[0], 4), dtype=np.uint8)
            padded[:, 1:4] = raw_u8
            arr = padded.view("<i4").reshape(-1).astype(np.float64) / 2147483648.0
        elif alsa_fmt == SND_PCM_FORMAT_S24_LE:
            raw_i32 = np.frombuffer(raw_bytes, dtype="<i4")
            # Sign-extend 24-bit value stored in lower 24 bits of 32-bit word
            shifted = (raw_i32 << 8).astype("<i4")
            arr = shifted.astype(np.float64) / 2147483648.0
        elif alsa_fmt == SND_PCM_FORMAT_FLOAT_LE:
            arr = np.frombuffer(raw_bytes, dtype="<f4").astype(np.float64)
        else:  # SND_PCM_FORMAT_S32_LE
            arr = np.frombuffer(raw_bytes, dtype="<i4").astype(np.float64) / 2147483648.0

        return arr.reshape(num_frames, channels)

    # -------------------------------------------------------------------------
    # Reference Playback Control
    # -------------------------------------------------------------------------
    @property
    def is_playing(self) -> bool:
        with self._play_lock:
            return self._is_playing

    @property
    def is_paused(self) -> bool:
        with self._play_lock:
            return self._is_paused

    @property
    def play_position_sec(self) -> float:
        return self._play_position_sec

    @property
    def play_duration_sec(self) -> float:
        return self._play_duration_sec

    @property
    def playing_filepath(self) -> Optional[Path]:
        return self._playing_filepath

    def start_playback(
        self,
        filepath: Path,
        output_device: Optional[AudioDeviceCapabilities],
        on_finished: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Start reference playback of a recorded WAV file."""
        if self.is_recording:
            return

        self.stop_playback()

        with self._play_lock:
            self._stop_play_event.clear()
            self._is_playing = True
            self._is_paused = False
            self._playing_filepath = filepath
            self._play_position_sec = 0.0

            self._play_thread = threading.Thread(
                target=self._playback_worker,
                args=(filepath, output_device, on_finished, on_error),
                daemon=True,
            )
            self._play_thread.start()

    def toggle_pause_playback(self) -> bool:
        """Toggle pause state during reference playback. Returns new paused state."""
        with self._play_lock:
            if not self._is_playing:
                return False
            self._is_paused = not self._is_paused
            return self._is_paused

    def stop_playback(self) -> None:
        """Stop active reference playback."""
        with self._play_lock:
            if not self._is_playing:
                return
            self._stop_play_event.set()
            self._is_paused = False
            thread = self._play_thread

        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

        with self._play_lock:
            self._is_playing = False
            self._is_paused = False
            self._play_position_sec = 0.0
            self._playing_filepath = None

    def _playback_worker(
        self,
        filepath: Path,
        output_device: Optional[AudioDeviceCapabilities],
        on_finished: Optional[Callable[[], None]],
        on_error: Optional[Callable[[str], None]],
    ) -> None:
        try:
            frames, sr, channels, _ = read_wav_as_float64(filepath)
            total_frames = frames.shape[0]
            self._play_duration_sec = (total_frames / sr) if sr > 0 else 0.0

            if (
                output_device is None
                or output_device.device_id.startswith("sim:")
                or not self.alsa.available
            ):
                self._playback_simulated_loop(frames, sr)
            else:
                self._playback_alsa_loop(frames, sr, channels, output_device)
        except Exception as exc:
            logger.exception("Playback error on %s: %s", filepath, exc)
            if on_error:
                on_error(str(exc))
        finally:
            with self._play_lock:
                self._is_playing = False
                self._is_paused = False
            if on_finished:
                on_finished()

    def _playback_simulated_loop(self, frames: np.ndarray, sample_rate: int) -> None:
        total_frames = frames.shape[0]
        chunk_frames = max(256, sample_rate // 25)
        chunk_sec = chunk_frames / sample_rate
        cursor = 0

        while cursor < total_frames and not self._stop_play_event.is_set():
            if self._is_paused:
                self._stop_play_event.wait(0.05)
                continue
            cursor = min(total_frames, cursor + chunk_frames)
            self._play_position_sec = cursor / sample_rate
            self._stop_play_event.wait(chunk_sec)

    def _playback_alsa_loop(
        self,
        frames: np.ndarray,
        sample_rate: int,
        channels: int,
        output_device: AudioDeviceCapabilities,
    ) -> None:
        assert self.alsa.lib is not None
        lib = self.alsa.lib

        # Use plughw: for playback so ALSA automatically adapts sample rate/channels if needed
        dev_str = output_device.device_id
        if dev_str.startswith("hw:"):
            dev_str = "plug" + dev_str

        pcm = ctypes.c_void_p()
        err = lib.snd_pcm_open(
            ctypes.byref(pcm),
            dev_str.encode("ascii"),
            SND_PCM_STREAM_PLAYBACK,
            0,
        )
        if err < 0 or not pcm:
            # Fallback to raw device id
            err = lib.snd_pcm_open(
                ctypes.byref(pcm),
                output_device.device_id.encode("ascii"),
                SND_PCM_STREAM_PLAYBACK,
                0,
            )
            if err < 0 or not pcm:
                raise RuntimeError(f"Cannot open playback device {output_device.device_id}")

        hw_params = ctypes.c_void_p()
        lib.snd_pcm_hw_params_malloc(ctypes.byref(hw_params))
        out_ch = max(1, min(2, channels))
        try:
            lib.snd_pcm_hw_params_any(pcm, hw_params)
            lib.snd_pcm_hw_params_set_access(pcm, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED)
            lib.snd_pcm_hw_params_set_format(pcm, hw_params, SND_PCM_FORMAT_S16_LE)
            if lib.snd_pcm_hw_params_set_channels(pcm, hw_params, out_ch) < 0:
                out_ch = 2
                lib.snd_pcm_hw_params_set_channels(pcm, hw_params, out_ch)
            lib.snd_pcm_hw_params_set_rate(pcm, hw_params, sample_rate, 0)
            if lib.snd_pcm_hw_params(pcm, hw_params) < 0:
                raise RuntimeError("Failed to configure ALSA playback device")
        finally:
            lib.snd_pcm_hw_params_free(hw_params)

        lib.snd_pcm_prepare(pcm)

        if frames.shape[1] == 1 and out_ch == 2:
            frames = np.column_stack((frames[:, 0], frames[:, 0]))
        elif frames.shape[1] >= 2 and out_ch == 1:
            frames = frames[:, :1]

        pcm16 = np.round(np.clip(frames, -1.0, 1.0) * 32767.0).astype("<i2")
        total_frames = pcm16.shape[0]
        chunk_frames = max(256, sample_rate // 25)
        cursor = 0

        try:
            while cursor < total_frames and not self._stop_play_event.is_set():
                if self._is_paused:
                    self._stop_play_event.wait(0.05)
                    continue

                end = min(total_frames, cursor + chunk_frames)
                block = pcm16[cursor:end]
                raw = block.tobytes()
                written = lib.snd_pcm_writei(pcm, raw, end - cursor)
                if written < 0:
                    lib.snd_pcm_recover(pcm, int(written), 1)
                    continue
                cursor += int(written)
                self._play_position_sec = cursor / sample_rate

            if not self._stop_play_event.is_set():
                lib.snd_pcm_drain(pcm)
        finally:
            lib.snd_pcm_drop(pcm)
            lib.snd_pcm_close(pcm)
