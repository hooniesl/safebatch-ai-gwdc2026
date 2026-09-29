"""9/28 Grok 1회 자문 반례 5건 회귀 검사(실패 주입). 네트워크·서명·송금 없음."""
import json
import pathlib
import tempfile
import threading
import unittest

from safebatch.flow import run_batch, approve_batch
from safebatch.policy import PaymentPolicy
from safebatch import gasfree_order as go
from safebatch import export as ex
from safebatch.intent_log import IntentLog
from safebatch.tests.test_gasfree_order import RECV, USDT, PROV, EOA, CSV, tokens_info, address_info, FakeClient

SIG = "ab" * 65
TRACE_OK = {"id": "t-1", "state": "SUCCEED", "txnState": "SOLIDITY", "txnHash": "h", "accountAddress": EOA,
            "targetAddress": RECV, "tokenAddress": USDT, "nonce": 3, "txnAmount": 1_500_000, "txnTotalFee": 10_000_000,
            "txnTotalCost": 11_500_000}


def setup(tmp, nonce=3):
    policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=10_000_000, allowlist=frozenset({RECV}))
    res = run_batch(CSV, policy, "b1")
    ap = approve_batch(policy, "b1", res["revision"], res["csv_sha256"], confirmed_by="tester")["approval"]
    row = res["rows"][0]
    o = go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                       approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                       address_info=address_info(nonce=nonce), tokens_info=tokens_info(), provider_address=PROV, now=1_000)
    body = go.verify_signed(o, {"message": dict(o.message), "sig": SIG}, now=1_100)
    log = IntentLog(pathlib.Path(tmp) / "i.jsonl")
    go.record_order_intents(log, o); log.append(o.payment_id, "SIGNED")
    return policy, o, body, log


class K1_TruncatedLog(unittest.TestCase):
    def test_truncated_last_line_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, o, body, log = setup(tmp)
            ok, _ = log.reserve_submit(o.payment_id, EOA, 3)
            self.assertTrue(ok)
            raw = log.path.read_bytes(); log.path.write_bytes(raw[:-15])     # 전원 단절로 마지막 행 잘림
            log2 = IntentLog(log.path)
            c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1"}}})
            out = go.submit_and_track(c, o, body, intent_log=log2, policy=policy, now=1_100)
            self.assertEqual(out["outcome"], "BLOCKED_BY_INTENT_LOG"); self.assertIn("corrupt", out["submit"]["blocked"])
            self.assertEqual(len(c.submitted), 0)

    def test_submitted_is_durable_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, o, body, log = setup(tmp)
            seen = {}

            class Peek(FakeClient):
                def submit(self, b):
                    seen["state_at_submit"] = IntentLog(log.path).state(o.payment_id)   # 새 인스턴스로 디스크 재읽기
                    return super().submit(b)
            c = Peek(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1"}}})
            go.submit_and_track(c, o, body, intent_log=log, policy=policy, now=1_100)
            self.assertEqual(seen["state_at_submit"], "SUBMITTED")


class K2_Concurrency(unittest.TestCase):
    def test_two_threads_one_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, o, body, log = setup(tmp)
            barrier = threading.Barrier(2)
            results = []
            c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1"}}})

            def worker():
                lg = IntentLog(log.path)
                barrier.wait()
                results.append(go.submit_and_track(c, o, body, intent_log=lg, policy=policy, now=1_100)["outcome"])
            ts = [threading.Thread(target=worker) for _ in range(2)]
            [t.start() for t in ts]; [t.join() for t in ts]
            self.assertEqual(len(c.submitted), 1, results)
            self.assertEqual(sorted(results)[0], "ACCEPTED_WAITING"); self.assertIn("BLOCKED_BY_INTENT_LOG", results)


class K3_ForeignTrace(unittest.TestCase):
    def test_unknown_without_stored_trace_cannot_be_closed_by_foreign_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, o, body, log = setup(tmp)
            c = FakeClient(submit_raises=RuntimeError("timeout"),
                           trace_resp={"http": 200, "body": {"code": 200, "data": dict(TRACE_OK, id="t-9")}})
            go.submit_and_track(c, o, body, intent_log=log, policy=policy, now=1_100)
            self.assertEqual(log.state(o.payment_id), "UNKNOWN")
            r = go.resolve_unknown(c, o, log, trace_id="t-9")
            self.assertEqual(r["outcome"], "STILL_UNKNOWN_NO_TRACE_ID"); self.assertTrue(log.nonce_locked(EOA, 3))

    def test_stored_trace_with_other_nonce_does_not_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, o, body, log = setup(tmp)
            c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1", "state": "WAITING"}}},
                           trace_resp={"http": 200, "body": {"code": 200, "data": dict(TRACE_OK, nonce=4)}})
            out = go.submit_and_track(c, o, body, poll=1, sleep_fn=lambda s: None, intent_log=log, policy=policy, now=1_100)
            self.assertEqual(out["outcome"], "FINAL_SUCCEED_UNRECONCILED"); self.assertEqual(log.state(o.payment_id), "ACCEPTED")
            self.assertTrue(log.nonce_locked(EOA, 3))
            c.trace_resp = {"http": 200, "body": {"code": 200, "data": TRACE_OK}}
            out2 = go.submit_and_track(c, o, body, poll=1, sleep_fn=lambda s: None, intent_log=log, policy=policy, now=1_100)
            self.assertEqual(out2["outcome"], "BLOCKED_BY_INTENT_LOG")          # 재제출 아님
            r = go.resolve_unknown(c, o, log, trace_id="t-1")                     # 조회 경로로만 종결(저장된 traceId)
            self.assertEqual(r["outcome"], "SUCCEED"); self.assertEqual(log.state(o.payment_id), "CONFIRMED")
            self.assertEqual(go.resolve_unknown(c, o, log, trace_id="t-2")["outcome"], "NOT_UNKNOWN_CONFIRMED")


