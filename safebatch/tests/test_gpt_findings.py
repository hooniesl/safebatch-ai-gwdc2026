"""9/28 GPT 1회 자문 지적 5건에 대한 회귀 검사(실제 코드 경로로 재현·차단 확인). 네트워크·서명·송금 없음."""
import pathlib
import tempfile
import unittest

from safebatch.flow import run_batch, approve_batch, revise_batch
from safebatch.policy import PaymentPolicy
from safebatch import gasfree_order as go
from safebatch.gasfree_client import GasFreeClient, GasFreeError
from safebatch.intent_log import IntentLog
from safebatch.tests.test_gasfree_order import RECV, USDT, PROV, EOA, CSV, tokens_info, address_info, FakeClient

SIG = "ab" * 65


def setup(fee=10_000_000):
    policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=fee, allowlist=frozenset({RECV}))
    res = run_batch(CSV, policy, "b1")
    ap = approve_batch(policy, "b1", res["revision"], res["csv_sha256"], confirmed_by="tester",
                       displayed_sha256=policy.display_digest("b1", res["revision"]))["approval"]
    row = res["rows"][0]
    o = go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                       approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                       address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV, now=1_000)
    body = go.verify_signed(o, {"message": dict(o.message), "sig": SIG}, now=1_100)
    return policy, res, ap, row, o, body


class G1_ApprovalBoundary(unittest.TestCase):
    def test_display_mismatch_rejects_approval(self):
        policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=10_000_000, allowlist=frozenset({RECV}))
        res = run_batch(CSV, policy, "b1")
        r = approve_batch(policy, "b1", res["revision"], res["csv_sha256"], confirmed_by="t", displayed_sha256="0" * 64)
        self.assertEqual(r["outcome"], "REJECTED_DISPLAY_MISMATCH"); self.assertIsNone(r["approval"])

    def test_submit_rechecks_policy_after_revision(self):
        policy, res, ap, row, o, body = setup()
        with tempfile.TemporaryDirectory() as tmp:
            log = IntentLog(pathlib.Path(tmp) / "i.jsonl"); go.record_order_intents(log, o); log.append(o.payment_id, "SIGNED")
            revise_batch(CSV.replace("1.5", "1.6"), policy, "b1", res["revision"], "req-z")   # 주문 생성 뒤 승인 무효화
            c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t"}}})
            with self.assertRaises(go.OrderError):
                go.submit_and_track(c, o, body, intent_log=log, policy=policy, now=1_100)
            self.assertEqual(len(c.submitted), 0)

    def test_mock_order_never_submits(self):
        policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=10_000_000, allowlist=frozenset({RECV}))
        res = run_batch(CSV, policy, "b1")
        ap = approve_batch(policy, "b1", res["revision"], res["csv_sha256"])["approval"]     # LOCAL_MOCK
        row = res["rows"][0]
        o = go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                           approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row, address_info=address_info(),
                           tokens_info=tokens_info(), provider_address=PROV, now=1_000, allow_mock_approval=True)
        body = go.verify_signed(o, {"message": dict(o.message), "sig": SIG}, now=1_100)
        c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t"}}})
        with self.assertRaises(go.OrderError):
            go.submit_and_track(c, o, body, require_intent_log=False, now=1_100)
        self.assertEqual(len(c.submitted), 0)

    def test_policy_required_for_real_path(self):
        policy, res, ap, row, o, body = setup()
        with tempfile.TemporaryDirectory() as tmp:
            log = IntentLog(pathlib.Path(tmp) / "i.jsonl"); go.record_order_intents(log, o); log.append(o.payment_id, "SIGNED")
            with self.assertRaises(go.OrderError):
                go.submit_and_track(FakeClient(), o, body, intent_log=log, now=1_100)      # policy 없음


class G2_BodySubstitution(unittest.TestCase):
    def test_substituted_body_refused(self):
        policy, res, ap, row, o, body = setup()
        bad = dict(body); bad["receiver"] = EOA
        c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t"}}})
        with self.assertRaises(go.OrderError):
            go.submit_and_track(c, o, bad, require_intent_log=False, now=1_100)
        bad2 = dict(body); bad2["requestId"] = "other"
        with self.assertRaises(go.OrderError):
            go.submit_and_track(c, o, bad2, require_intent_log=False, now=1_100)
        bad3 = dict(body); bad3["sig"] = "zz"
        with self.assertRaises(go.OrderError):
            go.submit_and_track(c, o, bad3, require_intent_log=False, now=1_100)
        self.assertEqual(len(c.submitted), 0)
        ok = go.submit_and_track(c, o, body, require_intent_log=False, now=1_100)
        self.assertTrue(ok["outcome"].startswith("ACCEPTED")); self.assertEqual(len(c.submitted), 1)

    def test_deadline_checked_at_submit(self):
        policy, res, ap, row, o, body = setup()
        with self.assertRaises(go.OrderError):
            go.submit_and_track(FakeClient(), o, body, require_intent_log=False, now=9_000)


