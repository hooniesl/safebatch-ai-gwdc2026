"""Furiosa A 데모 3흐름 안전 검사(9/28 부사장 §2 1~5 반영). 네트워크·Kiln·서명 없음."""
import dataclasses
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import demo_flows as D  # noqa: E402

RECV = "TMDKznuDWaZwfZHcM61FVFstyYNmK6Njk1"
NOW = 1_000_000
FEE = 10_000_000


def rules(**kw):
    base = dict(goal="업비트 USDT → 바이낸스 내 계정", guide_plan_id="fa4b723f945db52e", receiver=RECV,
                mode="max_within_budget", amount_units=50_000_000, budget_total_units=60_000_000,
                min_receive_units=20_000_000, deadline_ts=NOW + 600, allowlist=(RECV,))
    base.update(kw)
    return D.UserRules(**base)


def ok_model(cands, r):
    return {"choice_index": 0, "reason": "fits"}, {"ok": True, "model": "qwen3-32b"}


def human_approve(plan, r, ctx):
    return D.Approval(approval_id=f"ap-{ctx['flow_id']}-{ctx['revision']}", flow_id=ctx["flow_id"], revision=ctx["revision"],
                      rules_digest=ctx["rules_digest"], plan_digest=ctx["plan_digest"], approved_by="tester", approved_at=NOW)


class RulesValidation(unittest.TestCase):
    def test_unknown_mode_and_bad_types_rejected(self):
        for kw in (dict(mode="whatever"), dict(amount_units=1.5), dict(amount_units=True), dict(budget_total_units="60"),
                   dict(min_receive_units=-1), dict(deadline_ts=0), dict(network="tron"), dict(allowlist=[RECV])):
            with self.assertRaises(D.RulesError, msg=str(kw)):
                rules(**kw)

    def test_no_trusted_fee_no_candidates(self):
        for fee in (None, 0, -1, "10", 1.0, True):
            self.assertEqual(D.candidate_plans(rules(), fee, NOW), ())
        f = D.run_flow(rules(), None, NOW, kiln_choose=ok_model, approve=human_approve, execute=lambda p, a: {"state": "CONFIRMED"})
        self.assertEqual(f["outcome"], "DECLINED"); self.assertIn("no trusted provider fee", f["reason"])

    def test_exact_never_reduced_and_totals_exact(self):
        self.assertEqual(D.candidate_plans(rules(mode="exact_amount", amount_units=55_000_000), FEE, NOW), ())
        p = D.candidate_plans(rules(mode="exact_amount", amount_units=40_000_000), FEE, NOW)[0]
        self.assertEqual((p.value_units, p.fee_units, p.total_units), (40_000_000, FEE, 50_000_000))


class AIFailureNeverExecutes(unittest.TestCase):
    def _run(self, chooser):
        calls = []
        f = D.run_flow(rules(), FEE, NOW, kiln_choose=chooser, approve=human_approve,
                       execute=lambda p, a: calls.append(1) or {"state": "CONFIRMED"})
        return f, calls

    def test_invalid_missing_failed_model_outputs_block_execution(self):
        cases = [lambda c, r: ({"choice_index": 9, "reason": "x"}, {}), lambda c, r: ("text", {}),
                 lambda c, r: ({"choice_index": 0, "value_units": 1}, {}), lambda c, r: (None, {})]
        for ch in cases:
            f, calls = self._run(ch)
            self.assertEqual(f["outcome"], "PLAN_ONLY_AI_UNAVAILABLE"); self.assertEqual(calls, []); self.assertFalse(f["executed"])

        def boom(c, r): raise RuntimeError("timeout")
        f, calls = self._run(boom)
        self.assertEqual(f["outcome"], "PLAN_ONLY_AI_UNAVAILABLE"); self.assertEqual(calls, [])
        f = D.run_flow(rules(), FEE, NOW, kiln_choose=None, approve=human_approve, execute=lambda p, a: {"state": "CONFIRMED"})
        self.assertEqual(f["outcome"], "PLAN_ONLY_AI_UNAVAILABLE")


