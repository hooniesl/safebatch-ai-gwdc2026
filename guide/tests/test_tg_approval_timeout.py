"""Approval transport failures recover through intent views only; all data is synthetic."""
import pathlib
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[3])]
from test_tg_approval import setup, USER, TEXT
import gwdc_tg_adapter as TG


class DeferredThread:
    def __init__(self, *, target, args=(), daemon=False):
        self.target, self.args, self.started = target, args, False

    def start(self):
        self.started = True

    def run(self):
        self.target(*self.args)


class ApprovalTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock, self.node, self.flow, self.ext, self.server, self.ad, self.sent = setup(self.tmp.name)
        summary = self.ad.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]
        self.ad.note_sent(USER, 900, summary)
        self.intent_id = self.ext.all()[0]["intent_id"]
        self.threads = []

        def make_thread(**kwargs):
            thread = DeferredThread(**kwargs)
            self.threads.append(thread)
            return thread

        patch = mock.patch.object(TG.threading, "Thread", side_effect=make_thread)
        patch.start()
        self.addCleanup(patch.stop)

    def approve(self, mid=2):
        return self.ad.handle("승인", chat_id=USER, message_id=mid, reply_to_id=900)[0]

    def test_server_finishes_but_transport_times_out_then_final_notified_once(self):
        real_approve = self.server.approve

        def approve_then_timeout(**kwargs):
            tracking = self.ad._load()["tracking"][self.intent_id]
            self.assertEqual(tracking["approval_key"], f"tg:{USER}:2")
            self.assertEqual(tracking["chat_id"], USER)
            real_approve(**kwargs)
            return 0, {"ok": False, "error": "server unreachable: TimeoutError"}

        self.server.approve = mock.Mock(side_effect=approve_then_timeout)
        self.server.intent_view_by_id = mock.Mock(wraps=self.server.intent_view_by_id)
        self.ad.watch = True
        reply = self.approve()
        self.assertIn("확인", reply)
        self.assertNotIn("실행하지 못", reply)
        self.assertNotIn("전송 1회 완료", reply)
        self.assertEqual(len(self.node.broadcasts), 1)
        self.assertEqual(len(self.threads), 1)
        self.assertTrue(self.threads[0].started)
        self.assertEqual(self.approve(), reply)
        self.approve(3)
        self.assertEqual(len(self.threads), 1, "Repeated approvals must reuse the same watcher")
        self.server.approve.assert_called_once()
        self.threads[0].run()
        self.assertTrue(any("송금 완료(확정)" in text for text in self.sent))
        self.assertEqual(self.server.intent_view_by_id.call_args.args, (self.intent_id,))
        self.ad._watch(USER, self.intent_id, "", max_polls=1)
        self.assertEqual(sum("송금 완료(확정)" in text for text in self.sent), 1)
        self.assertEqual(len(self.node.broadcasts), 1)

    def test_transport_exception_keeps_readonly_tracking_and_restart_replays_no_approval(self):
        self.server.approve = mock.Mock(side_effect=TimeoutError("secret URL must not be reflected"))
        self.server.intent_view_by_id = mock.Mock(wraps=self.server.intent_view_by_id)
        reply = self.approve()
        self.assertNotIn("secret URL", reply)
        self.assertIn(self.intent_id, self.ad._load()["tracking"])
        self.server.intent_view_by_id.assert_not_called()
        self.assertEqual(self.threads, [], "watch=False starts no background requests")
        restarted = TG.Adapter(server=self.server, send_fn=self.ad.send_fn, now_fn=self.clock,
            wallets=self.ad.wallets, enabled_fn=lambda: True, watch=True,
            sleep_fn=lambda _: None, state_file=self.ad.state_file)
        self.assertTrue(restarted.should_route("승인", USER, 900))
        result = restarted.handle("승인", chat_id=USER, message_id=3, reply_to_id=900)[0]
        self.assertNotIn("실행하지 못", result)
        self.server.approve.assert_called_once()
        self.assertEqual(len(self.threads), 1)
        self.assertEqual(len(self.node.broadcasts), 0)

    def test_already_and_idempotent_responses_start_result_tracking(self):
        for response in (
            (409, {"ok": False, "already": True}),
            (200, {"ok": True, "idempotent": True}),
            (409, {"ok": False, "idempotent": True, "state": "INVESTIGATE"}),
        ):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp:
                _, node, _, ext, server, adapter, _ = setup(tmp)
                summary = adapter.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]
                adapter.note_sent(USER, 900, summary)
                server.approve = mock.Mock(return_value=response)
                adapter.watch = True
                before = len(self.threads)
                reply = adapter.handle("승인", chat_id=USER, message_id=2, reply_to_id=900)[0]
                self.assertNotIn("실행하지 못", reply)
                self.assertNotIn("전송 1회 완료", reply)
                self.assertEqual(len(self.threads), before + 1)
                self.assertIn(ext.all()[0]["intent_id"], adapter._load()["tracking"])
                self.assertEqual(len(node.broadcasts), 0)

    def test_acknowledgement_without_execution_proof_never_claims_sent(self):
        self.server.approve = mock.Mock(return_value=(200, {"ok": True, "state": "APPROVED"}))
        reply = self.approve()
        self.assertIn("승인 접수", reply)
        self.assertNotIn("서명 1회", reply)
        self.assertNotIn("전송 1회", reply)
        self.assertNotIn("완료", reply)
        self.assertEqual(len(self.node.broadcasts), 0)

    def test_malformed_or_server_error_responses_keep_tracking(self):
        for response in ((502, {}), (200, []), (200, {})):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp:
                _, _, _, ext, server, adapter, _ = setup(tmp)
                summary = adapter.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]
                adapter.note_sent(USER, 900, summary)
                server.approve = mock.Mock(return_value=response)
                reply = adapter.handle("승인", chat_id=USER, message_id=2, reply_to_id=900)[0]
                self.assertNotIn("실행하지 못", reply)
                self.assertIn(ext.all()[0]["intent_id"], adapter._load()["tracking"])
                adapter.handle("승인", chat_id=USER, message_id=3, reply_to_id=900)
                server.approve.assert_called_once()

    def test_tracker_survives_temporary_status_lookup_failure(self):
        real_approve = self.server.approve
        self.server.approve = mock.Mock(side_effect=lambda **kwargs: (real_approve(**kwargs), (0, {}))[1])
        self.approve()
        final_view = self.server.intent_view_by_id(self.intent_id)
        self.server.intent_view_by_id = mock.Mock(side_effect=[TimeoutError("private"), (200, []), final_view])
        messages = self.ad._watch(USER, self.intent_id, "", max_polls=3)
        self.assertTrue(any("송금 완료(확정)" in text for text in messages))
        self.assertEqual(len(self.node.broadcasts), 1)
        self.server.approve.assert_called_once()

    def test_inflight_approval_cannot_be_reported_cancelled(self):
        self.server.approve = mock.Mock(return_value=(0, {}))
        self.approve()
        result = self.ad.handle("취소", chat_id=USER, message_id=3, reply_to_id=900)[0]
        self.assertNotIn("취소했습니다", result)
        self.assertNotIn("아무것도 보내지", result)
        self.assertIn(self.intent_id, self.ad._load()["tracking"])
        self.server.approve.assert_called_once()

    def test_approval_wins_race_after_cancel_selected_target(self):
        cancel_selected, continue_cancel = Event(), Event()
        original_forget = self.ad._forget_awaiting
        replies = []

        def pause_cancel(*args, **kwargs):
            if kwargs.get("require_untracked"):
                cancel_selected.set()
                self.assertTrue(continue_cancel.wait(3))
            return original_forget(*args, **kwargs)

        self.ad._forget_awaiting = pause_cancel
        self.server.approve = mock.Mock(return_value=(0, {}))
        thread = Thread(target=lambda: replies.extend(self.ad.handle("취소", chat_id=USER, message_id=3, reply_to_id=900)))
        thread.start()
        try:
            self.assertTrue(cancel_selected.wait(3))
            self.approve()
        finally:
            continue_cancel.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(replies), 1)
        self.assertNotIn("취소했습니다", replies[0])
        self.assertIn(self.intent_id, self.ad._load()["tracking"])
        self.server.approve.assert_called_once()

    def test_failed_durable_tracking_prevents_approval_post(self):
        original_save = self.ad._save

        def fail_tracking(state):
            if state.get("tracking"):
                raise OSError("synthetic disk failure")
            return original_save(state)

        self.ad._save = fail_tracking
        self.server.approve = mock.Mock()
        self.approve()
        self.server.approve.assert_not_called()
        self.assertEqual(len(self.node.broadcasts), 0)

    def test_http_timeout_is_unknown_without_retry_or_sensitive_error_text(self):
        server = TG.Server.__new__(TG.Server)
        server.base = "http://127.0.0.1:1"
        server.bot_key = "synthetic-key"
        server._session_token = "synthetic-session"
        with mock.patch.object(TG.urllib.request, "urlopen", side_effect=TimeoutError("private URL and key")) as opener:
            code, body = server._http("POST", "/api/intent/approve", {"intent_id": self.intent_id})
        self.assertEqual(code, 0)
        self.assertFalse(body["ok"])
        self.assertIn("TimeoutError", body["error"])
        self.assertNotIn("private URL", body["error"])
        opener.assert_called_once()

    def seed_failed_final_notification(self):
        self.approve()
        self.assertEqual(len(self.node.broadcasts), 1)
        state = self.ad._load()
        state["notify"][self.intent_id] = {"chat_id": USER, "token": "", "states": {
            "FINAL": {"ok": False, "attempts": 1, "t": int(self.clock())}
        }}
        self.ad._save(state)
        self.server.approve = mock.Mock(side_effect=AssertionError("Status recovery must never approve again"))

    def second_adapter(self, send_fn):
        return TG.Adapter(server=self.server, send_fn=send_fn, now_fn=self.clock,
            wallets=self.ad.wallets, enabled_fn=lambda: True, watch=False,
            sleep_fn=lambda _: None, state_file=self.ad.state_file)

    def test_watcher_and_failed_retry_share_one_durable_delivery_claim(self):
        self.seed_failed_final_notification()
        entered, release, retry_done = Event(), Event(), Event()
        deliveries, retries, failures = [], [], []

        def send(chat_id, text):
            deliveries.append((chat_id, text))
            entered.set()
            if not release.wait(3):
                raise TimeoutError("synthetic delivery blocked")

        self.ad.send_fn = send
        other = self.second_adapter(send)

        def watch():
            try:
                self.ad._watch(USER, self.intent_id, "", max_polls=1)
            except BaseException as error:
                failures.append(type(error).__name__)

        def retry():
            try:
                retries.append(other.resend_failed_notifications())
            except BaseException as error:
                failures.append(type(error).__name__)
            finally:
                retry_done.set()

        watcher = Thread(target=watch)
        watcher.start()
        retry_thread = None
        try:
            self.assertTrue(entered.wait(3))
            retry_thread = Thread(target=retry)
            retry_thread.start()
            self.assertTrue(retry_done.wait(1), "No file lock or second send may block the retry during network delivery")
        finally:
            release.set()
            watcher.join(3)
            if retry_thread is not None:
                retry_thread.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(retries, [0])
        self.assertEqual(len(deliveries), 1)
        notice = self.ad._load()["notify"][self.intent_id]["states"]["FINAL"]
        self.assertTrue(notice["ok"])
        self.assertEqual(notice["attempts"], 2)
        self.server.approve.assert_not_called()
        self.assertEqual(len(self.node.broadcasts), 1)

    def test_interrupted_delivery_is_not_retried_after_restart(self):
        self.seed_failed_final_notification()
        deliveries = []

        def interrupted_send(chat_id, text):
            deliveries.append((chat_id, text))
            raise SystemExit("simulate process termination after transport accepted the message")

        self.ad.send_fn = interrupted_send
        with self.assertRaises(SystemExit):
            self.ad._watch(USER, self.intent_id, "", max_polls=1)
        notice = self.ad._load()["notify"][self.intent_id]["states"]["FINAL"]
        self.assertTrue(notice.get("claim"))
        self.assertFalse(notice["ok"])
        send = mock.Mock()
        restarted = self.second_adapter(send)
        self.clock.t += TG.WATCH_S * 2
        self.assertEqual(restarted.resend_failed_notifications(), 0)
        self.assertEqual(restarted._watch(USER, self.intent_id, "", max_polls=1), [])
        send.assert_not_called()
        self.assertEqual(len(deliveries), 1)
        self.server.approve.assert_not_called()

    def test_failed_claim_persistence_prevents_delivery(self):
        self.seed_failed_final_notification()
        save = self.ad._save

        def disk_failure(state):
            if state["notify"][self.intent_id]["states"]["FINAL"].get("claim"):
                raise OSError("synthetic notification reservation failure")
            return save(state)

        self.ad._save = disk_failure
        self.ad.send_fn = mock.Mock()
        with self.assertRaises(OSError):
            self.ad._watch(USER, self.intent_id, "", max_polls=1)
        self.ad.send_fn.assert_not_called()

    def test_completed_failure_keeps_existing_bounded_retry_policy(self):
        self.seed_failed_final_notification()
        self.ad.send_fn = mock.Mock(side_effect=[RuntimeError("synthetic send failure"), None])
        self.assertEqual(self.ad._watch(USER, self.intent_id, "", max_polls=1), [])
        self.assertEqual(self.ad.resend_failed_notifications(), 1)
        self.assertEqual(self.ad.resend_failed_notifications(), 0)
        notice = self.ad._load()["notify"][self.intent_id]["states"]["FINAL"]
        self.assertTrue(notice["ok"])
        self.assertEqual(notice["attempts"], TG.NOTIFY_MAX_ATTEMPTS)
        self.assertEqual(self.ad.send_fn.call_count, 2)
        self.server.approve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