class K4_BodyNonceSplit(unittest.TestCase):
    def test_body_nonce_change_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, o, body, log = setup(tmp)
            bad = dict(body); bad["nonce"] = 8
            c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1"}}})
            with self.assertRaises(go.OrderError):
                go.submit_and_track(c, o, bad, intent_log=log, policy=policy, now=1_100)
            self.assertEqual(len(c.submitted), 0); self.assertEqual(log.state(o.payment_id), "SIGNED")


class K5_AccountLockAnd400(unittest.TestCase):
    def test_other_payment_blocked_while_account_has_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, o, body, log = setup(tmp)
            c = FakeClient(submit_raises=RuntimeError("timeout"))
            go.submit_and_track(c, o, body, intent_log=log, policy=policy, now=1_100)      # A → UNKNOWN(nonce 3)
            log.append("b1:r1:99", "DRAFTED", user=EOA, nonce=4); log.append("b1:r1:99", "AWAITING_HUMAN"); log.append("b1:r1:99", "SIGNED")
            ok, why = log.can_submit("b1:r1:99", EOA, 4)
            self.assertFalse(ok); self.assertIn("unresolved", why)

    def test_undocumented_400_is_unknown(self):
        self.assertEqual(go.classify_submit_response({"http": 400, "body": {"code": 400, "reason": "SomethingElse"}}), "UNKNOWN")
        self.assertEqual(go.classify_submit_response({"http": 400, "body": {"code": 400, "reason": "NonceNotMatchException"}}), "REJECTED")

    def test_replay_flags_stopped_row_submission_and_foreign_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, o, body, log = setup(tmp)
            c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1", "state": "WAITING"}}},
                           trace_resp={"http": 200, "body": {"code": 200, "data": TRACE_OK}})
            go.submit_and_track(c, o, body, poll=1, sleep_fn=lambda s: None, intent_log=log, policy=policy, now=1_100)
            e = ex.build_export(policy, "b1", log, {"t-1": TRACE_OK})
            self.assertTrue(ex.replay_check(e)["ok"])
            e2 = ex.build_export(policy, "b1", log, {"t-1": TRACE_OK}, human_stops=[o.payment_id])   # 중단됐는데 제출됨
            self.assertFalse(ex.replay_check(e2)["ok"])
            log.append("b1:r1:77", "DRAFTED", user=EOA, nonce=9); log.append("b1:r1:77", "AWAITING_HUMAN")
            log.append("b1:r1:77", "SIGNED"); log.append("b1:r1:77", "SUBMITTED")                    # decisions 밖 제출
            e3 = ex.build_export(policy, "b1", log, {"t-1": TRACE_OK})
            self.assertFalse(ex.replay_check(e3)["ok"])
            self.assertEqual(json.loads(log.path.read_text().splitlines()[0])["scope"].split(":")[0], "nile")


if __name__ == "__main__":
    unittest.main()
