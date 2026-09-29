"""9/28 GPT-02 지적 4건 회귀 검사(실패 주입). 네트워크·실지갑 없음."""
import os
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import executor as X  # noqa: E402
from order_store import OrderStore  # noqa: E402
from signer import FileSigner  # noqa: E402
from safebatch.intent_log import IntentLog, IntentError  # noqa: E402
from test_executor import Env, rules, ok_model, ok_client, T0, EOA, real_sign  # noqa: E402
from test_signing_boundary import order as make_order, sign_with, OWNER, OTHER  # noqa: E402
from tronpy.keys import PrivateKey  # noqa: E402


class F1_LockedTransitions(unittest.TestCase):
    def test_concurrent_signed_appends_cannot_regress_after_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = IntentLog(pathlib.Path(tmp) / "i.jsonl")
            log.append("p", "DRAFTED", user=EOA, nonce=3); log.append("p", "AWAITING_HUMAN")
            barrier = threading.Barrier(2); errs = []
            def w():
                try:
                    barrier.wait(); log.append("p", "SIGNED")
                except IntentError as e:
                    errs.append(str(e))
                except Exception as e:
                    errs.append("other:" + repr(e))
            ts = [threading.Thread(target=w) for _ in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]
            # 둘 중 하나는 잠금 안에서 prev=SIGNED 를 보고 illegal transition 으로 거부된다(SIGNED→SIGNED 불허)
            self.assertEqual(len([e for e in errs if "illegal transition" in e]), 1, errs)
            ok, _ = log.reserve_submit("p", EOA, 3); self.assertTrue(ok)
            self.assertEqual(log.state("p"), "SUBMITTED")
            with self.assertRaises(IntentError):                       # 지연된 SIGNED 쓰기는 이제 거부
                log.append("p", "SIGNED")
            self.assertEqual(log.state("p"), "SUBMITTED")


class F2_NoReactivation(unittest.TestCase):
    def test_cancelled_and_consumed_orders_are_not_reactivated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = make_order()
            store.put_pending(od); store.cancel(od["payment_id"], od["snapshot_sha256"])
            rec = store.put_pending(od)                                       # 같은 지문 재등록 → 상태 보존
            self.assertEqual(rec["state"], "CANCELLED")
            self.assertFalse(store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OWNER, od), now=T0 + 1)[0])
            self.assertEqual(FileSigner(store, timeout_s=5, sleep_fn=lambda s: None, now_fn=lambda: T0).sign(od)["refused"][:9], "cancelled")
            od2 = dict(od, snapshot_sha256="f" * 64)
            with self.assertRaises(ValueError):                              # 다른 지문 → 충돌 거부
                store.put_pending(od2)
            self.assertEqual(store.get(od["payment_id"])["state"], "CANCELLED")

    def test_consumed_not_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = make_order(); store.put_pending(od)
            store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OWNER, od), now=T0 + 1)
            self.assertIsNotNone(store.consume(od["payment_id"], od["snapshot_sha256"])[0])
            r = FileSigner(store, timeout_s=5, sleep_fn=lambda s: None, now_fn=lambda: T0).sign(od)
            self.assertIn("consumed", r["refused"]); self.assertEqual(store.get(od["payment_id"])["state"], "CONSUMED")