class ApprovalBinding(unittest.TestCase):
    def test_string_or_foreign_or_reused_approval_blocked(self):
        calls = []
        ex = lambda p, a: calls.append(1) or {"state": "CONFIRMED"}  # noqa: E731
        f = D.run_flow(rules(), FEE, NOW, kiln_choose=ok_model, approve=lambda p, r, c: "ap-1", execute=ex)
        self.assertEqual(f["outcome"], "BLOCKED_APPROVAL_INVALID"); self.assertEqual(calls, [])
        stale = {}
        def first(p, r, c):
            stale["ap"] = human_approve(p, r, c); return stale["ap"]
        used = set()
        f1 = D.run_flow(rules(), FEE, NOW, kiln_choose=ok_model, approve=first, execute=ex, flow_id="f1", revision=1, used_approvals=used)
        self.assertEqual(f1["outcome"], "EXECUTED_CONFIRMED")
        f2 = D.run_flow(rules(budget_total_units=40_000_000), FEE, NOW, kiln_choose=ok_model, approve=lambda p, r, c: stale["ap"],
                        execute=ex, flow_id="f2", revision=2, used_approvals=used)      # 이전 승인 재사용 시도
        self.assertEqual(f2["outcome"], "BLOCKED_APPROVAL_INVALID"); self.assertEqual(len(calls), 1)
        f1b = D.run_flow(rules(), FEE, NOW, kiln_choose=ok_model, approve=lambda p, r, c: stale["ap"], execute=ex,
                         flow_id="f1", revision=1, used_approvals=used)                   # 같은 승인 두 번
        self.assertEqual(f1b["reason"], "approval already used"); self.assertEqual(len(calls), 1)

    def test_tampered_plan_after_approval_blocked(self):
        def tamper_approve(p, r, c):
            other = dataclasses.replace(p, value_units=p.value_units + 1)
            return D.Approval("ap-t", c["flow_id"], c["revision"], c["rules_digest"], other.digest(), "tester", NOW)
        f = D.run_flow(rules(), FEE, NOW, kiln_choose=ok_model, approve=tamper_approve, execute=lambda p, a: {"state": "CONFIRMED"})
        self.assertEqual(f["outcome"], "BLOCKED_APPROVAL_INVALID"); self.assertIn("plan digest", f["reason"])

    def test_candidates_are_immutable(self):
        c = D.candidate_plans(rules(), FEE, NOW)[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            c.value_units = 1  # type: ignore[misc]


class PreExecutionRecheck(unittest.TestCase):
    def test_deadline_passes_while_waiting_for_approval(self):
        clock = {"t": NOW}
        calls = []
        f = D.run_flow(rules(deadline_ts=NOW + 10), FEE, NOW, kiln_choose=ok_model, approve=human_approve,
                       execute=lambda p, a: calls.append(1) or {"state": "CONFIRMED"}, now_fn=lambda: clock["t"] + 11)
        self.assertEqual(f["outcome"], "DECLINED"); self.assertIn("conditions changed", f["reason"]); self.assertEqual(calls, [])


class ExecutionClassification(unittest.TestCase):
    def test_dict_presence_is_not_success(self):
        for res, outcome, executed in (({"state": "CONFIRMED", "tx": "h"}, "EXECUTED_CONFIRMED", True),
                                       ({"state": "ACCEPTED"}, "EXECUTED_ACCEPTED_PENDING", True),
                                       ({"state": "UNKNOWN"}, "EXECUTION_UNKNOWN_NO_RESEND", False),
                                       ({"state": "REJECTED"}, "EXECUTION_REJECTED", False),
                                       ({"status": "ok"}, "EXECUTION_UNKNOWN_NO_RESEND", False),
                                       (None, "NOT_SUBMITTED", False), ({}, "EXECUTION_UNKNOWN_NO_RESEND", False)):
            f = D.run_flow(rules(), FEE, NOW, kiln_choose=ok_model, approve=human_approve, execute=lambda p, a, r=res: r)
            self.assertEqual((f["outcome"], f["executed"]), (outcome, executed), res)

    def test_unknown_result_does_not_resend_in_same_flow(self):
        n = {"c": 0}
        def ex(p, a): n["c"] += 1; return {"state": "UNKNOWN"}
        used = set()
        D.run_flow(rules(), FEE, NOW, kiln_choose=ok_model, approve=human_approve, execute=ex, flow_id="f", revision=1, used_approvals=used)
        f = D.run_flow(rules(), FEE, NOW, kiln_choose=ok_model, approve=human_approve, execute=ex, flow_id="f", revision=1, used_approvals=used)
        self.assertEqual(f["outcome"], "BLOCKED_APPROVAL_INVALID"); self.assertEqual(n["c"], 1)   # 같은 승인 ID 재사용 차단


class Bundle(unittest.TestCase):
    def test_three_flows_normal_adapt_decline(self):
        execs = []
        b = D.scenario_bundle(rules(), FEE, NOW, kiln_choose=ok_model, approve=human_approve,
                              execute=lambda p, a: execs.append(p.value_units) or {"state": "CONFIRMED"})
        f1, f2, f3 = b["flows"]
        self.assertEqual(f1["outcome"], "EXECUTED_CONFIRMED"); self.assertEqual(execs[0], 50_000_000)
        self.assertEqual(f2["outcome"], "EXECUTED_CONFIRMED"); self.assertEqual(execs[1], 20_000_000)
        self.assertNotEqual(f1["approval"]["approval_id"], f2["approval"]["approval_id"])
        self.assertEqual(f3["outcome"], "DECLINED"); self.assertEqual(f3["reason"], "deadline passed"); self.assertFalse(f3["executed"])
        self.assertEqual(len({f["flow_id"] for f in b["flows"]}), 3)

    def test_exact_mode_declines_on_budget_cut(self):
        b = D.scenario_bundle(rules(mode="exact_amount", amount_units=45_000_000), FEE, NOW, kiln_choose=ok_model,
                              approve=human_approve, execute=lambda p, a: {"state": "CONFIRMED"})
        self.assertEqual([f["outcome"] for f in b["flows"]], ["EXECUTED_CONFIRMED", "DECLINED", "DECLINED"])


if __name__ == "__main__":
    unittest.main()
