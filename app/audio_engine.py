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

METER_FLOOR_DB: float = -60.0
CLIP_THRESHOLD_LINEAR: float = 0.988553  # Approx -0.1 dBFS


def linear_to_dbfs(level: float, floor_db: float = METER_FLOOR_DB) -> float:
    """Convert a linear amplitude [0.0, 1.0+] to dBFS clamped to [floor_db, 0.0]."""
    clamped = max(0.0, float(level))
    if clamped <= 1e-6:
        return float(floor_db)
    db = 20.0 * math.log10(min(1.0, clamped))
    return max(float(floor_db), min(0.0, db))


def dbfs_to_ratio(db: float, floor_db: float = METER_FLOOR_DB) -> float:
    """Map a dBFS value in [floor_db, 0.0] to a normalized meter ratio [0.0, 1.0]."""
    span = abs(float(floor_db))
    if span <= 0.0:
        return 0.0
    clamped_db = max(float(floor_db), min(0.0, float(db)))
    return (clamped_db - float(floor_db)) / span


@dataclass
class VuMeterReadings:
    """Real-time input signal VU meter and Peak Hold telemetry."""

    rms_level: float = 0.0              # Smoothed RMS linear [0.0, 1.0]
    peak_level: float = 0.0             # Smoothed instantaneous peak linear [0.0, 1.0]
    peak_hold_level: float = 0.0        # Maintained maximum peak linear [0.0, 1.0]
    rms_db: float = METER_FLOOR_DB      # Smoothed RMS in dBFS [-60.0, 0.0]
    peak_db: float = METER_FLOOR_DB     # Smoothed instantaneous peak in dBFS [-60.0, 0.0]
    peak_hold_db: float = METER_FLOOR_DB  # Maintained maximum peak in dBFS [-60.0, 0.0]
    channel_peaks: Tuple[float, ...] = (0.0,)       # Per-channel smoothed peak linear
    channel_rms: Tuple[float, ...] = (0.0,)         # Per-channel smoothed RMS linear
    channel_peak_holds: Tuple[float, ...] = (0.0,)  # Per-channel maintained peak hold linear
    channels: int = 1                   # Active metered channels (1 or 2)
    clipped: bool = False               # True if peak reached >= -0.1 dBFS since last reset
    active: bool = False                # True when input monitoring or recording is active


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


def resample_mono_signal(
    samples: np.ndarray, src_rate: int, dst_rate: int
) -> np.ndarray:
    """Resample a 1D float64 mono audio array from src_rate to dst_rate using linear interpolation."""
    flat = np.asarray(samples, dtype=np.float64).reshape(-1)
    if flat.size == 0 or src_rate <= 0 or dst_rate <= 0 or src_rate == dst_rate:
        return flat.copy()
    dst_len = max(1, int(round(flat.size * float(dst_rate) / float(src_rate))))
    if dst_len == flat.size:
        return flat.copy()
    src_positions = np.linspace(0.0, float(flat.size - 1), num=dst_len, dtype=np.float64)
    src_indices = np.arange(flat.size, dtype=np.float64)
    return np.interp(src_positions, src_indices, flat)


def synthesize_tone_segment(
    tone_hz: float,
    tone_amplitude: float,
    sample_rate: int,
    start_pos: int,
    count: int,
    total_samples: int,
    fade_samples: int,
) -> np.ndarray:
    """Synthesize a click-free raised-cosine faded sine wave segment."""
    if count <= 0 or total_samples <= 0 or sample_rate <= 0:
        return np.zeros(max(0, int(count)), dtype=np.float64)
    sample_indices = np.arange(
        start_pos, start_pos + count, dtype=np.float64
    )
    sine_wave = float(tone_amplitude) * np.sin(
        2.0 * math.pi * float(tone_hz) * sample_indices / float(sample_rate)
    )
    envelope = np.ones(count, dtype=np.float64)
    if fade_samples > 0:
        fade_in_mask = sample_indices < fade_samples
        if np.any(fade_in_mask):
            envelope[fade_in_mask] = 0.5 * (
                1.0
                - np.cos(
                    math.pi * sample_indices[fade_in_mask] / fade_samples
                )
            )
        fade_out_start = total_samples - fade_samples
        fade_out_mask = sample_indices >= fade_out_start
        if np.any(fade_out_mask):
            rel = total_samples - sample_indices[fade_out_mask]
            envelope[fade_out_mask] = 0.5 * (
                1.0 - np.cos(math.pi * rel / fade_samples)
            )
    return sine_wave * envelope


