"""Synthetic voice text through the real adapter; no network, paid AI or live wallet."""
import pathlib
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[3])]
from test_tg_approval import setup, USER, TEXT, WALLETS, MAC
from test_phone_chat_voice import C
import phone_chat as PC
import phone_ai as AI
import gwdc_tg_adapter as TG


class VoiceInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock, self.node, self.flow, self.ext, self.server, self.ad, self.sent = setup(self.tmp.name)
        self.ad.contacts = C
        self.flow.contacts = C
        self.counter = mock.Mock(wraps=self.server.create_intent)
        self.server.create_intent = self.counter

    def handle(self, text, mid=1, **kwargs):
        return self.ad.handle(text, chat_id=USER, message_id=mid, **kwargs)[0]

    def test_voice_original_quantity_and_same_message_across_channels(self):
        original = "아이폰으로 MacBook 지갑에 트론 두 개 보내"
        summary = self.handle(original, is_text=False)
        self.assertTrue(summary.startswith("🧾 승인 대기"), summary)
        self.assertIn("인식 원문: " + original, summary)
        self.assertIn("2 TRX", summary)
        self.assertEqual(self.ext.all()[0]["external_key"], f"tg:{USER}:1")
        self.assertEqual(self.handle(original, is_text=False), summary)
        self.assertEqual(self.handle(original, is_text=True), summary)
        self.assertIn("거부", self.handle(original.replace("두 개", "세 개"), is_text=False))
        self.assertEqual(self.counter.call_count, 1)
        self.assertEqual(len(self.node.broadcasts), 0)

    def test_wallet_amount_and_alias_clarifications_share_context(self):
        self.assertIn("어느 지갑", self.handle("맥북 지갑한테 트론 두 개 보내", is_text=False))
        self.assertTrue(self.ad.should_route("아이폰", USER))
        summary = self.handle("아이폰", 2)
        self.assertTrue(summary.startswith("🧾 승인 대기"), summary)
        self.assertEqual(self.counter.call_count, 1)
        self.assertIn("몇 TRX", self.handle("아이폰으로 맥북지갑한테 트론 보내", 3))
        self.assertTrue(self.ad.should_route("세 개", USER))
        summary = self.handle("세 개", 4, is_text=False)
        self.assertIn("3 TRX", summary)
        self.assertIn("누구에게", self.handle("아이폰으로 트론 1개 보내", 5))
        self.assertTrue(self.ad.should_route("맥북 지갑", USER))
        self.assertIn("1 TRX", self.handle("맥북 지갑", 6, is_text=False))
        self.assertEqual(self.counter.call_count, 3)
        self.assertEqual(len(self.node.broadcasts), 0)

    def test_negative_clears_pending_summary_without_ai(self):
        summary = self.handle("아이폰으로 " + TEXT)
        self.ad.note_sent(USER, 101, summary)
        self.assertIn("만들지 않았습니다", self.handle("아이폰으로 맥북지갑한테 트론 두 개 보내지 마", 2, is_text=False))
        self.assertIn("없습니다", self.handle("승인", 3, reply_to_id=101))
        self.assertEqual(self.counter.call_count, 1)
        self.assertEqual(len(self.node.broadcasts), 0)
        self.assertNotIn("🧾 승인 대기", self.handle("아이폰으로 " + TEXT))

    def test_correction_asks_one_amount_and_never_approves_old_summary(self):
        summary = self.handle("아이폰으로 " + TEXT)
        self.ad.note_sent(USER, 101, summary)
        self.assertIn("수량", self.handle("아이폰으로 맥북지갑한테 트론 두 개 말고 세 개 보내", 2, is_text=False))
        self.assertEqual(self.counter.call_count, 1)
        self.assertIn("없습니다", self.handle("승인", 3, reply_to_id=101))
        summary = self.handle("3 TRX", 4)
        self.assertTrue(summary.startswith("🧾 승인 대기"), summary)
        self.assertIn("3 TRX", summary)
        self.assertEqual(self.counter.call_count, 2)
        self.assertEqual(len(self.node.broadcasts), 0)

    def test_uncertain_inputs_never_create_intent(self):
        for index, text in enumerate((
            "트론 2개 3개를 맥북지갑한테 보내", "맥북지갑한테 트론 -2개 보내",
            "맥북지갑한테 트론 0.0000001개 보내", "맥북지갑한테 트론 1e2개 보내",
            "맥북지갑한테 트론 1,000개 보내", "맥북지갑한테 트론 1/2개 보내",
            "맥북지갑한테 아까처럼 트론 보내", "맥북지갑한테 메인넷 트론 2개 보내",
            "맥북지갑한테 TRX 2개 USDT 3개 보내", "다른맥북지갑한테 트론 2개 보내",
            "맥북지갑한테 트론 2개, 철이에게 트론 2개 보내",
            "맥북지갑한테 트론 2개 보내면 안 돼", "맥북지갑한테 트론 2개 보내면 안돼",
            "맥북지갑한테 트론 2개씩 두 번 보내", "맥북지갑한테 트론 스물 두 개 보내",
            "맥북지갑한테 트론 마이너스 2개 보내", "맥북지갑한테 트론 두 개 반 보내",
        ), 1):
            with self.subTest(text=text):
                self.assertNotIn("🧾 승인 대기", self.handle("아이폰으로 " + text, index))
        self.assertEqual(self.counter.call_count, 0)
        self.assertEqual(self.ext.all(), [])
        self.assertEqual(len(self.node.broadcasts), 0)

    def test_registered_wallet_recipient_is_not_stripped_or_used_as_sender(self):
        wallets = {**WALLETS, "default": "아이폰"}
        text = "안드로이드로 아이폰한테 트론 2개 보내"
        self.assertEqual(TG.resolve_wallet(text, wallets)["label"], "안드로이드")
        self.assertEqual(TG.strip_wallet_words(text, wallets), "아이폰한테 트론 2개 보내")
        self.assertIsNone(TG.resolve_wallet("다른아이폰으로 맥북지갑한테 트론 2개 보내", wallets))
        self.assertIsNone(TG.resolve_wallet("미등록지갑에서 맥북지갑한테 트론 2개 보내", wallets))
        self.assertIsNone(TG.resolve_wallet("갤럭시로 맥북지갑한테 트론 2개 보내", wallets))
        self.assertIsNone(TG.resolve_wallet("갤럭시 로 맥북지갑한테 트론 2개 보내", wallets))

    def test_pending_question_can_be_cancelled(self):
        self.assertIn("어느 지갑", self.handle(TEXT))
        self.assertTrue(self.ad.should_route("취소", USER))
        self.assertIn("취소했습니다", self.handle("취소", 2))
        self.assertFalse(self.ad.should_route("아이폰", USER))
        self.assertEqual(self.counter.call_count, 0)

    def test_summary_reply_is_bound_even_when_sent_in_reverse_order(self):
        first = self.handle("아이폰으로 " + TEXT)
        second = self.handle("안드로이드로 " + TEXT, 2)
        self.ad.note_sent(USER, 202, second)
        self.ad.note_sent(USER, 101, first)
        self.handle("승인", 3, reply_to_id=101)
        records = self.ext.all()
        first_rec = next(r for r in records if r["external_key"] == f"tg:{USER}:1")
        second_rec = next(r for r in records if r["external_key"] == f"tg:{USER}:2")
        self.assertIn("approval", first_rec)
        self.assertNotIn("approval", second_rec)
        self.assertEqual(len(self.node.broadcasts), 1)

    def test_duplicate_concurrent_voice_creates_only_one_intent(self):
        barrier = threading.Barrier(4)
        replies = []
        def run():
            barrier.wait()
            replies.append(self.handle("아이폰으로 맥북지갑한테 트론 두 개 보내", is_text=False))
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(len(replies), 4)
        self.assertEqual(self.counter.call_count, 1)
        self.assertEqual(len(self.ext.all()), 1)
        self.assertEqual(len(self.node.broadcasts), 0)

    def test_authentication_and_forwarded_audio_at_common_entry(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=USER, type="private"),
            from_user=SimpleNamespace(id=USER), message_id=1, content_type="voice")
        bot = mock.Mock()
        bot.reply_to.return_value = SimpleNamespace(message_id=10)
        text = "아이폰으로 맥북지갑한테 트론 두 개 보내"
        with mock.patch.object(TG, "_adapter", self.ad):
            message.chat.type = "group"
            self.assertFalse(TG.dispatch_message(message, bot, USER, text=text, is_text=False))
            message.chat.type = "private"; message.from_user.id = "other"
            self.assertFalse(TG.dispatch_message(message, bot, USER, text=text, is_text=False))
            message.from_user = None
            self.assertFalse(TG.dispatch_message(message, bot, USER, text=text, is_text=False))
            message.from_user = SimpleNamespace(id=USER); message.forward_origin = object()
            self.assertTrue(TG.dispatch_message(message, bot, USER, text=text, is_text=False))
            self.assertIn("전달", bot.reply_to.call_args.args[1])
        self.assertEqual(self.counter.call_count, 0)

    def test_unsafe_request_is_rejected_before_model_call(self):
        provider = mock.Mock()
        result = AI.decide("맥북지갑한테 트론 두 개 보내지 마", C, provider=provider)
        self.assertEqual(result["kind"], "question")
        provider.structure.assert_not_called()

    def test_model_result_keeps_original_separate_from_normalization(self):
        text = "맥북지갑한테 트론 두 개 보내"
        result = AI.decide(text, C)
        self.assertEqual(result["text"], text)
        self.assertEqual(result["normalized_text"], "맥북지갑한테 트론 2개 보내")

    def test_corrupt_adapter_state_does_not_fall_through_to_general_handler(self):
        self.ad.state_file.write_text("{broken", encoding="utf-8")
        with mock.patch.object(TG, "_adapter", self.ad), self.assertRaises(ValueError):
            TG.should_route("승인", USER)
        self.assertEqual(self.counter.call_count, 0)


if __name__ == "__main__":
    unittest.main()
