"""gasfree_order 연결 검사 — 로컬 정책 원장 + 가짜 GasFree 응답. 서명·송금·네트워크 없음."""
import unittest

from safebatch.flow import run_batch, approve_batch, revise_batch
from safebatch.policy import PaymentPolicy
from safebatch import gasfree_order as go

RECV = "TMDKznuDWaZwfZHcM61FVFstyYNmK6Njk1"
USDT = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"
PROV = "TKtWbdzEq5ss9vTS9kwRhBp5mXmBfBns3E"
EOA = "TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz"
GF = "TLGVf7MRsLG7XxBkJKy8wnCVcDnAeXYNCb"
CSV = f"recipient,amount,memo\n{RECV},1.5,test payout row\n"


def tokens_info():
    return [{"tokenAddress": USDT, "symbol": "USDT", "decimal": 6, "activateFee": 10_000_000,
             "transferFee": 10_000_000, "supported": True}]


def address_info(active=True, allow=True, balance=100_000_000, frozen=0, nonce=3, snake=False):
    d = {"accountAddress": EOA, "gasFreeAddress": GF, "active": active, "nonce": nonce,
         "assets": [{"tokenAddress": USDT, "tokenSymbol": "USDT", "activateFee": 10_000_000,
                     "transferFee": 10_000_000, "decimal": 6, "frozen": frozen, "balance": balance}]}
    d["allow_submit" if snake else "allowSubmit"] = allow
    return {"code": 200, "data": d}


class FakeClient:
    def __init__(self, submit_resp=None, trace_resp=None, submit_raises=None):
        self.submit_resp, self.trace_resp, self.submit_raises = submit_resp, trace_resp, submit_raises
        self.submitted = []

    def submit(self, body):
        self.submitted.append(body)
        if self.submit_raises:
            raise self.submit_raises
        return self.submit_resp

    def trace(self, trace_id):
        return self.trace_resp


def approved_setup(allowlist=(RECV,)):
    policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=10_000_000, allowlist=set(allowlist))
    res = run_batch(CSV, policy, "b1")
    ap = approve_batch(policy, "b1", res["revision"], res["csv_sha256"], confirmed_by="tester")
    assert ap["outcome"] == "APPROVED_HUMAN_CONFIRMED", ap
    row = res["rows"][0]
    return policy, res, ap["approval"], row


class BuildOrder(unittest.TestCase):
    def test_happy_path_builds_typed_data(self):
        policy, res, ap, row = approved_setup()
        o = go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                           approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                           address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV, now=1_000)
        m = o.message
        self.assertEqual(m["receiver"], RECV); self.assertEqual(m["value"], "1500000")
        self.assertEqual(m["maxFee"], "10000000")          # active → transferFee only
        self.assertEqual(m["nonce"], 3); self.assertEqual(m["deadline"], str(1_000 + 600))
        self.assertEqual(o.domain["chainId"], 3448148188)
        self.assertTrue(all(c["pass"] for c in o.checks))
        self.assertIn("NILE", o.human_summary())
        # 같은 지급 → 같은 requestId
        o2 = go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                            approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                            address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV, now=2_000)
        self.assertEqual(o.request_id, o2.request_id)

    def test_inactive_account_adds_activate_fee(self):
        policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=20_000_000, allowlist={RECV})  # 활성화비 포함 예약
        res = run_batch(CSV, policy, "b1")
        ap = approve_batch(policy, "b1", res["revision"], res["csv_sha256"], confirmed_by="tester")["approval"]
        row = res["rows"][0]
        o = go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                           approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                           address_info=address_info(active=False), tokens_info=tokens_info(), provider_address=PROV)
        self.assertEqual(o.message["maxFee"], "20000000")

    def test_snake_case_allow_submit_is_read(self):
        policy, res, ap, row = approved_setup()
        o = go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                           approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                           address_info=address_info(snake=True), tokens_info=tokens_info(), provider_address=PROV)
        self.assertEqual(o.message["nonce"], 3)

    def _expect_fail(self, name, **kw):
        policy, res, ap, row = approved_setup()
        args = dict(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                    approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                    address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV)
        args.update(kw)
        with self.assertRaises(go.OrderError) as cm:
            go.build_order(**args)
        self.assertIn(name, str(cm.exception))

    def test_blocks_when_not_allowed_to_submit(self):
        self._expect_fail("allow_submit", address_info=address_info(allow=False))

    def test_blocks_when_insufficient_after_frozen(self):
        self._expect_fail("sufficient_available", address_info=address_info(balance=12_000_000, frozen=1_000_000))

    def test_blocks_when_balance_not_visible(self):
        ai = address_info(); del ai["data"]["assets"][0]["balance"]
        self._expect_fail("balance_present", address_info=ai)

    def test_blocks_wrong_csv_hash(self):
        self._expect_fail("csv_sha256_current", csv_sha256="0" * 64)

    def test_blocks_tampered_row_amount(self):
        policy, res, ap, row = approved_setup()
        bad = dict(row); bad["amount_units"] = row["amount_units"] + 1
        with self.assertRaises(go.OrderError) as cm:
            go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                           approval_id=ap["approval_id"], payment_id=row["payment_id"], row=bad,
                           address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV)
        self.assertIn("row_matches_record", str(cm.exception))

    def test_blocks_after_revision_supersedes_approval(self):
        policy, res, ap, row = approved_setup()
        csv2 = CSV.replace("1.5", "1.6")
        r2 = revise_batch(csv2, policy, "b1", res["revision"], "req-1")
        self.assertEqual(r2["stage"], "AWAITING_HUMAN_APPROVAL")
        with self.assertRaises(go.OrderError) as cm:   # 옛 revision·옛 승인으로 주문서 생성 불가
            go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                           approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                           address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV)
        self.assertIn("revision_current", str(cm.exception))

    def test_blocks_unsupported_token(self):
        t = tokens_info(); t[0]["supported"] = False
        self._expect_fail("token_supported", tokens_info=t)


