"""9/28 VP_NILE_AI_LINK_REVIEW 세 항목 검사(합성, 네트워크·실지갑 없음).
1) AI 결과 → 실행 연결: meta.ok 아님/수동 모드/이어받기 기록 검증.  2) prepare 아티팩트 ↔ execute 전체 확인 지문 결합(변조·재사용 차단).
3) TRX 총상한 = Energy fee_limit + Bandwidth 최대 분리(대역폭 소진·단가 변경·영수증 3기준)."""
import copy
import dataclasses
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
from executor import ApprovalLedger  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402
from safebatch.policy import PaymentPolicy  # noqa: E402
from tests.test_nile_path import FakeNode, sign_like_wallet, rules as base_rules, SENDER, OWNER, OTHER, RECV, T0  # noqa: E402


def rules(**kw):
    return base_rules(**{"receiver": SENDER, "allowlist": (SENDER,), "mode": "exact_amount", "deadline_ts": T0 + 3600, **kw})   # 자체 전송 1 USDT


class LinkEnv:
    """prepare 아티팩트 → execute 를 CLI 없이 재현. human_confirm 은 사람이 읽은 지문(confirm)과 요약 지문이 같을 때만 승인."""
    def __init__(self, tmp, node, r=None, ai_record=None, ai_record_ref=None, kiln_choose=None, artifact=None, confirm=None, sign_fn=None, kiln_log=None):
        self.node, self.r = node, (r or rules())
        self.clock = {"t": T0}
        self.artifact = artifact if artifact is not None else RL.prepare_artifact(node, self.r, SENDER, ai_record=ai_record, ai_record_ref=ai_record_ref, now=T0,
                                                                                  kiln_log=kiln_log or NX.KILN_LOG)
        self.confirm = confirm if confirm is not None else self.artifact.get("confirm_sha256")
        self.policy = PaymentPolicy(budget_units=100_000_000, fee_units=0, allowlist=frozenset({self.r.receiver}))
        self.intent_log = IntentLog(pathlib.Path(tmp) / "i.jsonl"); self.approvals = ApprovalLedger(pathlib.Path(tmp) / "a.jsonl")
        self.kiln_choose = kiln_choose or (lambda c, r: ({"choice_index": 0, "reason": "ok"}, {"ok": True, "model": "qwen3-32b"}))
        self.ai_record_ref = ai_record_ref; self.signed = 0; self.sign_fn = sign_fn; self.confirm_calls = []

    def human_confirm(self, summary):
        self.confirm_calls.append(summary)
        return "Codex" if summary["confirm_sha256"] == self.confirm else None

    def sign(self, order):
        self.signed += 1
        return self.sign_fn(order) if self.sign_fn else {"signed_tx": sign_like_wallet(order["unsigned_tx"], OWNER)}

    def run(self, r=None, flow_id="f1"):
        r = r or self.r
        return NX.run_nile_flow(r, policy=self.policy, batch_id=f"b-{flow_id}", intent_log=self.intent_log, approvals=self.approvals, node=self.node, sender=SENDER,
                                quote_fn=lambda: NX.quote_from_node(self.node, SENDER, r.receiver, r.amount_units, now_fn=lambda: self.clock["t"]),
                                human_confirm=self.human_confirm, human_sign=self.sign, kiln_choose=self.kiln_choose, flow_id=flow_id,
                                now_fn=lambda: self.clock["t"], artifact=self.artifact, ai_record_ref=self.ai_record_ref, sleep_fn=lambda s: None, receipt_polls=2)


