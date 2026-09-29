"""9/28 전면 재점검 R1~R5 회귀 검사. 네트워크·서명·송금 없음."""
import pathlib
import tempfile
import unittest

from safebatch.flow import run_batch, approve_batch, revise_batch
from safebatch.policy import PaymentPolicy
from safebatch import gasfree_order as go
from safebatch.intent_log import IntentLog
from safebatch.tests.test_gasfree_order import RECV, USDT, PROV, EOA, CSV, tokens_info, address_info, FakeClient

SIG = "ab" * 65


def base(confirmed_by="tester"):
    policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=10_000_000, allowlist=frozenset({RECV}))
    res = run_batch(CSV, policy, "b1")
    ap = approve_batch(policy, "b1", res["revision"], res["csv_sha256"], confirmed_by=confirmed_by)
    row = res["rows"][0]
    return policy, res, ap["approval"], row


def build(policy, res, ap, row, **kw):
    args = dict(policy=policy, batch_id="b1", revision=res["revision"], csv_sha256=res["csv_sha256"],
                approval_id=ap["approval_id"], payment_id=row["payment_id"], row=row,
                address_info=address_info(), tokens_info=tokens_info(), provider_address=PROV, now=1_000)
    args.update(kw)
    return go.build_order(**args)


class R1_HumanApprovalBoundary(unittest.TestCase):
    def test_mock_approval_is_rejected_by_default(self):
        policy, res, ap, row = base(confirmed_by=None)
        self.assertEqual(ap["mode"], "LOCAL_MOCK")
        with self.assertRaises(go.OrderError) as cm:
            build(policy, res, ap, row)
        self.assertIn("approval_is_human_confirmed", str(cm.exception))
        o = build(policy, res, ap, row, allow_mock_approval=True)        # 검사용 우회만 허용
        self.assertEqual(o.approval_mode, "LOCAL_MOCK")

    def test_human_approval_binds_displayed_content(self):
        policy, res, ap, row = base()
        self.assertEqual(ap["mode"], "HUMAN_CONFIRMED")
        self.assertEqual(ap["displayed_sha256"], policy.display_digest("b1", res["revision"]))
        o = build(policy, res, ap, row)
        self.assertEqual(o.approval_mode, "HUMAN_CONFIRMED")
        # 승인 뒤 수정 revision → 옛 승인·옛 화면 지문으로는 주문서 불가
        r2 = revise_batch(CSV.replace("1.5", "1.6"), policy, "b1", res["revision"], "req-x")
        self.assertEqual(r2["stage"], "AWAITING_HUMAN_APPROVAL")
        with self.assertRaises(go.OrderError):
            build(policy, res, ap, row)

    def test_approval_network_must_match(self):
        policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=10_000_000, allowlist=frozenset({RECV}))
        res = run_batch(CSV, policy, "b1")
        ap = approve_batch(policy, "b1", res["revision"], res["csv_sha256"], confirmed_by="t", network="tron")["approval"]
        with self.assertRaises(go.OrderError) as cm:
            build(policy, res, ap, res["rows"][0])
        self.assertIn("approval_network_matches", str(cm.exception))

    def test_mainnet_network_never_builds(self):
        policy, res, ap, row = base()
        with self.assertRaises(go.OrderError) as cm:
            build(policy, res, ap, row, network="tron")
        self.assertIn("network_is_nile", str(cm.exception))


class R2_SnapshotAndDomain(unittest.TestCase):
    def test_mutated_order_is_refused(self):
        policy, res, ap, row = base()
        o = build(policy, res, ap, row)
        o.message["value"] = "9999999"                                     # 생성 후 변조
        with self.assertRaises(go.OrderError) as cm:
            go.verify_signed(o, {"message": dict(o.message), "sig": SIG}, now=1_100)
        self.assertIn("snapshot", str(cm.exception))

    def test_wrong_domain_or_types_refused(self):
        policy, res, ap, row = base()
        o = build(policy, res, ap, row)
        bad_domain = dict(o.domain, chainId=728126428)                     # mainnet chainId
        with self.assertRaises(go.OrderError):
            go.verify_signed(o, {"message": dict(o.message), "sig": SIG, "domain": bad_domain}, now=1_100)
        with self.assertRaises(go.OrderError):
            go.verify_signed(o, {"message": dict(o.message), "sig": SIG, "types": {"X": []}}, now=1_100)
        ok = go.verify_signed(o, {"message": dict(o.message), "sig": SIG, "domain": dict(o.domain), "types": o.types}, now=1_100)
        self.assertEqual(ok["requestId"], o.request_id)


class R3_Reconcile(unittest.TestCase):
    def _o(self):
        policy, res, ap, row = base()
        return build(policy, res, ap, row)

    def _trace(self, **kw):
        t = {"id": "t-1", "state": "SUCCEED", "txnState": "SOLIDITY", "txnHash": "h", "accountAddress": EOA,
             "targetAddress": RECV, "tokenAddress": USDT, "nonce": 3, "txnAmount": 1_500_000, "txnTotalFee": 10_000_000}
        t.update(kw); return t

    def test_other_nonce_is_not_reconciled(self):
        o = self._o()
        self.assertEqual(go.reconcile(o, self._trace(), expected_trace_id="t-1")["verdict"], "RECONCILED")
        r = go.reconcile(o, self._trace(nonce=4), expected_trace_id="t-1")
        self.assertFalse(r["nonce_match"]); self.assertNotEqual(r["verdict"], "RECONCILED")

    def test_other_trace_id_or_user_not_reconciled(self):
        o = self._o()
        self.assertNotEqual(go.reconcile(o, self._trace(id="t-other"), expected_trace_id="t-1")["verdict"], "RECONCILED")
        self.assertNotEqual(go.reconcile(o, self._trace(accountAddress=RECV), expected_trace_id="t-1")["verdict"], "RECONCILED")
        self.assertFalse(go.reconcile(o, self._trace(), expected_trace_id="t-1")["onchain_verified_independently"])


