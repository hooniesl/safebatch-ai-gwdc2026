"""Import the real audio handler with all external integrations mocked."""

import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADMIN = "12345"


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    return result


class VoiceHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.model = Path(self.temp.name) / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text("{}")
        (self.model / "weights.npz").write_bytes(b"mock weights")
        self.bot = mock.Mock()
        self.bot.message_handler.side_effect = lambda **kwargs: lambda fn: fn
        self.bot.get_file.return_value = types.SimpleNamespace(file_path="mock.ogg")
        self.bot.download_file.return_value = b"mock audio"
        self.transcribe = mock.Mock(return_value={"text": "맥북지갑에 트론 2개"})
        self.snapshot = mock.Mock(return_value=str(self.model))
        self.dispatch = mock.Mock(return_value=True)
        self.bridge = module(
            "vp_bridge", extract_question=mock.Mock(return_value=None),
            run_async=mock.Mock(),
        )
        config = module(
            "hani_config", bot=self.bot, client=mock.Mock(), MODEL_NAME="mock",
            SEARCH_CONFIG={}, safe_generate=mock.Mock(), MUSIC_ROOM_ID=-999,
            photo_memory={}, pending_cleanup={}, FISH_API_KEY="mock",
            ADMIN_CHAT_ID=ADMIN,
        )
        fake_modules = {
            "hani_config": config,
            "requests": module("requests"),
            "memory_manager": module("memory_manager", save_memory=mock.Mock(), load_memory=mock.Mock()),
            "cubase_brain": module("cubase_brain", CubaseBrain=mock.Mock()),
            "file_cleaner": module("file_cleaner", resolve_path=mock.Mock(), scan_duplicates=mock.Mock(), format_scan_report=mock.Mock()),
            "tts_cost_log": module("tts_cost_log", log_fish_call=mock.Mock()),
            "mlx_whisper": module("mlx_whisper", transcribe=self.transcribe),
            "huggingface_hub": module("huggingface_hub", snapshot_download=self.snapshot),
            "vp_bridge": self.bridge,
            "gwdc_tg_adapter": module("gwdc_tg_adapter", dispatch_message=self.dispatch),
        }
        patches = mock.patch.dict(sys.modules, fake_modules)
        patches.start()
        self.addCleanup(patches.stop)
        spec = importlib.util.spec_from_file_location("_gwdc_test_media_handler", ROOT / "media_handler.py")
        self.handler = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", list(sys.path)), mock.patch.object(os, "makedirs"):
            spec.loader.exec_module(self.handler)
        self.paths = []
        actual_mkstemp = tempfile.mkstemp

        def make_temp(*args, **kwargs):
            kwargs["dir"] = self.temp.name
            fd, path = actual_mkstemp(*args, **kwargs)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.paths.append(path)
            return fd, path

        temp_patch = mock.patch.object(self.handler.tempfile, "mkstemp", side_effect=make_temp)
        temp_patch.start()
        self.addCleanup(temp_patch.stop)

    def message(self, content_type="voice", chat_id=12345, sender_id=12345, chat_type="private"):
        media = types.SimpleNamespace(file_id="mock-file")
        return types.SimpleNamespace(
            message_id=88, chat=types.SimpleNamespace(id=chat_id, type=chat_type),
            from_user=types.SimpleNamespace(id=sender_id), content_type=content_type,
            voice=media if content_type == "voice" else None,
            audio=media if content_type == "audio" else None,
            forward_date=None, forward_origin=None, reply_to_message=None,
        )

    def assert_clean(self):
        self.assertTrue(self.paths)
        self.assertTrue(all(not Path(path).exists() for path in self.paths))

    def test_voice_and_audio_keep_original_message_and_refuse_text_approval(self):
        for content_type in ("voice", "audio"):
            with self.subTest(content_type=content_type):
                msg = self.message(content_type)
                msg.forward_origin = types.SimpleNamespace(type="user")
                msg.reply_to_message = types.SimpleNamespace(message_id=77)
                self.handler.handle_audio(msg)
                self.dispatch.assert_called_with(msg, self.bot, ADMIN, text="맥북지갑에 트론 2개", is_text=False)
                self.assertIs(self.dispatch.call_args.args[0], msg)
                self.assertEqual(msg.message_id, 88)
                self.assertEqual(msg.content_type, content_type)
                self.assertEqual(msg.reply_to_message.message_id, 77)
                self.assertIsNotNone(msg.forward_origin)
                self.assertFalse(hasattr(msg, "text"))
                self.bot.send_message.assert_not_called()
                self.assert_clean()

    def test_cached_model_only_and_quiet_transcription(self):
        self.handler.handle_audio(self.message())
        self.snapshot.assert_called_once_with(repo_id=self.handler.MLX_WHISPER_MODEL, local_files_only=True)
        kwargs = self.transcribe.call_args.kwargs
        self.assertEqual(kwargs["path_or_hf_repo"], str(self.model))
        self.assertEqual(kwargs["language"], "ko")
        self.assertIsNone(kwargs["verbose"])
        self.assertEqual(kwargs["no_speech_threshold"], 0.6)
        self.assertFalse(kwargs["condition_on_previous_text"])
        self.assert_clean()

    def test_unauthorized_or_incomplete_identity_never_dispatches(self):
        messages = [
            self.message(chat_type="group"), self.message(sender_id=999),
            self.message(chat_id=999, sender_id=999), self.message(chat_type=None),
            self.message(sender_id=None),
        ]
        missing_sender = self.message()
        del missing_sender.from_user
        messages.append(missing_sender)
        missing_type = self.message()
        del missing_type.chat.type
        messages.append(missing_type)
        for msg in messages:
            with self.subTest(message=msg):
                self.handler.handle_audio(msg)
                self.dispatch.assert_not_called()
                self.bridge.extract_question.assert_not_called()
                self.bridge.run_async.assert_not_called()
        self.assertEqual(self.bot.send_message.call_count, len(messages))
        self.assert_clean()

    def test_bridge_has_priority_over_adapter(self):
        self.transcribe.return_value = {"text": "부사장 진행 상황 알려줘"}
        self.bridge.extract_question.return_value = "진행 상황 알려줘"
        msg = self.message()
        self.handler.handle_audio(msg)
        self.bridge.run_async.assert_called_once_with("진행 상황 알려줘", msg.chat.id, source="음성")
        self.dispatch.assert_not_called()
        self.bot.send_message.assert_not_called()
        self.assert_clean()

    def test_empty_bridge_question_does_not_dispatch(self):
        self.bridge.extract_question.return_value = ""
        self.handler.handle_audio(self.message())
        self.bridge.run_async.assert_not_called()
        self.dispatch.assert_not_called()
        self.assertIn("이어서", self.bot.reply_to.call_args.args[1])
        self.assert_clean()

    def test_ordinary_audio_keeps_existing_text_response(self):
        self.transcribe.return_value = {"text": " 오늘 날씨가 좋네 "}
        self.dispatch.return_value = False
        msg = self.message()
        self.handler.handle_audio(msg)
        self.bot.send_message.assert_called_once_with(msg.chat.id, "오늘 날씨가 좋네")
        self.assert_clean()

    def test_empty_or_silent_result_never_routes(self):
        results = [
            {}, {"text": " \n\t "}, {"text": "승인", "no_speech": True},
            {"text": "승인", "no_speech_prob": 0.99},
            {"text": "승인", "segments": [{"text": "승인", "no_speech_prob": 0.98}]},
        ]
        for result in results:
            with self.subTest(result=result):
                self.transcribe.return_value = result
                self.handler.handle_audio(self.message())
                self.dispatch.assert_not_called()
                self.bridge.extract_question.assert_not_called()
        self.assertEqual(self.bot.send_message.call_count, len(results))
        self.assert_clean()

    def test_voice_approval_is_always_marked_nontext(self):
        self.transcribe.return_value = {"text": "승인"}
        msg = self.message()
        self.handler.handle_audio(msg)
        self.dispatch.assert_called_once_with(msg, self.bot, ADMIN, text="승인", is_text=False)
        self.assert_clean()

    def test_transcription_error_is_redacted_and_cleans_audio(self):
        self.transcribe.side_effect = RuntimeError("api-key-and-private-audio")
        self.handler.handle_audio(self.message())
        self.dispatch.assert_not_called()
        self.assertNotIn("api-key-and-private-audio", str(self.bot.mock_calls))
        self.assertIn("실패", self.bot.reply_to.call_args.args[1])
        self.assert_clean()

    def test_missing_model_never_transcribes_or_downloads(self):
        self.snapshot.side_effect = FileNotFoundError("mock secret path")
        self.handler.handle_audio(self.message())
        self.transcribe.assert_not_called()
        self.dispatch.assert_not_called()
        self.assertNotIn("mock secret path", str(self.bot.mock_calls))
        self.assert_clean()

    def test_incomplete_model_never_transcribes(self):
        (self.model / "weights.npz").unlink()
        self.handler.handle_audio(self.message())
        self.transcribe.assert_not_called()
        self.dispatch.assert_not_called()
        self.assert_clean()

    def test_download_error_cleans_temp_and_hides_error_body(self):
        self.bot.download_file.side_effect = RuntimeError("private-download-url")
        self.handler.handle_audio(self.message())
        self.transcribe.assert_not_called()
        self.dispatch.assert_not_called()
        self.assertNotIn("private-download-url", str(self.bot.mock_calls))
        self.assert_clean()

    def test_adapter_error_does_not_fall_through(self):
        self.dispatch.side_effect = RuntimeError("secret server URL")
        self.handler.handle_audio(self.message())
        self.bot.send_message.assert_not_called()
        self.assertNotIn("secret server URL", str(self.bot.mock_calls))
        self.assert_clean()

    def test_bridge_error_does_not_fall_through(self):
        self.bridge.extract_question.return_value = "맥북지갑에 트론 2개"
        self.bridge.run_async.side_effect = RuntimeError("private bridge URL")
        self.handler.handle_audio(self.message())
        self.dispatch.assert_not_called()
        self.bot.send_message.assert_not_called()
        self.assertNotIn("private bridge URL", str(self.bot.mock_calls))
        self.assert_clean()

    def test_concurrent_requests_use_distinct_files_and_serial_stt(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = 0
        maximum = 0

        def download(_path):
            barrier.wait(timeout=3)
            return b"mock audio"

        def transcribe(path, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                self.assertEqual(Path(path).read_bytes(), b"mock audio")
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                time.sleep(0.03)
                return {"text": "인식문"}
            finally:
                with lock:
                    active -= 1

        self.bot.download_file.side_effect = download
        self.transcribe.side_effect = transcribe
        threads = [threading.Thread(target=self.handler.handle_audio, args=(self.message(),)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.paths), 2)
        self.assertEqual(len(set(self.paths)), 2)
        self.assertEqual(self.transcribe.call_count, 2)
        self.assertEqual(self.dispatch.call_count, 2)
        self.assertEqual(maximum, 1)
        self.assert_clean()

    def test_real_adapter_voice_summary_text_approval_and_final_notification(self):
        guide = ROOT / "AI_CONTEST" / "gwdc_2026" / "guide"
        with mock.patch.dict(sys.modules), mock.patch.object(
            sys, "path", [str(guide / "tests"), str(guide), str(guide.parent), str(ROOT), *sys.path]
        ):
            # Import the existing real FakeServer/flow fixture. Only transport,
            # STT and unrelated bot dependencies remain mocked.
            sys.modules.pop("requests", None)
            sys.modules.pop("gwdc_tg_adapter", None)
            real_adapter = importlib.import_module("gwdc_tg_adapter")
            spec = importlib.util.spec_from_file_location(
                "_gwdc_voice_approval_fixture", guide / "tests" / "test_tg_approval.py"
            )
            approval = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(approval)
            _, node, flow, ext, server, adapter, sent = approval.setup(Path(self.temp.name) / "approval")
            self.bot.reply_to.return_value = types.SimpleNamespace(message_id=900)
            self.transcribe.return_value = {"text": "아이폰으로 맥북지갑한테 트론 두 개 보내"}
            voice_message = self.message()
            with mock.patch.object(real_adapter, "_adapter", adapter):
                self.handler.handle_audio(voice_message)
                summary = self.bot.reply_to.call_args.args[1]
                self.assertTrue(summary.startswith("🧾 승인 대기"), summary)
                self.assertIn("2 TRX", summary)
                record = ext.all()[0]
                self.assertEqual(record["external_key"], f"tg:{ADMIN}:{voice_message.message_id}")
                self.assertEqual(len(node.broadcasts), 0)

                # An audio reply saying approval must never authorize a transfer.
                voice_approval = self.message()
                voice_approval.message_id = 89
                voice_approval.reply_to_message = types.SimpleNamespace(message_id=900)
                self.transcribe.return_value = {"text": "승인"}
                self.handler.handle_audio(voice_approval)
                self.assertIn("음성", self.bot.reply_to.call_args.args[1])
                self.assertEqual(len(node.broadcasts), 0)

                text_approval = self.message(content_type="text")
                text_approval.message_id = 90
                text_approval.text = "승인"
                text_approval.reply_to_message = types.SimpleNamespace(message_id=900)
                self.assertTrue(real_adapter.dispatch_message(text_approval, self.bot, ADMIN))
                self.assertEqual(len(node.broadcasts), 1)
                view = ext.view(ext.get(record["intent_id"]), flow)
                self.assertEqual(view["state"], "FINAL")
                completed = adapter._watch(ADMIN, record["intent_id"], "", max_polls=2)
                self.assertTrue(any("송금 완료(확정)" in text for text in completed), completed)
                self.assertTrue(any("송금 완료(확정)" in text for text in sent), sent)
                self.assertEqual(len(node.broadcasts), 1)
            self.assert_clean()


if __name__ == "__main__":
    unittest.main()