# ── 2. 아티팩트 ↔ 확인 지문 ─────────────────────────────────────────────────
class ArtifactBinding(unittest.TestCase):
    def test_reviewed_artifact_reaches_signature_and_confirms_with_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = LinkEnv(tmp, FakeNode()); f = env.run()
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED", f.get("execution"))
            self.assertEqual(env.node.broadcasts[0]["raw_data_hex"], env.artifact["unsigned"]["raw_data_hex"])      # 재작성 없음
            self.assertEqual(f["order"]["confirm_sha256"], env.artifact["confirm_sha256"]); self.assertEqual(f["order"]["expire_at_ms"], rules().deadline_ts * 1000)
            ap = env.policy.approvals["b-f1:r1:approval"]; self.assertEqual(ap["displayed_extra"]["tx_id"], env.artifact["unsigned"]["txID"])
            self.assertEqual(ap["displayed_extra"]["trx_total_cap_sun"], 5_000_000); self.assertEqual(ap["displayed_extra"]["deadline_ts"], rules().deadline_ts)

    def _tampered(self, tmp, **rule_changes):
        """사람은 원래 아티팩트 지문을 읽었는데 실행 시 규칙/발신자가 바뀐 경우 → 승인·서명·방송 0."""
        node = FakeNode(); art = RL.prepare_artifact(node, rules(), SENDER, now=T0)
        r2 = dataclasses.replace(rules(), **rule_changes)
        env = LinkEnv(tmp, node, r=r2, artifact=art, confirm=art["confirm_sha256"])
        f = env.run(); return env, f

    def test_trx_cap_5_to_50_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, f = self._tampered(tmp, trx_fee_cap_sun=50_000_000)
            self.assertNotEqual(f["outcome"], "EXECUTED_CONFIRMED"); self.assertEqual(env.signed, 0); self.assertEqual(env.node.broadcasts, [])

    def test_deadline_extension_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, f = self._tampered(tmp, deadline_ts=T0 + 7200)
            self.assertEqual(env.signed, 0); self.assertEqual(env.node.broadcasts, []); self.assertIn(f["outcome"], ("DECLINED", "NOT_SUBMITTED"))

    def test_sender_change_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); art = RL.prepare_artifact(node, rules(), SENDER, now=T0)
            art2 = copy.deepcopy(art); art2["sender"] = RECV                                     # 아티팩트 발신자만 바꿔치기(지문은 옛것)
            env = LinkEnv(tmp, node, artifact=art2, confirm=art["confirm_sha256"]); f = env.run()
            self.assertEqual(env.signed, 0); self.assertEqual(node.broadcasts, [])

    def test_stale_preview_digest_reuse_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); old = RL.prepare_artifact(node, rules(), SENDER, now=T0 - 100)   # 이전 미리보기(다른 timestamp → 다른 바이트/지문)
            new = RL.prepare_artifact(node, rules(), SENDER, now=T0)
            self.assertNotEqual(old["confirm_sha256"], new["confirm_sha256"])
            env = LinkEnv(tmp, node, artifact=new, confirm=old["confirm_sha256"]); f = env.run()  # 사람은 옛 지문을 읽음
            self.assertEqual(f["outcome"], "DECLINED"); self.assertEqual(f["reason"], "human did not approve"); self.assertEqual(env.signed, 0)

    def test_artifact_bytes_swapped_after_confirm_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); art = RL.prepare_artifact(node, rules(), SENDER, now=T0)
            other = RL.prepare_artifact(node, rules(), SENDER, now=T0 - 5)
            art2 = copy.deepcopy(art); art2["unsigned"] = other["unsigned"]                      # 지문은 그대로, 바이트만 교체
            env = LinkEnv(tmp, node, artifact=art2, confirm=art["confirm_sha256"]); f = env.run()
            self.assertEqual(env.signed, 0); self.assertEqual(node.broadcasts, []); self.assertIn(f["outcome"], ("DECLINED", "NOT_SUBMITTED"))   # 요약 지문이 달라져 사람이 승인 안 함

    def test_expired_artifact_blocked_no_auto_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); art = RL.prepare_artifact(node, rules(), SENDER, now=T0)
            env = LinkEnv(tmp, node, artifact=art); env.clock["t"] = rules().deadline_ts + 1
            f = env.run(); self.assertEqual(f["outcome"], "DECLINED"); self.assertEqual(env.signed, 0)

    def test_approval_extra_mismatch_at_execute_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = LinkEnv(tmp, FakeNode())
            real_execute_state = {}
            orig = NX.make_nile_hooks
            def patched(**kw):
                a, e, st = orig(**kw); real_execute_state.update(st)
                def e2(plan, ap):
                    st["extra"] = dict(st["extra"], trx_total_cap_sun=50_000_000)               # 승인 뒤 조건 변조
                    return e(plan, ap)
                return a, e2, st
            NX.make_nile_hooks = patched
            try:
                f = env.run()
            finally:
                NX.make_nile_hooks = orig
            self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertIn("approval no longer matches", f["execution"]["detail"]["reason"]); self.assertEqual(env.signed, 0)


