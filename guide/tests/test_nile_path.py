"""일반 Nile TRC20 경로 검사(9/28 사장 선택). 네트워크 없음: 노드는 가짜, 서명은 검사용 키(실지갑 아님).
고정 자료 fixture_nile_unsigned_selftransfer.json 은 9/28 21:3x Nile 노드(triggersmartcontract)가 실제로 만든 미서명 거래(서명·방송 없음)."""
import copy
import dataclasses
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import demo_flows as D  # noqa: E402
import nile_executor as NX  # noqa: E402
from executor import ApprovalLedger  # noqa: E402
from order_store import OrderStore  # noqa: E402
from signer import FileSigner  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch.intent_log import IntentLog, SCOPE_NILE_TRC20  # noqa: E402
from safebatch.policy import PaymentPolicy  # noqa: E402
from tronpy.keys import PrivateKey  # noqa: E402

FIX = json.loads((pathlib.Path(__file__).parent / "fixture_nile_unsigned_selftransfer.json").read_text())["transaction"]
REAL_WALLET = "TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz"
OWNER = PrivateKey(bytes.fromhex("11" * 32)); SENDER = OWNER.public_key.to_base58check_address()
OTHER = PrivateKey(bytes.fromhex("22" * 32))
RECV = "TMDKznuDWaZwfZHcM61FVFstyYNmK6Njk1"          # 합성 검사 수취인(실제 승인 수취인 아님)
T0 = 1_790_600_000
BLOCK = {"blockID": "000000000440d2ce165795af00989df8" + "00" * 16, "number": 71357134}


def sign_like_wallet(unsigned: dict, key=OWNER, v_offset=0) -> dict:
    h = hashlib.sha256(bytes.fromhex(unsigned["raw_data_hex"])).digest()
    raw = key.sign_msg_hash(h); sig = bytes.fromhex(raw.hex() if hasattr(raw, "hex") else bytes(raw).hex())
    sig = sig[:64] + bytes([sig[64] + v_offset])
    return {"txID": unsigned["txID"], "raw_data": unsigned["raw_data"], "raw_data_hex": unsigned["raw_data_hex"], "signature": [sig.hex()], "visible": False}


class FakeNode(NT.NileNode):
    """가짜 Nile 노드. 실제 응답 형태(9/28 실측)를 흉내 낸다."""
    def __init__(self, *, trx_sun=1_000_000_000, usdt=1_000_000_000, energy=14650, price=100, broadcast_resp=None, broadcast_exc=None,
                 receipt=None, solid=True, fail_ret=False, bw_price=1000, free_net=600):
        super().__init__(runner=lambda *a: (_ for _ in ()).throw(AssertionError("no network")))
        self.trx_sun, self.usdt, self.energy, self.price, self.bw_price, self.free_net = trx_sun, usdt, energy, price, bw_price, free_net
        self.broadcast_resp = broadcast_resp or {"http": 200, "body": {"result": True, "txid": "x"}}
        self.broadcast_exc = broadcast_exc
        self.broadcasts = []; self.chain = []; self.receipt = receipt; self.solid = solid; self.fail_ret = fail_ret; self.calls = []   # chain = 노드가 접수한 것만

    def now_block(self, solid=False):
        return {"http": 200, "body": {"blockID": BLOCK["blockID"], "block_header": {"raw_data": {"number": BLOCK["number"]}}}}

    def account(self, addr):
        return {"http": 200, "body": {"address": addr, "balance": self.trx_sun}}

    def account_resource(self, addr):
        return {"http": 200, "body": {"freeNetLimit": self.free_net, "freeNetUsed": 0}}

    def chain_parameters(self):
        return {"http": 200, "body": {"chainParameter": [{"key": "getEnergyFee", "value": self.price}, {"key": "getTransactionFee", "value": self.bw_price}]}}

    def constant_call(self, owner, contract, selector, parameter_hex):
        if selector.startswith("balanceOf"):
            return {"http": 200, "body": {"result": {"result": True}, "constant_result": [format(self.usdt, "x").rjust(64, "0")], "energy_used": 935}}
        amt = int(parameter_hex[64:], 16)
        if amt > self.usdt:
            return {"http": 200, "body": {"result": {"result": True, "message": "REVERT opcode executed"}, "constant_result": [""], "energy_used": 1984,
                                          "transaction": {"ret": [{"ret": "FAILED"}]}}}
        return {"http": 200, "body": {"result": {"result": True}, "constant_result": ["0" * 64], "energy_used": self.energy, "transaction": {"ret": [{}]}}}

    def broadcast(self, signed_tx):
        self.broadcasts.append(signed_tx)
        if self.broadcast_exc:
            self.chain.append(signed_tx)                    # 전송 예외여도 노드에는 도달했다고 가정(응답 유실 사례)
            raise self.broadcast_exc
        if self.broadcast_resp["body"].get("result") is True or self.broadcast_resp["body"].get("code") == "DUP_TRANSACTION_ERROR":
            self.chain.append(signed_tx)
        return self.broadcast_resp

    def tx_by_id(self, txid):
        b = self.chain[-1] if self.chain else None
        if b is None or b["txID"] != txid:
            return {"http": 200, "body": {}}
        return {"http": 200, "body": {"txID": txid, "raw_data_hex": b["raw_data_hex"], "signature": b["signature"],
                                      "ret": [{"contractRet": "FAILED" if self.fail_ret else "SUCCESS"}]}}

    def tx_info(self, txid, solid=False):
        if not self.chain or self.chain[-1]["txID"] != txid:
            return {"http": 200, "body": {}}
        if solid and not self.solid:
            return {"http": 200, "body": {}}
        if self.receipt is not None:
            return {"http": 200, "body": dict(self.receipt, id=txid)}
        b = self.chain[-1]; d = NT.decode_raw(b["raw_data_hex"]); p = d["contracts"][0]["param"]
        data = p["data"]; to20 = data[8 + 24:8 + 64]; amt = data[8 + 64:]
        return {"http": 200, "body": {"id": txid, "blockNumber": 71357200, "fee": 1_465_000, "contractResult": ["0" * 20],
                                      "receipt": {"energy_usage_total": self.energy, "net_usage": 345, "energy_fee": 1_465_000, "result": "REVERT" if self.fail_ret else "SUCCESS"},
                                      "log": [] if self.fail_ret else [{"address": p["contract_address"][2:], "topics": [NT.TRANSFER_TOPIC, "0" * 24 + p["owner_address"][2:], "0" * 24 + to20], "data": amt}]}}


