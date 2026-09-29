"""Grok 2차 자문(9/28 22:12, ADVISOR_GROK_02_RESPONSE) 4건 회귀 검사(합성). 실지갑·네트워크 없음."""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import demo_flows as D  # noqa: E402
import nile_executor as NX  # noqa: E402
import run_nile_live as RL  # noqa: E402
from order_store import OrderStore  # noqa: E402
from signer import FileSigner  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch.intent_log import IntentLog, SCOPE_NILE  # noqa: E402
from tests.test_nile_path import FakeNode, sign_like_wallet, SENDER, OWNER, T0  # noqa: E402
from tests.test_nile_link import LinkEnv, rules, ai_record  # noqa: E402


class G1_PreBroadcastCostRecheck(unittest.TestCase):
    def test_bandwidth_price_rise_during_signature_wait_blocks_broadcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); env = LinkEnv(tmp, node)
            orig = node.chain_parameters
            def sign_then_raise_price(order):                       # 서명 대기 중 대역폭 단가 인상
                node.chain_parameters = lambda: {"http": 200, "body": {"chainParameter": [{"key": "getEnergyFee", "value": 100}, {"key": "getTransactionFee", "value": 50_000}]}}
                return {"signed_tx": sign_like_wallet(order["unsigned_tx"], OWNER)}
            env.sign_fn = sign_then_raise_price
            f = env.run(); self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertIn("pre-broadcast bounds", f["execution"]["detail"]["reason"])
            self.assertEqual(node.broadcasts, []); self.assertEqual(env.intent_log.state(f["execution"]["detail"]["payment_id"]), "CANCELLED")
            node.chain_parameters = orig

    def test_receipt_over_cap_is_mismatch_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); real = node.tx_info
            def over(txid, solid=False):
                r = real(txid, solid)
                if r["body"].get("receipt"):
                    r["body"]["fee"] = 6_000_000; r["body"]["receipt"]["energy_fee"] = 6_000_000
                return r
            node.tx_info = over
            env = LinkEnv(tmp, node); f = env.run()
            self.assertEqual(f["outcome"], "EXECUTION_MISMATCH_NEEDS_HUMAN"); self.assertFalse(f["executed"]); self.assertEqual(f["execution"]["state"], "MISMATCH")
            self.assertEqual(env.intent_log.state(f["execution"]["detail"]["payment_id"]), "ACCEPTED")     # 재방송은 계속 차단

    def test_signed_size_uses_actual_signature_count(self):
        self.assertEqual(NT.signed_tx_size_bytes("00" * 211, 1), (1 + 2 + 211) + (1 + 1 + 65))        # 전송 281 = raw(214) + sig(67), 임의 +4 없음(9/28 8차 검수)
        self.assertEqual(NT.signed_tx_size_bytes("00" * 211, 2), (1 + 2 + 211) + 2 * (1 + 1 + 65))
        self.assertEqual(NT.charged_bandwidth_bytes("00" * 211, 1, 1), 281 + 64)                      # 과금 345 = 전송 + MAX_RESULT_SIZE_IN_TX


class G2_AIRecordBinding(unittest.TestCase):
    def _log(self, tmp):
        p = pathlib.Path(tmp) / "k.jsonl"; p.write_text(json.dumps({"call_id": "req-1", "request_id": "req-1"}) + "\n"); return p

    def test_other_flow_id_or_partially_used_or_non_ai_by_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp)
            good = ai_record(); good["ai_work"]["raw_ai"] = {"next_action": "propose_plan"}
            ok_choose = NX.kiln_choose_from_record(good, kiln_log=log, expect_flow_id="intent-abc")
            self.assertTrue(ok_choose((), rules())[1]["ok"])
            for rec, exp in ((dict(good, flow_id="intent-zzz"), "flow_id"), (ai_record(status="partially_used"), "only 'used'"),
                             ({**good, "ai_work": {**good["ai_work"], "items": [{"action": "다음 행동 제안", "by": "AI 미사용(규칙)", "summary": "x"}]}}, "AI-backed"),
                             ({**good, "ai_work": {**good["ai_work"], "raw_ai": {"next_action": "ask"}}}, "propose_plan")):
                ch, meta = NX.kiln_choose_from_record(rec, kiln_log=log, expect_flow_id="intent-abc")((), rules())
                self.assertFalse(meta["ok"], exp); self.assertIn(exp, meta["why"])
            self.assertIn("computed by code", ok_choose((), rules())[1]["model_role"])            # 모델 역할을 과장하지 않음