# ── 3. 비용 분리 ────────────────────────────────────────────────────────────
class CostSplit(unittest.TestCase):
    def test_plan_splits_cap_into_energy_limit_and_bandwidth_max(self):
        art = RL.prepare_artifact(FakeNode(), rules(), SENDER, now=T0); p = art["plan"]
        bw = NX.BANDWIDTH_BYTES_TRANSFER * 1000                                                        # 345,000 sun (과금 바이트 기준)
        self.assertEqual((p["trx_bandwidth_max_sun"], p["trx_fee_limit_sun"], p["trx_total_max_sun"]), (bw, 5_000_000 - bw, 5_000_000))
        d = NT.decode_raw(art["unsigned"]["raw_data_hex"]); self.assertEqual(d["fee_limit"], 5_000_000 - bw)              # 체인 fee_limit = Energy 한도
        self.assertGreaterEqual(p["trx_fee_limit_sun"], p["trx_est_sun"])
        self.assertIn("Bandwidth 최대", art["human_readable"]); self.assertIn("지금 예상 총비용", art["human_readable"])

    def test_free_bandwidth_exhausted_still_within_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(free_net=0); env = LinkEnv(tmp, node); f = env.run()
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED"); self.assertGreater(f["quote"]["expected_total_sun"], f["quote"]["est_energy_sun"])
            self.assertLessEqual(f["chosen"]["plan"]["trx_total_max_sun"], 5_000_000)

    def test_cap_too_small_for_energy_plus_bandwidth_declines(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = LinkEnv(tmp, FakeNode(), r=rules(trx_fee_cap_sun=2_100_000)); f = env.run()          # energy 1.9045 + bw 0.345 = 2.2495 > 2.1
            self.assertEqual(f["outcome"], "DECLINED"); self.assertIn("cannot cover", f["reason"]); self.assertEqual(env.signed, 0)

    def test_price_change_before_execute_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); env = LinkEnv(tmp, node)
            calls = {"n": 0}
            orig = node.chain_parameters
            def cp():
                calls["n"] += 1
                if calls["n"] >= 2:                                                       # 실행 직전 재조회에서 대역폭 단가 인상
                    return {"http": 200, "body": {"chainParameter": [{"key": "getEnergyFee", "value": 100}, {"key": "getTransactionFee", "value": 5000}]}}
                return orig()
            node.chain_parameters = cp
            f = env.run(); self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertIn("quote changed", f["execution"]["detail"]["reason"]); self.assertEqual(node.broadcasts, [])

    def test_receipt_over_bounds_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); real = node.tx_info
            def over(txid, solid=False):
                r = real(txid, solid)
                if r["body"].get("receipt"):
                    r["body"]["fee"] = 6_000_000; r["body"]["receipt"]["energy_fee"] = 6_000_000     # 총상한 초과
                return r
            node.tx_info = over
            env = LinkEnv(tmp, node); f = env.run()
            self.assertEqual(f["execution"]["detail"]["outcome"], "ONCHAIN_MISMATCH_NEEDS_HUMAN"); self.assertNotEqual(env.intent_log.state(f["execution"]["detail"]["payment_id"]), "CONFIRMED")
            self.assertFalse(f["execution"]["detail"]["receipt"]["fee_within_limit"])

    def test_receipt_net_fee_over_bandwidth_max_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); real = node.tx_info
            def over(txid, solid=False):
                r = real(txid, solid)
                if r["body"].get("receipt"):
                    r["body"]["receipt"]["net_fee"] = 900_000
                return r
            node.tx_info = over
            env = LinkEnv(tmp, node); f = env.run(); self.assertEqual(f["execution"]["detail"]["outcome"], "ONCHAIN_MISMATCH_NEEDS_HUMAN")


# ── 1. AI 결과 → 실행 연결 ──────────────────────────────────────────────────
def ai_record(plan_id="fa4b723f945db52e", outcome="VALIDATED", used=True, status="used", by="AI(qwen3-32b) 설명 · 근거는 서버 자료", request_id="req-1"):
    return {"flow_id": "intent-abc", "plan_id": plan_id, "kiln": {"outcome": outcome, "used": used, "model": "qwen3-32b", "request_id": request_id},
            "ai_work": {"ai_status": status, "raw_ai": {"next_action": "propose_plan"}, "items": [{"step": "맞춤 안내 · 다음 행동", "action": "다음 행동 제안", "by": by, "summary": "원화 준비 뒤 진행"}]}}