class F3_NoFalseNotSubmitted(unittest.TestCase):
    def test_block_due_to_prior_submission_is_not_reported_as_not_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            self.assertEqual(env.run()["outcome"], "EXECUTED_CONFIRMED")
            env2 = Env(tmp, ok_client()); env2.policy = env.policy
            env2.approvals = X.ApprovalLedger(pathlib.Path(tmp) / "a2.jsonl")   # 승인 원장은 새것 → 실행기의 intent 차단 매핑까지 도달
            f = env2.run()                                                   # intent 원장 CONFIRMED → 차단
            self.assertEqual(f["execution"]["state"], "CONFIRMED"); self.assertNotEqual(f["outcome"], "NOT_SUBMITTED")
            self.assertEqual(env2.client.submitted, [])

    def test_unknown_state_block_reports_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeClientTimeout())
            f1 = env.run(); self.assertEqual(f1["outcome"], "EXECUTION_UNKNOWN_NO_RESEND")
            env2 = Env(tmp, ok_client()); env2.policy = env.policy
            env2.approvals = X.ApprovalLedger(pathlib.Path(tmp) / "a2.jsonl")
            f2 = env2.run()
            self.assertEqual(f2["execution"]["state"], "UNKNOWN"); self.assertEqual(env2.client.submitted, [])

    def test_ledger_write_failure_after_submission_is_unknown_not_pre_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeClientTimeout())
            class FlakyLog(IntentLog):                                       # SUBMITTED 기록 뒤 UNKNOWN 기록에서 디스크 오류
                def append(self, pid, state, **kw):
                    if state == "UNKNOWN":
                        raise OSError("disk fsync failed")
                    return super().append(pid, state, **kw)
            env.intent_log = FlakyLog(pathlib.Path(tmp) / "intents.jsonl")
            f = env.run()
            self.assertNotEqual(f["outcome"], "NOT_SUBMITTED"); self.assertEqual(f["execution"]["state"], "UNKNOWN")
            self.assertEqual(len(env.client.submitted), 1)
            self.assertEqual(env.intent_log.state(f["execution"]["detail"]["payment_id"]), "SUBMITTED")   # 재전송 계속 차단


    def test_prior_failed_is_preserved_not_reported_as_not_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeClientTimeout())
            f1 = env.run(); pid = f1["execution"]["detail"]["payment_id"]
            env.intent_log.append(pid, "FAILED", trace_id="t-prev", reason="provider reported FAILED")   # UNKNOWN → FAILED (조회로 확정)
            env2 = Env(tmp, ok_client()); env2.policy = env.policy
            env2.approvals = X.ApprovalLedger(pathlib.Path(tmp) / "a2.jsonl")
            f2 = env2.run()
            self.assertEqual(f2["execution"]["state"], "FAILED"); self.assertEqual(f2["outcome"], "EXECUTION_FAILED_EARLIER_NO_RESEND")
            self.assertNotEqual(f2["outcome"], "NOT_SUBMITTED"); self.assertEqual(env2.client.submitted, [])
            self.assertEqual(f2["execution"]["detail"]["prior"]["trace_id"], "t-prev")
            self.assertEqual(env2.intent_log.state(pid), "FAILED")                                         # 과거 상태 보존
            from safebatch import export as ex
            e = ex.build_export(env.policy, "b-f1", env2.intent_log, {})
            self.assertEqual([p for p in e["payments"] if p["payment_id"] == pid][0]["intent_state"], "FAILED")


class FakeClientTimeout:
    def __init__(self): self.submitted = []
    def submit(self, body): self.submitted.append(body); raise RuntimeError("socket timeout")
    def trace(self, t): raise RuntimeError("no")


class F4_HonestSignatureWording(unittest.TestCase):
    def test_cancel_wording_and_withdrawn_reason_reach_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = make_order(); store.put_pending(od)
            ok, msg = store.cancel(od["payment_id"], od["snapshot_sha256"])
            self.assertIn("wallet already produced a signature is unknown", msg); self.assertNotIn("nothing signed", msg)
            od2 = make_order("b9:r1:2"); store.put_pending(od2)
            store.store_signature(od2["payment_id"], od2["snapshot_sha256"], sign_with(OWNER, od2), now=T0 + 1)
            store.cancel(od2["payment_id"], od2["snapshot_sha256"])
            r = FileSigner(store, timeout_s=5, sleep_fn=lambda s: None, now_fn=lambda: T0).sign(od2)
            self.assertIn("withdrawn", r["refused"]); self.assertIn("NOT submitted", r["refused"])


class ExecutorSignerCheck(unittest.TestCase):
    def test_executor_rejects_signature_from_other_key_even_if_signer_callback_returns_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            def other_sign(order):
                from safebatch import tip712 as T
                h = T.typed_data_hash(order.domain, order.types, "PermitTransfer", order.message)
                sig = OTHER.sign_msg_hash(h); return {"message": dict(order.message), "sig": (sig.hex() if hasattr(sig, "hex") else bytes(sig).hex())}
            env.sign = other_sign
            f = env.run()
            self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertIn("signer mismatch", f["execution"]["detail"]["reason"])
            self.assertEqual(env.client.submitted, [])


if __name__ == "__main__":
    unittest.main()