class StudioTalkbackProcessor:
    """Sample-accurate DSP router for Normal Mode and Studio Mode with Talkback.

    In Studio Mode:
      - Always outputs 2 channels (Stereo: Left = Ch 0, Right = Ch 1).
      - When Talkback is OFF:
          Left  = Microphone audio (from selected input_channel)
          Right = Silence (0.0), or the release marker tone (default 0.5s)
                  immediately after Talkback is concluded.
      - When Talkback is ON (momentary push-to-talk, local or remote producer):
          Left  = Silence (0.0)
          Right = First `tone_duration_sec` seconds (default 1.0s) is a sine
                  marker tone (e.g. 1000 Hz or 440 Hz), followed immediately
                  by the mix of the Local Talent Microphone and the Remote
                  Producer's Talkback audio on the Right channel until
                  Talkback is released.
    """

    def __init__(
        self,
        sample_rate: int,
        studio_mode: bool = True,
        output_channels: int = 2,
        input_channel: int = 0,
        tone_hz: float = 1000.0,
        tone_duration_sec: float = 1.0,
        release_tone_duration_sec: float = 0.5,
        tone_amplitude: float = 0.5,  # -6 dBFS
    ) -> None:
        self.sample_rate = max(8000, int(sample_rate))
        self.studio_mode = bool(studio_mode)
        self.output_channels = 2 if self.studio_mode else max(1, min(2, int(output_channels)))
        self.input_channel = max(0, int(input_channel))
        self.tone_hz = float(tone_hz)
        self.tone_duration_sec = max(0.0, float(tone_duration_sec))
        self.release_tone_duration_sec = max(0.0, float(release_tone_duration_sec))
        self.tone_amplitude = float(tone_amplitude)

        self._lock = threading.Lock()
        self._local_talkback_active = False
        self._remote_talkback_active = False
        self._remote_audio_fifo = np.zeros(0, dtype=np.float64)
        self._max_fifo_samples = self.sample_rate * 4
        self._tone_samples_total = int(round(self.sample_rate * self.tone_duration_sec))
        self._tone_samples_emitted = 0
        self._fade_samples = (
            min(
                int(round(self.sample_rate * 0.005)),  # 5ms click-free fade
                max(1, self._tone_samples_total // 4),
            )
            if self._tone_samples_total > 0
            else 0
        )

        self._release_tone_samples_total = int(
            round(self.sample_rate * self.release_tone_duration_sec)
        )
        # Start at _release_tone_samples_total so no release tone plays until Talkback is concluded
        self._release_tone_samples_emitted = self._release_tone_samples_total
        self._release_fade_samples = (
            min(
                int(round(self.sample_rate * 0.005)),  # 5ms click-free fade
                max(1, self._release_tone_samples_total // 4),
            )
            if self._release_tone_samples_total > 0
            else 0
        )

    @property
    def talkback_active(self) -> bool:
        with self._lock:
            return self._local_talkback_active or self._remote_talkback_active

    @property
    def local_talkback_active(self) -> bool:
        with self._lock:
            return self._local_talkback_active

    @property
    def remote_talkback_active(self) -> bool:
        with self._lock:
            return self._remote_talkback_active

    def _apply_talkback_transition_locked(self, was_active: bool, now_active: bool) -> None:
        if now_active and not was_active:
            self._tone_samples_emitted = 0
            self._release_tone_samples_emitted = self._release_tone_samples_total
        elif not now_active and was_active:
            self._release_tone_samples_emitted = 0
            self._remote_audio_fifo = np.zeros(0, dtype=np.float64)

    def set_talkback(self, active: bool) -> None:
        """Activate or deactivate local momentary Talkback mode."""
        with self._lock:
            was_active = self._local_talkback_active or self._remote_talkback_active
            self._local_talkback_active = bool(active)
            now_active = self._local_talkback_active or self._remote_talkback_active
            self._apply_talkback_transition_locked(was_active, now_active)

    def set_remote_talkback(self, active: bool) -> None:
        """Activate or deactivate Remote Producer momentary Talkback mode."""
        with self._lock:
            was_active = self._local_talkback_active or self._remote_talkback_active
            self._remote_talkback_active = bool(active)
            now_active = self._local_talkback_active or self._remote_talkback_active
            self._apply_talkback_transition_locked(was_active, now_active)

    def push_remote_audio(
        self, samples: np.ndarray, input_sample_rate: int = 0
    ) -> None:
        """Queue incoming Remote Producer talkback audio for Right-channel Studio Mode recording."""
        src_sr = int(input_sample_rate) if int(input_sample_rate) > 0 else self.sample_rate
        resampled = resample_mono_signal(samples, src_sr, self.sample_rate)
        if resampled.size == 0:
            return
        clipped = np.clip(resampled, -1.0, 1.0)
        with self._lock:
            combined = np.concatenate((self._remote_audio_fifo, clipped))
            if combined.size > self._max_fifo_samples:
                combined = combined[-self._max_fifo_samples :]
            self._remote_audio_fifo = combined

    def _synthesize_tone_segment(
        self, start_pos: int, count: int, total_samples: int, fade_samples: int
    ) -> np.ndarray:
        """Synthesize a click-free raised-cosine faded sine wave segment."""
        return synthesize_tone_segment(
            tone_hz=self.tone_hz,
            tone_amplitude=self.tone_amplitude,
            sample_rate=self.sample_rate,
            start_pos=start_pos,
            count=count,
            total_samples=total_samples,
            fade_samples=fade_samples,
        )

    def process_block(
        self,
        input_frames: np.ndarray,
        local_mic_muted: bool = False,
    ) -> np.ndarray:
        """Process a block of normalized float64 samples in range [-1.0, 1.0].

        Args:
            input_frames: Shape (num_frames, hw_channels) float64 array.
            local_mic_muted: If True (e.g. talent Cough/Privacy mute active),
                suppresses the local mic from being mixed onto the Right
                channel during remote-initiated Talkback unless local Talkback
                is explicitly held.

        Returns:
            Output array of shape (num_frames, output_channels) in float64 [-1.0, 1.0].
        """
        if input_frames.ndim == 1:
            input_frames = input_frames[:, np.newaxis]

        num_frames, hw_channels = input_frames.shape
        if num_frames == 0:
            return np.zeros((0, self.output_channels), dtype=np.float64)

        mic_ch_idx = min(self.input_channel, hw_channels - 1)
        if local_mic_muted and not self.local_talkback_active:
            mic_signal = np.zeros(num_frames, dtype=np.float64)
        else:
            mic_signal = input_frames[:, mic_ch_idx]

        if not self.studio_mode:
            if self.output_channels == 1:
                return mic_signal[:, np.newaxis].copy()
            # Stereo Normal Mode
            if local_mic_muted:
                return np.zeros((num_frames, 2), dtype=np.float64)
            if hw_channels >= 2:
                return input_frames[:, :2].copy()
            return np.column_stack((mic_signal, mic_signal))

        # Studio Mode: always Stereo (Left = Take audio, Right = Talkback tone + mixed Producer & Local Mic)
        out = np.zeros((num_frames, 2), dtype=np.float64)

        with self._lock:
            local_tb = self._local_talkback_active
            remote_tb = self._remote_talkback_active
            tb_active = local_tb or remote_tb
            tone_pos = self._tone_samples_emitted
            tone_total = self._tone_samples_total

            if not tb_active:
                # Normal Studio state: Mic on Left (0), Right (1) plays release tone if concluding, else silence
                out[:, 0] = mic_signal
                rel_pos = self._release_tone_samples_emitted
                rel_total = self._release_tone_samples_total
                rel_remaining = max(0, rel_total - rel_pos)
                rel_count = min(num_frames, rel_remaining)
                if rel_count > 0:
                    out[:rel_count, 1] = self._synthesize_tone_segment(
                        rel_pos,
                        rel_count,
                        rel_total,
                        self._release_fade_samples,
                    )
                    self._release_tone_samples_emitted += rel_count
                return out

            # Talkback active: Left (0) is muted (zeros).
            # Right (1) gets the press tone first, then mixes both the Local Talent Mic
            # and the Remote Producer's Talkback audio onto the Right channel.
            tone_remaining = max(0, tone_total - tone_pos)
            tone_count = min(num_frames, tone_remaining)

            if tone_count > 0:
                out[:tone_count, 1] = self._synthesize_tone_segment(
                    tone_pos,
                    tone_count,
                    tone_total,
                    self._fade_samples,
                )
                self._tone_samples_emitted += tone_count

            if tone_count < num_frames:
                voice_count = num_frames - tone_count
                rem_seg = np.zeros(voice_count, dtype=np.float64)
                if self._remote_audio_fifo.size > 0:
                    avail = min(voice_count, self._remote_audio_fifo.size)
                    rem_seg[:avail] = self._remote_audio_fifo[:avail]
                    self._remote_audio_fifo = self._remote_audio_fifo[avail:]

                if local_mic_muted and not local_tb:
                    local_seg = np.zeros(voice_count, dtype=np.float64)
                else:
                    local_seg = mic_signal[tone_count:]

                out[tone_count:, 1] = np.clip(local_seg + rem_seg, -1.0, 1.0)

        return out


class WavFileWriter:
    """Streaming RIFF WAVE writer supporting 16-bit, 24-bit, and 32-bit linear PCM.

    Supports closing the WAV file on pause (flushing valid RIFF/WAVE headers to prevent
    corruption) and reopening the exact same file in append mode ('r+b') on resume.
    """

    def __init__(
        self,
        filepath: Path,
        sample_rate: int,
        channels: int,
        bit_depth: int,
        append: bool = False,
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
        self._file = None

        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        if append and self.filepath.is_file() and self.filepath.stat().st_size >= 44:
            self.reopen()
        else:
            self._file = self.filepath.open("wb+")
            self._write_header(0)
            try:
                os.chmod(self.filepath, 0o666)
            except OSError:
                pass

    @property
    def is_open(self) -> bool:
        """Return True if the underlying OS file handle is currently open."""
        return self._file is not None and not self._file.closed

    def reopen(self) -> None:
        """Reopen an existing closed WAV file in 'r+b' mode so new frames append seamlessly."""
        if self.is_open:
            return
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        if self.filepath.is_file() and self.filepath.stat().st_size >= 44:
            self._file = self.filepath.open("r+b")
            self._file.seek(0, os.SEEK_END)
            file_size = self._file.tell()
            raw_data_bytes = max(0, file_size - 44)
            usable_data_bytes = (
                (raw_data_bytes // self.block_align) * self.block_align
                if self.block_align > 0
                else raw_data_bytes
            )
            if usable_data_bytes != raw_data_bytes:
                self._file.truncate(44 + usable_data_bytes)
            self.data_bytes_written = usable_data_bytes
            self.frames_written = (
                usable_data_bytes // self.block_align if self.block_align > 0 else 0
            )
            self._write_header(self.data_bytes_written)
            self._file.seek(0, os.SEEK_END)
        else:
            self._file = self.filepath.open("wb+")
            self.data_bytes_written = 0
            self.frames_written = 0
            self._write_header(0)
        try:
            os.chmod(self.filepath, 0o666)
        except OSError:
            pass

    def _write_header(self, data_size: int) -> None:
        if not self.is_open:
            return
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
        if not self.is_open:
            self.reopen()
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
        if self.is_open:
            self._write_header(self.data_bytes_written)
            self._file.flush()

    def close(self) -> None:
        if self.is_open:
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
    """Manages audio hardware discovery, input monitoring, VU/Peak Hold metering, WAV recording, Talkback routing, and reference playback."""

    def __init__(self, simulate_if_empty: bool = False) -> None:
        self.alsa = AlsaLibWrapper()
        self.simulate_if_empty = simulate_if_empty or (
            os.environ.get("ALPINE_RECORDER_SIMULATE_AUDIO", "0") == "1"
        )

        # Cache probed hardware capabilities so background enumeration does not fail
        # with EBUSY while the capture device is held open for live input monitoring.
        self._hw_probe_cache: Dict[Tuple[str, int], Tuple[List[int], Dict[int, int], int, int]] = {}

        # Real-time VU Meter and Peak Hold state
        self._meter_lock = threading.Lock()
        self._peak_level: float = 0.0
        self._rms_level: float = 0.0
        self._peak_hold_level: float = 0.0
        self._channel_peaks: List[float] = [0.0]
        self._channel_rms: List[float] = [0.0]
        self._channel_peak_holds: List[float] = [0.0]
        self._meter_channels: int = 1
        self._clipped: bool = False
        self._last_meter_update_monotonic: float = 0.0

        # Continuous Input Monitoring state (for level setting while app is active)
        self._monitor_lock = threading.Lock()
        self._is_monitoring = False
        self._stop_monitor_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._active_monitor_signature: Optional[Tuple[str, int, int, bool, int, int]] = None
        self._desired_monitor_params: Optional[Tuple[AudioDeviceCapabilities, int, int, bool, int, int]] = None

        self._record_lock = threading.Lock()
        self._writer_lock = threading.Lock()
        self._is_recording = False
        self._is_record_paused = False
        self._stop_record_event = threading.Event()
        self._record_thread: Optional[threading.Thread] = None
        self._record_start_monotonic: float = 0.0
        self._record_accumulated_sec: float = 0.0
        self._current_processor: Optional[StudioTalkbackProcessor] = None
        self._current_recording_path: Optional[Path] = None
        self._active_writer: Optional[WavFileWriter] = None

        self._play_lock = threading.Lock()
        self._is_playing = False
        self._is_paused = False
        self._stop_play_event = threading.Event()
        self._play_thread: Optional[threading.Thread] = None
        self._play_position_sec: float = 0.0
        self._play_duration_sec: float = 0.0
        self._playing_filepath: Optional[Path] = None

        # Remote Producer bidirectional audio & privacy/cough state
        self._live_listeners_lock = threading.Lock()
        self._live_audio_listeners: List[Callable[[np.ndarray, int, bool], None]] = []
        self._talent_mic_send_enabled: bool = True
        self._talent_cough_muted: bool = False
        self._local_talkback_active: bool = False
        self._remote_talkback_active: bool = False
        self._last_remote_talkback_peak: float = 0.0
        self._last_remote_talkback_rms: float = 0.0
        self._last_remote_talkback_mono: float = 0.0

        # Talkback marker tone parameters for headset notification on press/release
        self._talkback_tone_hz: float = 1000.0
        self._talkback_tone_duration_sec: float = 1.0
        self._talkback_release_tone_duration_sec: float = 0.5
        self._talkback_tone_amplitude: float = 0.5

        # Live Talkback headphone monitor output for prompting the talent
        self._tb_out_lock = threading.Lock()
        self._tb_out_fifo = np.zeros(0, dtype=np.float64)
        self._tb_out_rate: int = 24000
        self._tb_out_device: Optional[AudioDeviceCapabilities] = None
        self._tb_out_thread: Optional[threading.Thread] = None
        self._tb_out_stop_event = threading.Event()
        self._tb_headset_tone_samples_emitted: int = 0
        self._tb_headset_tone_samples_total: int = 0

    # -------------------------------------------------------------------------
    # Hardware Enumeration
    # -------------------------------------------------------------------------
    def enumerate_devices(self, force_probe: bool = False) -> List[AudioDeviceCapabilities]:
        """Discover available audio devices and query their supported sample rates & bit depths."""
        was_monitoring = False
        if force_probe:
            self._hw_probe_cache.clear()
            if self.is_monitoring and not self.is_recording:
                was_monitoring = True
                self._stop_monitoring_thread()

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

        if was_monitoring:
            self._resume_monitoring_if_configured()

        return devices

    def get_capture_devices(self, force_probe: bool = False) -> List[AudioDeviceCapabilities]:
        return [d for d in self.enumerate_devices(force_probe=force_probe) if d.is_capture]

    def get_playback_devices(self, force_probe: bool = False) -> List[AudioDeviceCapabilities]:
        return [d for d in self.enumerate_devices(force_probe=force_probe) if d.is_playback]

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

                # 1. Try ALSA libasound hardware probe (falling back to cached probe if busy monitoring)
                stream_dir = (
                    SND_PCM_STREAM_CAPTURE if is_capture else SND_PCM_STREAM_PLAYBACK
                )
                cache_key = (dev_id, stream_dir)
                probed = self.alsa.probe_hw_params(dev_id, stream_dir)
                if probed:
                    self._hw_probe_cache[cache_key] = probed
                else:
                    probed = self._hw_probe_cache.get(cache_key)

                if probed:
                    rates, fmt_map, min_ch, max_ch = (
                        list(probed[0]),
                        dict(probed[1]),
                        probed[2],
                        probed[3],
                    )

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
    # VU Meter, Peak Hold & Continuous Input Monitoring
    # -------------------------------------------------------------------------
    @property
    def is_monitoring(self) -> bool:
        with self._monitor_lock:
            return self._is_monitoring

    @property
    def peak_level(self) -> float:
        with self._meter_lock:
            return self._peak_level

    @property
    def rms_level(self) -> float:
        with self._meter_lock:
            return self._rms_level

    @property
    def peak_hold_level(self) -> float:
        with self._meter_lock:
            return self._peak_hold_level

    @property
    def peak_clipped(self) -> bool:
        with self._meter_lock:
            return self._clipped

    def reset_peak_hold(self) -> None:
        """Reset maintained Peak Hold level and clip indicator while keeping live metering active."""
        with self._meter_lock:
            self._peak_hold_level = 0.0
            self._channel_peak_holds = [0.0] * max(1, self._meter_channels)
            self._clipped = False

    def _clear_live_meter_levels(self) -> None:
        """Immediately zero live RMS and Peak VU meter levels (e.g. when the mic is muted)."""
        with self._meter_lock:
            self._peak_level = 0.0
            self._rms_level = 0.0
            self._channel_peaks = [0.0] * max(1, self._meter_channels)
            self._channel_rms = [0.0] * max(1, self._meter_channels)
            self._last_meter_update_monotonic = time.monotonic()

    def get_vu_readings(self) -> VuMeterReadings:
        """Return a snapshot of current VU meter levels (linear & dBFS) and maintained Peak Hold."""
        active = self.is_recording or self.is_monitoring
        muted = self.is_mic_muted
        now = time.monotonic()

        with self._meter_lock:
            if muted:
                self._peak_level = 0.0
                self._rms_level = 0.0
                self._channel_peaks = [0.0] * max(1, self._meter_channels)
                self._channel_rms = [0.0] * max(1, self._meter_channels)
            elif not active and (self._peak_level > 0.0 or self._rms_level > 0.0):
                dt = max(0.0, now - self._last_meter_update_monotonic)
                if dt > 0.08:
                    decay = 0.65 ** min(20.0, dt / 0.05)
                    self._peak_level = self._peak_level * decay if self._peak_level > 1e-4 else 0.0
                    self._rms_level = self._rms_level * decay if self._rms_level > 1e-4 else 0.0
                    self._channel_peaks = [
                        (p * decay if p > 1e-4 else 0.0) for p in self._channel_peaks
                    ]
                    self._channel_rms = [
                        (r * decay if r > 1e-4 else 0.0) for r in self._channel_rms
                    ]
                    self._last_meter_update_monotonic = now

            rms_lin = self._rms_level
            peak_lin = self._peak_level
            hold_lin = self._peak_hold_level
            ch_peaks = tuple(self._channel_peaks)
            ch_rms = tuple(self._channel_rms)
            ch_holds = tuple(self._channel_peak_holds)
            num_ch = self._meter_channels
            clipped = self._clipped

        return VuMeterReadings(
            rms_level=rms_lin,
            peak_level=peak_lin,
            peak_hold_level=hold_lin,
            rms_db=linear_to_dbfs(rms_lin),
            peak_db=linear_to_dbfs(peak_lin),
            peak_hold_db=linear_to_dbfs(hold_lin),
            channel_peaks=ch_peaks,
            channel_rms=ch_rms,
            channel_peak_holds=ch_holds,
            channels=num_ch,
            clipped=clipped,
            active=active,
        )

    @staticmethod
    def _extract_metered_input(
        input_frames: np.ndarray,
        studio_mode: bool,
        channels: int,
        input_channel: int,
    ) -> np.ndarray:
        """Extract the active input channel(s) to feed the VU & Peak Hold meter."""
        if input_frames.ndim == 1:
            input_frames = input_frames[:, np.newaxis]
        num_frames, hw_channels = input_frames.shape
        if num_frames == 0 or hw_channels == 0:
            target_ch = 1 if (studio_mode or channels <= 1) else 2
            return np.zeros((0, target_ch), dtype=np.float64)

        mic_ch_idx = min(max(0, int(input_channel)), hw_channels - 1)
        if studio_mode or channels <= 1:
            return input_frames[:, mic_ch_idx : mic_ch_idx + 1]

        # Normal Stereo mode (2 channels)
        if hw_channels >= 2:
            return input_frames[:, :2]
        mic_col = input_frames[:, mic_ch_idx]
        return np.column_stack((mic_col, mic_col))

    def _update_meter_from_block(self, metered_block: np.ndarray) -> None:
        """Update smoothed VU levels and Peak Hold from an input audio block."""
        if metered_block.size == 0:
            return
        if metered_block.ndim == 1:
            metered_block = metered_block[:, np.newaxis]

        num_frames, num_ch = metered_block.shape
        if num_frames == 0 or num_ch == 0:
            return

        if self.is_mic_muted:
            with self._meter_lock:
                if self._meter_channels != num_ch or len(self._channel_peaks) != num_ch:
                    self._meter_channels = num_ch
                    self._channel_peak_holds = [0.0] * num_ch
                self._peak_level = 0.0
                self._rms_level = 0.0
                self._channel_peaks = [0.0] * num_ch
                self._channel_rms = [0.0] * num_ch
                self._last_meter_update_monotonic = time.monotonic()
            return

        abs_block = np.abs(metered_block)
        ch_block_peaks = np.max(abs_block, axis=0)
        ch_block_rms = np.sqrt(np.mean(np.square(metered_block), axis=0))

        overall_peak = float(np.max(ch_block_peaks))
        overall_rms = float(np.max(ch_block_rms))
        now = time.monotonic()

        with self._meter_lock:
            dt = max(0.005, min(0.5, now - self._last_meter_update_monotonic)) if self._last_meter_update_monotonic > 0 else 0.04
            self._last_meter_update_monotonic = now

            # Smooth release ballistics (~20 dB/sec release) with instant attack
            release_coef = 0.80 ** (dt / 0.04)

            if self._meter_channels != num_ch or len(self._channel_peaks) != num_ch:
                self._meter_channels = num_ch
                self._channel_peaks = [0.0] * num_ch
                self._channel_rms = [0.0] * num_ch
                self._channel_peak_holds = [0.0] * num_ch
                self._peak_hold_level = 0.0
                self._clipped = False

            self._peak_level = max(overall_peak, self._peak_level * release_coef)
            self._rms_level = max(overall_rms, self._rms_level * release_coef)

            if overall_peak > self._peak_hold_level:
                self._peak_hold_level = min(1.0, overall_peak)
            if overall_peak >= CLIP_THRESHOLD_LINEAR:
                self._clipped = True

            for idx in range(num_ch):
                cp = float(ch_block_peaks[idx])
                cr = float(ch_block_rms[idx])
                self._channel_peaks[idx] = max(cp, self._channel_peaks[idx] * release_coef)
                self._channel_rms[idx] = max(cr, self._channel_rms[idx] * release_coef)
                if cp > self._channel_peak_holds[idx]:
                    self._channel_peak_holds[idx] = min(1.0, cp)

    def start_monitoring(
        self,
        device: Optional[AudioDeviceCapabilities],
        sample_rate: int,
        bit_depth: int,
        studio_mode: bool = False,
        channels: int = 1,
        input_channel: int = 0,
    ) -> None:
        """Start or update live input signal monitoring so the VU meter & Peak Hold work while idle."""
        if device is None or not device.is_capture:
            self.stop_monitoring()
            return

        sr = max(8000, int(sample_rate or device.max_sample_rate))
        bd = int(bit_depth or device.max_bit_depth)
        st_mode = bool(studio_mode)
        ch = 2 if st_mode else max(1, min(2, int(channels)))
        in_ch = max(0, int(input_channel))

        sig = (device.device_id, sr, bd, st_mode, ch, in_ch)
        with self._monitor_lock:
            prev_sig = self._active_monitor_signature
            self._desired_monitor_params = (device, sr, bd, st_mode, ch, in_ch)
            if (
                self._is_monitoring
                and prev_sig == sig
                and self._monitor_thread is not None
                and self._monitor_thread.is_alive()
            ):
                return

        if prev_sig is not None and prev_sig[0] != device.device_id:
            self.reset_peak_hold()

        if self.is_recording or self.is_playing:
            return

        self._stop_monitoring_thread()

        with self._monitor_lock:
            self._stop_monitor_event.clear()
            self._is_monitoring = True
            self._active_monitor_signature = sig
            self._monitor_thread = threading.Thread(
                target=self._monitor_worker,
                args=(device, sr, bd, st_mode, ch, in_ch),
                daemon=True,
            )
            self._monitor_thread.start()

    def stop_monitoring(self) -> None:
        """Stop live input monitoring and clear desired monitoring state."""
        with self._monitor_lock:
            self._desired_monitor_params = None
        self._stop_monitoring_thread()

    def _stop_monitoring_thread(self) -> None:
        with self._monitor_lock:
            if not self._is_monitoring and self._monitor_thread is None:
                return
            self._stop_monitor_event.set()
            thread = self._monitor_thread

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)

        with self._monitor_lock:
            self._is_monitoring = False
            self._monitor_thread = None
            self._active_monitor_signature = None

    def _resume_monitoring_if_configured(self) -> None:
        with self._monitor_lock:
            params = self._desired_monitor_params
        if params is not None and not self.is_recording and not self.is_playing:
            self.start_monitoring(*params)

    def _monitor_worker(
        self,
        device: AudioDeviceCapabilities,
        sample_rate: int,
        bit_depth: int,
        studio_mode: bool,
        channels: int,
        input_channel: int,
    ) -> None:
        try:
            if device.device_id.startswith("sim:") or not self.alsa.available:
                self._monitor_simulated_loop(
                    sample_rate, studio_mode, channels, input_channel
                )
            else:
                self._monitor_alsa_loop(
                    device, sample_rate, bit_depth, studio_mode, channels, input_channel
                )
        except Exception as exc:
            logger.debug("Input monitor stopped on %s: %s", device.device_id, exc)
        finally:
            with self._monitor_lock:
                if self._monitor_thread is threading.current_thread():
                    self._is_monitoring = False

    @staticmethod
    def _generate_simulated_input_block(
        chunk_frames: int, sample_rate: int, phase: float
    ) -> np.ndarray:
        """Generate a realistic dynamic simulated microphone input block for level-setting."""
        t_vec = (np.arange(chunk_frames, dtype=np.float64) + phase) / sample_rate
        # Modulated envelope between ~-24 dBFS and -10 dBFS with periodic transient peaks at ~-6.5 dBFS
        slow_env = 0.14 + 0.10 * np.sin(2.0 * math.pi * 0.65 * t_vec)
        transient = 0.22 * np.power(np.maximum(0.0, np.sin(2.0 * math.pi * 0.25 * t_vec)), 12)
        amp_l = slow_env + transient
        amp_r = slow_env * (0.88 + 0.10 * np.cos(2.0 * math.pi * 0.45 * t_vec)) + 0.85 * transient
        carrier = np.sin(2.0 * math.pi * 440.0 * t_vec)
        sig_l = amp_l * carrier
        sig_r = amp_r * carrier
        return np.column_stack((sig_l, sig_r))

    def _monitor_simulated_loop(
        self,
        sample_rate: int,
        studio_mode: bool,
        channels: int,
        input_channel: int,
    ) -> None:
        chunk_frames = max(256, sample_rate // 25)
        chunk_duration = chunk_frames / sample_rate
        phase = 0.0

        while not self._stop_monitor_event.is_set():
            t0 = time.monotonic()
            in_block = self._generate_simulated_input_block(chunk_frames, sample_rate, phase)
            phase += chunk_frames
            if self.is_mic_muted:
                in_block = np.zeros_like(in_block)

            metered = self._extract_metered_input(
                in_block, studio_mode, channels, input_channel
            )
            self._update_meter_from_block(metered)
            self._dispatch_live_input_block(metered, sample_rate)

            elapsed = time.monotonic() - t0
            sleep_for = max(0.005, chunk_duration - elapsed)
            self._stop_monitor_event.wait(sleep_for)

    def _monitor_alsa_loop(
        self,
        device: AudioDeviceCapabilities,
        sample_rate: int,
        bit_depth: int,
        studio_mode: bool,
        channels: int,
        input_channel: int,
    ) -> None:
        assert self.alsa.lib is not None
        lib = self.alsa.lib

        pcm = ctypes.c_void_p()
        err = lib.snd_pcm_open(
            ctypes.byref(pcm),
            device.device_id.encode("ascii"),
            SND_PCM_STREAM_CAPTURE,
            0,
        )
        if err < 0 or not pcm:
            return

        hw_params = ctypes.c_void_p()
        lib.snd_pcm_hw_params_malloc(ctypes.byref(hw_params))
        try:
            alsa_fmt = device.alsa_format_map.get(bit_depth, SND_PCM_FORMAT_S16_LE)
            hw_channels = max(
                device.min_channels,
                min(device.max_channels, max(2, input_channel + 1)),
            )

            if lib.snd_pcm_hw_params_any(pcm, hw_params) < 0:
                return
            if lib.snd_pcm_hw_params_set_access(pcm, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED) < 0:
                return
            if lib.snd_pcm_hw_params_set_format(pcm, hw_params, alsa_fmt) < 0:
                return
            if lib.snd_pcm_hw_params_set_channels(pcm, hw_params, hw_channels) < 0:
                hw_channels = device.min_channels
                if lib.snd_pcm_hw_params_set_channels(pcm, hw_params, hw_channels) < 0:
                    return
            if lib.snd_pcm_hw_params_set_rate(pcm, hw_params, sample_rate, 0) < 0:
                return
            if lib.snd_pcm_hw_params(pcm, hw_params) < 0:
                return
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

        try:
            while not self._stop_monitor_event.is_set():
                frames_read = lib.snd_pcm_readi(pcm, buf, period_frames)
                if frames_read < 0:
                    recovered = lib.snd_pcm_recover(pcm, int(frames_read), 1)
                    if recovered < 0:
                        break
                    continue
                if frames_read == 0:
                    continue

                raw_slice = buf.raw[: frames_read * frame_bytes]
                in_float = self._decode_alsa_bytes(
                    raw_slice, int(frames_read), hw_channels, alsa_fmt
                )
                if self.is_mic_muted:
                    in_float = np.zeros_like(in_float)
                metered = self._extract_metered_input(
                    in_float, studio_mode, channels, input_channel
                )
                self._update_meter_from_block(metered)
                self._dispatch_live_input_block(metered, sample_rate)
        finally:
            lib.snd_pcm_drop(pcm)
            lib.snd_pcm_close(pcm)

    # -------------------------------------------------------------------------
    # Remote Producer Bidirectional Audio & Privacy / Cough Controls
    # -------------------------------------------------------------------------
    @property
    def talent_mic_send_enabled(self) -> bool:
        return self._talent_mic_send_enabled

    def set_talent_mic_send_enabled(self, enabled: bool) -> None:
        """Enable or disable sending the talent's live microphone feed (and unmuting/muting mic input)."""
        self._talent_mic_send_enabled = bool(enabled)
        if not self._talent_mic_send_enabled:
            self._clear_live_meter_levels()

    @property
    def talent_cough_muted(self) -> bool:
        return self._talent_cough_muted

    def set_talent_cough_muted(self, muted: bool) -> None:
        """Set momentary Cough / Privacy mute state on the talent's microphone."""
        self._talent_cough_muted = bool(muted)
        if self._talent_cough_muted:
            self._clear_live_meter_levels()

    @property
    def is_mic_muted(self) -> bool:
        """Return True when the local microphone is muted (momentary cough mute or toggled mute)."""
        return bool(self._talent_cough_muted or not self._talent_mic_send_enabled)

    @property
    def is_talent_mic_live_to_producer(self) -> bool:
        """Return True when the talent's microphone is permitted to stream to the remote producer."""
        return not self.is_mic_muted

    def register_live_audio_listener(
        self, callback: Callable[[np.ndarray, int, bool], None]
    ) -> None:
        """Register a listener `(mono_frames, sample_rate, is_muted)` for live talent mic streaming."""
        with self._live_listeners_lock:
            if callback not in self._live_audio_listeners:
                self._live_audio_listeners.append(callback)

    def unregister_live_audio_listener(
        self, callback: Callable[[np.ndarray, int, bool], None]
    ) -> None:
        with self._live_listeners_lock:
            if callback in self._live_audio_listeners:
                self._live_audio_listeners.remove(callback)

    def _dispatch_live_input_block(
        self, metered_block: np.ndarray, sample_rate: int
    ) -> None:
        with self._live_listeners_lock:
            listeners = list(self._live_audio_listeners)
        if not listeners or metered_block.size == 0:
            return

        if metered_block.ndim == 2:
            mono = metered_block[:, 0]
        else:
            mono = metered_block.reshape(-1)

        is_muted = not self.is_talent_mic_live_to_producer
        payload = np.zeros_like(mono) if is_muted else mono
        for cb in listeners:
            try:
                cb(payload, sample_rate, is_muted)
            except Exception as exc:
                logger.debug("Live audio listener error: %s", exc)

    def set_talkback_output_device(
        self, device: Optional[AudioDeviceCapabilities]
    ) -> None:
        """Configure the output device used for live remote producer talkback headphone monitoring."""
        with self._tb_out_lock:
            self._tb_out_device = device

    def set_talkback_tone_params(
        self,
        tone_hz: float = 1000.0,
        tone_duration_sec: float = 1.0,
        release_tone_duration_sec: float = 0.5,
        tone_amplitude: float = 0.5,
    ) -> None:
        """Configure the marker tone frequency and durations played into the headset on Talkback press/release."""
        self._talkback_tone_hz = float(tone_hz)
        self._talkback_tone_duration_sec = max(0.0, float(tone_duration_sec))
        self._talkback_release_tone_duration_sec = max(0.0, float(release_tone_duration_sec))
        self._talkback_tone_amplitude = float(tone_amplitude)

    @property
    def talkback_headset_tone_samples_emitted(self) -> int:
        with self._tb_out_lock:
            return self._tb_headset_tone_samples_emitted

    @property
    def talkback_headset_tone_samples_total(self) -> int:
        with self._tb_out_lock:
            return self._tb_headset_tone_samples_total

    def _emit_headset_talkback_tone(self, is_press: bool) -> None:
        """Synthesize and queue the Talkback press or release marker tone into the talent headset output."""
        if self.is_playing:
            return
        proc = self._current_processor
        if proc is not None:
            tone_hz = proc.tone_hz
            dur_sec = proc.tone_duration_sec if is_press else proc.release_tone_duration_sec
            amp = proc.tone_amplitude
        else:
            tone_hz = self._talkback_tone_hz
            dur_sec = (
                self._talkback_tone_duration_sec
                if is_press
                else self._talkback_release_tone_duration_sec
            )
            amp = self._talkback_tone_amplitude

        target_sr = 24000
        total_samples = int(round(target_sr * max(0.0, float(dur_sec))))
        if total_samples <= 0:
            if not is_press:
                with self._tb_out_lock:
                    self._tb_out_fifo = np.zeros(0, dtype=np.float64)
            return

        fade_samples = min(
            int(round(target_sr * 0.005)),  # 5ms click-free fade
            max(1, total_samples // 4),
        )
        tone_samples = synthesize_tone_segment(
            tone_hz=tone_hz,
            tone_amplitude=amp,
            sample_rate=target_sr,
            start_pos=0,
            count=total_samples,
            total_samples=total_samples,
            fade_samples=fade_samples,
        )
        with self._tb_out_lock:
            # Replace any stale queued samples so the press or release tone plays immediately into the headset
            self._tb_out_fifo = np.zeros(0, dtype=np.float64)
            self._tb_headset_tone_samples_total += total_samples
        self._queue_talkback_headphone_audio(tone_samples, target_sr, is_tone=True)

    @property
    def remote_talkback_active(self) -> bool:
        proc = self._current_processor
        if proc is not None and self.is_recording and not self.is_record_paused:
            return proc.remote_talkback_active
        return self._remote_talkback_active

    @property
    def local_talkback_active(self) -> bool:
        proc = self._current_processor
        if proc is not None and self.is_recording and not self.is_record_paused:
            return proc.local_talkback_active
        return self._local_talkback_active

    @property
    def last_remote_talkback_peak(self) -> float:
        return self._last_remote_talkback_peak

    def get_remote_talkback_levels(self) -> Tuple[float, float]:
        """Return (peak_linear, rms_linear) for remote producer talkback with smooth decay."""
        if not self.remote_talkback_active:
            return (0.0, 0.0)
        now = time.monotonic()
        dt = max(0.0, now - self._last_remote_talkback_mono)
        if dt > 0.06 and (self._last_remote_talkback_peak > 0.0 or self._last_remote_talkback_rms > 0.0):
            decay = 0.70 ** min(20.0, dt / 0.05)
            self._last_remote_talkback_peak = (
                self._last_remote_talkback_peak * decay
                if self._last_remote_talkback_peak > 1e-4
                else 0.0
            )
            self._last_remote_talkback_rms = (
                self._last_remote_talkback_rms * decay
                if self._last_remote_talkback_rms > 1e-4
                else 0.0
            )
            self._last_remote_talkback_mono = now
        return (self._last_remote_talkback_peak, self._last_remote_talkback_rms)

    def set_remote_talkback(self, active: bool) -> None:
        """Set momentary Talkback state triggered by a connected Remote Producer."""
        if active and self.is_record_paused:
            self._remote_talkback_active = False
            return
        was_tb_active = self.talkback_active
        self._remote_talkback_active = bool(active)
        if not active:
            self._last_remote_talkback_peak = 0.0
            self._last_remote_talkback_rms = 0.0
        proc = self._current_processor
        if proc is not None:
            proc.set_remote_talkback(bool(active))
        now_tb_active = self.talkback_active
        if now_tb_active and not was_tb_active:
            self._emit_headset_talkback_tone(is_press=True)
        elif not now_tb_active and was_tb_active:
            self._emit_headset_talkback_tone(is_press=False)

    def push_remote_talkback_audio(
        self,
        samples: np.ndarray,
        sample_rate: int = 24000,
        input_sample_rate: Optional[int] = None,
    ) -> None:
        """Feed incoming Remote Producer microphone audio to both Studio Mode Right-Ch recording and Talent headphones."""
        if input_sample_rate is not None:
            sample_rate = int(input_sample_rate)
        flat = np.asarray(samples, dtype=np.float64).reshape(-1)
        if flat.size == 0:
            return
        blk_peak = float(np.max(np.abs(flat)))
        blk_rms = float(np.sqrt(np.mean(np.square(flat))))
        self._last_remote_talkback_peak = max(blk_peak, self._last_remote_talkback_peak * 0.80)
        self._last_remote_talkback_rms = max(blk_rms, self._last_remote_talkback_rms * 0.80)
        self._last_remote_talkback_mono = time.monotonic()

        # 1. If recording in Studio Mode (and not paused), feed into StudioTalkbackProcessor Right channel
        proc = self._current_processor
        if proc is not None and self.is_recording and not self.is_record_paused:
            proc.push_remote_audio(flat, input_sample_rate=sample_rate)

        # 2. Queue for low-latency headphone monitoring so the talent hears the producer's prompt
        self._queue_talkback_headphone_audio(flat, sample_rate)

    def _queue_talkback_headphone_audio(
        self, samples: np.ndarray, sample_rate: int, is_tone: bool = False
    ) -> None:
        if self.is_playing:
            return
        target_sr = 24000
        resampled = resample_mono_signal(samples, sample_rate, target_sr)
        if resampled.size == 0:
            return

        with self._tb_out_lock:
            self._tb_out_rate = target_sr
            combined = np.concatenate((self._tb_out_fifo, np.clip(resampled, -1.0, 1.0)))
            # Allow up to 4.0s buffer so full marker tones (up to 3.0s) + live voice never truncate the tone
            max_samples = target_sr * 4 if is_tone else max(target_sr * 2, self._tb_out_fifo.size + resampled.size)
            max_samples = min(max_samples, target_sr * 5)
            if combined.size > max_samples:
                combined = combined[-max_samples:]
            self._tb_out_fifo = combined

            if self._tb_out_thread is None or not self._tb_out_thread.is_alive():
                self._tb_out_stop_event.clear()
                self._tb_out_thread = threading.Thread(
                    target=self._talkback_headphone_worker,
                    daemon=True,
                )
                self._tb_out_thread.start()

    def _talkback_headphone_worker(self) -> None:
        """Low-latency playback worker that streams talkback marker tones and remote producer talkback to the talent's headset."""
        with self._tb_out_lock:
            out_dev = self._tb_out_device
            sr = self._tb_out_rate

        if (
            out_dev is None
            or out_dev.device_id.startswith("sim:")
            or not self.alsa.available
        ):
            # Simulated drain loop
            while not self._tb_out_stop_event.is_set():
                with self._tb_out_lock:
                    if self._tb_out_fifo.size == 0:
                        return
                    take = min(480, self._tb_out_fifo.size)
                    self._tb_out_fifo = self._tb_out_fifo[take:]
                    self._tb_headset_tone_samples_emitted += take
                time.sleep(take / float(max(8000, sr)))
            return

        assert self.alsa.lib is not None
        lib = self.alsa.lib
        dev_str = out_dev.device_id
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
            with self._tb_out_lock:
                self._tb_out_fifo = np.zeros(0, dtype=np.float64)
            return

        hw_params = ctypes.c_void_p()
        lib.snd_pcm_hw_params_malloc(ctypes.byref(hw_params))
        out_ch = 2
        try:
            lib.snd_pcm_hw_params_any(pcm, hw_params)
            lib.snd_pcm_hw_params_set_access(pcm, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED)
            lib.snd_pcm_hw_params_set_format(pcm, hw_params, SND_PCM_FORMAT_S16_LE)
            if lib.snd_pcm_hw_params_set_channels(pcm, hw_params, out_ch) < 0:
                out_ch = 1
                lib.snd_pcm_hw_params_set_channels(pcm, hw_params, out_ch)
            lib.snd_pcm_hw_params_set_rate(pcm, hw_params, sr, 0)
            if lib.snd_pcm_hw_params(pcm, hw_params) < 0:
                return
        finally:
            lib.snd_pcm_hw_params_free(hw_params)

        lib.snd_pcm_prepare(pcm)
        idle_ticks = 0
        try:
            while not self._tb_out_stop_event.is_set() and not self.is_playing:
                with self._tb_out_lock:
                    avail = self._tb_out_fifo.size
                    if avail > 0:
                        take = min(960, avail)
                        mono_chunk = self._tb_out_fifo[:take]
                        self._tb_out_fifo = self._tb_out_fifo[take:]
                        self._tb_headset_tone_samples_emitted += take
                    else:
                        mono_chunk = None

                if mono_chunk is None:
                    idle_ticks += 1
                    if idle_ticks > 25:  # Close idle playback stream after ~500ms of silence
                        break
                    time.sleep(0.02)
                    continue

                idle_ticks = 0
                if out_ch == 2:
                    stereo = np.column_stack((mono_chunk, mono_chunk))
                    pcm16 = np.round(np.clip(stereo, -1.0, 1.0) * 32767.0).astype("<i2")
                else:
                    pcm16 = np.round(np.clip(mono_chunk, -1.0, 1.0) * 32767.0).astype("<i2")

                raw = pcm16.tobytes()
                written = lib.snd_pcm_writei(pcm, raw, mono_chunk.size)
                if written < 0:
                    lib.snd_pcm_recover(pcm, int(written), 1)
        finally:
            lib.snd_pcm_drop(pcm)
            lib.snd_pcm_close(pcm)

    # -------------------------------------------------------------------------
    # Recording State & Control
    # -------------------------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        with self._record_lock:
            return self._is_recording

    @property
    def is_record_paused(self) -> bool:
        with self._record_lock:
            return self._is_recording and self._is_record_paused

    @property
    def current_recording_path(self) -> Optional[Path]:
        with self._record_lock:
            return self._current_recording_path

    @property
    def is_recording_file_open(self) -> bool:
        with self._writer_lock:
            return self._active_writer is not None and self._active_writer.is_open

    @property
    def recording_bytes_written(self) -> int:
        with self._writer_lock:
            if self._active_writer is None:
                return 0
            return 44 + int(self._active_writer.data_bytes_written)

    @property
    def elapsed_seconds(self) -> float:
        with self._record_lock:
            if not self._is_recording:
                return 0.0
            if self._is_record_paused:
                return self._record_accumulated_sec
            return self._record_accumulated_sec + max(
                0.0, time.monotonic() - self._record_start_monotonic
            )

    @property
    def talkback_active(self) -> bool:
        proc = self._current_processor
        if proc is not None and self.is_recording and not self.is_record_paused:
            return proc.talkback_active
        return bool(self._local_talkback_active or self._remote_talkback_active)

    def set_talkback(self, active: bool) -> None:
        """Set momentary Talkback state (during recording or standby monitoring)."""
        if active and self.is_record_paused:
            self._local_talkback_active = False
            return
        was_tb_active = self.talkback_active
        self._local_talkback_active = bool(active)
        proc = self._current_processor
        if proc is not None:
            proc.set_talkback(active)
        now_tb_active = self.talkback_active
        if now_tb_active and not was_tb_active:
            self._emit_headset_talkback_tone(is_press=True)
        elif not now_tb_active and was_tb_active:
            self._emit_headset_talkback_tone(is_press=False)

    def pause_recording(self) -> bool:
        """Pause active recording, flushing and closing the WAV file on disk to prevent corruption."""
        with self._record_lock:
            if not self._is_recording:
                return False
            if self._is_record_paused:
                return True
            now = time.monotonic()
            self._record_accumulated_sec += max(
                0.0, now - self._record_start_monotonic
            )
            self._record_start_monotonic = now
            self._is_record_paused = True
            proc = self._current_processor

        self._local_talkback_active = False
        self._remote_talkback_active = False
        if proc is not None:
            proc.set_talkback(False)
            proc.set_remote_talkback(False)

        with self._writer_lock:
            if self._active_writer is not None and self._active_writer.is_open:
                self._active_writer.close()

        return True

    def resume_recording(self) -> bool:
        """Reopen the paused WAV file and resume recording to the same file."""
        with self._record_lock:
            if not self._is_recording:
                return False
            if not self._is_record_paused:
                return True

        with self._writer_lock:
            if self._active_writer is not None and not self._active_writer.is_open:
                self._active_writer.reopen()

        with self._record_lock:
            self._record_start_monotonic = time.monotonic()
            self._is_record_paused = False

        return True

    def toggle_pause_recording(self) -> bool:
        """Toggle pause/resume on the active recording. Returns True if now paused."""
        if not self.is_recording:
            return False
        if self.is_record_paused:
            self.resume_recording()
            return False
        self.pause_recording()
        return True

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
        tone_duration_sec: float = 1.0,
        release_tone_duration_sec: float = 0.5,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Start recording from the selected hardware device to a WAV file."""
        self.stop_playback()
        # Release idle monitor handle so recording thread has exclusive ALSA capture access
        self._stop_monitoring_thread()

        self.set_talkback_tone_params(
            tone_hz=tone_hz,
            tone_duration_sec=tone_duration_sec,
            release_tone_duration_sec=release_tone_duration_sec,
        )

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
                release_tone_duration_sec=release_tone_duration_sec,
            )
            if self._local_talkback_active:
                processor.set_talkback(True)
            if self._remote_talkback_active:
                processor.set_remote_talkback(True)
            self._current_processor = processor
            self._current_recording_path = output_path
            self._stop_record_event.clear()
            self._is_recording = True
            self._is_record_paused = False
            self._record_accumulated_sec = 0.0
            self._record_start_monotonic = time.monotonic()

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

        with self._writer_lock:
            if self._active_writer is not None and self._active_writer.is_open:
                self._active_writer.close()
            self._active_writer = None

        with self._record_lock:
            self._is_recording = False
            self._is_record_paused = False
            self._record_accumulated_sec = 0.0
            self._current_processor = None
            self._current_recording_path = None

        # Resume live input monitoring so VU meter & Peak Hold remain active
        self._resume_monitoring_if_configured()

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
            with self._writer_lock:
                writer = WavFileWriter(
                    filepath=output_path,
                    sample_rate=sample_rate,
                    channels=processor.output_channels,
                    bit_depth=bit_depth,
                )
                self._active_writer = writer
                if self.is_record_paused:
                    writer.close()

            if device.device_id.startswith("sim:") or not self.alsa.available:
                self._record_simulated_loop(sample_rate, processor, writer)
            else:
                self._record_alsa_loop(device, sample_rate, bit_depth, processor, writer)

        except Exception as exc:
            logger.exception("Recording error on %s: %s", device.device_id, exc)
            if on_error:
                on_error(str(exc))
        finally:
            with self._writer_lock:
                if writer is not None:
                    writer.close()
                if self._active_writer is writer:
                    self._active_writer = None
            with self._record_lock:
                self._is_recording = False
                self._is_record_paused = False

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
            in_block = self._generate_simulated_input_block(chunk_frames, sample_rate, phase)
            phase += chunk_frames
            if self.is_mic_muted:
                in_block = np.zeros_like(in_block)

            metered = self._extract_metered_input(
                in_block,
                processor.studio_mode,
                processor.output_channels,
                processor.input_channel,
            )
            self._update_meter_from_block(metered)
            self._dispatch_live_input_block(metered, sample_rate)

            if not self.is_record_paused:
                with self._writer_lock:
                    if not self.is_record_paused:
                        if not writer.is_open:
                            writer.reopen()
                        out_block = processor.process_block(
                            in_block,
                            local_mic_muted=not self.is_talent_mic_live_to_producer,
                        )
                        writer.write_float_frames(out_block)

                        if t0 - last_header_flush >= 1.0:
                            writer.flush_header()
                            last_header_flush = t0
            else:
                with self._writer_lock:
                    if writer.is_open:
                        writer.close()

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
                if self.is_mic_muted:
                    in_float = np.zeros_like(in_float)
                metered = self._extract_metered_input(
                    in_float,
                    processor.studio_mode,
                    processor.output_channels,
                    processor.input_channel,
                )
                self._update_meter_from_block(metered)
                self._dispatch_live_input_block(metered, sample_rate)

                if not self.is_record_paused:
                    with self._writer_lock:
                        if not self.is_record_paused:
                            if not writer.is_open:
                                writer.reopen()
                            out_float = processor.process_block(
                                in_float,
                                local_mic_muted=not self.is_talent_mic_live_to_producer,
                            )
                            writer.write_float_frames(out_float)

                            now = time.monotonic()
                            if now - last_header_flush >= 1.0:
                                writer.flush_header()
                                last_header_flush = now
                else:
                    with self._writer_lock:
                        if writer.is_open:
                            writer.close()
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
        self._stop_monitoring_thread()

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

        self._resume_monitoring_if_configured()

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
            self._resume_monitoring_if_configured()
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