class AILink(unittest.TestCase):
    def _log(self, tmp, ids=("req-1",)):
        p = pathlib.Path(tmp) / "kiln_calls.jsonl"
        p.write_text("".join(json.dumps({"call_id": i, "request_id": i, "model": "qwen3-32b"}) + "\n" for i in ids)); return p

    def test_meta_not_ok_never_reaches_approval_even_with_valid_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = LinkEnv(tmp, FakeNode(), kiln_choose=lambda c, r: ({"choice_index": 0, "reason": "단일 후보"}, {"ok": False, "model": None}))
            f = env.run(); self.assertEqual(f["outcome"], "PLAN_ONLY_AI_UNAVAILABLE"); self.assertEqual(env.confirm_calls, []); self.assertEqual(env.signed, 0)

    def test_manual_tech_check_is_flagged_not_a_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = LinkEnv(tmp, FakeNode(), kiln_choose=NX.manual_tech_check_choose); f = env.run()
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED"); self.assertEqual(f["ai_mode"], "MANUAL_TECH_CHECK"); self.assertFalse(f["a_demo_eligible"])
            self.assertEqual(f["order"]["ai_mode"], "MANUAL_TECH_CHECK")

    def test_carried_record_ok_and_linked_in_same_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp); rec = ai_record()
            choose = NX.kiln_choose_from_record(rec, kiln_log=log, expect_flow_id="intent-abc")
            env = LinkEnv(tmp, FakeNode(), ai_record=rec, ai_record_ref="intent-abc.json:deadbeef", kiln_choose=choose, kiln_log=log); f = env.run(flow_id="intent-abc")
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED"); self.assertEqual(f["ai_mode"], "AI_CARRIED"); self.assertTrue(f["a_demo_eligible"])
            self.assertEqual(f["kiln"]["request_id"], "req-1"); self.assertEqual(env.artifact["ai_record_check"]["ok"], True)
            ap = env.policy.approvals["b-intent-abc:r1:approval"]; self.assertEqual(ap["displayed_extra"]["ai_record_ref"], "intent-abc.json:deadbeef")

    def test_carried_record_rejected_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp)
            for rec, why in ((ai_record(plan_id="other"), "plan_id"), (ai_record(outcome="INVALID_JSON", used=False), "outcome"),
                             (ai_record(status="unused_failed"), "status"), (ai_record(by="규칙"), "AI-backed"), (ai_record(request_id="req-9"), "request_id")):
                choose = NX.kiln_choose_from_record(rec, kiln_log=log)
                env = LinkEnv(tmp, FakeNode(), ai_record=rec, ai_record_ref="x:1", kiln_choose=choose, kiln_log=log)
                self.assertEqual(env.artifact["ai_record_check"]["ok"], False, why); self.assertIn(why, env.artifact["ai_record_check"]["why"])
                f = env.run(); self.assertEqual(f["outcome"], "PLAN_ONLY_AI_UNAVAILABLE", why); self.assertEqual(env.signed, 0)


if __name__ == "__main__":
    unittest.main()


class AiWorkOnOrder(unittest.TestCase):
    """9/29 VP: 서명 화면에서 AI가 한 일과 코드가 검사한 일을 구분. 주문에 ai_work(ai/code/human)·goal 이 실린다."""
    def test_order_carries_ai_work_and_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = LinkEnv(tmp, FakeNode()); f = env.run()
            od = f["order"]; self.assertEqual(od["goal"], env.r.goal); w = od["ai_work"]
            self.assertEqual(w["ai"]["mode"], "AI_LIVE"); self.assertGreaterEqual(len(w["code"]), 4); self.assertIn("서명", w["human"])
        info = {"mode": "AI_LIVE", "model": "qwen3-32b", "request_id": "req-9", "choice_index": 0, "reason": "규칙에 맞음"}
        w = NX.build_ai_work(info, None, rules(), "AI_LIVE"); self.assertEqual((w["ai"]["reason"], w["ai"]["request_id"]), ("규칙에 맞음", "req-9"))
        self.assertEqual(NX.build_ai_work({"mode": "MANUAL_TECH_CHECK"}, None, rules(), None)["ai"]["mode"], "MANUAL_TECH_CHECK")
        s = pathlib.Path(__file__).resolve().parents[1].joinpath("sign.html").read_text(encoding="utf-8")
        self.assertIn("AI가 한 일", s); self.assertIn("코드가 검사한 일", s)