class G3_FeeAndDisplay(unittest.TestCase):
    def test_fee_compared_to_row_reservation_not_current_policy(self):
        policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=1_000, allowlist=frozenset({RECV}))
        res = run_batch(CSV, policy, "b1")                       # 행은 fee 1,000 으로 예약됨
        policy.fee_units = 50_000_000                            # 이후 정책값 상향
        ap = approve_batch(policy, "b1", res["revision"], res["csv_sha256"], confirmed_by="t")["approval"]
        with self.assertRaises(go.OrderError) as cm:
            go.build_order(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                           approval_id=ap["approval_id"], payment_id=res["rows"][0]["payment_id"], row=res["rows"][0],
                           address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV)
        self.assertIn("fee_reservation_covers_max_fee", str(cm.exception))

    def test_human_summary_uses_integer_formatting(self):
        policy, res, ap, row, o, body = setup()
        o2 = dataclasses_replace_value(o, "123456789012345678901")
        self.assertIn("123456789012345.678901", o2.human_summary())


def dataclasses_replace_value(o, value):
    import copy
    o2 = copy.deepcopy(o); o2.message["value"] = value; return o2


class G4_NileAndAssetBinding(unittest.TestCase):
    def test_client_refuses_url_variants(self):
        for base, prefix in (("https://open.gasfree.io", "/tron"), ("https://open.gasfree.io/", "/nile"),
                             ("https://open-test.gasfree.io", "/tron"), ("https://open-test.gasfree.io/", "nile/")):
            if (base.rstrip("/"), "/" + prefix.strip("/")) == ("https://open-test.gasfree.io", "/nile"):
                GasFreeClient(base=base, prefix=prefix, keys=("k", "s"))          # 정규화 후 Nile 이면 허용
                continue
            with self.assertRaises(GasFreeError):
                GasFreeClient(base=base, prefix=prefix, keys=("k", "s"))

    def _fail(self, name, **kw):
        policy, res, ap, row, o, body = setup()
        args = dict(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                    approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                    address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV)
        args.update(kw)
        with self.assertRaises(go.OrderError) as cm:
            go.build_order(**args)
        self.assertIn(name, str(cm.exception))

    def test_wrong_decimal_refused(self):
        t = tokens_info(); t[0]["decimal"] = 18
        self._fail("token_decimal_matches_csv_units", tokens_info=t)

    def test_bad_provider_or_addresses_refused(self):
        self._fail("provider_address_tron_format", provider_address="")
        self._fail("provider_address_tron_format", provider_address="TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBg")  # 체크섬 오류
        ai = address_info(); ai["data"]["accountAddress"] = "0xabc"
        self._fail("user_eoa_tron_format", address_info=ai)

    def test_bad_nonce_refused(self):
        for bad in (-1, 1.5, "3", True):
            ai = address_info(nonce=bad)
            self._fail("nonce_is_uint", address_info=ai)


class G5_TraceIdentification(unittest.TestCase):
    def test_trace_id_mismatch_or_error_not_confirmed(self):
        policy, res, ap, row, o, body = setup()
        for tr in ({"http": 200, "body": {"code": 200, "data": {"id": "t-OTHER", "state": "SUCCEED", "txnHash": "h"}}},
                   {"http": 500, "body": {"code": 500, "data": {"id": "t-1", "state": "SUCCEED"}}},
                   {"http": 200, "body": {"_raw": "x"}}):
            with tempfile.TemporaryDirectory() as tmp:
                log = IntentLog(pathlib.Path(tmp) / "i.jsonl"); go.record_order_intents(log, o); log.append(o.payment_id, "SIGNED")
                c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t-1", "state": "WAITING"}}}, trace_resp=tr)
                out = go.submit_and_track(c, o, body, poll=1, sleep_fn=lambda s: None, intent_log=log, policy=policy, now=1_100)
                self.assertNotEqual(out["outcome"], "FINAL_SUCCEED", tr)
                self.assertEqual(log.state(o.payment_id), "ACCEPTED")

    def test_reconcile_rejects_negative_fee_and_inconsistent_total(self):
        policy, res, ap, row, o, body = setup()
        base = {"id": "t-1", "state": "SUCCEED", "txnState": "SOLIDITY", "txnHash": "h", "accountAddress": EOA,
                "targetAddress": RECV, "tokenAddress": USDT, "nonce": 3, "txnAmount": 1_500_000, "txnTotalFee": 10_000_000}
        self.assertEqual(go.reconcile(o, dict(base, txnTotalCost=11_500_000), expected_trace_id="t-1")["verdict"], "RECONCILED")
        self.assertNotEqual(go.reconcile(o, dict(base, txnTotalFee=-5), expected_trace_id="t-1")["verdict"], "RECONCILED")
        self.assertNotEqual(go.reconcile(o, dict(base, txnTotalCost=99), expected_trace_id="t-1")["verdict"], "RECONCILED")


if __name__ == "__main__":
    unittest.main()
