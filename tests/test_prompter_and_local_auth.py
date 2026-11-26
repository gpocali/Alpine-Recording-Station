"""Unit and integration tests for the Teleprompter, Whisper AI lookahead, script persistence, and local Web UI logins."""

from __future__ import annotations

import json
import os
import ssl
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from app.audio_engine import AudioEngine
from app.config_manager import ConfigManager
from app.prompter_engine import (
    PrompterTracker,
    WhisperSpeechWorker,
    extract_spoken_tokens,
    html_to_plain_text,
    sanitize_script_html,
)
from app.remote_producer import RemoteProducerServer
from app.web_auth import WebUserManager

try:
    from PyQt6.QtWidgets import QApplication

    HAS_PYQT6 = True
except Exception:
    HAS_PYQT6 = False


def _ensure_qapp():
    if not HAS_PYQT6:
        return None
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestPrompterEngine(unittest.TestCase):
    def test_sanitize_rich_text_html_preserves_formatting_and_strips_xss(self) -> None:
        raw = (
            "<h2>Intro</h2>"
            "<p>Welcome <b>everyone</b> to <span style='color: #FFDF5D; background-image: url(evil)'>Studio</span> "
            "<i>live</i>!</p>"
            "<script>alert(1)</script>"
            "<div onclick='evil()'>Safe text</div>"
            "<iframe src='https://evil.example'></iframe>"
        )
        cleaned = sanitize_script_html(raw)
        self.assertIn("<h2>Intro</h2>", cleaned)
        self.assertIn("<b>everyone</b>", cleaned)
        self.assertIn("<i>live</i>", cleaned)
        self.assertIn("color: #FFDF5D", cleaned)
        self.assertNotIn("<script", cleaned.lower())
        self.assertNotIn("alert(1)", cleaned)
        self.assertNotIn("onclick", cleaned.lower())
        self.assertNotIn("url(evil)", cleaned.lower())
        self.assertNotIn("<iframe", cleaned.lower())
        self.assertIn("Safe text", cleaned)

    def test_bracket_exclusion_for_parentheses_square_and_curly_brackets(self) -> None:
        script = (
            "Hello world (pause two seconds) welcome [smiling warmly] "
            "to the {SFX: thunder crash} alpine studio [nested (cue) here] today!"
        )
        tokens, non_spoken = extract_spoken_tokens(script)
        words = [t.raw for t in tokens]
        self.assertEqual(words, ["Hello", "world", "welcome", "to", "the", "alpine", "studio", "today"])
        self.assertEqual(len(non_spoken), 4)
        for s, e in non_spoken:
            self.assertIn(script[s], "([{")
            self.assertIn(script[e - 1], ")]}")

    def test_lookahead_window_and_whisper_matching_excludes_directions(self) -> None:
        tracker = PrompterTracker(lookahead_words=5)
        text = (
            "Welcome back (take a deep breath) to Alpine [pause 2s] Recording {music fades} "
            "Station where audio quality matters every single day."
        )
        tracker.set_text(text, reset_cursor=True)
        prompt = tracker.get_lookahead_prompt()
        self.assertEqual(prompt, "Welcome back to Alpine Recording")
        self.assertNotIn("breath", prompt)
        self.assertNotIn("pause", prompt)
        self.assertNotIn("music", prompt)

        # Simulated Whisper transcript advances past bracketed cues smoothly
        new_idx = tracker.match_transcript("welcome back to alpine")
        self.assertEqual(new_idx, 4)
        self.assertEqual(tracker.get_current_token().raw, "Recording")  # type: ignore[union-attr]

        # Configurable lookahead size
        tracker.lookahead_words = 3
        self.assertEqual(tracker.get_lookahead_prompt(), "Recording Station where")

    def test_edit_script_without_losing_reading_position(self) -> None:
        tracker = PrompterTracker(lookahead_words=10)
        original = (
            "First paragraph opening copy for the broadcast. "
            "Second paragraph introduces the microphone preamp chain. "
            "Third paragraph wraps up with the closing tagline."
        )
        tracker.set_text(original, reset_cursor=True)
        # Advance cursor to "introduces"
        intro_idx = next(t.index for t in tracker.tokens if t.normalized == "introduces")
        tracker.set_cursor(intro_idx)
        anchor = tracker.capture_anchor(scroll_ratio=0.45)

        # Producer inserts a brand new intro paragraph at the top!
        edited = (
            "Brand new producer slate and extra opening lines added live. "
            "First paragraph opening copy for the broadcast. "
            "Second paragraph introduces the microphone preamp chain. "
            "Third paragraph wraps up with the closing tagline."
        )
        new_idx, _ratio = tracker.update_text_preserving_position(edited, anchor=anchor)
        self.assertEqual(tracker.tokens[new_idx].normalized, "introduces")
        self.assertGreater(new_idx, intro_idx)

    def test_whisper_worker_advances_when_live_and_gates_on_mute_or_talkback(self) -> None:
        tracker = PrompterTracker(lookahead_words=6)
        tracker.set_text("Testing one two three (ignore me) four five six", reset_cursor=True)
        advanced = []
        prompts_seen = []

        def fake_transcribe(_audio: np.ndarray, prompt: str) -> str:
            prompts_seen.append(prompt)
            return "testing one two"

        worker = WhisperSpeechWorker(
            tracker=tracker,
            on_advance=lambda idx, txt: advanced.append((idx, txt)),
            transcribe_fn=fake_transcribe,
        )
        self.assertTrue(worker.start())
        try:
            loud = np.full(16000, 0.25, dtype=np.float32)
            # Muted audio should be ignored
            worker.push_audio(loud, 16000, is_muted=True)
            time.sleep(0.25)
            self.assertEqual(len(advanced), 0)

            # Talkback active should also gate audio
            worker.set_talkback_active(True)
            worker.push_audio(loud, 16000, is_muted=False)
            time.sleep(0.25)
            self.assertEqual(len(advanced), 0)

            # Unmuted & Talkback off -> should transcribe & advance
            worker.set_talkback_active(False)
            worker.push_audio(loud, 16000, is_muted=False)
            deadline = time.time() + 2.5
            while not advanced and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(advanced, "Expected WhisperSpeechWorker to advance cursor")
            self.assertEqual(advanced[-1][0], 3)
            self.assertTrue(any("ignore" not in p for p in prompts_seen))
        finally:
            worker.stop()


class TestLocalWebAuthAndHttpsEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.storage_dir = base / "storage"
        self.etc_dir = base / "etc"
        self.cfg = ConfigManager(storage_dir=str(self.storage_dir), etc_config_dir=str(self.etc_dir))
        self.cfg.settings.project_name = "AlphaProject"
        self.cfg.save()
        self.cfg.ensure_project_dir("AlphaProject")
        self.engine = AudioEngine(simulate_if_empty=True)
        self.script_events = []
        self.project_events = []
        self.server = RemoteProducerServer(
            audio_engine=self.engine,
            config_manager=self.cfg,
            on_project_name_changed=lambda name: self.project_events.append(name),
            on_script_updated=lambda proj, h: self.script_events.append((proj, h)),
            host="127.0.0.1",
            port=0,
            runtime_dir=base / "runtime",
        )
        ok = self.server.start(port=0)
        self.assertTrue(ok)
        self.base_url = f"https://127.0.0.1:{self.server.port}"
        self.ssl_ctx = ssl._create_unverified_context()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self.ssl_ctx),
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.engine.stop_monitoring()
        self._tmp.cleanup()

    def _req(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        token: str = "",
    ) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        headers = {
            "Origin": self.base_url,
            "Sec-Fetch-Site": "same-origin",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=5) as resp:
                return (resp.status, json.loads(resp.read().decode("utf-8")))
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="ignore")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"raw": raw}
            return (err.code, parsed)

    def test_web_user_manager_crud_and_token_revocation(self) -> None:
        mgr = WebUserManager(etc_config_dir=self.etc_dir, storage_dir=self.storage_dir)
        ok, _ = mgr.add_user("director", "studioSecret123")
        self.assertTrue(ok)
        acct = mgr.verify_credentials("director", "studioSecret123")
        self.assertIsNotNone(acct)
        self.assertIsNone(mgr.verify_credentials("director", "wrongPass"))

        tok = mgr.create_auth_token(acct)  # type: ignore[arg-type]
        self.assertIsNotNone(mgr.verify_auth_token(tok))

        # Changing password revokes prior token
        ok_pw, _ = mgr.change_password("director", "newSecret456")
        self.assertTrue(ok_pw)
        self.assertIsNone(mgr.verify_auth_token(tok))

        # Reloading from disk preserves account and secret
        mgr2 = WebUserManager(etc_config_dir=self.etc_dir, storage_dir=self.storage_dir)
        self.assertIsNotNone(mgr2.verify_credentials("director", "newSecret456"))

        ok_rm, _ = mgr2.remove_user("director")
        self.assertTrue(ok_rm)
        self.assertEqual(mgr2.list_users(), [])

    def test_standing_local_login_projects_and_script_sync_over_https(self) -> None:
        ok, _ = self.server.web_user_manager.add_user("producer1", "myPass1234")
        self.assertTrue(ok)

        # Login with local account WITHOUT knowing temporary session_id or PIN
        st, resp = self._req(
            "POST",
            "/api/login",
            {"login_mode": "local", "username": "producer1", "password": "myPass1234"},
        )
        self.assertEqual(st, 200)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["auth_kind"], "local")
        local_token = resp["auth_token"]

        # Rotating temporary session does NOT invalidate local standing login
        self.server.rotate_session()
        st, st_resp = self._req("GET", "/api/status", token=local_token)
        self.assertEqual(st, 200)
        self.assertEqual(st_resp["auth_kind"], "local")

        # Create a new project & list all projects
        st, cr = self._req(
            "POST",
            "/api/projects",
            {"project_name": "BetaCommercial", "set_active": False},
            token=local_token,
        )
        self.assertEqual(st, 200)
        self.assertEqual(cr["project_name"], "BetaCommercial")

        st, pl = self._req("GET", "/api/projects", token=local_token)
        self.assertEqual(st, 200)
        names = [p["project_name"] for p in pl["projects"]]
        self.assertIn("AlphaProject", names)
        self.assertIn("BetaCommercial", names)

        # Save rich text script into BetaCommercial project directory
        st, sv = self._req(
            "POST",
            "/api/script",
            {
                "project_name": "BetaCommercial",
                "html": "<p>Hello <b>Beta</b> (pause) <script>bad()</script>world!</p>",
            },
            token=local_token,
        )
        self.assertEqual(st, 200)
        self.assertTrue(sv["ok"])
        self.assertNotIn("<script", sv["html"])
        script_file = self.storage_dir / "BetaCommercial" / "script.html"
        self.assertTrue(script_file.is_file())
        self.assertIn("<b>Beta</b>", script_file.read_text(encoding="utf-8"))
        self.assertEqual(len(self.script_events), 1)
        self.assertEqual(self.script_events[0][0], "BetaCommercial")

        # Switch active project and rename
        st, sel = self._req(
            "POST",
            "/api/projects/select",
            {"project_name": "BetaCommercial"},
            token=local_token,
        )
        self.assertEqual(st, 200)
        self.assertEqual(self.cfg.settings.project_name, "BetaCommercial")

        st, ren = self._req(
            "POST",
            "/api/projects/rename",
            {"old_name": "BetaCommercial", "new_name": "BetaFinal"},
            token=local_token,
        )
        self.assertEqual(st, 200)
        self.assertEqual(ren["project_name"], "BetaFinal")
        self.assertTrue((self.storage_dir / "BetaFinal" / "script.html").is_file())

        # Temporary session guest cannot access /api/projects (403)
        st, s_login = self._req(
            "POST",
            "/api/login",
            {
                "session_id": self.server.session.session_id,
                "password": self.server.session.session_passcode,
            },
        )
        self.assertEqual(st, 200)
        guest_token = s_login["auth_token"]
        st, forbid = self._req("GET", "/api/projects", token=guest_token)
        self.assertEqual(st, 403)


