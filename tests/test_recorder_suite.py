"""Unit tests for Alpine Recording Station audio DSP, WAV I/O, config persistence, and Samba admin."""

from __future__ import annotations

import datetime
import re
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.audio_engine import (
    SND_PCM_FORMAT_S16_LE,
    SND_PCM_FORMAT_S24_3LE,
    SND_PCM_FORMAT_S32_LE,
    StudioTalkbackProcessor,
    WavFileWriter,
    parse_proc_usb_stream0,
    read_wav_as_float64,
)
from app.config_manager import (
    ConfigManager,
    generate_recording_filename,
    sanitize_project_name,
)
from app.samba_manager import SambaManager


class TestStudioTalkbackProcessor(unittest.TestCase):
    def test_studio_mode_routing_and_half_second_tone(self) -> None:
        sample_rate = 48000
        proc = StudioTalkbackProcessor(
            sample_rate=sample_rate,
            studio_mode=True,
            output_channels=1,  # Studio mode should force 2 channels regardless
            input_channel=0,
            tone_hz=1000.0,
            tone_duration_sec=0.5,
            tone_amplitude=0.5,
        )
        self.assertEqual(proc.output_channels, 2)

        # Constant mic signal of +0.25 on channel 0
        block_1s = np.full((sample_rate, 1), 0.25, dtype=np.float64)

        # 1. Normal Studio state (Talkback OFF): Left = 0.25, Right = 0.0
        out_normal = proc.process_block(block_1s)
        self.assertEqual(out_normal.shape, (sample_rate, 2))
        np.testing.assert_allclose(out_normal[:, 0], 0.25)
        np.testing.assert_allclose(out_normal[:, 1], 0.0)

        # 2. Activate Talkback for 1.0 second (48,000 samples):
        #    - Left channel must be completely muted (0.0)
        #    - Right channel first 24,000 samples (0.5s) must be the 1000 Hz tone
        #    - Right channel remaining 24,000 samples (0.5s..1.0s) must be the mic signal (0.25)
        proc.set_talkback(True)
        out_tb = proc.process_block(block_1s)
        np.testing.assert_allclose(out_tb[:, 0], 0.0)

        half_sec_samples = sample_rate // 2
        tone_segment = out_tb[:half_sec_samples, 1]
        mic_segment = out_tb[half_sec_samples:, 1]

        # Verify tone segment has expected peak (~0.5) and zero crossings for 1000 Hz
        self.assertAlmostEqual(float(np.max(np.abs(tone_segment))), 0.5, places=2)
        # Verify mic segment after 0.5s tone equals the mic input (0.25)
        np.testing.assert_allclose(mic_segment, 0.25)

        # 3. Release Talkback: Left returns to Mic (0.25), Right returns to Silence (0.0)
        proc.set_talkback(False)
        out_released = proc.process_block(block_1s)
        np.testing.assert_allclose(out_released[:, 0], 0.25)
        np.testing.assert_allclose(out_released[:, 1], 0.0)

    def test_normal_mode_mono_and_stereo(self) -> None:
        sr = 44100
        in_stereo = np.column_stack(
            (np.full(100, 0.3, dtype=np.float64), np.full(100, -0.3, dtype=np.float64))
        )

        mono_proc = StudioTalkbackProcessor(
            sample_rate=sr, studio_mode=False, output_channels=1, input_channel=0
        )
        out_mono = mono_proc.process_block(in_stereo)
        self.assertEqual(out_mono.shape, (100, 1))
        np.testing.assert_allclose(out_mono[:, 0], 0.3)

        stereo_proc = StudioTalkbackProcessor(
            sample_rate=sr, studio_mode=False, output_channels=2, input_channel=0
        )
        out_stereo = stereo_proc.process_block(in_stereo)
        self.assertEqual(out_stereo.shape, (100, 2))
        np.testing.assert_allclose(out_stereo[:, 0], 0.3)
        np.testing.assert_allclose(out_stereo[:, 1], -0.3)


class TestWavReadWrite(unittest.TestCase):
    def test_wav_16_24_32_bit_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sr = 96000
            t = np.arange(1024, dtype=np.float64) / sr
            stereo_sig = np.column_stack(
                (0.5 * np.sin(2 * np.pi * 440 * t), -0.25 * np.cos(2 * np.pi * 880 * t))
            )

            for bit_depth, tol in ((16, 1e-4), (24, 1e-6), (32, 1e-7)):
                wav_path = tmp / f"test_{bit_depth}.wav"
                writer = WavFileWriter(wav_path, sample_rate=sr, channels=2, bit_depth=bit_depth)
                writer.write_float_frames(stereo_sig)
                writer.close()

                frames, read_sr, read_ch, read_bd = read_wav_as_float64(wav_path)
                self.assertEqual(read_sr, sr)
                self.assertEqual(read_ch, 2)
                self.assertEqual(read_bd, bit_depth)
                self.assertEqual(frames.shape, stereo_sig.shape)
                np.testing.assert_allclose(frames, stereo_sig, atol=tol)


