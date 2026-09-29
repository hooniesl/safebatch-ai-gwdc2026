"""intent 원장(nonce 잠금·재제출 금지) + 단일 export/replay 검사. 네트워크·서명 없음."""
import json
import pathlib
import tempfile
import unittest

from safebatch.flow import run_batch, approve_batch
from safebatch.policy import PaymentPolicy
from safebatch import gasfree_order as go
from safebatch.intent_log import IntentLog, IntentError
from safebatch import export as ex
from safebatch.tests.test_gasfree_order import (RECV, USDT, PROV, EOA, GF, tokens_info, address_info, FakeClient)

# 수신자 3개를 모두 다르게 둔다(csvcheck 는 같은 수신자 반복을 중복 의심으로 표시해 후보에서 뺀다).
CSV3 = ("recipient,amount,memo\n"
        f"{RECV},1.5,row A ok\n"
        f"{EOA},999999,row B budget\n"
        f"{GF},1,row C not allowlisted\n")
SIG = "cd" * 65


def setup(tmp):
    policy = PaymentPolicy(budget_units=100_000_000, fee_units=10_000_000, allowlist=frozenset({RECV, EOA}))
    res = run_batch(CSV3, policy, "b9")
    ap = approve_batch(policy, "b9", res["revision"], res["csv_sha256"], confirmed_by="tester")
    row = res["rows"][0]
    order = go.build_order(policy=policy, batch_id="b9", revision=res["revision"], csv_sha256=res["csv_sha256"],
                           approval_id=ap["approval"]["approval_id"], payment_id=row["payment_id"], row=row,
                           address_info=address_info(nonce=7), tokens_info=tokens_info(), provider_address=PROV, now=1_000)
    log = IntentLog(pathlib.Path(tmp) / "intents.jsonl")
    go.record_order_intents(log, order)
    return policy, res, ap, row, order, log