class AdaptationChangeBlock(unittest.TestCase):
    """9/29 VP 적응 흐름: 금액 조정은 max_within_budget 목표에서만 코드가 계산하고, 변경 전/후/이유를 아티팩트·주문·Kiln 입력에 보여준다."""
    def _prior(self):
        return RL.prepare_artifact(FakeNode(), rules(), SENDER, now=T0)                       # exact 1 USDT (정상 흐름 아티팩트 역할)

    def test_prepare_with_prior_computes_before_after_and_adjusted_value(self):
        prior = self._prior()
        r2 = rules(mode="max_within_budget", amount_units=1_000_000, budget_total_units=600_000, min_receive_units=500_000)
        art = RL.prepare_artifact(FakeNode(), r2, SENDER, now=T0, prior=prior, change_reason="사용자가 예산을 0.6 USDT 로 줄임")
        self.assertEqual(art["plan"]["value_units"], 600_000)                                  # 코드가 예산 안에서 조정
        ch = art["change"]; self.assertEqual(ch["before"]["planned_value_units"], 1_000_000); self.assertEqual(ch["after"]["planned_value_units"], 600_000)
        self.assertIn("budget_total_units", ch["changed_fields"]); self.assertIn("mode", ch["changed_fields"]); self.assertEqual(ch["user_reason"], "사용자가 예산을 0.6 USDT 로 줄임")
        self.assertEqual(ch["prior_artifact_txid"], prior["unsigned"]["txID"]); self.assertIn("조건 변경(전 → 후 · 이유)", art["human_readable"])
        self.assertNotEqual(art["confirm_sha256"], prior["confirm_sha256"])                    # 지문은 새 조건을 반영

    def test_exact_amount_goal_is_declined_not_adjusted(self):
        prior = self._prior()
        r2 = rules(mode="exact_amount", amount_units=1_000_000, budget_total_units=600_000)
        art = RL.prepare_artifact(FakeNode(), r2, SENDER, now=T0, prior=prior, change_reason="예산 축소")
        self.assertIsNone(art["plan"]); self.assertIn("exceeds budget", art["decline_reason"]); self.assertIsNotNone(art["change"])

    def test_kiln_messages_and_order_carry_change(self):
        import kiln_choose as KC
        prior = self._prior(); r2 = rules(mode="max_within_budget", amount_units=1_000_000, budget_total_units=600_000, min_receive_units=500_000)
        art = RL.prepare_artifact(FakeNode(), r2, SENDER, now=T0, prior=prior, change_reason="예산 축소")
        cands = D.candidate_plans(r2, 0, T0, NX.quote_from_node(FakeNode(), SENDER, SENDER, 1_000_000, now_fn=lambda: T0).trx_quote())
        msg = KC.build_messages(cands, r2, {"change": art["change"]}); body = json.loads(msg[1]["content"])
        self.assertEqual(body["condition_change"]["before"]["budget_total_units"], 1_000_000); self.assertEqual(body["condition_change"]["after"]["budget_total_units"], 600_000)
        self.assertNotIn("condition_change", json.loads(KC.build_messages(cands, r2)[1]["content"]))
        with tempfile.TemporaryDirectory() as tmp:
            env = LinkEnv(tmp, FakeNode(), r=r2, artifact=art)
            ai_info = {"mode": "AI_LIVE", "model": "qwen3-32b", "reason": "예산 0.6 안에서 0.6 전송", "choice_index": 0, "change": art["change"]}
            f = NX.run_nile_flow(r2, policy=env.policy, batch_id="b-adapt", intent_log=env.intent_log, approvals=env.approvals, node=env.node, sender=SENDER,
                                 quote_fn=lambda: NX.quote_from_node(env.node, SENDER, SENDER, r2.amount_units, now_fn=lambda: T0), human_confirm=env.human_confirm, human_sign=env.sign,
                                 kiln_choose=env.kiln_choose, flow_id="adapt", now_fn=lambda: T0, artifact=art, sleep_fn=lambda s: None, receipt_polls=2, ai_info=ai_info)
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED"); od = f["order"]
            self.assertEqual(od["amount_units"], 600_000); self.assertEqual(od["change"]["after"]["planned_value_units"], 600_000); self.assertEqual(od["ai_work"]["ai"]["reason"], "예산 0.6 안에서 0.6 전송")
        s = pathlib.Path(__file__).resolve().parents[1].joinpath("sign.html").read_text(encoding="utf-8")
        self.assertIn("조건 변경(적응)", s); self.assertIn("eth_requestAccounts", s); self.assertIn("tron_requestAccounts", s); self.assertIn("function tronWebNow", s)
        self.assertLess(s.index('method: "eth_requestAccounts"'), s.index('method: "tron_requestAccounts"'))     # 최신 경로 우선, legacy 폴백