def rules(**kw):
    base = dict(goal="일반 Nile USDT 테스트 전송(연습)", guide_plan_id="fa4b723f945db52e", receiver=RECV, mode="max_within_budget", amount_units=1_000_000,
                budget_total_units=1_000_000, min_receive_units=500_000, deadline_ts=T0 + 900, allowlist=(RECV,), path="nile_trc20", trx_fee_cap_sun=5_000_000)
    base.update(kw)
    return D.UserRules(**base)


def ok_model(c, r): return {"choice_index": 0, "reason": "fits"}, {"ok": True, "model": "qwen3-32b"}


class Env:
    def __init__(self, tmp, node, sender=SENDER, key=OWNER, sign_fn=None):
        self.policy = PaymentPolicy(budget_units=100_000_000, fee_units=0, allowlist=frozenset({RECV}))
        self.intent_log = IntentLog(pathlib.Path(tmp) / "intents.jsonl")
        self.approvals = ApprovalLedger(pathlib.Path(tmp) / "approvals.jsonl")
        self.node, self.sender, self.key = node, sender, key
        self.clock = {"t": T0}; self.signed = 0; self.sign_fn = sign_fn

    def quote(self, r=None):
        r = r or rules()
        return NX.quote_from_node(self.node, self.sender, r.receiver, r.amount_units, now_fn=lambda: self.clock["t"])

    def sign(self, order):
        self.signed += 1
        if self.sign_fn:
            return self.sign_fn(order)
        return {"signed_tx": sign_like_wallet(order["unsigned_tx"], self.key)}

    def run(self, r=None, flow_id="f1", revision=1, confirm=lambda s: "owner"):
        r = r or rules()
        return NX.run_nile_flow(r, policy=self.policy, batch_id=f"b-{flow_id}", intent_log=self.intent_log, approvals=self.approvals, node=self.node,
                                sender=self.sender, quote_fn=lambda: self.quote(r), human_confirm=confirm, human_sign=self.sign, kiln_choose=ok_model,
                                flow_id=flow_id, revision=revision, now_fn=lambda: self.clock["t"], sleep_fn=lambda s: None, receipt_polls=2)