class TestUsbStreamParser(unittest.TestCase):
    def test_parse_proc_usb_stream0(self) -> None:
        sample_stream0 = """
Focusrite Scarlett Solo USB at usb-0000:00:14.0-1, high speed : USB Audio

Playback:
  Status: Stop
  Interface 1
    Altset 1
    Format: S32_LE
    Channels: 2
    Endpoint: 0x01 (1 OUT) (ASYNC)
    Rates: 44100, 48000, 88200, 96000, 176400, 192000

Capture:
  Status: Stop
  Interface 2
    Altset 1
    Format: S16_LE
    Channels: 2
    Rates: 44100, 48000
  Interface 2
    Altset 2
    Format: S24_3LE
    Channels: 2
    Rates: 44100, 48000, 88200, 96000, 192000
"""
        rates, fmt_map, min_ch, max_ch = parse_proc_usb_stream0(sample_stream0)
        self.assertEqual(rates, [44100, 48000, 88200, 96000, 192000])
        self.assertIn(16, fmt_map)
        self.assertIn(24, fmt_map)
        self.assertEqual(fmt_map[16], SND_PCM_FORMAT_S16_LE)
        self.assertEqual(fmt_map[24], SND_PCM_FORMAT_S24_3LE)
        self.assertEqual(min_ch, 2)
        self.assertEqual(max_ch, 2)


class TestConfigAndProjectFiltering(unittest.TestCase):
    def test_filename_format_and_prefix_filtering(self) -> None:
        tz = datetime.timezone(datetime.timedelta(hours=-5), name="EST")
        dt = datetime.datetime(2026, 11, 5, 14, 22, 9, tzinfo=tz)
        fname = generate_recording_filename("Album Vocal", dt=dt)
        self.assertEqual(fname, "Album_Vocal_2026-11-05_14.22.09_EST.wav")
        self.assertTrue(
            re.match(r"^Album_Vocal_\d{4}-\d{2}-\d{2}_\d{2}\.\d{2}\.\d{2}_[A-Za-z0-9+-]+\.wav$", fname)
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            storage = base / "storage"
            etc = base / "etc"
            cm = ConfigManager(storage_dir=str(storage), etc_config_dir=str(etc))
            cm.settings.project_name = "PodcastA"
            cm.settings.sample_rate = 96000
            cm.settings.bit_depth = 24
            cm.settings.studio_mode = True
            cm.save()

            # Write 2 WAV files for PodcastA and 1 for OtherProject
            for name in (
                "PodcastA_2026-11-05_10.00.00_EST.wav",
                "PodcastA_2026-11-05_10.05.00_EST.wav",
                "OtherProject_2026-11-05_10.10.00_EST.wav",
            ):
                w = WavFileWriter(storage / name, sample_rate=48000, channels=2, bit_depth=24)
                w.write_float_frames(np.zeros((4800, 2), dtype=np.float64))
                w.close()

            podcast_takes = cm.list_project_recordings("PodcastA")
            self.assertEqual(len(podcast_takes), 2)
            self.assertTrue(all(t.filename.startswith("PodcastA_") for t in podcast_takes))

            other_takes = cm.list_project_recordings("OtherProject")
            self.assertEqual(len(other_takes), 1)
            self.assertEqual(other_takes[0].filename, "OtherProject_2026-11-05_10.10.00_EST.wav")

            # Verify reload persistence
            cm2 = ConfigManager(storage_dir=str(storage), etc_config_dir=str(etc))
            self.assertEqual(cm2.settings.project_name, "PodcastA")
            self.assertEqual(cm2.settings.sample_rate, 96000)
            self.assertEqual(cm2.settings.bit_depth, 24)
            self.assertTrue(cm2.settings.studio_mode)


class TestSambaManager(unittest.TestCase):
    def test_samba_user_validation_and_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            cm = ConfigManager(
                storage_dir=str(base / "storage"), etc_config_dir=str(base / "etc")
            )
            sm = SambaManager(cm)
            # Force simulated registry mode for unit testing
            sm.pdbedit_bin = None
            sm.smbpasswd_bin = None

            ok, _ = sm.add_user("root", "secret123")
            self.assertFalse(ok)

            ok, _ = sm.add_user("bad user!", "secret123")
            self.assertFalse(ok)

            ok, _ = sm.add_user("engineer1", "123")
            self.assertFalse(ok)

            ok, _ = sm.add_user("engineer1", "studioPass!")
            self.assertTrue(ok)
            self.assertEqual(sm.list_users(), ["engineer1"])

            ok, _ = sm.change_password("engineer1", "newStudioPass!")
            self.assertTrue(ok)

            ok, _ = sm.remove_user("engineer1")
            self.assertTrue(ok)
            self.assertEqual(sm.list_users(), [])


if __name__ == "__main__":
    unittest.main()