class RevisionDigestConsistency(unittest.TestCase):
    """9/29 01:18 실사고: 아티팩트 revision 2 → approve 지문(ctx revision) 과 execute 재계산(정책 배치 revision 1) 불일치 → NOT_SUBMITTED, Kiln 호출만 소모.
    수정 후: 사람이 본 지문의 revision 으로 재계산해 실행까지 간다. 그리고 그 실패 flow 의 검증된 Kiln 결과를 새 호출 없이 이어받을 수 있다."""
    def test_artifact_revision_2_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            r2 = rules(mode="max_within_budget", amount_units=1_000_000, budget_total_units=600_000, min_receive_units=500_000)
            art = RL.prepare_artifact(FakeNode(), r2, SENDER, now=T0, revision=2)
            env = LinkEnv(tmp, FakeNode(), r=r2, artifact=art)
            f = NX.run_nile_flow(r2, policy=env.policy, batch_id="b-rev2", intent_log=env.intent_log, approvals=env.approvals, node=env.node, sender=SENDER,
                                 quote_fn=lambda: NX.quote_from_node(env.node, SENDER, SENDER, r2.amount_units, now_fn=lambda: T0), human_confirm=env.human_confirm, human_sign=env.sign,
                                 kiln_choose=env.kiln_choose, flow_id="rev2", revision=2, now_fn=lambda: T0, artifact=art, sleep_fn=lambda s: None, receipt_polls=2)
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED", f.get("execution")); self.assertEqual(f["order"]["amount_units"], 600_000)

    def _prior_ok(self, tmp, r2, art, cands):
        raw = '{"choice_index": 0, "reason": "예산 축소 반영"}'
        prior = {"flow_id": "old-flow", "rules_digest": r2.digest(), "confirm_sha256": art["confirm_sha256"], "executed": False, "order": None,
                 "execution": {"state": "NOT_SUBMITTED", "detail": {"payment_id": "old:r1:2", "reason": "confirm digest mismatch"}},
                 "kiln": {"ok": True, "validation": "ok", "model": "qwen3-32b", "request_id": None, "raw": raw, "usage": {"prompt_tokens": 813}},
                 "chosen": {"choice_index": 0, "reason": "예산 축소 반영", "plan_digest": cands[0].digest()}}
        log = pathlib.Path(tmp) / "k.jsonl"; log.write_text(json.dumps({"flow_id": "old-flow", "call_id": "c0ffee01", "ok": True, "http": 200, "model": "qwen3-32b", "ts": "t"}) + "\n")
        return prior, log

    def test_reuse_prior_flow_kiln_result_without_new_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            r2 = rules(mode="max_within_budget", amount_units=1_000_000, budget_total_units=600_000, min_receive_units=500_000)
            art = RL.prepare_artifact(FakeNode(), r2, SENDER, now=T0, revision=2)
            q = NX.quote_from_node(FakeNode(), SENDER, SENDER, r2.amount_units, now_fn=lambda: T0); cands = D.candidate_plans(r2, 0, T0, q.trx_quote())
            prior, log = self._prior_ok(tmp, r2, art, cands)
            ilog = IntentLog(pathlib.Path(tmp) / "i.jsonl")
            ch, meta = NX.kiln_choose_from_flow(prior, artifact=art, kiln_log=log, intent_log=ilog)(cands, r2)
            self.assertTrue(meta["ok"], meta); self.assertTrue(meta["carried"]); self.assertEqual(meta["call_id"], "c0ffee01"); self.assertEqual(meta["additional_calls"], 0)
            self.assertEqual(ch, {"choice_index": 0, "reason": "예산 축소 반영"})
            def refused(p=None, **kw):
                _, m = NX.kiln_choose_from_flow(p or prior, artifact=kw.get("artifact", art), kiln_log=kw.get("log", log), intent_log=kw.get("ilog", ilog), order_store=kw.get("store"))(cands, r2)
                self.assertFalse(m["ok"]); return m["why"]
            # ① 서명 전 종료 강제
            self.assertIn("did not end before signing", refused(dict(prior, executed=True)))
            self.assertIn("did not end before signing", refused(dict(prior, execution={"state": "ACCEPTED", "detail": {"payment_id": "old:r1:2"}})))
            self.assertIn("did not end before signing", refused(dict(prior, order={"payment_id": "old:r1:2"})))
            il2 = IntentLog(pathlib.Path(tmp) / "i2.jsonl")
            for st in ("DRAFTED", "AWAITING_HUMAN", "SIGNED"):
                il2.append("old:r1:2", st) if st == "DRAFTED" else il2.append("old:r1:2", st)
            self.assertIn("signing/submission history", refused(ilog=il2))
            from order_store import OrderStore
            st = OrderStore(pathlib.Path(tmp) / "pend"); st.put_pending({"payment_id": "old:r1:2", "snapshot_sha256": "s"})
            rec = st.get("old:r1:2"); st._transition(rec, "CONSUMED"); st._write(st._path("old:r1:2"), rec)
            self.assertEqual(st.get("old:r1:2")["state"], "CONSUMED"); self.assertIn("is CONSUMED", refused(store=st))
            # ② 실제 성공 호출 연결
            self.assertIn("not uniquely found", refused(log=pathlib.Path(tmp) / "none.jsonl"))
            badlog = pathlib.Path(tmp) / "bad.jsonl"; badlog.write_text(json.dumps({"flow_id": "old-flow", "call_id": "x", "ok": False, "http": 500, "model": "qwen3-32b"}) + "\n")
            self.assertIn("not uniquely found", refused(log=badlog))
            wrongmodel = pathlib.Path(tmp) / "wm.jsonl"; wrongmodel.write_text(json.dumps({"flow_id": "old-flow", "call_id": "x", "ok": True, "http": 200, "model": "other"}) + "\n")
            self.assertIn("not uniquely found", refused(log=wrongmodel))
            self.assertIn("does not reproduce", refused(dict(prior, kiln=dict(prior["kiln"], raw='{"choice_index": 0, "reason": "다른 문장"}'))))
            self.assertIn("does not reproduce", refused(dict(prior, kiln=dict(prior["kiln"], raw=""))))
            reqlog = pathlib.Path(tmp) / "rq.jsonl"; reqlog.write_text(json.dumps({"flow_id": "old-flow", "call_id": "x", "ok": True, "http": 200, "model": "qwen3-32b", "request_id": "R1"}) + "\n")
            self.assertIn("not uniquely found", refused(dict(prior, kiln=dict(prior["kiln"], request_id="R2")), log=reqlog))
            # ③ 같은 규칙·계획·지문
            self.assertIn("rules digest", refused(dict(prior, rules_digest="0000")))
            self.assertIn("plan digest", refused(dict(prior, chosen=dict(prior["chosen"], plan_digest="zz"))))
            self.assertIn("artifact confirm digest", refused(dict(prior, confirm_sha256="ff")))
            env = LinkEnv(tmp, FakeNode(), r=r2, artifact=art)
            f = NX.run_nile_flow(r2, policy=env.policy, batch_id="b-reuse", intent_log=env.intent_log, approvals=env.approvals, node=env.node, sender=SENDER,
                                 quote_fn=lambda: NX.quote_from_node(env.node, SENDER, SENDER, r2.amount_units, now_fn=lambda: T0), human_confirm=env.human_confirm, human_sign=env.sign,
                                 kiln_choose=NX.kiln_choose_from_flow(prior, artifact=art, kiln_log=log, intent_log=ilog), flow_id="reuse", revision=2, now_fn=lambda: T0, artifact=art, sleep_fn=lambda s: None, receipt_polls=2)
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED"); self.assertEqual(f["ai_mode"], "AI_CARRIED"); self.assertTrue(f["a_demo_eligible"])