# ── 1. 바이트 수준: 독립 디코더 vs 실제 노드 산출물 ───────────────────────────
class RawBytes(unittest.TestCase):
    def test_decoder_matches_node_json_and_txid(self):
        d = NT.decode_raw(FIX["raw_data_hex"]); rd = FIX["raw_data"]
        self.assertEqual(NT.txid_of(FIX["raw_data_hex"]), FIX["txID"])
        self.assertEqual((d["ref_block_bytes"], d["ref_block_hash"], d["expiration"], d["timestamp"], d["fee_limit"]),
                         (rd["ref_block_bytes"], rd["ref_block_hash"], rd["expiration"], rd["timestamp"], rd["fee_limit"]))
        p = d["contracts"][0]["param"]; v = rd["contract"][0]["parameter"]["value"]
        self.assertEqual(p["owner_address"], NT.addr21_hex(v["owner_address"])); self.assertEqual(p["contract_address"], NT.addr21_hex(v["contract_address"]))
        self.assertEqual(p["data"], v["data"]); self.assertEqual(d["contracts"][0]["type"], 31); self.assertFalse(d["unexpected"])

    def test_encoder_reproduces_node_bytes(self):
        rd = copy.deepcopy(FIX["raw_data"]); v = rd["contract"][0]["parameter"]["value"]
        v["owner_address"] = NT.addr21_hex(v["owner_address"]); v["contract_address"] = NT.addr21_hex(v["contract_address"]); v["call_value"] = 0
        self.assertEqual(NT.encode_raw_data(rd), FIX["raw_data_hex"])

    def test_node_fixture_passes_spec_verification_and_tampering_fails(self):
        rd = FIX["raw_data"]
        spec = NT.TransferSpec(sender=REAL_WALLET, receiver=REAL_WALLET, token=NT.NILE_USDT, amount_units=1_000_000, fee_limit_sun=5_000_000, expire_at_ms=rd["expiration"])
        checks = NT.verify_unsigned({"txID": FIX["txID"], "raw_data_hex": FIX["raw_data_hex"], "raw_data": rd}, spec, now_ms=rd["timestamp"])
        self.assertTrue(all(c["pass"] for c in checks)); self.assertGreaterEqual(len(checks), 18)
        for bad in (dict(amount_units=2_000_000), dict(receiver=RECV), dict(fee_limit_sun=4_000_000), dict(sender=RECV), dict(expire_at_ms=rd["expiration"] + 1)):
            with self.assertRaises(NT.NileTxError, msg=str(bad)):
                NT.verify_unsigned({"txID": FIX["txID"], "raw_data_hex": FIX["raw_data_hex"]}, dataclasses.replace(spec, **bad), now_ms=rd["timestamp"])
        # 원본 바이트에 메모 필드(10) 를 끼워 넣은 변조 → 거부
        tampered = FIX["raw_data_hex"] + "520568656c6c6f"
        with self.assertRaises(NT.NileTxError):
            NT.verify_unsigned({"txID": NT.txid_of(tampered), "raw_data_hex": tampered}, spec, now_ms=rd["timestamp"])


# ── 2. 서명 검증 ────────────────────────────────────────────────────────────
class SignedTx(unittest.TestCase):
    def setUp(self):
        self.spec = NT.TransferSpec(sender=SENDER, receiver=RECV, token=NT.NILE_USDT, amount_units=1_000_000, fee_limit_sun=5_000_000, expire_at_ms=T0 * 1000 + 600_000)
        self.unsigned = NT.build_unsigned(FakeNode(), self.spec, now_ms=T0 * 1000)

    def test_owner_signature_accepted_both_v_forms(self):
        for off in (0, 27):
            body = NT.verify_signed(self.unsigned, sign_like_wallet(self.unsigned, OWNER, off), self.spec, now_ms=T0 * 1000 + 1000)
            self.assertEqual(body["signer"], SENDER); self.assertEqual(body["txID"], self.unsigned["txID"])

    def test_other_key_modified_bytes_expired_rejected(self):
        with self.assertRaises(NT.NileTxError):
            NT.verify_signed(self.unsigned, sign_like_wallet(self.unsigned, OTHER), self.spec, now_ms=T0 * 1000)
        s = sign_like_wallet(self.unsigned, OWNER); s["raw_data_hex"] = s["raw_data_hex"][:-2] + "03"
        with self.assertRaises(NT.NileTxError):
            NT.verify_signed(self.unsigned, s, self.spec, now_ms=T0 * 1000)
        with self.assertRaises(NT.NileTxError):
            NT.verify_signed(self.unsigned, sign_like_wallet(self.unsigned, OWNER), self.spec, now_ms=self.spec.expire_at_ms + 1)
        s2 = sign_like_wallet(self.unsigned, OWNER); s2["signature"] = s2["signature"] * 2
        with self.assertRaises(NT.NileTxError):
            NT.verify_signed(self.unsigned, s2, self.spec, now_ms=T0 * 1000)


