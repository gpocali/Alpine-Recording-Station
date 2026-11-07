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
            self.assertEqual(cm2.settings.talkback_tone_hz, 440.0)
            self.assertEqual(cm2.settings.talkback_tone_duration_sec, 1.5)
            self.assertEqual(cm2.settings.talkback_release_tone_duration_sec, 0.75)
            self.assertFalse(cm2.settings.on_screen_keyboard)

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
            from app.ui.main_window import MainWindow, ProjectPrefixDialog
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
            self.assertIn("Episode_42_", dlg.preview_label.text())

            win = MainWindow(
                config_manager=cm,
                audio_engine=engine,
                samba_manager=sm,
                fullscreen=False,
            )
            try:
                self.assertFalse(hasattr(win, "mode_toggle_btn"))
                self.assertTrue(hasattr(win, "project_prefix_btn"))
                win._on_project_name_changed("Episode 42")
                self.assertIn("Episode_42_", win.filename_preview_label.text())
                self.assertIn("Episode_42_*.wav", win.takes_title_label.text())
            finally:
                win.close()
        _ = app


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

    def test_talent_mic_privacy_and_cough_mute_live_listener(self) -> None:
        engine = AudioEngine(simulate_if_empty=True)
        received = []

        def listener(mono: np.ndarray, sr: int, is_muted: bool) -> None:
            received.append((mono.copy(), sr, is_muted))

        engine.register_live_audio_listener(listener)
        sample_block = np.full(480, 0.25, dtype=np.float32)

        # Default: talent mic send enabled, not cough muted
        engine._dispatch_live_input_block(sample_block, 48000)
        self.assertEqual(len(received), 1)
        self.assertFalse(received[-1][2])
        self.assertAlmostEqual(float(np.mean(received[-1][0])), 0.25, places=3)

        # Momentary cough mute -> listener receives zeroed samples & is_muted=True
        engine.set_talent_cough_muted(True)
        engine._dispatch_live_input_block(sample_block, 48000)
        self.assertTrue(received[-1][2])
        self.assertAlmostEqual(float(np.max(np.abs(received[-1][0]))), 0.0, places=6)

        # Release cough mute, disable mic send for privacy -> still muted
        engine.set_talent_cough_muted(False)
        engine.set_talent_mic_send_enabled(False)
        engine._dispatch_live_input_block(sample_block, 48000)
        self.assertTrue(received[-1][2])
        self.assertAlmostEqual(float(np.max(np.abs(received[-1][0]))), 0.0, places=6)

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

                # 3. Authenticated /api/status succeeds
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


if __name__ == "__main__":
    unittest.main()