class SignedSubmitTrack(unittest.TestCase):
    def _order(self):
        policy, res, ap, row = approved_setup()
        return go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                              approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                              address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV, now=1_000)

    SIG = "ab" * 65

    def test_verify_signed_requires_identical_message(self):
        o = self._order()
        body = go.verify_signed(o, {"message": dict(o.message), "sig": "0x" + self.SIG}, now=1_100)
        self.assertEqual(body["value"], 1_500_000); self.assertEqual(body["sig"], self.SIG)
        self.assertEqual(body["requestId"], o.request_id)
        tampered = dict(o.message); tampered["receiver"] = EOA
        with self.assertRaises(go.OrderError):
            go.verify_signed(o, {"message": tampered, "sig": self.SIG}, now=1_100)
        with self.assertRaises(go.OrderError):   # deadline 경과 → 재서명 필요
            go.verify_signed(o, {"message": dict(o.message), "sig": self.SIG}, now=5_000)
        with self.assertRaises(go.OrderError):
            go.verify_signed(o, {"message": dict(o.message), "sig": "zz"}, now=1_100)

    def test_transport_error_is_unknown_not_retry(self):
        o = self._order()
        body = go.verify_signed(o, {"message": dict(o.message), "sig": self.SIG}, now=1_100)
        c = FakeClient(submit_raises=RuntimeError("timeout"))
        out = go.submit_and_track(c, o, body, require_intent_log=False, now=1_100)
        self.assertEqual(out["outcome"], "UNKNOWN_SUBMIT_TRANSPORT")
        self.assertEqual(len(c.submitted), 1)          # 재송금 없음

    def test_provider_reject_is_discarded(self):
        o = self._order()
        body = go.verify_signed(o, {"message": dict(o.message), "sig": self.SIG}, now=1_100)
        c = FakeClient(submit_resp={"http": 200, "body": {"code": 400, "reason": "NonceNotMatchException", "message": "x"}})
        out = go.submit_and_track(c, o, body, require_intent_log=False, now=1_100)
        self.assertEqual(out["outcome"], "REJECTED_BY_PROVIDER"); self.assertIsNone(out["trace_id"])

    def test_accept_then_trace_to_succeed_and_reconcile(self):
        o = self._order()
        body = go.verify_signed(o, {"message": dict(o.message), "sig": self.SIG}, now=1_100)
        trace = {"id": "t-1", "state": "SUCCEED", "txnState": "SOLIDITY", "txnHash": "deadbeef",
                 "targetAddress": RECV, "tokenAddress": USDT, "nonce": 3, "txnAmount": 1_500_000,
                 "txnTotalFee": 10_000_000, "txnTotalCost": 11_500_000}
        c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1", "state": "WAITING"}}},
                       trace_resp={"http": 200, "body": {"code": 200, "data": trace}})
        out = go.submit_and_track(c, o, body, poll=2, sleep_fn=lambda s: None, require_intent_log=False, now=1_100)
        self.assertEqual(out["outcome"], "FINAL_SUCCEED")
        rec = go.reconcile(o, out["trace"])
        self.assertEqual(rec["verdict"], "RECONCILED"); self.assertEqual(rec["row_no"], 2)
        self.assertEqual(rec["memo"], "test payout row")

    def test_reconcile_flags_amount_mismatch(self):
        o = self._order()
        trace = {"state": "SUCCEED", "txnState": "ON_CHAIN", "txnHash": "h", "targetAddress": RECV,
                 "tokenAddress": USDT, "nonce": 3, "txnAmount": 1_400_000, "txnTotalFee": 10_000_000}
        rec = go.reconcile(o, trace)
        self.assertFalse(rec["amount_match"]); self.assertEqual(rec["verdict"], "MISMATCH_OR_INCOMPLETE")

    def test_reconcile_pending(self):
        o = self._order()
        rec = go.reconcile(o, {"state": "CONFIRMING"})
        self.assertEqual(rec["verdict"], "PENDING")


if __name__ == "__main__":
    unittest.main()