@unittest.skipUnless(HAS_PYQT6, "PyQt6 not installed")
class TestPrompterDialogAndSettingsUi(unittest.TestCase):
    def setUp(self) -> None:
        self._app = _ensure_qapp()
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.cfg = ConfigManager(storage_dir=str(base / "storage"), etc_config_dir=str(base / "etc"))
        self.cfg.settings.project_name = "PrompterTest"
        self.cfg.save()
        self.engine = AudioEngine(simulate_if_empty=True)

    def tearDown(self) -> None:
        self.engine.stop_monitoring()
        self._tmp.cleanup()

    def test_prompter_dialog_font_scroll_modes_and_position_preserving_update(self) -> None:
        from app.ui.prompter_dialog import PrompterDialog

        self.cfg.save_project_script(
            "<p>Opening line one two three. (stage cue) Middle anchor phrase here. Closing line.</p>",
            project_name="PrompterTest",
        )
        actions = []
        dlg = PrompterDialog(
            config_manager=self.cfg,
            audio_engine=self.engine,
            on_record_clicked=lambda: actions.append("rec"),
            on_pause_clicked=lambda: actions.append("pause"),
            on_talkback_pressed=lambda: actions.append("tb_on"),
            on_talkback_released=lambda: actions.append("tb_off"),
            on_cough_pressed=lambda: actions.append("cough_on"),
            on_cough_released=lambda: actions.append("cough_off"),
        )
        try:
            # Verify compact critical buttons exist
            self.assertIsNotNone(dlg.record_btn)
            self.assertIsNotNone(dlg.pause_btn)
            self.assertIsNotNone(dlg.talkback_btn)
            self.assertIsNotNone(dlg.cough_btn)

            # Adjust font size and verify persistence
            orig_size = dlg._font_size
            dlg.adjust_font_size(6)
            self.assertEqual(dlg._font_size, orig_size + 6)
            self.assertEqual(self.cfg.settings.prompter_font_size, orig_size + 6)

            # Switch to autoscroll mode & adjust speed
            dlg._toggle_scroll_mode()
            self.assertEqual(dlg._scroll_mode, "autoscroll")
            orig_speed = dlg._autoscroll_speed
            dlg.adjust_autoscroll_speed(15.0)
            self.assertEqual(dlg._autoscroll_speed, orig_speed + 15.0)

            # Set reading position to "anchor", then push live edit inserting text above
            anchor_idx = next(t.index for t in dlg.tracker.tokens if t.normalized == "anchor")
            dlg.tracker.set_cursor(anchor_idx)
            dlg.apply_script_html(
                "<p>Inserted brand new intro paragraph! Opening line one two three. "
                "(stage cue) Middle anchor phrase here. Closing line.</p>",
                preserve_position=True,
            )
            self.assertEqual(dlg.tracker.get_current_token().normalized, "anchor")  # type: ignore[union-attr]
        finally:
            dlg.close()

    def test_settings_dialog_saves_prompter_ai_and_lookahead(self) -> None:
        from app.samba_manager import SambaManager
        from app.ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(
            config_manager=self.cfg,
            audio_engine=self.engine,
            samba_manager=SambaManager(),
        )
        try:
            dlg.prompter_ai_combo.setCurrentIndex(1)
            dlg.prompter_lookahead_spin.setValue(18)
            dlg.prompter_font_spin.setValue(44)
            dlg.prompter_scroll_mode_combo.setCurrentIndex(1)
            dlg.prompter_speed_spin.setValue(65.0)
            dlg._on_save_settings()
            self.assertTrue(self.cfg.settings.prompter_ai_enabled)
            self.assertEqual(self.cfg.settings.prompter_lookahead_words, 18)
            self.assertEqual(self.cfg.settings.prompter_font_size, 44)
            self.assertEqual(self.cfg.settings.prompter_scroll_mode, "autoscroll")
            self.assertEqual(self.cfg.settings.prompter_autoscroll_speed, 65.0)
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()
