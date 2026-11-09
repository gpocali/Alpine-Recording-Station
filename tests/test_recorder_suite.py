"""Unit tests for Alpine Recording Station audio DSP, WAV I/O, config persistence, and Samba admin."""

from __future__ import annotations

import datetime
import re
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.audio_engine import (
    METER_FLOOR_DB,
    SND_PCM_FORMAT_S16_LE,
    SND_PCM_FORMAT_S24_3LE,
    SND_PCM_FORMAT_S32_LE,
    AudioEngine,
    StudioTalkbackProcessor,
    WavFileWriter,
    dbfs_to_ratio,
    linear_to_dbfs,
    parse_proc_usb_stream0,
    read_wav_as_float64,
)
from app.config_manager import (
    ConfigManager,
    compute_wav_byte_rate,
    format_clock_display,
    format_remaining_hms,
    format_remaining_time,
    format_storage_bytes,
    generate_recording_filename,
    parse_proc_mounts,
    resolve_mount_for_path,
    sanitize_project_name,
)
from app.samba_manager import SambaManager


class TestStudioTalkbackProcessor(unittest.TestCase):
    def test_studio_mode_routing_default_1s_press_and_05s_release_tone(self) -> None:
        sample_rate = 48000
        proc = StudioTalkbackProcessor(
            sample_rate=sample_rate,
            studio_mode=True,
            output_channels=1,  # Studio mode should force 2 channels regardless
            input_channel=0,
            tone_hz=1000.0,
            tone_duration_sec=1.0,
            release_tone_duration_sec=0.5,
            tone_amplitude=0.5,
        )
        self.assertEqual(proc.output_channels, 2)

        # Constant mic signal of +0.25 on channel 0
        block_1s = np.full((sample_rate, 1), 0.25, dtype=np.float64)
        block_2s = np.full((sample_rate * 2, 1), 0.25, dtype=np.float64)

        # 1. Normal Studio state (Talkback OFF): Left = 0.25, Right = 0.0 (no release tone initially)
        out_normal = proc.process_block(block_1s)
        self.assertEqual(out_normal.shape, (sample_rate, 2))
        np.testing.assert_allclose(out_normal[:, 0], 0.25)
        np.testing.assert_allclose(out_normal[:, 1], 0.0)

        # 2. Activate Talkback for 2.0 seconds (96,000 samples):
        #    - Left channel must be completely muted (0.0)
        #    - Right channel first 48,000 samples (1.0s) must be the 1000 Hz press tone
        #    - Right channel remaining 48,000 samples (1.0s..2.0s) must be the mic signal (0.25)
        proc.set_talkback(True)
        out_tb = proc.process_block(block_2s)
        np.testing.assert_allclose(out_tb[:, 0], 0.0)

        one_sec_samples = sample_rate
        press_tone_segment = out_tb[:one_sec_samples, 1]
        mic_segment = out_tb[one_sec_samples:, 1]

        # Verify 1.0s press tone segment has expected peak (~0.5)
        self.assertAlmostEqual(float(np.max(np.abs(press_tone_segment))), 0.5, places=2)
        # Verify mic segment after 1.0s press tone equals the mic input (0.25)
        np.testing.assert_allclose(mic_segment, 0.25)

        # 3. Release Talkback (conclude talkback mode):
        #    - Left channel returns to Mic (0.25)
        #    - Right channel first 24,000 samples (0.5s) must be the release tone
        #    - Right channel remaining 24,000 samples (0.5s..1.0s) must return to Silence (0.0)
        proc.set_talkback(False)
        out_released = proc.process_block(block_1s)
        np.testing.assert_allclose(out_released[:, 0], 0.25)

        half_sec_samples = sample_rate // 2
        release_tone_segment = out_released[:half_sec_samples, 1]
        silent_segment = out_released[half_sec_samples:, 1]
        self.assertAlmostEqual(float(np.max(np.abs(release_tone_segment))), 0.5, places=2)
        np.testing.assert_allclose(silent_segment, 0.0)

        # 4. Subsequent blocks remain silent on Right channel
        out_after = proc.process_block(block_1s)
        np.testing.assert_allclose(out_after[:, 0], 0.25)
        np.testing.assert_allclose(out_after[:, 1], 0.0)

    def test_custom_tone_frequency_and_durations(self) -> None:
        sample_rate = 48000
        proc = StudioTalkbackProcessor(
            sample_rate=sample_rate,
            studio_mode=True,
            output_channels=2,
            input_channel=0,
            tone_hz=440.0,
            tone_duration_sec=0.5,
            release_tone_duration_sec=0.25,
            tone_amplitude=0.5,
        )
        block_1s = np.full((sample_rate, 1), 0.25, dtype=np.float64)

        proc.set_talkback(True)
        out_tb = proc.process_block(block_1s)
        press_samples = int(0.5 * sample_rate)
        self.assertAlmostEqual(float(np.max(np.abs(out_tb[:press_samples, 1]))), 0.5, places=2)
        np.testing.assert_allclose(out_tb[press_samples:, 1], 0.25)

        proc.set_talkback(False)
        out_rel = proc.process_block(block_1s)
        rel_samples = int(0.25 * sample_rate)
        self.assertAlmostEqual(float(np.max(np.abs(out_rel[:rel_samples, 1]))), 0.5, places=2)
        np.testing.assert_allclose(out_rel[rel_samples:, 1], 0.0)

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

    def test_wav_pause_close_and_reopen_resume_same_file(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sr = 48000
            part1 = np.full((1200, 2), 0.25, dtype=np.float64)
            part2 = np.full((800, 2), -0.50, dtype=np.float64)
            expected = np.vstack((part1, part2))

            for bit_depth, tol in ((16, 1e-4), (24, 1e-6), (32, 1e-7)):
                wav_path = tmp / f"pause_resume_{bit_depth}.wav"
                writer = WavFileWriter(
                    wav_path, sample_rate=sr, channels=2, bit_depth=bit_depth
                )
                writer.write_float_frames(part1)
                self.assertTrue(writer.is_open)

                # Simulate Pause: close the file on disk so headers are finalized
                writer.close()
                self.assertFalse(writer.is_open)

                # Verify file on disk is completely valid while paused
                paused_frames, p_sr, p_ch, p_bd = read_wav_as_float64(wav_path)
                self.assertEqual((p_sr, p_ch, p_bd), (sr, 2, bit_depth))
                self.assertEqual(paused_frames.shape, part1.shape)
                np.testing.assert_allclose(paused_frames, part1, atol=tol)

                # Simulate Resume: reopen the exact same file and append part2
                writer.reopen()
                self.assertTrue(writer.is_open)
                writer.write_float_frames(part2)
                writer.close()
                self.assertFalse(writer.is_open)

                full_frames, f_sr, f_ch, f_bd = read_wav_as_float64(wav_path)
                self.assertEqual((f_sr, f_ch, f_bd), (sr, 2, bit_depth))
                self.assertEqual(full_frames.shape, expected.shape)
                np.testing.assert_allclose(full_frames, expected, atol=tol)

            # Verify AudioEngine pause_recording() and resume_recording() lifecycle
            engine = AudioEngine(simulate_if_empty=True)
            devs = engine.get_capture_devices()
            self.assertTrue(len(devs) >= 1)
            rec_file = tmp / "engine_pause_resume.wav"

            engine.start_recording(
                device=devs[0],
                output_path=rec_file,
                sample_rate=48000,
                bit_depth=24,
                studio_mode=True,
                channels=2,
            )
            time.sleep(0.12)
            self.assertTrue(engine.is_recording)
            self.assertFalse(engine.is_record_paused)

            # Pause recording -> file must be closed on disk and valid
            self.assertTrue(engine.pause_recording())
            self.assertTrue(engine.is_recording)
            self.assertTrue(engine.is_record_paused)
            self.assertFalse(engine.is_recording_file_open)
            paused_elapsed = engine.elapsed_seconds
            size_at_pause = rec_file.stat().st_size
            self.assertGreater(size_at_pause, 44)

            time.sleep(0.10)
            # Elapsed timer and file size must stay frozen while paused
            self.assertAlmostEqual(engine.elapsed_seconds, paused_elapsed, places=3)
            self.assertEqual(rec_file.stat().st_size, size_at_pause)

            # Resume recording -> same file is reopened and grows
            self.assertTrue(engine.resume_recording())
            self.assertTrue(engine.is_recording)
            self.assertFalse(engine.is_record_paused)
            time.sleep(0.12)

            finished = engine.stop_recording()
            self.assertEqual(finished, rec_file)
            self.assertFalse(engine.is_recording)
            self.assertFalse(engine.is_record_paused)
            self.assertGreater(rec_file.stat().st_size, size_at_pause)


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
            self.assertEqual(cm.settings.talkback_tone_duration_sec, 1.0)
            self.assertEqual(cm.settings.talkback_release_tone_duration_sec, 0.5)
            self.assertEqual(cm.settings.talkback_tone_hz, 1000.0)
            cm.settings.project_name = "PodcastA"
            cm.settings.sample_rate = 96000
            cm.settings.bit_depth = 24
            cm.settings.studio_mode = True
            cm.settings.talkback_tone_hz = 440.0
            cm.settings.talkback_tone_duration_sec = 1.5
            cm.settings.talkback_release_tone_duration_sec = 0.75
            cm.settings.on_screen_keyboard = False
            cm.save()

            # Write 2 WAV files for PodcastA in its project subfolder and 1 for OtherProject
            podcast_dir = cm.ensure_project_dir("PodcastA")
            other_dir = cm.ensure_project_dir("OtherProject")
            for folder, name in (
                (podcast_dir, "PodcastA_2026-11-05_10.00.00_EST.wav"),
                (podcast_dir, "PodcastA_2026-11-05_10.05.00_EST.wav"),
                (other_dir, "OtherProject_2026-11-05_10.10.00_EST.wav"),
            ):
                w = WavFileWriter(folder / name, sample_rate=48000, channels=2, bit_depth=24)
                w.write_float_frames(np.zeros((4800, 2), dtype=np.float64))
                w.close()

            podcast_takes = cm.list_project_recordings("PodcastA")
            self.assertEqual(len(podcast_takes), 2)
            self.assertTrue(all(t.filename.startswith("PodcastA_") for t in podcast_takes))

            # Rename one of the takes inside PodcastA/ so it can be categorized directly
            ok, msg, renamed_path = cm.rename_recording(
                podcast_dir / "PodcastA_2026-11-05_10.05.00_EST.wav",
                "Intro_Segment_Select",
            )
            self.assertTrue(ok, msg)
            self.assertIsNotNone(renamed_path)
            if renamed_path is not None:
                self.assertEqual(renamed_path.name, "Intro_Segment_Select.wav")
                self.assertEqual(renamed_path.parent, podcast_dir)
                self.assertTrue(renamed_path.is_file())

            # Renamed file remains in the PodcastA project recordings list
            podcast_takes_after = cm.list_project_recordings("PodcastA")
            self.assertEqual(len(podcast_takes_after), 2)
            filenames_after = {t.filename for t in podcast_takes_after}
            self.assertIn("Intro_Segment_Select.wav", filenames_after)
            self.assertIn("PodcastA_2026-11-05_10.00.00_EST.wav", filenames_after)

            # Prevent overwriting an existing file in the same project folder
            dup_ok, _dup_msg, dup_path = cm.rename_recording(
                podcast_dir / "PodcastA_2026-11-05_10.00.00_EST.wav",
                "Intro_Segment_Select.wav",
            )
            self.assertFalse(dup_ok)
            self.assertIsNone(dup_path)

            other_takes = cm.list_project_recordings("OtherProject")
            self.assertEqual(len(other_takes), 1)
            self.assertEqual(other_takes[0].filename, "OtherProject_2026-11-05_10.10.00_EST.wav")

            # Verify reload persistence
            cm2 = ConfigManager(storage_dir=str(storage), etc_config_dir=str(etc))
            self.assertEqual(cm2.settings.project_name, "PodcastA")
            self.assertEqual(cm2.settings.sample_rate, 96000)
            self.assertEqual(cm2.settings.bit_depth, 24)
            self.assertTrue(cm2.settings.studio_mode)
            self.assertEqual(cm2.settings.talkback_tone_hz, 440.0)
            self.assertEqual(cm2.settings.talkback_tone_duration_sec, 1.5)
            self.assertEqual(cm2.settings.talkback_release_tone_duration_sec, 0.75)
            self.assertFalse(cm2.settings.on_screen_keyboard)
            self.assertEqual(cm2.settings.clock_time_format, "12h")
            self.assertEqual(cm2.settings.clock_date_format, "YYYY-MM-DD")

            cm2.settings.clock_time_format = "24h"
            cm2.settings.clock_date_format = "DD/MM/YYYY"
            cm2.save()

            cm3 = ConfigManager(storage_dir=str(storage), etc_config_dir=str(etc))
            self.assertEqual(cm3.settings.clock_time_format, "24h")
            self.assertEqual(cm3.settings.clock_date_format, "DD/MM/YYYY")

    def test_studio_clock_time_and_regional_date_formatting(self) -> None:
        tz = datetime.timezone(datetime.timedelta(hours=-5), name="EST")
        dt_pm = datetime.datetime(2026, 11, 8, 20, 5, 30, tzinfo=tz)
        dt_midnight = datetime.datetime(2026, 1, 15, 0, 9, 4, tzinfo=tz)
        dt_noon = datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=tz)

        # Default: 12-hour with YYYY-MM-DD
        self.assertEqual(
            format_clock_display(dt=dt_pm),
            "2026-11-08  •  08:05:30 PM",
        )
        self.assertEqual(
            format_clock_display(time_format="12h", date_format="YYYY-MM-DD", dt=dt_midnight),
            "2026-01-15  •  12:09:04 AM",
        )
        self.assertEqual(
            format_clock_display(time_format="12h", date_format="YYYY-MM-DD", dt=dt_noon),
            "2026-07-04  •  12:00:00 PM",
        )

        # 24-hour option
        self.assertEqual(
            format_clock_display(time_format="24h", date_format="YYYY-MM-DD", dt=dt_pm),
            "2026-11-08  •  20:05:30",
        )
        self.assertEqual(
            format_clock_display(time_format="24h", date_format="YYYY-MM-DD", dt=dt_midnight),
            "2026-01-15  •  00:09:04",
        )

        # Regional date formats
        self.assertEqual(
            format_clock_display(time_format="12h", date_format="MM/DD/YYYY", dt=dt_pm),
            "11/08/2026  •  08:05:30 PM",
        )
        self.assertEqual(
            format_clock_display(time_format="24h", date_format="DD/MM/YYYY", dt=dt_pm),
            "08/11/2026  •  20:05:30",
        )
        self.assertEqual(
            format_clock_display(time_format="24h", date_format="DD.MM.YYYY", dt=dt_pm),
            "08.11.2026  •  20:05:30",
        )
        self.assertEqual(
            format_clock_display(time_format="12h", date_format="DD MMM YYYY", dt=dt_pm),
            "08 Nov 2026  •  08:05:30 PM",
        )
        self.assertEqual(
            format_clock_display(time_format="12h", date_format="MMM DD, YYYY", dt=dt_pm),
            "Nov 08, 2026  •  08:05:30 PM",
        )
        self.assertEqual(
            format_clock_display(time_format="12h", date_format="none", dt=dt_pm),
            "08:05:30 PM",
        )

    def test_recursive_storage_permission_masking(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            storage = base / "storage"
            etc = base / "etc"
            cm = ConfigManager(storage_dir=str(storage), etc_config_dir=str(etc))

            nested_dir = storage / "imported" / "session_sub"
            nested_dir.mkdir(parents=True, exist_ok=True)
            nested_file = nested_dir / "external_take.wav"
            nested_file.write_bytes(b"RIFF....WAVE")

            # Simulate restrictive permissions on newly appeared directory & file
            os.chmod(nested_file, 0o400)
            os.chmod(nested_dir, 0o700)

            cm.enforce_storage_permissions()

            file_mode = nested_file.stat().st_mode & 0o666
            dir_mode = nested_dir.stat().st_mode & 0o777
            self.assertEqual(file_mode & 0o600, 0o600)
            self.assertEqual(dir_mode & 0o700, 0o700)
            if os.name != "nt":
                self.assertEqual(file_mode, 0o666)
                self.assertEqual(dir_mode, 0o777)
            # Clean up read-only bits on Windows if any before temp dir cleanup
            os.chmod(nested_file, 0o666)
            os.chmod(nested_dir, 0o777)


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


class TestVuMeterAndPeakHold(unittest.TestCase):
    def test_dbfs_conversions_and_peak_hold_maintenance(self) -> None:
        self.assertAlmostEqual(linear_to_dbfs(1.0), 0.0, places=3)
        self.assertAlmostEqual(linear_to_dbfs(0.5), -6.0206, places=2)
        self.assertAlmostEqual(linear_to_dbfs(0.0), METER_FLOOR_DB, places=3)
        self.assertAlmostEqual(dbfs_to_ratio(-60.0), 0.0, places=3)
        self.assertAlmostEqual(dbfs_to_ratio(-30.0), 0.5, places=3)
        self.assertAlmostEqual(dbfs_to_ratio(0.0), 1.0, places=3)

        engine = AudioEngine(simulate_if_empty=True)

        # Feed a moderate stereo block (-12 dBFS ~ 0.2512)
        block_mod = np.column_stack(
            (np.full(512, 0.25, dtype=np.float64), np.full(512, 0.125, dtype=np.float64))
        )
        engine._update_meter_from_block(block_mod)
        r1 = engine.get_vu_readings()
        self.assertEqual(r1.channels, 2)
        self.assertAlmostEqual(r1.peak_hold_level, 0.25, places=4)
        self.assertAlmostEqual(r1.channel_peak_holds[0], 0.25, places=4)
        self.assertAlmostEqual(r1.channel_peak_holds[1], 0.125, places=4)
        self.assertFalse(r1.clipped)

        # Feed a transient peak block (0.8 ~ -1.94 dBFS) on Left channel
        block_peak = np.column_stack(
            (np.full(512, 0.80, dtype=np.float64), np.full(512, 0.20, dtype=np.float64))
        )
        engine._update_meter_from_block(block_peak)

        # Feed several silent blocks; Peak Hold must maintain at 0.80
        block_silent = np.zeros((512, 2), dtype=np.float64)
        for _ in range(10):
            engine._update_meter_from_block(block_silent)

        r2 = engine.get_vu_readings()
        self.assertAlmostEqual(r2.peak_hold_level, 0.80, places=4)
        self.assertAlmostEqual(r2.channel_peak_holds[0], 0.80, places=4)
        self.assertAlmostEqual(r2.channel_peak_holds[1], 0.20, places=4)
        self.assertLess(r2.peak_level, 0.80)

        # Trigger clip (>= 0.99) and verify clip latch + reset_peak_hold()
        block_clip = np.full((512, 2), 0.995, dtype=np.float64)
        engine._update_meter_from_block(block_clip)
        r3 = engine.get_vu_readings()
        self.assertTrue(r3.clipped)
        self.assertAlmostEqual(r3.peak_hold_level, 0.995, places=4)

        engine.reset_peak_hold()
        r4 = engine.get_vu_readings()
        self.assertFalse(r4.clipped)
        self.assertEqual(r4.peak_hold_level, 0.0)
        self.assertEqual(r4.channel_peak_holds, (0.0, 0.0))


class TestOnScreenKeyboard(unittest.TestCase):
    def test_on_screen_keyboard_editing_and_field_switching(self) -> None:
        try:
            import os

            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication, QLineEdit
            from app.ui.on_screen_keyboard import OnScreenKeyboardDialog, OskFieldSpec
        except Exception:
            self.skipTest("PyQt6 not available in test environment")
            return

        app = QApplication.instance() or QApplication([])

        proj_edit = QLineEdit("Session")
        dlg = OnScreenKeyboardDialog(target=proj_edit, title="Project Prefix")
        dlg.press_clear()
        dlg.toggle_shift()
        dlg.press_key("V")
        dlg.press_key("o")
        dlg.press_key("c")
        dlg.press_key("a")
        dlg.press_key("l")
        dlg.press_key("_")
        dlg.press_key("1")
        dlg.press_backspace()
        dlg.press_key("2")
        dlg.accept()
        self.assertEqual(proj_edit.text(), "Vocal_2")

        # Verify Cancel restores initial text
        dlg_cancel = OnScreenKeyboardDialog(target=proj_edit, title="Project Prefix")
        dlg_cancel.press_clear()
        dlg_cancel.press_key("X")
        dlg_cancel.reject()
        self.assertEqual(proj_edit.text(), "Vocal_2")

        # Verify multi-field switching (e.g. Samba Username -> Password -> Confirm)
        user_edit = QLineEdit("")
        pw_edit = QLineEdit("")
        pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        siblings = [
            OskFieldSpec(user_edit, "Samba Username"),
            OskFieldSpec(pw_edit, "Samba Password"),
        ]
        dlg_multi = OnScreenKeyboardDialog(
            target=user_edit,
            title="Samba Username",
            sibling_fields=siblings,
        )
        for ch in "eng1":
            dlg_multi.press_key(ch)
        dlg_multi.switch_to_next_field()
        self.assertEqual(user_edit.text(), "eng1")
        self.assertIs(dlg_multi.target_line_edit, pw_edit)
        for ch in "pass99":
            dlg_multi.press_key(ch)
        dlg_multi.accept()
        self.assertEqual(pw_edit.text(), "pass99")
        _ = app


class TestPostBootServiceAndRecoveryConfig(unittest.TestCase):
    def test_scripts_have_lf_line_endings_and_post_boot_vt7_service(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        scripts = [
            repo_root / "install.sh",
            repo_root / "config" / "alpine-recorder",
            repo_root / "config" / "alpine-recorder.initd",
            repo_root / "config" / "xinitrc",
        ]
        for script in scripts:
            self.assertTrue(script.is_file(), f"Missing script: {script}")
            raw = script.read_bytes()
            self.assertNotIn(b"\r\n", raw, f"Script contains CRLF line endings: {script}")

        install_text = (repo_root / "install.sh").read_text(encoding="utf-8")
        # Ensure install.sh cleans up legacy tty1 autologin/autostart rather than installing them
        self.assertNotIn("exec /bin/login -f root", install_text)
        self.assertNotIn("exec startx /root/.xinitrc", install_text)
        self.assertNotIn("apk add --no-cache", install_text)
        self.assertIn("APK_CACHE_DIR=", install_text)
        self.assertIn("apk cache sync", install_text)
        self.assertIn("ALPINE_RECORDER_KIOSK_AUTOSTART", install_text)
        self.assertIn("/etc/init.d/alpine-recorder", install_text)
        self.assertIn("/usr/local/bin/alpine-recorder", install_text)
        self.assertIn("lbu exclude /var/lib/samba/private/msg.sock", install_text)

        initd_text = (repo_root / "config" / "alpine-recorder.initd").read_text(encoding="utf-8")
        self.assertIn("#!/sbin/openrc-run", initd_text)
        self.assertIn("after bootmisc modules udev eudev alsa samba local networking", initd_text)

        launcher_text = (repo_root / "config" / "alpine-recorder").read_text(encoding="utf-8")
        self.assertIn("run_doctor", launcher_text)
        self.assertIn("ensure_runtime_packages", launcher_text)
        self.assertIn("fix-packages", launcher_text)
        self.assertIn("do_recover", launcher_text)
        self.assertIn("max_fast_fails=3", launcher_text)
        self.assertNotIn("-nocursor", launcher_text)

        xinit_text = (repo_root / "config" / "xinitrc").read_text(encoding="utf-8")
        self.assertIn("ALPINE_RECORDER_SINGLE_RUN", xinit_text)
        self.assertIn("fast_crashes", xinit_text)
        self.assertIn("xsetroot -cursor_name left_ptr", xinit_text)

        smb_text = (repo_root / "config" / "smb.conf.template").read_text(encoding="utf-8")
        self.assertIn("admin users = @sambashare", smb_text)
        self.assertIn("force user = root", smb_text)
        self.assertIn("force group = sambashare", smb_text)
        self.assertIn("inherit owner = windows and unix", smb_text)
        self.assertIn("create mask = 0777", smb_text)
        self.assertIn("force create mode = 0666", smb_text)
        self.assertIn("directory mask = 2777", smb_text)
        self.assertIn("force directory mode = 2777", smb_text)
        self.assertIn("nt acl support = no", smb_text)
        self.assertIn("map readonly = no", smb_text)


class TestComboBoxDropdownStyles(unittest.TestCase):
    def test_combo_box_popup_stylesheet_and_settings_dropdowns(self) -> None:
        from app.ui.styles import (
            build_combo_popup_stylesheet,
            build_main_stylesheet,
            configure_combo_box_popup,
        )

        main_qss = build_main_stylesheet(1.0)
        self.assertIn("QComboBox QAbstractItemView", main_qss)
        self.assertIn("QComboBox QAbstractItemView::item", main_qss)
        self.assertIn("QComboBox QAbstractItemView::item:selected", main_qss)

        popup_qss = build_combo_popup_stylesheet(1.0)
        self.assertIn("QAbstractItemView", popup_qss)
        self.assertIn("background-color: #161B22;", popup_qss)
        self.assertIn("color: #F0F6FC;", popup_qss)
        self.assertIn("selection-background-color: #1F6FEB;", popup_qss)
        self.assertIn("selection-color: #FFFFFF;", popup_qss)

        try:
            import os

            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtGui import QPalette
            from PyQt6.QtWidgets import QApplication, QComboBox, QStyledItemDelegate
            from app.ui.settings_dialog import SettingsDialog
        except Exception:
            return

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            cm = ConfigManager(
                storage_dir=str(base / "storage"), etc_config_dir=str(base / "etc")
            )
            engine = AudioEngine(simulate_if_empty=True)
            sm = SambaManager(cm)
            sm.pdbedit_bin = None
            sm.smbpasswd_bin = None

            dlg = SettingsDialog(
                config_manager=cm,
                audio_engine=engine,
                samba_manager=sm,
            )
            combos = dlg.findChildren(QComboBox)
            self.assertGreaterEqual(len(combos), 11)
            self.assertEqual(float(dlg.press_tone_duration_combo.currentData()), 1.0)
            self.assertEqual(float(dlg.release_tone_duration_combo.currentData()), 0.5)
            self.assertEqual(float(dlg.tone_freq_combo.currentData()), 1000.0)
            for combo in combos:
                self.assertIsInstance(combo.itemDelegate(), QStyledItemDelegate)
                view = combo.view()
                self.assertIsNotNone(view)
                if view is not None:
                    self.assertEqual(
                        view.palette().color(QPalette.ColorRole.Base).name().upper(),
                        "#161B22",
                    )
                    self.assertEqual(
                        view.palette().color(QPalette.ColorRole.Text).name().upper(),
                        "#F0F6FC",
                    )
        _ = (app, configure_combo_box_popup)


class TestStorageMonitoringAndRemainingTime(unittest.TestCase):
    def test_byte_rate_and_remaining_time_calculations(self) -> None:
        # 24-bit (3 bytes) * 48,000 Hz * 1 ch (Mono) = 144,000 bytes/sec
        self.assertEqual(
            compute_wav_byte_rate(48000, 24, channels=1, studio_mode=False),
            144000,
        )
        # Studio mode forces 2 channels -> 288,000 bytes/sec
        self.assertEqual(
            compute_wav_byte_rate(48000, 24, channels=1, studio_mode=True),
            288000,
        )
        # 32-bit (4 bytes) * 96,000 Hz * 2 ch (Stereo) = 768,000 bytes/sec
        self.assertEqual(
            compute_wav_byte_rate(96000, 32, channels=2, studio_mode=False),
            768000,
        )

        self.assertEqual(format_storage_bytes(512), "512 B")
        self.assertEqual(format_storage_bytes(2048), "2.0 KB")
        self.assertEqual(format_storage_bytes(10 * 1024 * 1024), "10.0 MB")
        self.assertEqual(format_storage_bytes(25 * (1024 ** 3)), "25.0 GB")
        self.assertEqual(format_remaining_time(3661.9), "01h 01m 01s")
        self.assertEqual(format_remaining_hms(3661.9), "01:01:01")
        self.assertEqual(format_remaining_time(125 * 3600 + 1800), "125h 30m")

    def test_storage_status_utilization_thresholds_and_settings_scaling(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            cm = ConfigManager(
                storage_dir=str(base / "storage"), etc_config_dir=str(base / "etc")
            )
            cm.settings.sample_rate = 48000
            cm.settings.bit_depth = 24
            cm.settings.channels = 1
            cm.settings.studio_mode = False

            # 1. Healthy drive: 50% used, 10 hours remaining at 48k/24b Mono (144,000 B/s)
            free_10h = 10 * 3600 * 144000
            total_cap = free_10h * 2
            st_ok = cm.get_storage_status(
                disk_usage_fn=lambda _p: (total_cap, free_10h, free_10h)
            )
            self.assertEqual(st_ok.level, "ok")
            self.assertFalse(st_ok.is_warning)
            self.assertFalse(st_ok.is_critical)
            self.assertAlmostEqual(st_ok.used_percent, 50.0, places=2)
            self.assertAlmostEqual(st_ok.remaining_seconds, 36000.0, places=2)
            self.assertEqual(st_ok.formatted_remaining, "10h 00m 00s")

            # Switching to Studio Mode (2 channels) halves remaining recording time to 5 hours
            st_studio = cm.get_storage_status(
                studio_mode=True,
                disk_usage_fn=lambda _p: (total_cap, free_10h, free_10h),
            )
            self.assertEqual(st_studio.effective_channels, 2)
            self.assertAlmostEqual(st_studio.remaining_seconds, 18000.0, places=2)
            self.assertEqual(st_studio.formatted_remaining, "05h 00m 00s")

            # 2. Warning threshold by drive utilization (85% used, even with plenty of time)
            st_warn_pct = cm.get_storage_status(
                disk_usage_fn=lambda _p: (100 * (1024 ** 3), 85 * (1024 ** 3), 15 * (1024 ** 3))
            )
            self.assertEqual(st_warn_pct.level, "warning")
            self.assertTrue(st_warn_pct.is_warning)
            self.assertFalse(st_warn_pct.is_critical)
            self.assertAlmostEqual(st_warn_pct.used_percent, 85.0, places=2)

            # 3. Warning threshold by low remaining recording time (20 minutes left, even at 40% used)
            free_20m = 20 * 60 * 144000
            st_warn_time = cm.get_storage_status(
                disk_usage_fn=lambda _p: (free_20m * 2, free_20m, free_20m)
            )
            self.assertEqual(st_warn_time.level, "warning")
            self.assertEqual(st_warn_time.formatted_remaining, "00h 20m 00s")

            # 4. Critical threshold by drive utilization (94% used)
            st_crit_pct = cm.get_storage_status(
                disk_usage_fn=lambda _p: (100 * (1024 ** 3), 94 * (1024 ** 3), 6 * (1024 ** 3))
            )
            self.assertEqual(st_crit_pct.level, "critical")
            self.assertTrue(st_crit_pct.is_critical)
            self.assertAlmostEqual(st_crit_pct.used_percent, 94.0, places=2)

            # 5. Critical threshold by low remaining time (5 minutes left)
            free_5m = 5 * 60 * 144000
            st_crit_time = cm.get_storage_status(
                disk_usage_fn=lambda _p: (free_5m * 4, free_5m * 3, free_5m)
            )
            self.assertEqual(st_crit_time.level, "critical")
            self.assertEqual(st_crit_time.formatted_remaining, "00h 05m 00s")

            # 6. Full drive (0 free bytes)
            st_full = cm.get_storage_status(
                disk_usage_fn=lambda _p: (100 * (1024 ** 3), 100 * (1024 ** 3), 0)
            )
            self.assertEqual(st_full.level, "full")
            self.assertTrue(st_full.is_critical)
            self.assertEqual(st_full.remaining_seconds, 0.0)
            self.assertEqual(st_full.formatted_remaining, "00h 00m 00s")

    def test_proc_mounts_persistent_drive_resolution(self) -> None:
        sample_mounts = """
rootfs / rootfs rw 0 0
tmpfs / tmpfs rw,relatime,size=1982464k,mode=755 0 0
devtmpfs /dev devtmpfs rw,relatime,size=10240k,nr_inodes=493021,mode=755 0 0
/dev/sda1 /media/sda1 vfat rw,relatime,fmask=0022,dmask=0022 0 0
/dev/sda2 /storage ext4 rw,noatime 0 0
"""
        mounts = parse_proc_mounts(sample_mounts)
        _p, dev, mnt, fstype, is_temp = resolve_mount_for_path(Path("/storage"), mounts)
        self.assertEqual(dev, "/dev/sda2")
        self.assertEqual(mnt, "/storage")
        self.assertEqual(fstype, "ext4")
        self.assertFalse(is_temp)

        # When /storage is not mounted yet and falls on root tmpfs
        unmounted_text = """
rootfs / rootfs rw 0 0
tmpfs / tmpfs rw,relatime,size=1982464k,mode=755 0 0
"""
        mounts_unmounted = parse_proc_mounts(unmounted_text)
        _p2, dev2, mnt2, fstype2, is_temp2 = resolve_mount_for_path(
            Path("/storage"), mounts_unmounted
        )
        self.assertEqual(dev2, "tmpfs")
        self.assertEqual(mnt2, "/")
        self.assertEqual(fstype2, "tmpfs")
        self.assertTrue(is_temp2)

    def test_storage_monitor_widget_and_settings_preview(self) -> None:
        try:
            import os

            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication
            from app.ui.storage_monitor import StorageMonitorWidget
            from app.ui.settings_dialog import SettingsDialog
        except Exception:
            return

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            cm = ConfigManager(
                storage_dir=str(base / "storage"), etc_config_dir=str(base / "etc")
            )
            widget = StorageMonitorWidget()
            self.assertFalse(hasattr(widget, "ring_gauge"))

            st_ok = cm.get_storage_status(
                sample_rate=48000,
                bit_depth=24,
                channels=1,
                studio_mode=False,
                disk_usage_fn=lambda _p: (100_000_000_000, 35_000_000_000, 65_000_000_000),
            )
            widget.set_status(st_ok)
            self.assertEqual(widget.value(), 35)
            self.assertIn("SPACE OK", widget.status_badge.text())
            self.assertIn("TIME REMAINING:", widget.remaining_label.text())

            st_warn = cm.get_storage_status(
                sample_rate=48000,
                bit_depth=24,
                channels=2,
                studio_mode=True,
                disk_usage_fn=lambda _p: (100_000_000_000, 84_000_000_000, 16_000_000_000),
            )
            widget.set_status(st_warn)
            self.assertEqual(widget.value(), 84)
            self.assertIn("LOW SPACE", widget.status_badge.text())
            self.assertIn("GETTING FULL", widget.title_label.text())

            st_crit = cm.get_storage_status(
                sample_rate=96000,
                bit_depth=32,
                channels=2,
                studio_mode=True,
                disk_usage_fn=lambda _p: (100_000_000_000, 95_000_000_000, 5_000_000_000),
            )
            widget.set_status(st_crit, is_recording=True)
            self.assertEqual(widget.value(), 95)
            self.assertIn("CRITICAL", widget.status_badge.text())
            self.assertIn("CRITICALLY FULL", widget.title_label.text())

            # Verify SettingsDialog live storage preview updates when format settings change
            engine = AudioEngine(simulate_if_empty=True)
            sm = SambaManager(cm)
            sm.pdbedit_bin = None
            sm.smbpasswd_bin = None
            dlg = SettingsDialog(
                config_manager=cm,
                audio_engine=engine,
                samba_manager=sm,
            )
            self.assertIn("Time Remaining:", dlg.storage_info_label.text())
            self.assertIn("/storage Utilization:", dlg.storage_info_label.text())
        _ = app


class TestUiGlyphsAndButtonMnemonics(unittest.TestCase):
    def test_dejavu_compatible_icons_and_escaped_button_ampersands(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        ui_dir = repo_root / "app" / "ui"
        for py_file in sorted(ui_dir.glob("*.py")):
            content = py_file.read_text(encoding="utf-8")
            # Ensure no Unicode 7.0 U+23F8 or SMP emojis (U+10000+, e.g. U+1F399)
            # that render as missing-glyph boxes in Alpine's ttf-dejavu fonts
            self.assertNotIn("\u23f8", content, f"Unsupported U+23F8 pause char in {py_file.name}")
            self.assertNotIn("\U0001f399", content, f"Unsupported U+1F399 mic emoji in {py_file.name}")
            non_bmp = [ch for ch in content if ord(ch) > 0xFFFF]
            self.assertEqual(
                non_bmp,
                [],
                f"Non-BMP character(s) found in {py_file.name}: {[hex(ord(c)) for c in non_bmp]}",
            )

        main_win_text = (ui_dir / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("❚❚", main_win_text)
        self.assertIn("◉  TALKBACK", main_win_text)
        self.assertIn("STOP && SAVE", main_win_text)
        self.assertIn("■  STOP\\nRECORDING", main_win_text)
        self.assertNotIn('"■   STOP & SAVE"', main_win_text)
        self.assertNotIn("mode_toggle_btn", main_win_text)

        settings_text = (ui_dir / "settings_dialog.py").read_text(encoding="utf-8")
        self.assertIn('"Save && Apply Settings"', settings_text)
        self.assertNotIn('"Save & Apply Settings"', settings_text)
        self.assertIn('"Audio && Recording Settings"', settings_text)

    def test_project_prefix_dialog_and_main_window_layout(self) -> None:
        try:
            import os

            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication
            from app.ui.main_window import (
                MainWindow,
                ProjectPrefixDialog,
                RenameRecordingDialog,
            )
        except Exception:
            return

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            cm = ConfigManager(
                storage_dir=str(base / "storage"), etc_config_dir=str(base / "etc")
            )
            engine = AudioEngine(simulate_if_empty=True)
            sm = SambaManager(cm)
            sm.pdbedit_bin = None
            sm.smbpasswd_bin = None

            dlg = ProjectPrefixDialog(config_manager=cm)
            dlg.prefix_edit.setText("Episode 42")
            self.assertEqual(dlg.project_name, "Episode 42")
            self.assertIn("Episode_42/Episode_42_", dlg.preview_label.text())

            win = MainWindow(
                config_manager=cm,
                audio_engine=engine,
                samba_manager=sm,
                fullscreen=False,
            )
            try:
                self.assertFalse(hasattr(win, "mode_toggle_btn"))
                self.assertTrue(hasattr(win, "project_prefix_btn"))
                self.assertTrue(hasattr(win, "rename_take_btn"))
                self.assertTrue(hasattr(win, "clock_label"))
                self.assertTrue(len(win.clock_label.text()) > 0)
                self.assertTrue(hasattr(win, "sys_resource_label"))
                self.assertIn("CPU:", win.sys_resource_label.text())
                self.assertIn("RAM:", win.sys_resource_label.text())
                win._on_project_name_changed("Episode 42")
                self.assertTrue((cm.storage_dir / "Episode_42").is_dir())
                self.assertIn("Episode_42/Episode_42_", win.filename_preview_label.text())
                self.assertIn("[Episode_42/]", win.takes_title_label.text())

                # Create a completed recording in the Episode_42 subfolder and test RenameRecordingDialog
                sample_wav = cm.ensure_project_dir("Episode 42") / "Episode_42_2026-11-08_12.00.00_EST.wav"
                w = WavFileWriter(sample_wav, sample_rate=48000, channels=1, bit_depth=24)
                w.write_float_frames(np.zeros((2400, 1), dtype=np.float64))
                w.close()
                win._refresh_recordings_list(select_path=sample_wav)
                self.assertTrue(win.rename_take_btn.isEnabled())

                rename_dlg = RenameRecordingDialog(
                    config_manager=cm,
                    filepath=sample_wav,
                    parent=win,
                )
                rename_dlg.name_edit.setText("Scene 1 Hero Line")
                self.assertIn("Episode_42/Scene_1_Hero_Line.wav", rename_dlg.preview_label.text())
                rename_dlg._on_save_clicked()
                self.assertIsNotNone(rename_dlg.renamed_path)
                if rename_dlg.renamed_path is not None:
                    self.assertEqual(rename_dlg.renamed_path.name, "Scene_1_Hero_Line.wav")
                    win._refresh_recordings_list(select_path=rename_dlg.renamed_path)
                    self.assertEqual(win._get_selected_recording_path(), rename_dlg.renamed_path)
            finally:
                win.close()
        _ = app

    def test_system_resource_cpu_and_ram_telemetry(self) -> None:
        from app.network_info import (
            SystemResourceMonitor,
            parse_proc_meminfo,
            parse_proc_stat_cpu,
        )

        stat_t1 = "cpu  1000 0 500 8500 0 0 0 0 0 0\ncpu0 1000 0 500 8500 0 0 0 0 0 0\n"
        stat_t2 = "cpu  1250 0 600 9050 100 0 0 0 0 0\ncpu0 1250 0 600 9050 100 0 0 0 0 0\n"
        # t1: total = 10000, idle = 8500
        # t2: total = 11000, idle = 9050 + 100 = 9150 -> delta total = 1000, delta idle = 650 -> busy = 350 -> 35.0%
        self.assertEqual(parse_proc_stat_cpu(stat_t1), (10000, 8500))
        self.assertEqual(parse_proc_stat_cpu(stat_t2), (11000, 9150))

        meminfo_sample = (
            "MemTotal:        4194304 kB\n"
            "MemFree:         1048576 kB\n"
            "MemAvailable:    2097152 kB\n"
            "Buffers:          262144 kB\n"
            "Cached:           786432 kB\n"
        )
        parsed_mem = parse_proc_meminfo(meminfo_sample)
        self.assertIsNotNone(parsed_mem)
        if parsed_mem is not None:
            total_b, used_b = parsed_mem
            self.assertEqual(total_b, 4 * 1024 ** 3)
            self.assertEqual(used_b, 2 * 1024 ** 3)

        cpu_samples = [(10000, 8500), (11000, 9150), (12000, 9350), (13000, 9400)]
        mem_samples = [
            (4 * 1024 ** 3, 2 * 1024 ** 3),       # 50% RAM
            (4 * 1024 ** 3, 2 * 1024 ** 3),       # 50% RAM
            (4 * 1024 ** 3, int(3.4 * 1024 ** 3)),  # 85% RAM (warning)
            (4 * 1024 ** 3, int(3.8 * 1024 ** 3)),  # 95% RAM (critical)
        ]
        cpu_idx = 0
        mem_idx = 0

        def fake_cpu():
            nonlocal cpu_idx
            val = cpu_samples[min(cpu_idx, len(cpu_samples) - 1)]
            cpu_idx += 1
            return val

        def fake_mem():
            nonlocal mem_idx
            val = mem_samples[min(mem_idx, len(mem_samples) - 1)]
            mem_idx += 1
            return val

        mon = SystemResourceMonitor(cpu_times_fn=fake_cpu, mem_info_fn=fake_mem)
        # Sample 1: CPU 35%, RAM 50% -> ok
        st1 = mon.sample()
        self.assertAlmostEqual(st1.cpu_percent, 35.0, places=1)
        self.assertAlmostEqual(st1.ram_percent, 50.0, places=1)
        self.assertEqual(st1.level, "ok")
        self.assertIn("CPU:  35%", st1.format_bar_text())
        self.assertIn("RAM:  50% (2.0 GB/4.0 GB)", st1.format_bar_text())

        # Sample 2: CPU 80%, RAM 85% -> warning
        st2 = mon.sample()
        self.assertAlmostEqual(st2.cpu_percent, 80.0, places=1)
        self.assertEqual(st2.level, "warning")
        self.assertTrue(st2.is_warning)
        self.assertIn("HIGH LOAD", st2.format_bar_text())

        # Sample 3: CPU 95%, RAM 95% -> critical
        st3 = mon.sample()
        self.assertAlmostEqual(st3.cpu_percent, 95.0, places=1)
        self.assertEqual(st3.level, "critical")
        self.assertTrue(st3.is_critical)
        self.assertIn("OVERLOAD", st3.format_bar_text())


class TestRemoteProducerAndSslSuite(unittest.TestCase):
    def test_qr_code_matrix_and_svg_generation(self) -> None:
        from app.qr_code import encode_qr_matrix, qr_matrix_to_svg

        url = "https://192.168.1.50:8443/?session=ARS-1A2B&token=abc123xyz"
        matrix = encode_qr_matrix(url)
        self.assertGreaterEqual(len(matrix), 21)
        self.assertEqual(len(matrix), len(matrix[0]))
        # Top-left finder pattern corner is always dark
        self.assertTrue(matrix[0][0])
        self.assertTrue(matrix[0][6])
        self.assertTrue(matrix[6][0])

        svg = qr_matrix_to_svg(matrix)
        self.assertIn("<svg", svg)
        self.assertIn("<path", svg)

    def test_ssl_certificate_generation_and_drop_import(self) -> None:
        from app.ssl_manager import SslCertificateManager

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cert_dir = base / "etc_ssl"
            drop_dir = base / "storage_ssl"
            mgr = SslCertificateManager(cert_dir=cert_dir, drop_dir=drop_dir)

            ok, msg = mgr.ensure_certificates()
            self.assertTrue(ok, msg)
            info = mgr.get_certificate_info()
            self.assertTrue(info.valid)
            self.assertEqual(info.mode, "self-signed")
            self.assertTrue(len(info.fingerprint_sha256) > 20)

            # Verify SSLContext creation succeeds
            ctx = mgr.create_ssl_context()
            self.assertIsNotNone(ctx)

            # Simulate dropping issued certificate pair into /storage/ssl
            other_dir = base / "issued"
            other_mgr = SslCertificateManager(cert_dir=other_dir, drop_dir= base / "unused")
            other_mgr.ensure_certificates()
            drop_dir.mkdir(parents=True, exist_ok=True)
            (drop_dir / "server.crt").write_bytes(other_mgr.default_cert_path.read_bytes())
            (drop_dir / "server.key").write_bytes(other_mgr.default_key_path.read_bytes())

            imported_ok, import_msg = mgr.import_from_drop_dir()
            self.assertTrue(imported_ok, import_msg)
            info_after = mgr.get_certificate_info()
            self.assertTrue(info_after.valid)
            self.assertEqual(info_after.mode, "custom-imported")

    def test_studio_talkback_remote_producer_routing(self) -> None:
        sr = 8000
        proc = StudioTalkbackProcessor(
            sample_rate=sr,
            studio_mode=True,
            output_channels=2,
            tone_hz=440.0,
            tone_duration_sec=0.1,
            release_tone_duration_sec=0.05,
        )
        talent_mic = np.full(800, 0.25, dtype=np.float64)

        # Normal state: Talent mic on Left, silence on Right
        st_normal = proc.process_block(talent_mic)
        self.assertAlmostEqual(float(np.mean(np.abs(st_normal[:, 0]))), 0.25, places=3)
        self.assertAlmostEqual(float(np.max(np.abs(st_normal[:, 1]))), 0.0, places=5)

        # Activate Remote Producer Talkback and push remote voice samples
        proc.set_remote_talkback(True)
        remote_voice = np.full(2400, 0.45, dtype=np.float64)
        proc.push_remote_audio(remote_voice, input_sample_rate=sr)

        # Block 1 (800 frames = 0.1s): Left channel muted, Right channel plays press marker tone
        st_press_tone = proc.process_block(talent_mic)
        self.assertAlmostEqual(float(np.max(np.abs(st_press_tone[:, 0]))), 0.0, places=5)
        self.assertGreater(float(np.max(np.abs(st_press_tone[:, 1]))), 0.3)

        # Block 2 (800 frames): Left channel still muted, Right channel mixes Producer (0.45) + Local Mic (0.25) = 0.70
        st_rem_voice = proc.process_block(talent_mic)
        self.assertAlmostEqual(float(np.max(np.abs(st_rem_voice[:, 0]))), 0.0, places=5)
        self.assertAlmostEqual(float(np.mean(st_rem_voice[:, 1])), 0.70, places=2)

        # Block 3 (800 frames) with local_mic_muted=True (Cough/Privacy mute): Right channel records only Producer (0.45)
        st_cough_muted = proc.process_block(talent_mic, local_mic_muted=True)
        self.assertAlmostEqual(float(np.max(np.abs(st_cough_muted[:, 0]))), 0.0, places=5)
        self.assertAlmostEqual(float(np.mean(st_cough_muted[:, 1])), 0.45, places=2)

        # Release Remote Producer Talkback -> emits 0.05s (400 frames) release marker tone on Right
        proc.set_remote_talkback(False)
        st_rel = proc.process_block(talent_mic)
        self.assertGreater(float(np.max(np.abs(st_rel[:400, 1]))), 0.3)
        # Left channel returns to normal talent mic (0.25)
        self.assertAlmostEqual(float(np.mean(st_rel[:, 0])), 0.25, places=2)

    def test_talkback_press_and_release_headset_tones(self) -> None:
        engine = AudioEngine(simulate_if_empty=True)
        devs = engine.get_playback_devices()
        self.assertTrue(len(devs) >= 1)
        engine.set_talkback_output_device(devs[0])
        engine.set_talkback_tone_params(
            tone_hz=440.0,
            tone_duration_sec=0.1,
            release_tone_duration_sec=0.05,
        )

        # 1. Remote producer presses Talkback -> queues 0.1s (2400 samples @ 24kHz) press tone into headset
        engine.set_remote_talkback(True)
        self.assertEqual(engine.talkback_headset_tone_samples_total, 2400)
        # Streaming producer voice during the press tone mixes into the live playhead without building up lag
        engine.push_remote_talkback_audio(np.full(480, 0.2, dtype=np.float32), sample_rate=24000)
        with engine._tb_out_lock:
            self.assertLessEqual(engine._tb_out_fifo.size, 2400)

        # 2. Remote producer releases Talkback -> queues 0.05s (1200 samples @ 24kHz) release tone into headset
        engine.set_remote_talkback(False)
        self.assertEqual(engine.talkback_headset_tone_samples_total, 2400 + 1200)
        # Late in-flight producer audio packets after release are ignored and do not overwrite the release tone
        engine.push_remote_talkback_audio(np.full(960, 0.5, dtype=np.float32), sample_rate=24000)
        deadline = time.time() + 1.0
        while time.time() < deadline:
            with engine._tb_out_lock:
                if engine._tb_out_fifo.size == 0:
                    break
            time.sleep(0.01)
        with engine._tb_out_lock:
            self.assertEqual(engine._tb_out_fifo.size, 0)
        self.assertGreaterEqual(engine.talkback_headset_tone_samples_emitted, 1200)

        # 3. Local Talkback button pressed & released -> also queues press and release tones into headset
        engine.set_talkback(True)
        self.assertEqual(engine.talkback_headset_tone_samples_total, 3600 + 2400)
        engine.set_talkback(False)
        self.assertEqual(engine.talkback_headset_tone_samples_total, 6000 + 1200)

    def test_talent_mic_privacy_and_cough_mute_live_listener(self) -> None:
        engine = AudioEngine(simulate_if_empty=True)
        received = []

        def listener(mono: np.ndarray, sr: int, is_muted: bool) -> None:
            received.append((mono.copy(), sr, is_muted))

        engine.register_live_audio_listener(listener)
        sample_block = np.full(480, 0.25, dtype=np.float32)

        # Default: talent mic send enabled, not cough muted
        engine._update_meter_from_block(sample_block)
        engine._dispatch_live_input_block(sample_block, 48000)
        self.assertEqual(len(received), 1)
        self.assertFalse(received[-1][2])
        self.assertAlmostEqual(float(np.mean(received[-1][0])), 0.25, places=3)
        self.assertAlmostEqual(engine.get_vu_readings().peak_level, 0.25, places=3)

        # Momentary cough mute -> listener receives zeroed samples, is_muted=True, and VU meter goes silent
        engine.set_talent_cough_muted(True)
        self.assertTrue(engine.is_mic_muted)
        self.assertEqual(engine.get_vu_readings().peak_level, 0.0)
        self.assertEqual(engine.get_vu_readings().rms_level, 0.0)
        engine._update_meter_from_block(sample_block)
        self.assertEqual(engine.get_vu_readings().peak_level, 0.0)
        engine._dispatch_live_input_block(sample_block, 48000)
        self.assertTrue(received[-1][2])
        self.assertAlmostEqual(float(np.max(np.abs(received[-1][0]))), 0.0, places=6)

        # Release cough mute, disable mic send (toggled Mute) -> still muted and VU meter remains silent
        engine.set_talent_cough_muted(False)
        engine.set_talent_mic_send_enabled(False)
        self.assertTrue(engine.is_mic_muted)
        engine._update_meter_from_block(sample_block)
        self.assertEqual(engine.get_vu_readings().peak_level, 0.0)
        self.assertEqual(engine.get_vu_readings().rms_level, 0.0)
        engine._dispatch_live_input_block(sample_block, 48000)
        self.assertTrue(received[-1][2])
        self.assertAlmostEqual(float(np.max(np.abs(received[-1][0]))), 0.0, places=6)

        # Re-enabling mic restores live VU meter response
        engine.set_talent_mic_send_enabled(True)
        self.assertFalse(engine.is_mic_muted)
        engine._update_meter_from_block(sample_block)
        self.assertAlmostEqual(engine.get_vu_readings().peak_level, 0.25, places=3)

        engine.unregister_live_audio_listener(listener)

    def test_remote_producer_https_and_websocket_session(self) -> None:
        import json
        import os
        import socket
        import ssl
        import struct
        import urllib.request
        from app.remote_producer import FRAME_PRODUCER_AUDIO, RemoteProducerServer, read_ws_frame

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cm = ConfigManager(storage_dir=base / "storage", etc_config_dir=base / "etc")
            engine = AudioEngine(simulate_if_empty=True)
            server = RemoteProducerServer(
                audio_engine=engine,
                config_manager=cm,
                host="127.0.0.1",
                port=0,
            )
            self.assertTrue(server.start(port=0))
            try:
                port = server.port
                self.assertGreater(port, 0)

                client_ctx = ssl.create_default_context()
                client_ctx.check_hostname = False
                client_ctx.verify_mode = ssl.CERT_NONE

                # 1. Unauthenticated /api/status returns 401
                req = urllib.request.Request(f"https://127.0.0.1:{port}/api/status")
                with self.assertRaises(urllib.error.HTTPError) as ctx_err:
                    urllib.request.urlopen(req, context=client_ctx, timeout=5)
                self.assertEqual(ctx_err.exception.code, 401)

                # 2. Authenticate via /api/login with session passcode
                login_body = json.dumps({
                    "session_id": server.session.session_id,
                    "password": server.session.session_passcode,
                }).encode("utf-8")
                login_req = urllib.request.Request(
                    f"https://127.0.0.1:{port}/api/login",
                    data=login_body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(login_req, context=client_ctx, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    login_data = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(login_data["ok"])
                    auth_token = login_data["auth_token"]

                # 3. Authenticated /api/status succeeds & Studio Mode VU channels report dBFS
                engine._update_meter_from_block(np.full((480, 1), 0.25, dtype=np.float64))
                snap_idle = server.build_status_snapshot()
                self.assertAlmostEqual(snap_idle["vu"]["channel_peaks"][0], -12.0, delta=0.5)
                self.assertEqual(snap_idle["vu"]["channel_peaks"][1], -60.0)

                status_req = urllib.request.Request(
                    f"https://127.0.0.1:{port}/api/status",
                    headers={"Authorization": f"Bearer {auth_token}"},
                )
                with urllib.request.urlopen(status_req, context=client_ctx, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    st_data = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(st_data["authenticated"])
                    self.assertEqual(st_data["session_id"], server.session.session_id)

                # 4. Connect over WSS (/ws/producer) and test Mic Gate + Remote Talkback
                raw_sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                tls_sock = client_ctx.wrap_socket(raw_sock, server_hostname="127.0.0.1")
                try:
                    ws_handshake = (
                        f"GET /ws/producer?auth={auth_token} HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n"
                    )
                    tls_sock.sendall(ws_handshake.encode("ascii"))
                    resp_hdr = b""
                    while b"\r\n\r\n" not in resp_hdr:
                        chunk = tls_sock.recv(1024)
                        if not chunk:
                            break
                        resp_hdr += chunk
                    self.assertIn(b"101 Switching Protocols", resp_hdr)

                    # Read initial state frame
                    frame = read_ws_frame(tls_sock)
                    self.assertIsNotNone(frame)
                    opcode, payload = frame
                    self.assertEqual(opcode, 0x1)
                    init_state = json.loads(payload.decode("utf-8"))
                    self.assertEqual(init_state["type"], "state")

                    # Helper to send masked client WebSocket frame
                    def send_masked(op: int, data: bytes) -> None:
                        mask = os.urandom(4)
                        hdr = bytearray([0x80 | op])
                        n = len(data)
                        if n < 126:
                            hdr.append(0x80 | n)
                        else:
                            hdr.append(0x80 | 126)
                            hdr.extend(struct.pack("!H", n))
                        hdr.extend(mask)
                        masked = bytes(data[i] ^ mask[i & 3] for i in range(n))
                        tls_sock.sendall(bytes(hdr) + masked)

                    # Arm producer mic and activate talkback
                    send_masked(0x1, json.dumps({
                        "type": "mic_state",
                        "send_enabled": True,
                        "cough_muted": False,
                    }).encode("utf-8"))
                    send_masked(0x1, json.dumps({
                        "type": "talkback",
                        "active": True,
                    }).encode("utf-8"))

                    # Wait briefly for server to process
                    deadline = time.time() + 2.0
                    while time.time() < deadline and not engine.remote_talkback_active:
                        time.sleep(0.02)
                    self.assertTrue(engine.remote_talkback_active)
                    self.assertTrue(engine.talkback_active)

                    # Push a binary producer audio frame
                    pcm = (np.full(240, 0.5, dtype=np.float32) * 32767.0).astype("<i2").tobytes()
                    bin_pkt = struct.pack("<BBH", FRAME_PRODUCER_AUDIO, 0, 240) + pcm
                    send_masked(0x2, bin_pkt)

                    # Cough-muting producer mic immediately drops remote talkback
                    send_masked(0x1, json.dumps({
                        "type": "mic_state",
                        "send_enabled": True,
                        "cough_muted": True,
                    }).encode("utf-8"))
                    deadline = time.time() + 2.0
                    while time.time() < deadline and engine.remote_talkback_active:
                        time.sleep(0.02)
                    self.assertFalse(engine.remote_talkback_active)
                finally:
                    tls_sock.close()

                # 5. Rotating session invalidates previous auth token
                server.rotate_session()
                self.assertFalse(server.session.verify_auth_token(auth_token))
            finally:
                server.stop()

    def test_talkback_latch_when_idle_and_auto_unlatch_to_ptt_on_record_start(self) -> None:
        import json
        from app.remote_producer import RemoteProducerServer, WebSocketConnection

        class _DummySock:
            def __init__(self) -> None:
                self.sent_payloads: list[bytes] = []

            def sendall(self, data: bytes) -> None:
                self.sent_payloads.append(data)

            def shutdown(self, _how: int) -> None:
                pass

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cm = ConfigManager(storage_dir=base / "storage", etc_config_dir=base / "etc")
            engine = AudioEngine(simulate_if_empty=True)
            devs = engine.get_capture_devices()
            self.assertGreaterEqual(len(devs), 1)
            play_devs = engine.get_playback_devices()
            engine.set_talkback_output_device(play_devs[0])
            engine.set_talkback_tone_params(
                tone_hz=440.0,
                tone_duration_sec=0.1,
                release_tone_duration_sec=0.05,
            )

            server = RemoteProducerServer(
                audio_engine=engine,
                config_manager=cm,
                host="127.0.0.1",
                port=0,
            )

            # Register a simulated WebSocket producer client
            dummy_sock = _DummySock()
            ws_conn = WebSocketConnection(dummy_sock, ("127.0.0.1", 55000), server.session.session_id)  # type: ignore[arg-type]
            with server._clients_lock:
                server._clients[ws_conn.client_id] = ws_conn

            # 1. Idle: Talent latches Talkback ON, then unlatches OFF on second toggle
            engine.set_talkback(True, latched=True)
            self.assertTrue(engine.local_talkback_active)
            self.assertTrue(engine.local_talkback_latched)
            self.assertTrue(engine.talkback_active)
            self.assertTrue(engine.talkback_latched)

            engine.set_talkback(False, latched=False)
            self.assertFalse(engine.local_talkback_active)
            self.assertFalse(engine.local_talkback_latched)
            self.assertFalse(engine.talkback_latched)

            # 2. Idle: Remote Producer latches Talkback ON via WebSocket JSON
            server._handle_ws_json(
                ws_conn,
                json.dumps({"type": "talkback", "active": True, "latched": True}).encode("utf-8"),
            )
            self.assertTrue(ws_conn.talkback_active)
            self.assertTrue(ws_conn.talkback_latched)
            self.assertTrue(engine.remote_talkback_active)
            self.assertTrue(engine.remote_talkback_latched)
            self.assertTrue(engine.talkback_active)
            self.assertTrue(engine.talkback_latched)

            # Also latch Talent Talkback ON while idle so both are latched before recording starts
            engine.set_talkback(True, latched=True)
            self.assertTrue(engine.local_talkback_latched)
            tones_before_rec = engine.talkback_headset_tone_samples_total

            # 3. Start recording -> both Talent and Producer Talkback must automatically unlatch
            wav_path = cm.storage_dir / "Unlatch_Test.wav"
            engine.start_recording(
                device=devs[0],
                output_path=wav_path,
                sample_rate=48000,
                bit_depth=16,
                studio_mode=True,
                channels=2,
                tone_hz=440.0,
                tone_duration_sec=0.1,
                release_tone_duration_sec=0.05,
            )
            try:
                self.assertTrue(engine.is_recording)
                self.assertFalse(engine.local_talkback_active)
                self.assertFalse(engine.local_talkback_latched)
                self.assertFalse(engine.remote_talkback_active)
                self.assertFalse(engine.remote_talkback_latched)
                self.assertFalse(engine.talkback_active)
                self.assertFalse(engine.talkback_latched)
                self.assertFalse(ws_conn.talkback_active)
                self.assertFalse(ws_conn.talkback_latched)
                # Headset release tone was emitted on unlatch
                self.assertEqual(engine.talkback_headset_tone_samples_total, tones_before_rec + 1200)
                # Connected WebSocket client was notified with talkback_unlatched message
                joined_frames = b"".join(dummy_sock.sent_payloads).decode("utf-8", errors="ignore")
                self.assertIn('"talkback_unlatched"', joined_frames)

                # 4. While recording, Talkback cannot latch (strictly momentary PTT for both Talent & Producer)
                engine.set_talkback(True, latched=True)
                self.assertTrue(engine.local_talkback_active)
                self.assertFalse(engine.local_talkback_latched)
                engine.set_talkback(False)
                self.assertFalse(engine.local_talkback_active)

                server._handle_ws_json(
                    ws_conn,
                    json.dumps({"type": "talkback", "active": True, "latched": True}).encode("utf-8"),
                )
                self.assertTrue(ws_conn.talkback_active)
                self.assertFalse(ws_conn.talkback_latched)
                self.assertTrue(engine.remote_talkback_active)
                self.assertFalse(engine.remote_talkback_latched)

                server._handle_ws_json(
                    ws_conn,
                    json.dumps({"type": "talkback", "active": False, "latched": False}).encode("utf-8"),
                )
                self.assertFalse(ws_conn.talkback_active)
                self.assertFalse(engine.remote_talkback_active)
            finally:
                engine.stop_recording()

            # 5. Verify Producer HTML includes accessible toggle attributes and latch/PTT logic
            html = server._render_producer_html(csp_nonce="testnonce", authenticated=True)
            self.assertIn('aria-pressed="false"', html)
            self.assertIn('aria-describedby="talkback-hint"', html)
            self.assertIn("talkback_unlatched", html)

    def test_talent_talkback_temporarily_unmutes_and_shared_talkback_keeps_open_without_extra_beep(self) -> None:
        engine = AudioEngine(simulate_if_empty=True)
        devs = engine.get_playback_devices()
        self.assertGreaterEqual(len(devs), 1)
        engine.set_talkback_output_device(devs[0])
        engine.set_talkback_tone_params(
            tone_hz=440.0,
            tone_duration_sec=0.1,
            release_tone_duration_sec=0.05,
        )

        received = []

        def listener(mono: np.ndarray, sr: int, is_muted: bool) -> None:
            received.append((mono.copy(), sr, is_muted))

        engine.register_live_audio_listener(listener)
        try:
            sample_block = np.full(480, 0.25, dtype=np.float32)

            # 1. Talent activates Mute (toggled mute: talent_mic_send_enabled=False)
            engine.set_talent_mic_send_enabled(False)
            self.assertTrue(engine.is_mic_muted)
            self.assertFalse(engine.is_talent_mic_live_to_producer)
            engine._update_meter_from_block(sample_block)
            engine._dispatch_live_input_block(sample_block, 48000)
            self.assertEqual(engine.get_vu_readings().peak_level, 0.0)
            self.assertTrue(received[-1][2])  # is_muted=True
            self.assertAlmostEqual(float(np.max(np.abs(received[-1][0]))), 0.0, places=6)

            # 2. Producer presses Talkback first -> 1 press beep (2400 samples @ 24kHz)
            engine.set_remote_talkback(True)
            self.assertTrue(engine.talkback_active)
            self.assertEqual(engine.talkback_headset_tone_samples_total, 2400)
            # Talent mic is still muted because Talent has not pressed Talkback yet
            self.assertTrue(engine.is_mic_muted)

            # 3. Talent ALSO presses Talkback while Producer is holding Talkback:
            #    - Talent mic temporarily unmutes (is_mic_muted=False, is_talent_mic_live_to_producer=True, is_talkback_mute_override=True)
            #    - NO additional press beep is emitted (still 2400 samples)
            engine.set_talkback(True)
            self.assertTrue(engine.local_talkback_active)
            self.assertTrue(engine.remote_talkback_active)
            self.assertTrue(engine.talkback_active)
            self.assertFalse(engine.is_mic_muted)
            self.assertTrue(engine.is_talkback_mute_override)
            self.assertTrue(engine.is_talent_mic_live_to_producer)
            self.assertEqual(engine.talkback_headset_tone_samples_total, 2400)

            engine._update_meter_from_block(sample_block)
            engine._dispatch_live_input_block(sample_block, 48000)
            self.assertAlmostEqual(engine.get_vu_readings().peak_level, 0.25, places=3)
            self.assertFalse(received[-1][2])  # is_muted=False
            self.assertAlmostEqual(float(np.mean(received[-1][0])), 0.25, places=3)

            # 4. Producer stops talking and releases Talkback while Talent is STILL holding Talkback:
            #    - Talkback remains open (talkback_active=True)
            #    - NO release or press beep is emitted (still 2400 samples)
            #    - Talent mic remains unmuted so Talent response is not cut off
            engine.set_remote_talkback(False)
            self.assertFalse(engine.remote_talkback_active)
            self.assertTrue(engine.local_talkback_active)
            self.assertTrue(engine.talkback_active)
            self.assertFalse(engine.is_mic_muted)
            self.assertTrue(engine.is_talent_mic_live_to_producer)
            self.assertEqual(engine.talkback_headset_tone_samples_total, 2400)

            # Verify StudioTalkbackProcessor also keeps Right channel open without extra beep or cutoff
            sr = 8000
            proc = StudioTalkbackProcessor(
                sample_rate=sr,
                studio_mode=True,
                output_channels=2,
                tone_hz=440.0,
                tone_duration_sec=0.1,
                release_tone_duration_sec=0.05,
            )
            talent_mic = np.full(800, 0.25, dtype=np.float64)
            proc.set_remote_talkback(True)
            # Drain initial 0.1s (800-sample) press tone
            _ = proc.process_block(talent_mic, local_mic_muted=True)
            # Talent presses Talkback while Producer is holding -> no new tone, both mixed
            proc.set_talkback(True)
            st_both = proc.process_block(talent_mic, local_mic_muted=False)
            self.assertAlmostEqual(float(np.max(np.abs(st_both[:, 0]))), 0.0, places=5)
            self.assertAlmostEqual(float(np.mean(st_both[:, 1])), 0.25, places=3)
            # Producer releases while Talent still holds -> stays open, no release tone, Talent not cut off
            proc.set_remote_talkback(False)
            st_talent_still_talking = proc.process_block(talent_mic, local_mic_muted=False)
            self.assertAlmostEqual(float(np.max(np.abs(st_talent_still_talking[:, 0]))), 0.0, places=5)
            self.assertAlmostEqual(float(np.mean(st_talent_still_talking[:, 1])), 0.25, places=3)

            # 5. Talent finishes responding and releases Talkback:
            #    - Single release beep is emitted (2400 + 1200 = 3600 samples)
            #    - Talent Mute is automatically restored (is_mic_muted=True, is_talkback_mute_override=False, VU meter cleared to 0.0)
            engine.set_talkback(False)
            self.assertFalse(engine.local_talkback_active)
            self.assertFalse(engine.talkback_active)
            self.assertEqual(engine.talkback_headset_tone_samples_total, 2400 + 1200)
            self.assertTrue(engine.is_mic_muted)
            self.assertFalse(engine.is_talkback_mute_override)
            self.assertFalse(engine.is_talent_mic_live_to_producer)
            self.assertFalse(engine.talent_mic_send_enabled)
            self.assertEqual(engine.get_vu_readings().peak_level, 0.0)
        finally:
            engine.unregister_live_audio_listener(listener)

    def test_install_script_remote_producer_dependencies_files_and_lbu(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        install_sh = (repo_root / "install.sh").read_text(encoding="utf-8")
        recovery_sh = (repo_root / "config" / "alpine-recorder").read_text(encoding="utf-8")

        # Dependencies
        self.assertIn("openssl", install_sh)
        self.assertIn("ca-certificates", install_sh)
        self.assertIn("openssl", recovery_sh)
        self.assertIn("ca-certificates", recovery_sh)

        # Placed files & SSL directories
        for rel_file in (
            "app/qr_code.py",
            "app/ssl_manager.py",
            "app/remote_producer.py",
            "app/ui/remote_producer_dialog.py",
        ):
            self.assertIn(rel_file, install_sh)
            self.assertTrue((repo_root / rel_file).is_file(), f"Missing {rel_file}")

        self.assertIn("${ETC_CONFIG_DIR}/ssl", install_sh)
        self.assertIn("${STORAGE_DIR}/ssl", install_sh)
        self.assertIn("SslCertificateManager", install_sh)

        # LBU inclusion and commit
        self.assertIn('lbu include "${APP_DEST}/app/qr_code.py"', install_sh)
        self.assertIn('lbu include "${APP_DEST}/app/ssl_manager.py"', install_sh)
        self.assertIn('lbu include "${APP_DEST}/app/remote_producer.py"', install_sh)
        self.assertIn('lbu include "${APP_DEST}/app/ui/remote_producer_dialog.py"', install_sh)
        self.assertIn('lbu include "${ETC_CONFIG_DIR}/ssl"', install_sh)
        self.assertIn("lbu commit", install_sh)

    def test_playback_streams_recorded_audio_and_status_to_remote_producer_even_when_mic_muted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = ConfigManager(config_path=tmp_path / "settings.json", storage_dir=tmp_path / "storage")
            engine = AudioEngine(simulate_if_empty=True)
            ssl_mgr = SslCertificateManager(cert_dir=tmp_path / "ssl", storage_ssl_dir=tmp_path / "storage" / "ssl")
            server = RemoteProducerServer(audio_engine=engine, config_manager=cfg, ssl_manager=ssl_mgr)

            # 1. Verify Studio Mode 2ch downmix preserves unity gain for both Left (VO) and Right (Talkback)
            stereo_vo = np.column_stack([
                np.full(480, 0.5, dtype=np.float32),
                np.zeros(480, dtype=np.float32),
            ])
            stereo_tb = np.column_stack([
                np.zeros(480, dtype=np.float32),
                np.full(480, -0.4, dtype=np.float32),
            ])
            mono_vo = AudioEngine._downmix_playback_to_mono(stereo_vo)
            mono_tb = AudioEngine._downmix_playback_to_mono(stereo_tb)
            self.assertAlmostEqual(float(np.mean(mono_vo)), 0.5, places=4)
            self.assertAlmostEqual(float(np.mean(mono_tb)), -0.4, places=4)

            # 2. Create a 2-channel Studio Mode WAV file on disk (0.4s @ 24kHz)
            proj_dir = cfg.ensure_project_dir("GuidedReview")
            wav_path = proj_dir / "GuidedReview_take1.wav"
            sr = 24000
            total_frames = int(sr * 0.45)
            left_ch = np.full(total_frames, 0.4, dtype=np.float32)
            right_ch = np.zeros(total_frames, dtype=np.float32)
            interleaved_i16 = np.column_stack([
                (left_ch * 32767.0).astype("<i2"),
                (right_ch * 32767.0).astype("<i2"),
            ])
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(interleaved_i16.tobytes())

            # 3. Connect a simulated Remote Producer WebSocket client and mute the talent's live mic
            class _CaptureSocket:
                def __init__(self) -> None:
                    self.binary_packets: list[bytes] = []

                def sendall(self, data: bytes) -> None:
                    # Extract payload from WebSocket binary frame (opcode 0x82)
                    if len(data) >= 2 and data[0] == 0x82:
                        length = data[1] & 0x7F
                        offset = 2
                        if length == 126:
                            length = struct.unpack("!H", data[2:4])[0]
                            offset = 4
                        elif length == 127:
                            length = struct.unpack("!Q", data[2:10])[0]
                            offset = 10
                        self.binary_packets.append(data[offset : offset + length])

                def close(self) -> None:
                    pass

            cap_sock = _CaptureSocket()
            ws_conn = _WebSocketConnection(cap_sock, ("127.0.0.1", 55100), "client-review")  # type: ignore[arg-type]
            with server._clients_lock:
                server._clients["client-review"] = ws_conn

            # Even with Talent Mic muted for privacy, playback of recorded clips MUST stream to the producer
            engine.set_talent_mic_send_enabled(False)
            self.assertTrue(engine.is_mic_muted)

            engine.start_playback(filepath=wav_path, output_device=None)
            try:
                deadline = time.monotonic() + 1.5
                while len(cap_sock.binary_packets) < 2 and time.monotonic() < deadline:
                    time.sleep(0.03)

                self.assertTrue(engine.is_playing)
                self.assertGreaterEqual(len(cap_sock.binary_packets), 1)
                pkt = cap_sock.binary_packets[-1]
                frame_type, flags, rate_div100 = struct.unpack("<BBH", pkt[:4])
                self.assertEqual(frame_type, FRAME_TALENT_AUDIO)
                self.assertEqual(flags & 0x01, 0)  # NOT muted!
                self.assertEqual(flags & 0x04, 0x04)  # Playback flag set
                self.assertEqual(rate_div100 * 100, STREAM_SAMPLE_RATE)
                pcm_samples = np.frombuffer(pkt[4:], dtype="<i2").astype(np.float32) / 32768.0
                self.assertGreater(float(np.max(np.abs(pcm_samples))), 0.25)

                # Verify Remote Producer status snapshot reflects PLAYING state, position/duration, and live VU
                snap = server.build_status_snapshot()
                self.assertEqual(snap["state"], "PLAYING")
                self.assertTrue(snap["is_playing"])
                self.assertFalse(snap["is_play_paused"])
                self.assertEqual(snap["playing_take_name"], "GuidedReview_take1.wav")
                self.assertGreater(snap["play_duration_sec"], 0.3)
                self.assertGreater(snap["vu"]["peak_db"], -15.0)

                # Test seeking during playback
                self.assertTrue(engine.seek_playback(0.20))
                self.assertAlmostEqual(engine.play_position_sec, 0.20, delta=0.12)

                # Test pausing playback updates snapshot to PLAYBACK_PAUSED
                paused = engine.toggle_pause_playback()
                self.assertTrue(paused)
                snap_paused = server.build_status_snapshot()
                self.assertEqual(snap_paused["state"], "PLAYBACK_PAUSED")
                self.assertTrue(snap_paused["is_playing"])
                self.assertTrue(snap_paused["is_play_paused"])
            finally:
                engine.stop_playback()

    def test_main_window_playback_controls_and_inline_clip_row_position(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PyQt6.QtWidgets import QApplication
            from app.ui.main_window import (
                MainWindow,
                ROLE_PLAYBACK_ACTIVE,
                ROLE_PLAYBACK_DUR_SEC,
                ROLE_PLAYBACK_PAUSED,
                ROLE_PLAYBACK_POS_SEC,
                ROLE_PLAYBACK_PROGRESS,
                format_clip_progress_bar,
                format_playback_time,
            )
        except Exception as exc:
            self.skipTest(f"PyQt6 unavailable for UI playback test: {exc}")
            return

        self.assertEqual(format_playback_time(0), "00:00")
        self.assertEqual(format_playback_time(65), "01:05")
        self.assertIn("●", format_clip_progress_bar(0.5, width=10))
        self.assertIn("50%", format_clip_progress_bar(0.5, width=10))

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = ConfigManager(config_path=tmp_path / "settings.json", storage_dir=tmp_path / "storage")
            cfg.settings.project_name = "InlinePlayTest"
            proj_dir = cfg.ensure_project_dir("InlinePlayTest")

            sr = 24000
            for idx, dur_s in enumerate((0.6, 0.5), start=1):
                wav_path = proj_dir / f"InlinePlayTest_take{idx}.wav"
                samples = (np.full(int(sr * dur_s), 0.3, dtype=np.float32) * 32767.0).astype("<i2")
                with wave.open(str(wav_path), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sr)
                    wf.writeframes(samples.tobytes())
                time.sleep(0.02)

            engine = AudioEngine(simulate_if_empty=True)
            samba = SambaManager(storage_dir=tmp_path / "storage")
            ssl_mgr = SslCertificateManager(cert_dir=tmp_path / "ssl", storage_ssl_dir=tmp_path / "storage" / "ssl")
            win = MainWindow(
                config_manager=cfg,
                audio_engine=engine,
                samba_manager=samba,
                ssl_manager=ssl_mgr,
                fullscreen=False,
            )
            try:
                self.assertEqual(win.recordings_list.count(), 2)
                win.recordings_list.setCurrentRow(0)
                self.assertEqual(win.play_btn.text(), "▶ Play")

                # Start playback on row 0
                win._on_play_pause_clicked()
                self.assertTrue(engine.is_playing)
                self.assertEqual(win.play_btn.text(), "❚❚ Pause")
                self.assertTrue(win.stop_play_btn.isEnabled())

                # Seek to 0.3s and refresh UI tick
                engine.seek_playback(0.30)
                win._on_ui_tick()

                item0 = win.recordings_list.item(0)
                item1 = win.recordings_list.item(1)
                self.assertIsNotNone(item0)
                self.assertIsNotNone(item1)
                self.assertTrue(bool(item0.data(ROLE_PLAYBACK_ACTIVE)))
                self.assertFalse(bool(item0.data(ROLE_PLAYBACK_PAUSED)))
                self.assertGreater(float(item0.data(ROLE_PLAYBACK_PROGRESS)), 0.2)
                self.assertGreater(float(item0.data(ROLE_PLAYBACK_DUR_SEC)), 0.4)
                self.assertIn("▶ PLAYING", item0.text())
                self.assertIn("00:00 / 00:01", item0.text())
                self.assertIn("%]", item0.text())
                self.assertFalse(bool(item1.data(ROLE_PLAYBACK_ACTIVE)))

                # Selecting row 1 while row 0 is playing switches play_btn to "▶ Play"
                # while keeping row 0's inline play position active
                win.recordings_list.setCurrentRow(1)
                self.assertEqual(win.play_btn.text(), "▶ Play")
                self.assertTrue(bool(item0.data(ROLE_PLAYBACK_ACTIVE)))

                # Reselect row 0 -> play_btn shows "❚❚ Pause" again; clicking pauses playback
                win.recordings_list.setCurrentRow(0)
                self.assertEqual(win.play_btn.text(), "❚❚ Pause")
                win._on_play_pause_clicked()
                self.assertTrue(engine.is_paused)
                self.assertEqual(win.play_btn.text(), "▶ Resume")
                self.assertTrue(bool(item0.data(ROLE_PLAYBACK_PAUSED)))
                self.assertIn("❚❚ PAUSED", item0.text())

                # Stop playback -> row 0 returns to standard static text and play_btn shows "▶ Play"
                win._on_stop_playback_clicked()
                self.assertFalse(engine.is_playing)
                self.assertEqual(win.play_btn.text(), "▶ Play")
                self.assertFalse(bool(item0.data(ROLE_PLAYBACK_ACTIVE)))
                self.assertNotIn("PLAYING", item0.text())
                self.assertNotIn("PAUSED", item0.text())
            finally:
                win.close()
                app.processEvents()


class TestStorageFileManagerAndRecursiveDeletion(unittest.TestCase):
    def _create_dummy_wav(
        self, path: Path, duration_sec: float = 0.25, sample_rate: int = 24000
    ) -> int:
        import wave

        path.parent.mkdir(parents=True, exist_ok=True)
        frames = int(sample_rate * duration_sec)
        samples = (np.full(frames, 0.25, dtype=np.float32) * 32767.0).astype("<i2")
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(samples.tobytes())
        return path.stat().st_size

    def test_config_manager_cross_project_listing_delete_and_recursive_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            storage_dir = tmp_path / "storage"
            cfg = ConfigManager(
                config_path=tmp_path / "settings.json",
                storage_dir=storage_dir,
            )
            cfg.settings.project_name = "Alpha"

            # Protected directories should never appear in user project folder lists
            (storage_dir / ".alpine-recorder").mkdir(parents=True, exist_ok=True)
            (storage_dir / "ssl").mkdir(parents=True, exist_ok=True)
            (storage_dir / "lost+found").mkdir(parents=True, exist_ok=True)

            # Create 3 project folders: Alpha (2 WAVs), Beta (1 WAV + 1 note), EmptyProj (0 files)
            w_a1 = storage_dir / "Alpha" / "Alpha_take1.wav"
            w_a2 = storage_dir / "Alpha" / "Alpha_take2.wav"
            w_b1 = storage_dir / "Beta" / "Beta_take1.wav"
            note_b = storage_dir / "Beta" / "session_notes.txt"
            empty_dir = storage_dir / "EmptyProj"
            empty_dir.mkdir(parents=True, exist_ok=True)

            sz_a1 = self._create_dummy_wav(w_a1, duration_sec=0.2)
            time.sleep(0.01)
            sz_a2 = self._create_dummy_wav(w_a2, duration_sec=0.4)
            time.sleep(0.01)
            sz_b1 = self._create_dummy_wav(w_b1, duration_sec=0.3)
            note_b.write_text("take notes", encoding="utf-8")

            # 1. List project folders
            folders = cfg.list_project_folders()
            folder_names = [f.project_name for f in folders]
            self.assertEqual(set(folder_names), {"Alpha", "Beta", "EmptyProj"})
            # Active project ("Alpha") should be sorted first
            self.assertEqual(folders[0].project_name, "Alpha")
            self.assertTrue(folders[0].is_active_project)
            self.assertEqual(folders[0].recording_count, 2)
            self.assertEqual(folders[0].total_size_bytes, sz_a1 + sz_a2)

            beta_summary = next(f for f in folders if f.project_name == "Beta")
            self.assertEqual(beta_summary.recording_count, 1)
            self.assertEqual(beta_summary.other_file_count, 1)
            self.assertFalse(beta_summary.is_empty)

            empty_summary = next(f for f in folders if f.project_name == "EmptyProj")
            self.assertTrue(empty_summary.is_empty)
            self.assertEqual(empty_summary.total_item_count, 0)

            # 2. List all recordings across all projects
            all_recs = cfg.list_all_recordings()
            self.assertEqual(len(all_recs), 3)
            self.assertEqual({r.project_name for r in all_recs}, {"Alpha", "Beta"})

            alpha_only = cfg.list_all_recordings(project_filter="Alpha")
            self.assertEqual(len(alpha_only), 2)
            self.assertTrue(all(r.project_name == "Alpha" for r in alpha_only))

            # 3. Delete single recording and verify freed bytes & safety checks
            outside_file = tmp_path / "outside.wav"
            self._create_dummy_wav(outside_file, duration_sec=0.1)
            ok_out, msg_out, freed_out = cfg.delete_recording(outside_file)
            self.assertFalse(ok_out)
            self.assertEqual(freed_out, 0)
            self.assertTrue(outside_file.exists())

            ok_del, msg_del, freed_del = cfg.delete_recording(w_a1)
            self.assertTrue(ok_del, msg_del)
            self.assertEqual(freed_del, sz_a1)
            self.assertFalse(w_a1.exists())
            self.assertEqual(len(cfg.list_all_recordings()), 2)

            # 4. Clean up empty project folders
            ok_clean_empty, msg_clean_empty, removed_cnt = cfg.cleanup_empty_project_folders()
            self.assertTrue(ok_clean_empty, msg_clean_empty)
            self.assertEqual(removed_cnt, 1)
            self.assertFalse(empty_dir.exists())
            self.assertTrue((storage_dir / ".alpine-recorder").exists())
            self.assertTrue((storage_dir / "ssl").exists())

            # 5. Cleanup project folder requires confirm_recursive=True
            ok_no_conf, msg_no_conf, _ = cfg.cleanup_project_folder("Beta", confirm_recursive=False)
            self.assertFalse(ok_no_conf)
            self.assertIn("confirmation", msg_no_conf.lower())
            self.assertTrue(w_b1.exists())

            ok_clean_beta, msg_clean_beta, freed_beta = cfg.cleanup_project_folder(
                "Beta", confirm_recursive=True
            )
            self.assertTrue(ok_clean_beta, msg_clean_beta)
            self.assertGreaterEqual(freed_beta, sz_b1)
            self.assertTrue((storage_dir / "Beta").is_dir())
            self.assertFalse(w_b1.exists())
            self.assertFalse(note_b.exists())

            # 6. Delete project folder requires confirm_recursive=True and blocks protected dirs
            ok_prot, _, _ = cfg.delete_project_folder(".alpine-recorder", confirm_recursive=True)
            self.assertFalse(ok_prot)
            ok_ssl, _, _ = cfg.delete_project_folder("ssl", confirm_recursive=True)
            self.assertFalse(ok_ssl)

            ok_del_noconf, _, _ = cfg.delete_project_folder("Alpha", confirm_recursive=False)
            self.assertFalse(ok_del_noconf)
            self.assertTrue(w_a2.exists())

            ok_del_alpha, msg_del_alpha, freed_alpha = cfg.delete_project_folder(
                "Alpha", confirm_recursive=True
            )
            self.assertTrue(ok_del_alpha, msg_del_alpha)
            self.assertEqual(freed_alpha, sz_a2)
            self.assertFalse((storage_dir / "Alpha").exists())

    def test_storage_file_manager_dialog_ui_and_two_step_recursive_confirmation(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PyQt6.QtWidgets import QApplication
            from app.ui.file_manager_dialog import (
                DeleteFileConfirmDialog,
                RecursiveProjectConfirmDialog,
                StorageFileManagerDialog,
            )
        except Exception as exc:
            self.skipTest(f"PyQt6 unavailable for File Manager UI test: {exc}")
            return

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            storage_dir = tmp_path / "storage"
            cfg = ConfigManager(
                config_path=tmp_path / "settings.json",
                storage_dir=storage_dir,
            )
            cfg.settings.project_name = "BookOne"

            w1 = storage_dir / "BookOne" / "BookOne_ch1.wav"
            w2 = storage_dir / "BookOne" / "BookOne_ch2.wav"
            w3 = storage_dir / "BookTwo" / "BookTwo_intro.wav"
            sz1 = self._create_dummy_wav(w1, duration_sec=0.20)
            time.sleep(0.01)
            sz2 = self._create_dummy_wav(w2, duration_sec=0.50)
            time.sleep(0.01)
            sz3 = self._create_dummy_wav(w3, duration_sec=0.35)

            engine = AudioEngine(simulate_if_empty=True)
            dlg = StorageFileManagerDialog(
                config_manager=cfg,
                audio_engine=engine,
                fullscreen=False,
            )
            try:
                # 1. Default view is All Projects ("*") showing all 3 recordings
                self.assertEqual(dlg.selected_project_filter, "*")
                self.assertEqual(dlg.files_list.count(), 3)
                self.assertEqual(dlg.project_list.count(), 3)  # All Projects + BookOne + BookTwo

                # 2. Test Fullscreen File List sidebar toggle
                self.assertTrue(dlg._folders_pane_visible)
                dlg.toggle_folders_sidebar()
                self.assertFalse(dlg._folders_pane_visible)
                self.assertIn("Show Project Folders", dlg.toggle_folders_pane_btn.text())
                dlg.toggle_folders_sidebar()
                self.assertTrue(dlg._folders_pane_visible)

                # 3. Test sorting by Largest Size First ("size_desc")
                idx_size_desc = dlg.sort_combo.findData("size_desc")
                self.assertGreaterEqual(idx_size_desc, 0)
                dlg.sort_combo.setCurrentIndex(idx_size_desc)
                first_rec = dlg.get_selected_recording()
                self.assertIsNotNone(first_rec)
                self.assertEqual(first_rec.filepath, w2)  # 0.50s is largest

                # 4. Test search filtering across projects
                dlg.search_edit.setText("BookTwo")
                self.assertEqual(dlg.files_list.count(), 1)
                self.assertEqual(dlg.get_selected_recording().filepath, w3)
                dlg.search_edit.setText("")
                self.assertEqual(dlg.files_list.count(), 3)

                # 5. Test DeleteFileConfirmDialog space impact and file deletion in File Manager
                status = cfg.get_storage_status()
                del_confirm = DeleteFileConfirmDialog(entry=first_rec, storage_status=status, parent=dlg)
                self.assertIn("Space to Clear", del_confirm.space_impact_label.text())
                del_confirm.close()

                ok_del, _, freed = dlg.execute_delete_recording(first_rec)
                self.assertTrue(ok_del)
                self.assertEqual(freed, sz2)
                self.assertEqual(dlg.session_cleared_bytes, sz2)
                self.assertIn("SPACE CLEARED: +", dlg.space_cleared_badge.text())
                self.assertEqual(dlg.files_list.count(), 2)

                # 6. Test 2-step RecursiveProjectConfirmDialog for cleanup and delete
                book_one_summary = next(
                    s for s in dlg._project_summaries if s.project_name == "BookOne"
                )
                book_one_recs = [
                    r for r in dlg._all_recordings if r.project_name == "BookOne"
                ]
                rec_dlg = RecursiveProjectConfirmDialog(
                    folder_summary=book_one_summary,
                    recordings=book_one_recs,
                    storage_status=status,
                    mode="delete",
                    parent=dlg,
                )
                # Step 1 initial state: final button is disabled & hidden, confirmed_recursive is False
                self.assertEqual(rec_dlg.current_step, 1)
                self.assertFalse(rec_dlg.confirmed_recursive)
                self.assertFalse(rec_dlg.final_confirm_btn.isEnabled())
                # Attempting final confirm during Step 1 does nothing
                rec_dlg._on_final_confirm_clicked()
                self.assertFalse(rec_dlg.confirmed_recursive)

                # Advance to Step 2: final button still disabled until checkbox is checked
                rec_dlg.advance_to_final_step()
                self.assertEqual(rec_dlg.current_step, 2)
                self.assertFalse(rec_dlg.final_confirm_btn.isEnabled())
                rec_dlg.confirm_checkbox.setChecked(True)
                self.assertTrue(rec_dlg.final_confirm_btn.isEnabled())
                rec_dlg._on_final_confirm_clicked()
                self.assertTrue(rec_dlg.confirmed_recursive)
                rec_dlg.close()

                # Execute recursive folder deletion of BookOne and verify space cleared updates
                ok_proj_del, _, proj_freed = dlg.execute_delete_project_folder(
                    "BookOne", confirm_recursive=True
                )
                self.assertTrue(ok_proj_del)
                self.assertEqual(proj_freed, sz1)
                self.assertEqual(dlg.session_cleared_bytes, sz2 + sz1)
                self.assertEqual(dlg.files_list.count(), 1)
                self.assertFalse((storage_dir / "BookOne").exists())
            finally:
                dlg.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