class Broadcast(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(NT.classify_broadcast({"http": 200, "body": {"result": True, "txid": "a"}})[0], "ACCEPTED")
        self.assertEqual(NT.classify_broadcast({"http": 200, "body": {"result": False, "code": "SIGERROR", "message": "56616c6964617465207369676e6174757265206572726f72"}}), ("REJECTED", "SIGERROR: Validate signature error"))
        self.assertEqual(NT.classify_broadcast({"http": 200, "body": {"result": False, "code": "DUP_TRANSACTION_ERROR"}})[0], "UNKNOWN")
        self.assertEqual(NT.classify_broadcast({"http": 502, "body": "<html>"})[0], "UNKNOWN")


# ── 3. 실행 흐름(가짜 노드) ───────────────────────────────────────────────────
class Flow(unittest.TestCase):
    def test_confirmed_flow_records_txid_and_separates_usdt_from_trx(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeNode()); f = env.run()
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED", f)
            ex = f["execution"]["detail"]; od = f["order"]
            self.assertEqual(ex["tx_hash"], od["tx_id"]); self.assertEqual(len(env.node.broadcasts), 1)
            self.assertEqual(env.node.broadcasts[0]["raw_data_hex"], od["unsigned_tx"]["raw_data_hex"])
            p = f["chosen"]["plan"]; self.assertEqual(p["fee_units"], 0)
            self.assertEqual(p["trx_bandwidth_max_sun"], NX.BANDWIDTH_BYTES_TRANSFER * 1000)          # 과금 345B × 1000 sun (전송 281B 아님)
            self.assertEqual(p["trx_fee_limit_sun"], 5_000_000 - p["trx_bandwidth_max_sun"]); self.assertEqual(p["trx_total_max_sun"], 5_000_000)
            self.assertEqual(ex["receipt"]["amount_actual"], 1_000_000); self.assertTrue(ex["receipt"]["raw_bytes_identical"])
            cur = env.intent_log.current(ex["payment_id"])
            self.assertEqual((cur["state"], cur["tx_hash"], cur["scope"]), ("CONFIRMED", od["tx_id"], SCOPE_NILE_TRC20))
            states = [e["state"] for e in env.intent_log.entries() if e["payment_id"] == ex["payment_id"]]
            self.assertEqual(states, ["DRAFTED", "AWAITING_HUMAN", "SIGNED", "SUBMITTED", "ACCEPTED", "CONFIRMED"])
            # 재실행(새 승인 원장): 이미 CONFIRMED → 재방송 0
            env2 = Env(tmp, env.node); env2.policy = env.policy; env2.approvals = ApprovalLedger(pathlib.Path(tmp) / "a2.jsonl")
            f2 = env2.run(); self.assertEqual(f2["execution"]["state"], "CONFIRMED"); self.assertEqual(len(env.node.broadcasts), 1)

    def test_transport_exception_after_reservation_is_unknown_then_resolved_by_txid(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(broadcast_exc=RuntimeError("socket timeout")); env = Env(tmp, node); f = env.run()
            self.assertEqual(f["outcome"], "EXECUTION_UNKNOWN_NO_RESEND"); pid = f["execution"]["detail"]["payment_id"]
            self.assertEqual(env.intent_log.state(pid), "UNKNOWN"); self.assertEqual(env.intent_log.current(pid)["tx_hash"], f["order"]["tx_id"])
            env2 = Env(tmp, node); env2.policy = env.policy; env2.approvals = ApprovalLedger(pathlib.Path(tmp) / "a2.jsonl")
            f2 = env2.run(); self.assertEqual(f2["execution"]["state"], "UNKNOWN"); self.assertEqual(len(node.broadcasts), 1)   # 재방송 없음
            node.broadcast_exc = None                                        # 실제로는 노드에 도달해 있었다고 가정 → txID 조회로만 종결
            r = NX.resolve_by_txid(node, env.intent_log, f["order"]); self.assertEqual(r["state"], "CONFIRMED"); self.assertEqual(len(node.broadcasts), 1)

    def test_node_rejection_is_rejected_not_unknown_and_stays_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(broadcast_resp={"http": 200, "body": {"result": False, "code": "TAPOS_ERROR", "message": "5461706f73206572726f72"}})
            env = Env(tmp, node); f = env.run()
            self.assertEqual(f["outcome"], "EXECUTION_REJECTED"); self.assertIn("TAPOS_ERROR", f["execution"]["detail"]["reason"])
            pid = f["execution"]["detail"]["payment_id"]; self.assertEqual(env.intent_log.state(pid), "REJECTED")
            env2 = Env(tmp, node); env2.policy = env.policy; env2.approvals = ApprovalLedger(pathlib.Path(tmp) / "a2.jsonl")
            f2 = env2.run(); self.assertEqual(f2["outcome"], "NOT_SUBMITTED"); self.assertIn("rejected by the node", f2["execution"]["detail"]["reason"]); self.assertEqual(len(node.broadcasts), 1)

    def test_onchain_revert_is_failed_and_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(fail_ret=True); env = Env(tmp, node); f = env.run()
            self.assertEqual(f["execution"]["state"], "FAILED"); pid = f["execution"]["detail"]["payment_id"]
            self.assertEqual(env.intent_log.state(pid), "FAILED")
            env2 = Env(tmp, node); env2.policy = env.policy; env2.approvals = ApprovalLedger(pathlib.Path(tmp) / "a2.jsonl")
            f2 = env2.run(); self.assertEqual(f2["outcome"], "EXECUTION_FAILED_EARLIER_NO_RESEND"); self.assertEqual(f2["execution"]["detail"]["prior"]["tx_hash"], f["order"]["tx_id"])

    def test_not_solid_yet_is_accepted_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(solid=False); env = Env(tmp, node); f = env.run()
            self.assertEqual(f["outcome"], "EXECUTED_ACCEPTED_PENDING"); self.assertEqual(env.intent_log.state(f["execution"]["detail"]["payment_id"]), "ACCEPTED")

    def test_receipt_amount_mismatch_needs_human_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); env = Env(tmp, node)
            real_info = node.tx_info
            def bad_info(txid, solid=False):
                r = real_info(txid, solid)
                if r["body"].get("log"):
                    r["body"]["log"][0]["data"] = "0" * 63 + "1"
                return r
            node.tx_info = bad_info
            f = env.run(); self.assertEqual(f["execution"]["detail"]["outcome"], "ONCHAIN_MISMATCH_NEEDS_HUMAN"); self.assertNotEqual(env.intent_log.state(f["execution"]["detail"]["payment_id"]), "CONFIRMED")

    def test_wrong_signer_blocked_before_broadcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); env = Env(tmp, node, sign_fn=lambda od: {"signed_tx": sign_like_wallet(od["unsigned_tx"], OTHER)})
            f = env.run(); self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertIn("signer mismatch", f["execution"]["detail"]["reason"]); self.assertEqual(node.broadcasts, [])

    def test_wallet_altered_bytes_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode()
            def alter(od):
                u = dict(od["unsigned_tx"]); rd = copy.deepcopy(u["raw_data"]); rd["fee_limit"] = 50_000_000
                v = rd["contract"][0]["parameter"]["value"]; v["call_value"] = 0
                u["raw_data"] = rd; u["raw_data_hex"] = NT.encode_raw_data(rd); u["txID"] = NT.txid_of(u["raw_data_hex"])
                return {"signed_tx": sign_like_wallet(u, OWNER)}
            env = Env(tmp, node, sign_fn=alter); f = env.run()
            self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertIn("differs", f["execution"]["detail"]["reason"]); self.assertEqual(node.broadcasts, [])


