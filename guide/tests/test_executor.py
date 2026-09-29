"""실행 진입점 통합 검사(가짜 GasFree 클라이언트·가짜 서명·실제 SafeBatch 코어·영속 원장). 네트워크 없음.
9/28 20:15 검수 3항목: ①실제 시각·재견적 ②영속 원장 재사용/재시작/동시 ③응답 유실 UNKNOWN 보존."""
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import executor as X  # noqa: E402
import demo_flows as D  # noqa: E402
from safebatch.policy import PaymentPolicy  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402
from safebatch.tests.test_gasfree_order import RECV, USDT, PROV, GF, tokens_info, address_info as _address_info, FakeClient  # noqa: E402
from safebatch import tip712 as T  # noqa: E402
from tronpy.keys import PrivateKey  # noqa: E402
OWNER_KEY = PrivateKey(bytes.fromhex("11" * 32)); EOA = OWNER_KEY.public_key.to_base58check_address()


def address_info(**kw):
    ai = _address_info(**kw); ai["data"]["accountAddress"] = EOA; return ai


def real_sign(order):
    h = T.typed_data_hash(order.domain, order.types, "PermitTransfer", order.message)
    sig = OWNER_KEY.sign_msg_hash(h); sig_hex = sig.hex() if hasattr(sig, "hex") else bytes(sig).hex()
    return {"message": dict(order.message), "sig": sig_hex}

SIG = "ab" * 65
T0 = 1_000_000


def rules(**kw):
    base = dict(goal="업비트 USDT → 바이낸스 내 계정 실습", guide_plan_id="fa4b723f945db52e", receiver=RECV, mode="max_within_budget",
                amount_units=1_500_000, budget_total_units=12_000_000, min_receive_units=1_000_000, deadline_ts=T0 + 600, allowlist=(RECV,))
    base.update(kw)
    return D.UserRules(**base)


def ok_model(c, r): return {"choice_index": 0, "reason": "fits"}, {"ok": True, "model": "qwen3-32b"}


class Env:
    def __init__(self, tmp, client, fee=10_000_000, nonce=3, allow=True):
        self.policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=20_000_000, allowlist=frozenset({RECV}))
        self.intent_log = IntentLog(pathlib.Path(tmp) / "intents.jsonl")
        self.approvals = X.ApprovalLedger(pathlib.Path(tmp) / "approvals.jsonl")
        self.client = client
        self.clock = {"t": T0}
        self.fee, self.nonce, self.allow = fee, nonce, allow
        self.quote_calls = 0
        self.signed = 0

    def quote(self):
        self.quote_calls += 1
        return X.Quote(address_info(nonce=self.nonce, allow=self.allow), tokens_info(), PROV, self.clock["t"], self.fee)

    def sign(self, order):
        self.signed += 1
        return real_sign(order)

    def run(self, r=None, flow_id="f1", revision=1, confirm=lambda s: "owner"):
        return X.run_real_flow(r or rules(), policy=self.policy, batch_id=f"b-{flow_id}", intent_log=self.intent_log,
                               approvals=self.approvals, quote_fn=self.quote, client=self.client, human_confirm=confirm,
                               human_sign=self.sign, kiln_choose=ok_model, flow_id=flow_id, revision=revision, now_fn=lambda: self.clock["t"])


def ok_client():
    trace = {"id": "t-1", "state": "SUCCEED", "txnState": "SOLIDITY", "txnHash": "h1", "accountAddress": EOA, "targetAddress": RECV,
             "tokenAddress": USDT, "nonce": 3, "txnAmount": 1_500_000, "txnTotalFee": 10_000_000, "txnTotalCost": 11_500_000}
    return FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1", "state": "WAITING"}}},
                      trace_resp={"http": 200, "body": {"code": 200, "data": trace}})


class HappyPath(unittest.TestCase):
    def test_full_flow_confirmed_with_persistent_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            f = env.run()
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED", f)
            self.assertEqual(f["execution"]["detail"]["tx_hash"], "h1")
            self.assertEqual(env.intent_log.state(f["execution"]["detail"]["payment_id"]), "CONFIRMED")
            self.assertEqual(env.client.submitted[0]["value"], 1_500_000)
            self.assertIn(f["approval"]["approval_id"], env.approvals.used_ids())
            self.assertGreaterEqual(env.quote_calls, 2)                       # 최초 + 실행 직전 재견적