class R4_NoDefaults(unittest.TestCase):
    def _fail(self, name, **kw):
        policy, res, ap, row = base()
        with self.assertRaises(go.OrderError) as cm:
            build(policy, res, ap, row, **kw)
        self.assertIn(name, str(cm.exception))

    def test_missing_transfer_fee_blocks(self):
        ai = address_info(); del ai["data"]["assets"][0]["transferFee"]
        self._fail("transferFee_present", address_info=ai)

    def test_missing_activate_fee_blocks_when_inactive(self):
        ai = address_info(active=False); del ai["data"]["assets"][0]["activateFee"]
        self._fail("activateFee_present", address_info=ai)

    def test_supported_must_be_explicit_true(self):
        t = tokens_info(); del t[0]["supported"]
        self._fail("token_supported_explicit", tokens_info=t)

    def test_negative_or_non_int_fee_blocks(self):
        ai = address_info(); ai["data"]["assets"][0]["transferFee"] = "10000000"
        self._fail("transferFee_is_nonneg_int", address_info=ai)
        ai = address_info(); ai["data"]["assets"][0]["frozen"] = -1
        self._fail("frozen_is_nonneg_int", address_info=ai)

    def test_missing_asset_entry_blocks(self):
        ai = address_info(); ai["data"]["assets"] = []
        self._fail("asset_entry_present", address_info=ai)

    def test_active_must_be_bool(self):
        ai = address_info(); ai["data"]["active"] = "true"
        self._fail("active_is_bool", address_info=ai)

    def test_policy_fee_reservation_must_cover_provider_max_fee(self):
        policy = PaymentPolicy(budget_units=1_000_000_000, fee_units=1_000, allowlist=frozenset({RECV}))
        res = run_batch(CSV, policy, "b1")
        ap = approve_batch(policy, "b1", res["revision"], res["csv_sha256"], confirmed_by="t")["approval"]
        with self.assertRaises(go.OrderError) as cm:
            build(policy, res, ap, res["rows"][0])
        self.assertIn("fee_reservation_covers_max_fee", str(cm.exception))


class R5_SubmitClassification(unittest.TestCase):
    def _signed(self):
        policy, res, ap, row = base()
        o = build(policy, res, ap, row)
        body = go.verify_signed(o, {"message": dict(o.message), "sig": SIG}, now=1_100)
        return o, body

    def test_intent_log_required_by_default(self):
        o, body = self._signed()
        c = FakeClient(submit_resp={"http": 200, "body": {"code": 200, "data": {"id": "t"}}})
        with self.assertRaises(go.OrderError):
            go.submit_and_track(c, o, body)
        self.assertEqual(len(c.submitted), 0)

    def test_5xx_and_non_json_are_unknown_not_rejected(self):
        o, body = self._signed()
        with tempfile.TemporaryDirectory() as tmp:
            for resp in ({"http": 502, "body": {"_raw": "<html>bad gateway"}},
                         {"http": 500, "body": {"code": 500, "reason": "InternalError"}},
                         {"http": 200, "body": {"_raw": "not json"}}):
                log = IntentLog(pathlib.Path(tmp) / f"i{resp['http']}{len(str(resp))}.jsonl")
                go.record_order_intents(log, o); log.append(o.payment_id, "SIGNED")
                c = FakeClient(submit_resp=resp)
                out = go.submit_and_track(c, o, body, intent_log=log, require_intent_log=False, now=1_100)
                self.assertEqual(out["outcome"], "UNKNOWN_SUBMIT_SERVER", resp)
                self.assertEqual(log.state(o.payment_id), "UNKNOWN")
                self.assertEqual(go.submit_and_track(c, o, body, intent_log=log, require_intent_log=False, now=1_100)["outcome"], "BLOCKED_BY_INTENT_LOG")
                self.assertEqual(len(c.submitted), 1)

    def test_explicit_400_with_reason_is_rejected(self):
        o, body = self._signed()
        with tempfile.TemporaryDirectory() as tmp:
            log = IntentLog(pathlib.Path(tmp) / "i.jsonl")
            go.record_order_intents(log, o); log.append(o.payment_id, "SIGNED")
            c = FakeClient(submit_resp={"http": 200, "body": {"code": 400, "reason": "NonceNotMatchException", "message": "x"}})
            out = go.submit_and_track(c, o, body, intent_log=log, require_intent_log=False, now=1_100)
            self.assertEqual(out["outcome"], "REJECTED_BY_PROVIDER"); self.assertEqual(log.state(o.payment_id), "REJECTED")

    def test_classifier_table(self):
        self.assertEqual(go.classify_submit_response({"http": 200, "body": {"code": 200}}), "ACCEPTED")
        self.assertEqual(go.classify_submit_response({"http": 400, "body": {"code": 400, "reason": "InvalidSignatureException"}}), "REJECTED")
        self.assertEqual(go.classify_submit_response({"http": 400, "body": {"code": 400, "reason": "X"}}), "UNKNOWN")  # 미문서 사유
        self.assertEqual(go.classify_submit_response({"http": 503, "body": {}}), "UNKNOWN")
        self.assertEqual(go.classify_submit_response({"http": None, "body": None}), "UNKNOWN")
        self.assertEqual(go.classify_submit_response({"http": 200, "body": {"code": 500, "reason": "Boom"}}), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