# ── 4. USDT/TRX 분리·적응·거절 ───────────────────────────────────────────────
class Separation(unittest.TestCase):
    def test_trx_cap_below_estimate_declines_without_touching_usdt(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeNode()); r = rules(trx_fee_cap_sun=500_000)          # 0.5 TRX < 예상 1.9 TRX
            f = env.run(r); self.assertEqual(f["outcome"], "DECLINED"); self.assertIn("USDT amount is never reduced", f["reason"]); self.assertEqual(f["candidates"], [])
            self.assertEqual(env.signed, 0); self.assertEqual(env.node.broadcasts, [])

    def test_trx_balance_below_cap_declines(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeNode(trx_sun=1_000_000)); f = env.run(); self.assertEqual(f["outcome"], "DECLINED"); self.assertIn("TRX balance", f["reason"])

    def test_usdt_budget_reduction_adapts_amount_but_keeps_trx_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeNode()); r = rules(amount_units=1_000_000, budget_total_units=700_000)
            env.quote = lambda rr=None: NX.quote_from_node(env.node, env.sender, RECV, 700_000, now_fn=lambda: env.clock["t"])
            f = env.run(r); self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED", f)
            p = f["chosen"]["plan"]; self.assertEqual((p["value_units"], p["fee_units"], p["trx_total_max_sun"]), (700_000, 0, 5_000_000))
            self.assertEqual(f["execution"]["detail"]["receipt"]["amount_actual"], 700_000)

    def test_exact_amount_over_usdt_budget_declined_no_reduction(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeNode()); r = rules(mode="exact_amount", amount_units=1_000_000, budget_total_units=700_000)
            f = env.run(r); self.assertEqual(f["outcome"], "DECLINED"); self.assertIn("exact amount", f["reason"]); self.assertEqual(env.signed, 0)

    def test_gasfree_path_rejects_zero_fee_and_nile_path_rejects_positive_usdt_fee(self):
        self.assertEqual(D.candidate_plans(rules(path="gasfree", trx_fee_cap_sun=0), 0, T0), ())
        self.assertEqual(D.candidate_plans(rules(), 10, T0, {"est_energy_sun": 1, "bandwidth_max_sun": 1, "balance_sun": 10}), ())
        with self.assertRaises(D.RulesError):
            rules(trx_fee_cap_sun=0)

    def test_usdt_balance_below_amount_no_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, FakeNode(usdt=100)); f = env.run(); self.assertEqual(f["outcome"], "DECLINED"); self.assertIn("USDT balance", f["reason"])