class RequoteAndClock(unittest.TestCase):
    def test_fee_change_before_execution_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            orig = env.quote
            def quote():
                q = orig()
                if env.quote_calls >= 2: q.fee_units = 11_000_000            # 실행 직전 수수료 변동
                return q
            f = X.run_real_flow(rules(), policy=env.policy, batch_id="b1", intent_log=env.intent_log, approvals=env.approvals,
                                quote_fn=quote, client=env.client, human_confirm=lambda s: "owner", human_sign=env.sign,
                                kiln_choose=ok_model, flow_id="f1", now_fn=lambda: env.clock["t"])
            self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertIn("quote changed", f["execution"]["detail"]["reason"])
            self.assertEqual(env.client.submitted, [])

    def test_stale_cached_quote_is_rejected_at_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            stale = X.Quote(address_info(nonce=3), tokens_info(), PROV, T0, 10_000_000)   # 캐시된 옛 견적(quoted_at=T0)
            def confirm(s):
                env.clock["t"] += X.QUOTE_TTL_S + 5                            # 승인 대기 중 시간 경과
                return "owner"
            f = X.run_real_flow(rules(), policy=env.policy, batch_id="b1", intent_log=env.intent_log, approvals=env.approvals,
                                quote_fn=lambda: stale, client=env.client, human_confirm=confirm, human_sign=env.sign,
                                kiln_choose=ok_model, flow_id="f1", now_fn=lambda: env.clock["t"])
            self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertIn("expired", f["execution"]["detail"]["reason"])
            self.assertEqual(env.client.submitted, [])

    def test_fresh_requote_after_wait_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            def confirm(s):
                env.clock["t"] += X.QUOTE_TTL_S + 5; return "owner"            # 대기했지만 실행 직전 새 견적이 같으면 진행
            f = env.run(confirm=confirm)
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED"); self.assertGreaterEqual(env.quote_calls, 2)

    def test_deadline_passes_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            def confirm(s):
                env.clock["t"] += 700; return "owner"                          # 기한 600초 초과
            f = env.run(confirm=confirm)
            self.assertEqual(f["outcome"], "DECLINED"); self.assertIn("conditions changed", f["reason"])
            self.assertEqual(env.client.submitted, [])

    def test_nonce_or_allow_change_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            def confirm(s): env.nonce = 4; return "owner"
            f = env.run(confirm=confirm)
            self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertEqual(env.client.submitted, [])


class PersistentDuplicateProtection(unittest.TestCase):
    def test_restart_replays_same_flow_zero_new_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            self.assertEqual(env.run()["outcome"], "EXECUTED_CONFIRMED")
            env2 = Env(tmp, ok_client())                                       # 프로세스 재시작: 같은 원장 파일, 새 인스턴스
            env2.policy = env.policy                                           # 정책 원장은 프로세스 메모리(코어 한계) — 같은 배치 재사용
            f = env2.run()
            self.assertNotEqual(f["outcome"], "EXECUTED_CONFIRMED")
            self.assertEqual(env2.client.submitted, [])

    def test_concurrent_same_flow_one_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ok_client()
            env = Env(tmp, client)
            barrier = threading.Barrier(2); results = []
            def worker():
                barrier.wait(); results.append(env.run()["outcome"])
            ts = [threading.Thread(target=worker) for _ in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]
            self.assertLessEqual(len(client.submitted), 1, results)


class ResponseLoss(unittest.TestCase):
    def test_transport_loss_persists_unknown_no_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeClient(submit_raises=RuntimeError("socket timeout")))
            f = env.run()
            self.assertEqual(f["outcome"], "EXECUTION_UNKNOWN_NO_RESEND")
            pid = f["execution"]["detail"]["payment_id"]
            self.assertEqual(env.intent_log.state(pid), "UNKNOWN"); self.assertEqual(len(env.client.submitted), 1)
            f2 = env.run()                                                     # 재시도: 제출 0 추가
            self.assertEqual(len(env.client.submitted), 1); self.assertNotEqual(f2["outcome"], "EXECUTED_CONFIRMED")

    def test_exception_after_submit_persists_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Client(FakeClient):
                def trace(self, tid): raise RuntimeError("trace crashed")
            c = Client(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1", "state": "WAITING"}}})
            env = Env(tmp, c)
            f = env.run()
            pid = f["execution"]["detail"]["payment_id"]
            self.assertIn(env.intent_log.state(pid), ("ACCEPTED", "UNKNOWN"))   # 제출됨은 보존, 미제출로 단정 안 함
            self.assertNotEqual(f["outcome"], "NOT_SUBMITTED")

    def test_pre_submit_failure_is_not_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            env.sign = lambda order: None                                        # 서명 거부
            f = env.run()
            self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertEqual(env.client.submitted, [])


class ApprovalScreenBinding(unittest.TestCase):
    def test_confirmed_summary_matches_signed_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client()); seen = {}
            def confirm(s): seen.update(s); return "owner"
            f = env.run(confirm=confirm)
            body = env.client.submitted[0]
            self.assertEqual(body["receiver"], seen["receiver"]); self.assertEqual(body["value"], seen["value_units"])
            self.assertEqual(body["maxFee"], seen["fee_units"]); self.assertEqual(seen["display_digest"], env.policy.display_digest("b-f1", 1))

    def test_not_confirmed_means_nothing_signed_or_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            f = env.run(confirm=lambda s: None)
            self.assertEqual(f["outcome"], "DECLINED"); self.assertEqual(env.signed, 0); self.assertEqual(env.client.submitted, [])


if __name__ == "__main__":
    unittest.main()