class DeclineFlow(unittest.TestCase):
    """9/29 VP 거절 흐름: 코드가 지급 불가를 강제(후보 0), AI 는 이유 설명만. 주문·서명·방송·원장 기록 0. 후보가 있으면 호출 전에 중단."""
    def _rules(self, **kw):
        return rules(mode="exact_amount", amount_units=1_000_000, budget_total_units=600_000, **kw)          # 정확히 1 USDT, 예산 0.6, 감액 불허

    def test_code_blocks_and_ai_only_explains_no_ledger_change(self):
        import kiln_choose as KC
        calls = []
        def fake_chat(messages, *, flow_id, max_tokens=200, **kw):
            calls.append(json.loads(messages[1]["content"]))
            return {"ok": True, "http": 200, "content": '{"decision": "decline", "reason": "예산 0.6 USDT 로는 정확히 1 USDT 를 보낼 수 없고 감액이 허용되지 않아 지급하지 않습니다."}', "usage": {"prompt_tokens": 1}, "model": "qwen3-32b", "request_id": None, "finish_reason": "stop", "error": None}
        with tempfile.TemporaryDirectory() as tmp:
            pend = pathlib.Path(tmp) / "pending"; il = pathlib.Path(tmp) / "i.jsonl"; il.write_text("")
            rec = RL.decline_flow(FakeNode(), self._rules(), SENDER, flow_id="dcl", kiln=True, now=T0, chat_fn=fake_chat, pending_dir=pend, intents_path=il)
            self.assertEqual(rec["outcome"], "DECLINED_BY_CODE_AI_EXPLAINED"); self.assertTrue(rec["code_block"]["blocked"]); self.assertIn("exceeds budget", rec["code_block"]["reason"])
            self.assertEqual((rec["signed"], rec["broadcast"], rec["onchain_tx"], rec["order"], rec["unsigned"]), (0, 0, 0, None, None)); self.assertTrue(rec["ledger_unchanged"])
            self.assertEqual(rec["ai_explanation"]["decision"], "decline"); self.assertIn("결정 권한 없음", rec["ai_work"]["ai"]["did"]); self.assertTrue(rec["a_demo_eligible"])
            self.assertEqual(calls[0]["candidates"], []); self.assertIn("exceeds budget", calls[0]["code_decline_reason"])
            self.assertEqual(len(list(pend.glob("*.json"))) if pend.exists() else 0, 0); self.assertEqual(il.read_text(), "")
        # 모델이 형식을 어기거나 다른 결정을 내도 실행에 쓰이지 않는다
        for bad in ('{"decision": "approve", "reason": "x"}', '{"decision": "decline", "reason": "y", "amount": 600000}', 'not json'):
            r2 = RL.decline_flow(FakeNode(), self._rules(), SENDER, flow_id="dcl2", kiln=True, now=T0, chat_fn=lambda m, **k: {"ok": True, "http": 200, "content": bad, "model": "qwen3-32b", "finish_reason": "stop"}, pending_dir=pathlib.Path(tmp) / "p2", intents_path=pathlib.Path(tmp) / "i2.jsonl")
            self.assertEqual(r2["outcome"], "DECLINED_BY_CODE_AI_EXPLANATION_UNAVAILABLE"); self.assertTrue(r2["code_block"]["blocked"]); self.assertEqual(r2["signed"] + r2["broadcast"], 0)

    def test_candidate_exists_means_no_call_at_all(self):
        called = {"n": 0}
        def fake_chat(messages, **kw): called["n"] += 1; return {"ok": True, "content": "{}"}
        with tempfile.TemporaryDirectory() as tmp:
            rec = RL.decline_flow(FakeNode(), rules(), SENDER, flow_id="nd", kiln=True, now=T0, chat_fn=fake_chat, pending_dir=pathlib.Path(tmp) / "p", intents_path=pathlib.Path(tmp) / "i.jsonl")
            self.assertEqual(rec["outcome"], "NOT_A_DECLINE_SCENARIO"); self.assertEqual(called["n"], 0); self.assertIsNone(rec["kiln"])

    def test_no_kiln_flag_declines_without_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = RL.decline_flow(FakeNode(), self._rules(), SENDER, flow_id="nk", kiln=False, now=T0, pending_dir=pathlib.Path(tmp) / "p", intents_path=pathlib.Path(tmp) / "i.jsonl")
            self.assertEqual(rec["outcome"], "DECLINED_BY_CODE_NO_AI_CALL"); self.assertIsNone(rec["kiln"]); self.assertFalse(rec["a_demo_eligible"])