# ── 5. 서명 저장소·파일 서명 경로(sign.html ↔ 서버) ─────────────────────────────
class StoreAndSigner(unittest.TestCase):
    def _order(self):
        q = NX.NileQuote(trx_sun=10**9, usdt_units=10**9, energy_est=14650, energy_price_sun=100, bandwidth_price_sun=1000, would_succeed=True, free_net_left=600, quoted_at=T0, sender=SENDER, receiver=RECV, amount_units=1_000_000, raw_len_bytes=211)
        plan = D.candidate_plans(rules(), 0, T0, q.trx_quote())[0]
        spec = NT.TransferSpec(sender=SENDER, receiver=RECV, token=NT.NILE_USDT, amount_units=1_000_000, fee_limit_sun=plan.trx_fee_limit_sun, expire_at_ms=T0 * 1000 + 600_000)
        unsigned = NT.build_unsigned(FakeNode(), spec, now_ms=T0 * 1000)
        return NX.make_nile_order(plan=plan, rules=rules(), sender=SENDER, unsigned=unsigned, quote=q, approval_id="ap1", batch_id="b", revision=1,
                                  csv_sha256="c" * 64, payment_id="b:r1:2", row_no=2, memo="m", expire_at_ms=spec.expire_at_ms)

    def test_store_verifies_signed_tx_and_signer_consumes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = self._order(); store.put_pending(od)
            ok, why = store.store_signature(od["payment_id"], od["snapshot_sha256"], {"signed_tx": sign_like_wallet(od["unsigned_tx"], OTHER)}, now=T0 + 1)
            self.assertFalse(ok); self.assertIn("signer mismatch", why)
            # 실행기(FileSigner) 가 대기하는 동안 사람이 /sign 에서 서명 → 서버 검증 → 소비 1회
            def human_signs_meanwhile(_s):
                ok2, who = store.store_signature(od["payment_id"], od["snapshot_sha256"], {"signed_tx": sign_like_wallet(od["unsigned_tx"], OWNER)}, now=T0 + 1)
                self.assertTrue(ok2, who); self.assertEqual(who, SENDER)
            fs = FileSigner(store, timeout_s=5, sleep_fn=human_signs_meanwhile, now_fn=lambda: T0 + 2)
            got = fs.sign(od); self.assertEqual(got["signed_tx"]["txID"], od["tx_id"])
            self.assertIn("consumed", fs.sign(od)["refused"])

    def test_signer_times_out_at_expiry_and_cancels(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = self._order()
            clock = {"t": T0}
            def tick(s): clock["t"] += 1000
            fs = FileSigner(store, timeout_s=10_000, sleep_fn=tick, now_fn=lambda: clock["t"])
            r = fs.sign(od); self.assertIn("deadline/timeout", r["refused"]); self.assertEqual(store.get(od["payment_id"])["state"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()