class G3_SolidityAndConflicts(unittest.TestCase):
    def test_solidity_failed_body_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); real = node.tx_info
            def solid_failed(txid, solid=False):
                r = real(txid, solid)
                if solid and r["body"].get("receipt"):
                    r["body"]["receipt"]["result"] = "FAILED"
                return r
            node.tx_info = solid_failed
            env = LinkEnv(tmp, node); f = env.run()
            self.assertNotEqual(f["execution"]["state"], "CONFIRMED"); self.assertNotEqual(env.intent_log.state(f["execution"]["detail"]["payment_id"]), "CONFIRMED")

    def test_second_executor_on_open_payment_reports_unknown_with_txid(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); env = LinkEnv(tmp, node)
            pid = "b-f1:r1:2"
            def sign_and_race(order):                                   # 서명 대기 중 다른 실행기가 같은 지급을 예약·방송한 상황
                env.intent_log.append(pid, "SIGNED"); env.intent_log.reserve_broadcast(pid, SENDER, order["tx_id"])
                return {"signed_tx": sign_like_wallet(order["unsigned_tx"], OWNER)}
            env.sign_fn = sign_and_race
            f = env.run(); self.assertEqual(f["execution"]["state"], "UNKNOWN"); self.assertEqual(f["execution"]["detail"]["tx_hash"], f["order"]["tx_id"])
            self.assertNotEqual(f["outcome"], "NOT_SUBMITTED"); self.assertEqual(node.broadcasts, [])

    def test_rejected_code_but_tx_found_on_chain_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(broadcast_resp={"http": 200, "body": {"result": False, "code": "DUP_TRANSACTION_ERROR"}})
            env = LinkEnv(tmp, node); f = env.run(); self.assertEqual(f["execution"]["state"], "UNKNOWN")
            node2 = FakeNode(broadcast_resp={"http": 200, "body": {"result": False, "code": "TAPOS_ERROR", "message": "5461706f73"}})
            real_tx = node2.tx_by_id; node2.tx_by_id = lambda txid: {"http": 200, "body": {}}; node2.tx_info = lambda txid, solid=False: {"http": 200, "body": {}}
            env2 = LinkEnv(tmp + "/b" if False else tempfile.mkdtemp(), node2); f2 = env2.run()
            self.assertEqual(f2["execution"]["state"], "REJECTED"); self.assertIn("NOT_FOUND", f2["execution"]["detail"]["reason"])

    def test_gasfree_can_submit_sees_nile_open_payment(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = IntentLog(pathlib.Path(tmp) / "i.jsonl")
            log.append("nile-p", "DRAFTED", user=SENDER, scope="nile:3448148188:trc20:x"); log.append("nile-p", "AWAITING_HUMAN"); log.append("nile-p", "SIGNED")
            self.assertTrue(log.reserve_broadcast("nile-p", SENDER, "a" * 64)[0])
            log.append("gf-p", "DRAFTED", user=SENDER, nonce=1, scope=SCOPE_NILE); log.append("gf-p", "AWAITING_HUMAN"); log.append("gf-p", "SIGNED")
            ok, why = log.can_submit("gf-p", SENDER, 1); self.assertFalse(ok); self.assertIn("unresolved", why)


class G4_TimestampAndQuarantine(unittest.TestCase):
    def test_artifact_verifies_after_long_signature_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = LinkEnv(tmp, FakeNode())
            def slow_sign(order):
                env.clock["t"] = T0 + 1800                                  # 30분 대기 뒤 서명(기한 1h 안)
                return {"signed_tx": sign_like_wallet(order["unsigned_tx"], OWNER)}
            env.sign_fn = slow_sign
            f = env.run(); self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED", f.get("execution"))

    def test_refused_signature_is_quarantined_not_broadcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); env = LinkEnv(tmp, node)
            store = OrderStore(pathlib.Path(tmp) / "orders"); signer = FileSigner(store, timeout_s=5, sleep_fn=lambda s: None, now_fn=lambda: env.clock["t"])
            # 사람이 /sign 에서 서명(가짜) → 실행기 소비 → 방송 직전 재검 실패(단가 급등) → 격리
            def human_sign(order):
                def tick(_s):
                    store.store_signature(order["payment_id"], order["snapshot_sha256"], {"signed_tx": sign_like_wallet(order["unsigned_tx"], OWNER)}, now=env.clock["t"])
                    node.chain_parameters = lambda: {"http": 200, "body": {"chainParameter": [{"key": "getEnergyFee", "value": 100}, {"key": "getTransactionFee", "value": 50_000}]}}
                signer.sleep_fn = tick
                return signer.sign(order)
            env.sign_fn = human_sign
            f = NX.run_nile_flow(env.r, policy=env.policy, batch_id="b-f1", intent_log=env.intent_log, approvals=env.approvals, node=node, sender=SENDER,
                                 quote_fn=lambda: NX.quote_from_node(node, SENDER, env.r.receiver, env.r.amount_units, now_fn=lambda: env.clock["t"]),
                                 human_confirm=env.human_confirm, human_sign=env.sign, kiln_choose=env.kiln_choose, flow_id="f1", now_fn=lambda: env.clock["t"],
                                 artifact=env.artifact, sleep_fn=lambda s: None, receipt_polls=1, on_refused=signer.refuse)
            self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertTrue(f["execution"]["detail"]["signature_quarantined"])
            rec = store.get(f["execution"]["detail"]["payment_id"]); self.assertEqual(rec["state"], "SIGNED_REFUSED_NOT_BROADCAST"); self.assertEqual(node.broadcasts, [])
            self.assertIn("quarantined", signer.sign(f["order"])["refused"])                     # 재사용 불가


if __name__ == "__main__":
    unittest.main()