class IntentLogRules(unittest.TestCase):
    def test_transitions_and_nonce_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, res, ap, row, order, log = setup(tmp)
            self.assertEqual(log.state(order.payment_id), "AWAITING_HUMAN")
            self.assertFalse(log.can_submit(order.payment_id, EOA, 7)[0])     # not SIGNED yet
            log.append(order.payment_id, "SIGNED")
            self.assertTrue(log.can_submit(order.payment_id, EOA, 7)[0])
            with self.assertRaises(IntentError):                              # illegal jump
                log.append(order.payment_id, "CONFIRMED")
            log.append(order.payment_id, "SUBMITTED"); log.append(order.payment_id, "UNKNOWN", reason="timeout")
            self.assertTrue(log.nonce_locked(EOA, 7))
            self.assertFalse(log.can_submit(order.payment_id, EOA, 7)[0])     # never resubmit UNKNOWN
            self.assertFalse(log.can_submit("b9:r1:99", EOA, 7)[0])           # other payment can't take nonce 7
            log.append(order.payment_id, "CONFIRMED", tx_hash="h1")
            self.assertFalse(log.nonce_locked(EOA, 7))
            self.assertFalse(log.can_submit(order.payment_id, EOA, 7)[0])     # confirmed → no resubmit
            lines = (pathlib.Path(tmp) / "intents.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 6)                                    # append-only, 6 transitions
            self.assertEqual(json.loads(lines[-1])["tx_hash"], "h1")

    def test_corrupt_line_is_counted_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "i.jsonl"; p.write_text('{"payment_id":"x","state":"DRAFTED"}\nnot json\n')
            log = IntentLog(p); self.assertEqual(len(log.entries()), 1); self.assertEqual(log.bad_lines, 1)


class SubmitWithIntentLog(unittest.TestCase):
    def test_timeout_then_resubmit_blocked_zero_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, res, ap, row, order, log = setup(tmp)
            body = go.verify_signed(order, {"message": dict(order.message), "sig": SIG}, now=1_100)
            log.append(order.payment_id, "SIGNED")
            c = FakeClient(submit_raises=RuntimeError("timeout"))
            out = go.submit_and_track(c, order, body, intent_log=log, policy=policy, now=1_100)
            self.assertEqual(out["outcome"], "UNKNOWN_SUBMIT_TRANSPORT"); self.assertEqual(len(c.submitted), 1)
            self.assertEqual(log.state(order.payment_id), "UNKNOWN")
            out2 = go.submit_and_track(c, order, body, intent_log=log, policy=policy, now=1_100)   # 재실행 → 제출 0건
            self.assertEqual(out2["outcome"], "BLOCKED_BY_INTENT_LOG"); self.assertEqual(len(c.submitted), 1)
            # 전송 예외로 traceId 가 원장에 없으면 외부에서 준 traceId 로 종결하지 않는다(9/28 Grok#3)
            c.trace_resp = {"http": 200, "body": {"code": 200, "data": {"id": "t-9", "state": "SUCCEED", "txnHash": "0xabc"}}}
            r = go.resolve_unknown(c, order, log, trace_id="t-9")
            self.assertEqual(r["outcome"], "STILL_UNKNOWN_NO_TRACE_ID"); self.assertEqual(log.state(order.payment_id), "UNKNOWN")
            self.assertTrue(log.nonce_locked(EOA, 7))

    def test_unknown_without_trace_id_stays_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, res, ap, row, order, log = setup(tmp)
            log.append(order.payment_id, "SIGNED"); log.append(order.payment_id, "SUBMITTED"); log.append(order.payment_id, "UNKNOWN")
            r = go.resolve_unknown(FakeClient(), order, log, trace_id=None)
            self.assertEqual(r["outcome"], "STILL_UNKNOWN_NO_TRACE_ID"); self.assertEqual(log.state(order.payment_id), "UNKNOWN")

    def test_accept_confirm_records_intents(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, res, ap, row, order, log = setup(tmp)
            body = go.verify_signed(order, {"message": dict(order.message), "sig": SIG}, now=1_100)
            log.append(order.payment_id, "SIGNED")
            trace = {"id": "t-1", "state": "SUCCEED", "txnState": "SOLIDITY", "txnHash": "deadbeef", "targetAddress": RECV,
                     "tokenAddress": USDT, "nonce": 7, "txnAmount": 1_500_000, "txnTotalFee": 10_000_000, "txnTotalCost": 11_500_000}
            c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1", "state": "WAITING"}}},
                           trace_resp={"http": 200, "body": {"code": 200, "data": trace}})
            out = go.submit_and_track(c, order, body, poll=1, sleep_fn=lambda s: None, intent_log=log, policy=policy, now=1_100)
            self.assertEqual(out["outcome"], "FINAL_SUCCEED"); self.assertEqual(log.state(order.payment_id), "CONFIRMED")
            states = [e["state"] for e in log.entries()]
            self.assertEqual(states, ["DRAFTED", "AWAITING_HUMAN", "SIGNED", "SUBMITTED", "ACCEPTED", "CONFIRMED"])


class ExportReplay(unittest.TestCase):
    def _confirmed(self, tmp):
        policy, res, ap, row, order, log = setup(tmp)
        body = go.verify_signed(order, {"message": dict(order.message), "sig": SIG}, now=1_100)
        log.append(order.payment_id, "SIGNED")
        trace = {"id": "t-1", "state": "SUCCEED", "txnState": "SOLIDITY", "txnHash": "deadbeef", "targetAddress": RECV,
                 "tokenAddress": USDT, "nonce": 7, "txnAmount": 1_500_000, "txnTotalFee": 10_000_000, "txnTotalCost": 11_500_000}
        c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1", "state": "WAITING"}}},
                       trace_resp={"http": 200, "body": {"code": 200, "data": trace}})
        go.submit_and_track(c, order, body, poll=1, sleep_fn=lambda s: None, intent_log=log, policy=policy, now=1_100)
        return policy, res, log, {"t-1": trace}

    def test_export_reconstructs_one_paid_two_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, res, log, traces = self._confirmed(tmp)
            e = ex.build_export(policy, "b9", log, traces, human_stops=[])
            rep = ex.replay_check(e)
            self.assertTrue(rep["ok"], rep)
            self.assertEqual(rep["blocked"], 2); self.assertEqual(rep["blocked_with_reason"], 2)   # 범위 밖 2건 사유 있음
            paid = [p for p in e["payments"] if p["intent_state"] == "CONFIRMED"]
            self.assertEqual(len(paid), 1); self.assertEqual(paid[0]["memo"], "row A ok"); self.assertEqual(paid[0]["row_no"], 2)
            self.assertEqual(paid[0]["tx_hash"], "deadbeef"); self.assertEqual(paid[0]["txn_total_fee"], 10_000_000)
            blocked_reasons = {p["decision"] for p in e["payments"] if ex.is_blocked(p["decision"])}
            self.assertEqual(blocked_reasons, {"BLOCKED_BUDGET_EXCEEDED_INCL_FEE", "BLOCKED_RECIPIENT_NOT_ALLOWED"})
            csv_text = ex.to_csv_rows(e)
            self.assertEqual(len(csv_text.strip().splitlines()), 1 + 3)
            json.loads(ex.dumps(e))                                             # 직렬화 가능

    def test_replay_detects_tampered_amount(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, res, log, traces = self._confirmed(tmp)
            traces["t-1"] = dict(traces["t-1"], txnAmount=1_400_000)
            rep = ex.replay_check(ex.build_export(policy, "b9", log, traces))
            self.assertFalse(rep["ok"]); self.assertTrue(any("chain amount" in p for p in rep["problems"]))

    def test_replay_detects_blocked_row_with_tx(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, res, log, traces = self._confirmed(tmp)
            e = ex.build_export(policy, "b9", log, traces)
            for p in e["payments"]:
                if ex.is_blocked(p["decision"]):
                    p["tx_hash"] = "sneaky"; break
            self.assertFalse(ex.replay_check(e)["ok"])


if __name__ == "__main__":
    unittest.main()
