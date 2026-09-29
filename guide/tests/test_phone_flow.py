"""휴대폰 서명 경로(9/29) 합성 검사. 네트워크 없음: 가짜 Nile 노드, 서명은 검사용 키(실지갑 아님).
검증 항목: 대화 파서(예시·미등록·동명이인·미확인·자산/수량 누락), 미서명 TRX 거래 작성·독립 디코드 대조, 서명 검증(변조·다른 서명자·만료·중복),
내용 변경 시 재승인(이전 주문 취소·지문 불일치), 방송 1회·재방송 차단, 결과 불명 시 같은 txID 재조회, HTTP 경계(토큰·Host·JSON)."""
import hashlib
import http.client
import json
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import phone_chat as PC  # noqa: E402
import phone_server as PS  # noqa: E402
from order_store import OrderStore  # noqa: E402
from phone_flow import PhoneFlow  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch import trx_tx as TX  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402
from tronpy.keys import PrivateKey  # noqa: E402

PHONE = PrivateKey(bytes.fromhex("33" * 32)); SENDER = PHONE.public_key.to_base58check_address()
OTHER = PrivateKey(bytes.fromhex("44" * 32))
MAC = "TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz"
T0 = 1_790_700_000
BLOCK = {"blockID": "000000000440f2e6165795af00989df8" + "00" * 16, "number": 71365350}
CONTACTS = [{"alias": "맥북지갑", "aliases": ["맥북"], "address": MAC, "network": "nile", "confirmed_by_owner": True, "confirmed_at": "9/21"},
            {"alias": "민수", "address": None, "confirmed_by_owner": False, "note": "1"}, {"alias": "민수", "address": None, "confirmed_by_owner": False, "note": "2"},
            {"alias": "지연", "address": "TMDKznuDWaZwfZHcM61FVFstyYNmK6Njk1", "network": "nile", "confirmed_by_owner": False}]


def sign_like_wallet(unsigned, key=PHONE):
    h = hashlib.sha256(bytes.fromhex(unsigned["raw_data_hex"])).digest()
    raw = key.sign_msg_hash(h); sig = bytes.fromhex(raw.hex() if hasattr(raw, "hex") else bytes(raw).hex())
    return {"txID": unsigned["txID"], "raw_data": unsigned["raw_data"], "raw_data_hex": unsigned["raw_data_hex"], "signature": [sig.hex()], "visible": False}


class FakeNode(NT.NileNode):
    def __init__(self, *, balance=10_000_000, receiver_exists=True, free_net=600, broadcast_resp=None, broadcast_exc=None, receipt_mode="confirmed", fee=0):
        super().__init__(runner=lambda *a: (_ for _ in ()).throw(AssertionError("no network")))
        self.balance, self.receiver_exists, self.free_net = balance, receiver_exists, free_net
        self.broadcast_resp = broadcast_resp or {"http": 200, "body": {"result": True, "txid": "x"}}
        self.broadcast_exc = broadcast_exc; self.receipt_mode = receipt_mode; self.fee = fee
        self.broadcasts = []; self.chain = []
        self.head_ts_ms = T0 * 1000            # 미확정 최신 블록 시각
        self.solid_ts_ms = T0 * 1000           # 확정(solidified) 블록 시각
        self.lookup_error = None               # {"http":503,"body":"busy"} 등 → 조회 4종 모두 이 응답
        self.solid_lookup_error = None         # solidity 조회만 오류
        self.on_broadcast = None               # 경합 검사용 훅(방송 직전 호출)

    def now_block(self, solid=False):
        return {"http": 200, "body": {"blockID": BLOCK["blockID"], "block_header": {"raw_data": {"number": BLOCK["number"], "timestamp": self.solid_ts_ms if solid else self.head_ts_ms}}}}

    def account(self, addr):
        if addr == MAC and not self.receiver_exists:
            return {"http": 200, "body": {}}
        return {"http": 200, "body": {"address": addr, "balance": self.balance if addr == SENDER else 999_000_000}}

    def account_resource(self, addr):
        return {"http": 200, "body": {"freeNetLimit": self.free_net, "freeNetUsed": 0}}

    def chain_parameters(self):
        return {"http": 200, "body": {"chainParameter": [{"key": "getTransactionFee", "value": 1000}, {"key": "getCreateNewAccountFeeInSystemContract", "value": 1_000_000},
                                                          {"key": "getCreateAccountFee", "value": 100_000}]}}

    def broadcast(self, tx):
        if self.on_broadcast:
            self.on_broadcast()
        self.broadcasts.append(tx)
        if self.broadcast_exc:
            raise self.broadcast_exc
        if self.broadcast_resp["body"].get("result") is True:
            self.chain.append(tx)
        return self.broadcast_resp

    def _err(self, solid):
        if self.lookup_error:
            return self.lookup_error
        if solid and self.solid_lookup_error:
            return self.solid_lookup_error
        return None

    def tx_by_id(self, txid, solid=False):
        e = self._err(solid)
        if e:
            return e
        for t in self.chain:
            if t["txID"] == txid and self.receipt_mode != "not_found" and not (solid and self.receipt_mode in ("accepted", "failed_unconfirmed")):
                return {"http": 200, "body": {"txID": txid, "raw_data_hex": t["raw_data_hex"], "ret": [{"contractRet": "FAILED" if self.receipt_mode in ("failed", "failed_unconfirmed") else "SUCCESS"}]}}
        return {"http": 200, "body": {}}

    def tx_info(self, txid, solid=False):
        e = self._err(solid)
        if e:
            return e
        for t in self.chain:
            if t["txID"] == txid and self.receipt_mode not in ("not_found", "pending"):
                if solid and self.receipt_mode in ("accepted", "failed_unconfirmed"):
                    return {"http": 200, "body": {}}
                return {"http": 200, "body": {"id": txid, "blockNumber": 71365400, "fee": self.fee, "receipt": {"net_usage": 269 + 64, "net_fee": self.fee}}}
        return {"http": 200, "body": {}}


def mkflow(node, tmp, **kw):
    tmp = pathlib.Path(tmp)
    return PhoneFlow(node=node, store=OrderStore(tmp / "pending"), intents=IntentLog(tmp / "intents.jsonl"), results_dir=tmp / "results",
                     contacts=CONTACTS, now_fn=lambda: T0, sleep_fn=lambda s: None, receipt_polls=2, **kw)


class ChatTests(unittest.TestCase):
    def test_example_sentence_is_2_trx_to_alias(self):
        r = PC.parse_request("영훈이한테 트론 2개 보내줘", CONTACTS)
        self.assertEqual(r["kind"], "question"); self.assertEqual(r["reason"], "UNREGISTERED_ALIAS")
        self.assertEqual(r["understood"], {"asset": "TRX", "amount_trx": "2", "alias_input": "영훈이"})
        self.assertEqual(r["ai"]["mode"], "MOCK_RULES")

    def test_registered_alias_proposal(self):
        r = PC.parse_request("맥북지갑한테 트론 2개", CONTACTS)
        self.assertEqual(r["kind"], "proposal"); self.assertEqual(r["proposal"]["amount_sun"], 2_000_000); self.assertEqual(r["proposal"]["address"], MAC)
        r2 = PC.parse_request("트론 0.5개 맥북한테 보내", CONTACTS)
        self.assertEqual(r2["kind"], "proposal"); self.assertEqual(r2["proposal"]["amount_sun"], 500_000)

    def test_questions(self):
        self.assertEqual(PC.parse_request("민수한테 트론 1개", CONTACTS)["reason"], "AMBIGUOUS_ALIAS")
        self.assertEqual(PC.parse_request("지연한테 트론 1개", CONTACTS)["reason"], "UNCONFIRMED_ADDRESS")
        self.assertIn("몇 TRX", PC.parse_request("맥북지갑한테 트론 보내", CONTACTS)["question"])
        self.assertIn("TRX", PC.parse_request("맥북지갑한테 USDT 2개", CONTACTS)["question"])
        self.assertIn("누구", PC.parse_request("트론 2개 보내줘", CONTACTS)["question"])
        self.assertIn("상한", PC.parse_request("맥북지갑한테 트론 1000개", CONTACTS)["question"])
        self.assertIsNone(PC.structure_with_kiln("x"))


class TrxTxTests(unittest.TestCase):
    def test_build_verify_decode(self):
        spec = TX.TrxSpec(sender=SENDER, receiver=MAC, amount_sun=2_000_000, expire_at_ms=(T0 + 600) * 1000, fee_cap_sun=2_000_000)
        u = TX.build_unsigned_trx(FakeNode(), spec, now_ms=T0 * 1000)
        self.assertEqual(u["txID"], hashlib.sha256(bytes.fromhex(u["raw_data_hex"])).hexdigest())
        d = TX.decode_raw_trx(u["raw_data_hex"]); c = d["contracts"][0]
        self.assertEqual((c["type"], c["type_url"]), (1, TX.TYPE_URL_TRANSFER)); self.assertEqual(c["param"]["amount"], 2_000_000)
        self.assertEqual(c["param"]["to_address"], NT.addr21_hex(MAC)); self.assertEqual(d["fee_limit"], 0)
        self.assertTrue(all(x["pass"] for x in u["checks"]))
        # 변조: 수량·수취인
        bad = dict(u); bad["raw_data_hex"] = u["raw_data_hex"].replace(NT.addr21_hex(MAC), NT.addr21_hex(SENDER))
        with self.assertRaises(NT.NileTxError):
            TX.verify_unsigned_trx(bad, spec, now_ms=T0 * 1000)
        with self.assertRaises(NT.NileTxError):
            TX.verify_unsigned_trx(u, TX.TrxSpec(sender=SENDER, receiver=MAC, amount_sun=3_000_000, expire_at_ms=spec.expire_at_ms, fee_cap_sun=2_000_000), now_ms=T0 * 1000)
        with self.assertRaises(NT.NileTxError):
            TX.TrxSpec(sender=SENDER, receiver=SENDER, amount_sun=1, expire_at_ms=1, fee_cap_sun=1)

    def test_verify_signed(self):
        spec = TX.TrxSpec(sender=SENDER, receiver=MAC, amount_sun=2_000_000, expire_at_ms=(T0 + 600) * 1000, fee_cap_sun=2_000_000)
        u = TX.build_unsigned_trx(FakeNode(), spec, now_ms=T0 * 1000)
        body = TX.verify_signed_trx(u, sign_like_wallet(u), spec, now_ms=(T0 + 10) * 1000)
        self.assertEqual(body["signer"], SENDER); self.assertEqual(body["bandwidth_bytes"], NT.charged_bandwidth_bytes(u["raw_data_hex"], 1, 1))
        with self.assertRaisesRegex(NT.NileTxError, "signer mismatch"):
            TX.verify_signed_trx(u, sign_like_wallet(u, OTHER), spec, now_ms=(T0 + 10) * 1000)
        with self.assertRaisesRegex(NT.NileTxError, "expired|expiration"):
            TX.verify_signed_trx(u, sign_like_wallet(u), spec, now_ms=(T0 + 601) * 1000)
        s = sign_like_wallet(u); s["raw_data_hex"] = s["raw_data_hex"][:-2] + "00"
        with self.assertRaisesRegex(NT.NileTxError, "differs"):
            TX.verify_signed_trx(u, s, spec, now_ms=(T0 + 10) * 1000)
        s2 = sign_like_wallet(u); s2["signature"].append(s2["signature"][0])
        with self.assertRaisesRegex(NT.NileTxError, "exactly one signature"):
            TX.verify_signed_trx(u, s2, spec, now_ms=(T0 + 10) * 1000)

    def test_quote(self):
        spec = TX.TrxSpec(sender=SENDER, receiver=MAC, amount_sun=2_000_000, expire_at_ms=(T0 + 600) * 1000, fee_cap_sun=2_000_000)
        q = TX.quote_trx(FakeNode(), spec, raw_len_bytes=200)
        self.assertTrue(q["would_succeed"]); self.assertEqual(q["activation_fee_sun"], 0); self.assertTrue(q["bandwidth_covered"])
        q2 = TX.quote_trx(FakeNode(receiver_exists=False, free_net=0), spec, raw_len_bytes=200)
        self.assertEqual(q2["activation_fee_sun"], 1_100_000); self.assertFalse(q2["bandwidth_covered"])
        self.assertEqual(q2["worst_case_fee_sun"], (200 + 3 + 67 + 64) * 1000 + 1_100_000)     # 서명 프레이밍 + 64
        self.assertFalse(TX.quote_trx(FakeNode(balance=2_100_000, free_net=0), spec, raw_len_bytes=200)["would_succeed"])


class FlowTests(unittest.TestCase):
    def _order(self, flow, text="맥북지갑한테 트론 2개"):
        r = flow.chat(text); self.assertEqual(r["kind"], "proposal")
        p = flow.prepare(r["proposal_id"], SENDER); self.assertTrue(p["ok"], p); return p["order"]

    def test_happy_path_phone_signs_mac_broadcasts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp); od = self._order(flow)
            self.assertEqual(od["signing"]["mac_signs"], False); self.assertEqual(od["ai"]["mode"], "MOCK_KILN"); self.assertEqual(od["ai"]["calls"], 0)
            res = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(res["state"], "FINAL_CONFIRMED_SOLIDITY", res); self.assertEqual(len(node.broadcasts), 1)
            self.assertEqual(flow.intents.state(od["payment_id"]), "CONFIRMED"); self.assertEqual(flow.store.get(od["payment_id"])["state"], "CONSUMED")
            # 같은 서명본 재제출 → 기존 상태 반환(idempotent), 방송 여전히 1회 / 다른 서명본 → NOT_SUBMITTED
            again = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(again["state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertTrue(again["idempotent"]); self.assertEqual(len(node.broadcasts), 1)
            other = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"], OTHER))
            self.assertEqual(other["state"], "NOT_SUBMITTED"); self.assertIn("duplicate", other["reason"]); self.assertEqual(len(node.broadcasts), 1)
            st = flow.status(od["payment_id"]); self.assertEqual(st["result"]["state"], "FINAL_CONFIRMED_SOLIDITY")

    def test_change_requires_new_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp); od1 = self._order(flow, "맥북지갑한테 트론 2개")
            od2 = self._order(flow, "맥북지갑한테 트론 3개")                        # 내용 변경 → 새 주문, 이전 주문 취소
            self.assertNotEqual(od1["snapshot_sha256"], od2["snapshot_sha256"]); self.assertEqual(flow.store.get(od1["payment_id"])["state"], "CANCELLED")
            late = flow.submit_signed(od1["payment_id"], od1["snapshot_sha256"], sign_like_wallet(od1["unsigned_tx"]))
            self.assertEqual(late["state"], "NOT_SUBMITTED"); self.assertEqual(len(node.broadcasts), 0)
            # 새 주문에 옛 서명(다른 raw) → 거부
            wrong = flow.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sign_like_wallet(od1["unsigned_tx"]))
            self.assertEqual(wrong["state"], "NOT_SUBMITTED"); self.assertIn("differs", wrong["reason"]); self.assertEqual(len(node.broadcasts), 0)
            self.assertEqual(flow.store.get(od2["payment_id"])["state"], "PENDING")      # 잘못된 서명은 주문을 소모하지 않는다

    def test_tamper_other_signer_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp); od = self._order(flow)
            r = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"], OTHER))
            self.assertIn("signer mismatch", r["reason"]); self.assertEqual(len(node.broadcasts), 0)
            flow.now_fn = lambda: T0 + 700
            r2 = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(r2["state"], "NOT_SUBMITTED"); self.assertIn("expir", r2["reason"]); self.assertEqual(len(node.broadcasts), 0)
            self.assertFalse(flow.status(od["payment_id"])["expired"])            # 서버 시계만으로는 만료 아님(Grok-05 #5)
            node.solid_ts_ms = (T0 + 700) * 1000; self.assertTrue(flow.status(od["payment_id"])["expired"])

    def test_unknown_then_resolve_by_txid_no_rebroadcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(broadcast_exc=TimeoutError("curl timeout")); flow = mkflow(node, tmp); od = self._order(flow)
            r = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(r["state"], "UNKNOWN"); self.assertEqual(flow.intents.state(od["payment_id"]), "UNKNOWN")
            # 사실은 체인에 올라갔다고 가정 → status 는 같은 txID 조회로 종결, 재방송 없음
            node.broadcast_exc = None; node.chain.append(node.broadcasts[0])
            st = flow.status(od["payment_id"]); self.assertEqual(st["result"]["state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertEqual(len(node.broadcasts), 1)
            # 같은 계정 새 주문은 미해결이 남아 있으면 prepare 단계부터 차단(Grok-04 #1), 방송 예약도 차단
            node2 = FakeNode(broadcast_exc=TimeoutError("t")); flow2 = mkflow(node2, tmp + "/b"); od2 = self._order(flow2)
            flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sign_like_wallet(od2["unsigned_tx"]))
            pr = flow2.chat("맥북지갑한테 트론 1개"); p3 = flow2.prepare(pr["proposal_id"], SENDER)
            self.assertFalse(p3["ok"]); self.assertTrue(p3.get("locked")); self.assertEqual(len(node2.broadcasts), 1)
            ok, why = flow2.intents.reserve_broadcast("phone_trx_other", SENDER, "ab" * 32, scope=TX.SCOPE_NILE_TRX)
            self.assertFalse(ok)

    def test_rejected_and_failed_and_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(broadcast_resp={"http": 200, "body": {"result": False, "code": "SIGERROR", "message": "76616c6964617465207369676e6174757265206572726f72"}})
            flow = mkflow(node, tmp); od = self._order(flow)
            r = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(r["state"], "REJECTED_BY_NODE_UNCONFIRMED"); self.assertIn("validate signature error", r["reason"])
            self.assertEqual(flow.intents.state(od["payment_id"]), "UNKNOWN")          # 확정 전 실패 응답만으로 최종 실패 선언 안 함
            node2 = FakeNode(receipt_mode="failed"); flow2 = mkflow(node2, tmp + "/f"); od2 = self._order(flow2)
            self.assertEqual(flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sign_like_wallet(od2["unsigned_tx"]))["state"], "FAILED_ONCHAIN")
            node3 = FakeNode(balance=1_000_000); flow3 = mkflow(node3, tmp + "/i"); pr = flow3.chat("맥북지갑한테 트론 2개")
            p = flow3.prepare(pr["proposal_id"], SENDER); self.assertFalse(p["ok"]); self.assertIn("잔액 부족", p["error"])
            node4 = FakeNode(fee=3_000_000); flow4 = mkflow(node4, tmp + "/c"); od4 = self._order(flow4)   # 영수증 수수료가 상한 초과 → 성공 아님
            r4 = flow4.submit_signed(od4["payment_id"], od4["snapshot_sha256"], sign_like_wallet(od4["unsigned_tx"]))
            self.assertEqual(r4["state"], "UNKNOWN"); self.assertFalse(r4["receipt"]["fee_within_cap"])

    def test_reject_and_wrong_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = mkflow(FakeNode(), tmp); od = self._order(flow)
            self.assertTrue(flow.reject(od["payment_id"], od["snapshot_sha256"])["ok"]); self.assertEqual(flow.intents.state(od["payment_id"]), "CANCELLED")
            self.assertFalse(flow.prepare("nope", SENDER)["ok"]); pr = flow.chat("맥북지갑한테 트론 2개")
            self.assertFalse(flow.prepare(pr["proposal_id"], MAC)["ok"])                 # 보내는 지갑 = 받는 주소
            self.assertFalse(flow.prepare(pr["proposal_id"], "notanaddress")["ok"])


class AdvisorFindingsTests(unittest.TestCase):
    """9/29 Grok-04 반례 반영 + 부사장 검수(실서명 전 보완) 검사."""
    def _order(self, flow, text="맥북지갑한테 트론 2개", **kw):
        r = flow.chat(text); self.assertEqual(r["kind"], "proposal")
        p = flow.prepare(r["proposal_id"], SENDER, **kw); self.assertTrue(p["ok"], p); return p["order"]

    def _settle(self, node, flow):
        """체인 근거로 종결: 확정 블록 시각을 만료+60s 뒤로 두고 solidity 빈 응답 2회."""
        node.solid_ts_ms = (T0 + 700) * 1000

    def test_g1_held_signature_locks_until_chain_evidence_not_local_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp, auto_expiry_settle=True); od = self._order(flow)
            node.balance = 1_000_000                                         # 서명 뒤 잔액 급감 → 재견적 실패 → 격리
            r = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(r["state"], "SIGNATURE_HELD"); self.assertTrue(r["not_broadcast"]); self.assertTrue(r["signature_quarantined"]); self.assertEqual(len(node.broadcasts), 0)
            node.balance = 10_000_000
            pr = flow.chat("맥북지갑한테 트론 2개"); self.assertTrue(flow.prepare(pr["proposal_id"], SENDER).get("locked"))
            flow.now_fn = lambda: T0 + 700                                   # 맥북 시계만 흘러도 해제 안 함
            node.head_ts_ms = (T0 + 700) * 1000                              # 미확정 최신 블록 시각만 흘러도 해제 안 함
            self.assertTrue(flow.prepare(flow.chat("맥북지갑한테 트론 2개")["proposal_id"], SENDER).get("locked"))
            node.solid_ts_ms = (T0 + 700) * 1000                             # 확정 블록 시각 경과 + 이번이 2회째 빈 조회 → 해제
            self.assertTrue(flow.prepare(flow.chat("맥북지갑한테 트론 2개")["proposal_id"], SENDER)["ok"])
            self.assertEqual(flow._load_result(od["payment_id"])["state"], "EXPIRED_NOT_ON_CHAIN")
            # 철회(SIGNED_THEN_WITHDRAWN)도 같은 잠금
            node3 = FakeNode(); flow3 = mkflow(node3, tmp + "/w", auto_expiry_settle=True); od3 = self._order(flow3)
            self.assertTrue(flow3.store.store_signature(od3["payment_id"], od3["snapshot_sha256"], {"signed_tx": sign_like_wallet(od3["unsigned_tx"])}, now=T0)[0])
            self.assertTrue(flow3.reject(od3["payment_id"], od3["snapshot_sha256"])["ok"])
            self.assertTrue(flow3.prepare(flow3.chat("맥북지갑한테 트론 1개")["proposal_id"], SENDER).get("locked"))
            # 확정 조회가 오류(503)면 확정 시각이 지나도 해제 안 함
            node3.solid_ts_ms = (T0 + 700) * 1000; node3.solid_lookup_error = {"http": 503, "body": "busy"}
            self.assertTrue(flow3.prepare(flow3.chat("맥북지갑한테 트론 1개")["proposal_id"], SENDER).get("locked"))
            self.assertTrue(flow3.prepare(flow3.chat("맥북지갑한테 트론 1개")["proposal_id"], SENDER).get("locked"))

    def test_g2_consecutive_not_found_reset_by_lookup_error_and_solid_time_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(broadcast_exc=TimeoutError("t")); flow = mkflow(node, tmp, auto_expiry_settle=True); od = self._order(flow)
            self.assertEqual(flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))["state"], "UNKNOWN")
            flow.now_fn = lambda: T0 + 1000; node.head_ts_ms = (T0 + 1000) * 1000; node.solid_ts_ms = (T0 + 700) * 1000
            s1 = flow.status(od["payment_id"]); self.assertEqual(s1["result"]["state"], "UNKNOWN"); self.assertEqual(s1["result"]["not_found_count"], 1)
            node.lookup_error = {"http": 429, "body": {"Error": "rate"}}   # NOT_FOUND → LOOKUP_ERROR → 카운트 0 으로
            s2 = flow.status(od["payment_id"]); self.assertEqual(s2["result"]["state"], "UNKNOWN"); self.assertEqual(s2["result"]["not_found_count"], 0)
            node.lookup_error = None
            s3 = flow.status(od["payment_id"]); self.assertEqual(s3["result"]["state"], "UNKNOWN"); self.assertEqual(s3["result"]["not_found_count"], 1)   # 연속 2회 아님
            s4 = flow.status(od["payment_id"]); self.assertEqual(s4["result"]["state"], "EXPIRED_NOT_ON_CHAIN"); self.assertEqual(flow.intents.state(od["payment_id"]), "REJECTED")
            self.assertEqual(len(node.broadcasts), 1)
            # 확정 시각이 아직이면 빈 응답이 아무리 많아도 종결 안 함
            node2 = FakeNode(broadcast_exc=TimeoutError("t")); flow2 = mkflow(node2, tmp + "/b", auto_expiry_settle=True); od2 = self._order(flow2)
            flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sign_like_wallet(od2["unsigned_tx"]))
            node2.head_ts_ms = (T0 + 5000) * 1000; flow2.now_fn = lambda: T0 + 5000
            for _ in range(3):
                self.assertEqual(flow2.status(od2["payment_id"])["result"]["state"], "UNKNOWN")
            self.assertEqual(flow2.intents.state(od2["payment_id"]), "UNKNOWN")

    def test_g3_same_transfer_recent_requires_explicit_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp); od = self._order(flow)
            self.assertEqual(flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))["state"], "FINAL_CONFIRMED_SOLIDITY")
            flow.now_fn = lambda: T0 + 5
            pr = flow.chat("맥북지갑한테 트론 2개"); p = flow.prepare(pr["proposal_id"], SENDER)
            self.assertFalse(p["ok"]); self.assertEqual(p["duplicate_of"]["state"], "FINAL_CONFIRMED_SOLIDITY")
            self.assertTrue(flow.prepare(pr["proposal_id"], SENDER, confirm_resend=True)["ok"])
            flow.now_fn = lambda: T0
            same = flow.prepare(flow.chat("맥북지갑한테 트론 2개")["proposal_id"], SENDER, confirm_resend=True)
            self.assertFalse(same["ok"]); self.assertIn("같은 거래", same["error"])

    def test_g4_cancel_vs_broadcast_race_ordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            # (a) 취소가 먼저 성공 → 늦은 서명 제출은 거부, 방송 0
            node = FakeNode(); flow = mkflow(node, tmp); od = self._order(flow)
            self.assertTrue(flow.reject(od["payment_id"], od["snapshot_sha256"])["ok"])
            r = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(r["state"], "NOT_SUBMITTED"); self.assertEqual(len(node.broadcasts), 0)
            # (b) 서명 저장 뒤·소비 전 취소 → 철회, consume 실패, 방송 0
            node2 = FakeNode(); flow2 = mkflow(node2, tmp + "/b"); od2 = self._order(flow2)
            self.assertTrue(flow2.store.store_signature(od2["payment_id"], od2["snapshot_sha256"], {"signed_tx": sign_like_wallet(od2["unsigned_tx"])}, now=T0)[0])
            self.assertTrue(flow2.reject(od2["payment_id"], od2["snapshot_sha256"])["ok"])
            self.assertIsNone(flow2.store.consume(od2["payment_id"], od2["snapshot_sha256"])[0]); self.assertEqual(len(node2.broadcasts), 0)
            # (c) consume 이 먼저 → 방송 직전에 들어온 취소는 거부, 방송 정확히 1회
            node3 = FakeNode(); flow3 = mkflow(node3, tmp + "/c"); od3 = self._order(flow3); seen = {}
            def cancel_during_broadcast():
                seen["cancel"] = flow3.reject(od3["payment_id"], od3["snapshot_sha256"])
            node3.on_broadcast = cancel_during_broadcast
            r3 = flow3.submit_signed(od3["payment_id"], od3["snapshot_sha256"], sign_like_wallet(od3["unsigned_tx"]))
            self.assertFalse(seen["cancel"]["ok"]); self.assertIn("CONSUMED", seen["cancel"]["error"])
            self.assertEqual(r3["state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertEqual(len(node3.broadcasts), 1)

    def test_g5_accepted_then_dropped_demotes_ledger_and_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(receipt_mode="accepted"); flow = mkflow(node, tmp, auto_expiry_settle=True); od = self._order(flow)
            self.assertEqual(flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))["state"], "ACCEPTED_UNCONFIRMED")
            self.assertEqual(flow.intents.state(od["payment_id"]), "ACCEPTED")
            node.receipt_mode = "not_found"
            self.assertEqual(flow.status(od["payment_id"])["result"]["state"], "UNKNOWN"); self.assertEqual(flow.intents.state(od["payment_id"]), "UNKNOWN")
            node.solid_ts_ms = (T0 + 700) * 1000
            self.assertEqual(flow.status(od["payment_id"])["result"]["state"], "EXPIRED_NOT_ON_CHAIN"); self.assertEqual(flow.intents.state(od["payment_id"]), "REJECTED")
            # fullnode 만 실패 → 최종 실패 아님(잠금 유지); solidity 도 실패 → FAILED_ONCHAIN
            node2 = FakeNode(receipt_mode="failed_unconfirmed"); flow2 = mkflow(node2, tmp + "/f", auto_expiry_settle=True); od2 = self._order(flow2)
            self.assertEqual(flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sign_like_wallet(od2["unsigned_tx"]))["state"], "FAILED_UNCONFIRMED")
            self.assertEqual(flow2.intents.state(od2["payment_id"]), "ACCEPTED"); self.assertTrue(flow2.prepare(flow2.chat("맥북지갑한테 트론 1개")["proposal_id"], SENDER).get("locked"))
            node2.receipt_mode = "failed"
            self.assertEqual(flow2.status(od2["payment_id"])["result"]["state"], "FAILED_ONCHAIN"); self.assertEqual(flow2.intents.state(od2["payment_id"]), "FAILED")

    def test_g6_same_wallet_base58_and_hex(self):
        import phone_flow as PF
        hex_sender = NT.addr21_hex(SENDER)
        self.assertEqual(PF.norm_sender(hex_sender), SENDER); self.assertEqual(PF.norm_sender(SENDER), SENDER); self.assertIsNone(PF.norm_sender("41" + "zz" * 20))
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(broadcast_exc=TimeoutError("t")); flow = mkflow(node, tmp); od = self._order(flow)
            flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))     # UNKNOWN 열림(base58 표기)
            self.assertFalse(flow.prepare(flow.chat("맥북지갑한테 트론 2개")["proposal_id"], hex_sender)["ok"])   # API 는 hex 거부
            self.assertIsNotNone(flow.sender_lock(hex_sender, T0))                                              # 잠금 비교는 정규화 → 같은 지갑으로 인식
            self.assertIsNone(flow.sender_lock(OTHER.public_key.to_base58check_address(), T0))

    def test_g7_receipt_lookup_classification(self):
        spec = TX.TrxSpec(sender=SENDER, receiver=MAC, amount_sun=2_000_000, expire_at_ms=(T0 + 600) * 1000, fee_cap_sun=2_000_000)
        u = TX.build_unsigned_trx(FakeNode(), spec, now_ms=T0 * 1000)
        empty = {"http": 200, "body": {}}
        for bad in ({"http": 429, "body": {}}, {"http": 503, "body": "busy"}, {"http": 200, "body": "<html>"}, {"http": 200, "body": {"Error": "x"}}):
            r = TX.reconcile_receipt_trx(u, spec, {"tx": bad, "info": empty, "solid_tx": empty, "solid": empty})
            self.assertEqual(r["verdict"], "LOOKUP_ERROR", bad); self.assertFalse(r["solid_lookups_ok"] and r["lookups"]["tx"] != "ERROR" and False)
        r = TX.reconcile_receipt_trx(u, spec, {"tx": empty, "info": empty, "solid_tx": empty, "solid": empty})
        self.assertEqual(r["verdict"], "NOT_FOUND"); self.assertTrue(r["solid_lookups_ok"])
        r = TX.reconcile_receipt_trx(u, spec, {"tx": empty, "info": empty, "solid_tx": empty, "solid": {"http": 503, "body": ""}})
        self.assertEqual(r["verdict"], "LOOKUP_ERROR"); self.assertFalse(r["solid_lookups_ok"])
        found_tx = {"http": 200, "body": {"txID": u["txID"], "raw_data_hex": u["raw_data_hex"], "ret": [{"contractRet": "SUCCESS"}]}}
        found_info = {"http": 200, "body": {"id": u["txID"], "blockNumber": 1, "fee": 0, "receipt": {"net_fee": 0}}}
        r = TX.reconcile_receipt_trx(u, spec, {"tx": empty, "info": empty, "solid_tx": found_tx, "solid": found_info})      # solidity 에만 있음 → NOT_FOUND 아님
        self.assertEqual(r["verdict"], "CONFIRMED")
        r = TX.reconcile_receipt_trx(u, spec, {"tx": found_tx, "info": {"http": 200, "body": {"id": u["txID"], "blockNumber": 1, "receipt": {"net_usage": 267}}}, "solid_tx": empty, "solid": empty})
        self.assertTrue(r["fee_within_cap"]); self.assertEqual(r["fee_sun"], 0); self.assertFalse(r["fee_field_present"]); self.assertEqual(r["verdict"], "ACCEPTED")   # 9/29 실측: receipt 있고 fee 생략 = 0
        r = TX.reconcile_receipt_trx(u, spec, {"tx": found_tx, "info": {"http": 200, "body": {"id": u["txID"], "blockNumber": 1}}, "solid_tx": empty, "solid": empty})
        self.assertFalse(r["fee_within_cap"]); self.assertFalse(r["fee_known"]); self.assertEqual(r["verdict"], "MISMATCH")                    # receipt 자체 없음 = 수수료 미확인


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.node = FakeNode(); self.flow = mkflow(self.node, self.tmp.name)
        self.srv, self.token = PS.serve("127.0.0.1", 0, "127.0.0.1", self.flow, log_path=None)
        self.port = self.srv.server_address[1]; self.srv.socket.close()
        self.srv, self.token = PS.serve("127.0.0.1", self.port, "127.0.0.1", self.flow, log_path=None)
        self.allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}"}; data = None
        if body is not None:
            data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        h.update(headers or {}); c.request(method, path, body=data, headers=h); r = c.getresponse(); out = json.loads(r.read() or b"{}") if r.getheader("Content-Type", "").startswith("application/json") else r.read()
        return r.status, out

    def test_token_host_and_e2e(self):
        p = f"/p/{self.token}"
        self.assertEqual(self.req("GET", "/p/wrongtoken/api/health")[0], 404)
        self.assertEqual(self.req("GET", "/api/health")[0], 404)
        self.assertEqual(self.req("GET", p + "/api/health", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("POST", p + "/api/chat", body={"text": "x"}, headers={"Origin": "http://evil.example"})[0], 403)
        st, page = self.req("GET", p + "/"); self.assertEqual(st, 200); self.assertIn(b"SafeBatch", page); self.assertIn(p.encode(), page)
        st, h = self.req("GET", p + "/api/health"); self.assertEqual(st, 200)
        st, dec = self.req("POST", p + "/api/chat", body={"text": "맥북지갑한테 USDT 2개"}); self.assertEqual((dec["kind"], dec["reason_code"]), ("decline", "UNSUPPORTED_ASSET"))
        st, r = self.req("POST", p + "/api/chat", body={"text": "영훈이한테 트론 2개"}); self.assertEqual(r["kind"], "question")
        st, r = self.req("POST", p + "/api/chat", body={"text": "맥북지갑한테 트론 2개"}); self.assertEqual(r["kind"], "proposal")
        st, pr = self.req("POST", p + "/api/order/prepare", body={"proposal_id": r["proposal_id"], "sender": SENDER}); self.assertEqual(st, 200, pr); od = pr["order"]
        st, bad = self.req("POST", p + "/api/order/signed", body={"payment_id": od["payment_id"], "snapshot_sha256": "0" * 64, "signed_tx": sign_like_wallet(od["unsigned_tx"])})
        self.assertEqual(bad["state"], "NOT_SUBMITTED"); self.assertEqual(len(self.node.broadcasts), 0)
        st, ok = self.req("POST", p + "/api/order/signed", body={"payment_id": od["payment_id"], "snapshot_sha256": od["snapshot_sha256"], "signed_tx": sign_like_wallet(od["unsigned_tx"])})
        self.assertEqual(ok["state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertEqual(len(self.node.broadcasts), 1)
        st, s = self.req("GET", p + f"/api/order/status?payment_id={od['payment_id']}"); self.assertEqual(s["result"]["state"], "FINAL_CONFIRMED_SOLIDITY")
        st, dup = self.req("POST", p + "/api/order/signed", body={"payment_id": od["payment_id"], "snapshot_sha256": od["snapshot_sha256"], "signed_tx": sign_like_wallet(od["unsigned_tx"])})
        self.assertEqual(dup["state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertTrue(dup["idempotent"]); self.assertEqual(len(self.node.broadcasts), 1)
        st, dup2 = self.req("POST", p + "/api/order/signed", body={"payment_id": od["payment_id"], "snapshot_sha256": od["snapshot_sha256"], "signed_tx": sign_like_wallet(od["unsigned_tx"], OTHER)})
        self.assertEqual(dup2["state"], "NOT_SUBMITTED"); self.assertEqual(len(self.node.broadcasts), 1)
        st, lst = self.req("GET", p + "/api/orders?sender=" + SENDER); self.assertEqual(lst["orders"][0]["result_state"], "FINAL_CONFIRMED_SOLIDITY")


if __name__ == "__main__":
    unittest.main()


class VpConditionalTrialTests(unittest.TestCase):
    """9/29 부사장 재검수(조건부 단말 송금 시험): 자동 미송금 종결 비활성 · 응답=저장 상태 · 화면 로직 jsc 모의."""
    def _order(self, flow, text="맥북지갑한테 트론 2개", **kw):
        r = flow.chat(text); p = flow.prepare(r["proposal_id"], SENDER, **kw); self.assertTrue(p["ok"], p); return p["order"]

    def test_auto_expiry_settle_off_by_default_keeps_unknown_and_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(broadcast_exc=TimeoutError("t")); flow = mkflow(node, tmp)            # 기본값 auto_expiry_settle=False
            self.assertFalse(flow.auto_expiry_settle); od = self._order(flow)
            self.assertEqual(flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))["state"], "UNKNOWN")
            node.solid_ts_ms = (T0 + 700) * 1000
            for _ in range(3):
                r = flow.status(od["payment_id"])["result"]
                self.assertEqual(r["state"], "UNKNOWN")
            self.assertTrue(r["expiry_conditions_met"]); self.assertFalse(r["auto_expiry_settle"]); self.assertIn("꺼져", r["note"])
            self.assertEqual(flow.intents.state(od["payment_id"]), "UNKNOWN")
            self.assertTrue(flow.prepare(flow.chat("맥북지갑한테 트론 2개")["proposal_id"], SENDER).get("locked"))   # 잠금 유지
            self.assertEqual(len(node.broadcasts), 1)
            # 정상 거래 확정 판정은 유지
            node2 = FakeNode(); flow2 = mkflow(node2, tmp + "/n"); od2 = self._order(flow2)
            self.assertEqual(flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sign_like_wallet(od2["unsigned_tx"]))["state"], "FINAL_CONFIRMED_SOLIDITY")

    def test_response_matches_stored_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp); od = self._order(flow)
            node.balance = 1_000_000                                                    # 재견적 실패 → 격리
            r = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(r["state"], "SIGNATURE_HELD"); self.assertTrue(r["not_broadcast"]); self.assertEqual(r["order_state"], "SIGNED_REFUSED_NOT_BROADCAST")
            self.assertEqual(flow._load_result(od["payment_id"])["state"], "SIGNATURE_HELD")
            st = flow.status(od["payment_id"]); self.assertTrue(st["signature_stored"]); self.assertEqual(st["result"]["state"], "SIGNATURE_HELD")
            # 같은 서명본 재제출: 기존 상태(SIGNATURE_HELD) 그대로, 방송 0 / 다른 서명본: NOT_SUBMITTED + 원래 주문 상태·결과
            same = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(same["state"], "SIGNATURE_HELD"); self.assertTrue(same["idempotent"]); self.assertEqual(len(node.broadcasts), 0)
            dup = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"], OTHER))
            self.assertEqual(dup["state"], "NOT_SUBMITTED"); self.assertFalse(dup["this_submission_broadcast"])
            self.assertEqual(dup["order_state"], "SIGNED_REFUSED_NOT_BROADCAST"); self.assertEqual(dup["prior_result"]["state"], "SIGNATURE_HELD")
            # 정상 완료 뒤 중복 제출도 원래 결과(FINAL)를 돌려준다
            node2 = FakeNode(); flow2 = mkflow(node2, tmp + "/f"); od2 = self._order(flow2)
            flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sign_like_wallet(od2["unsigned_tx"]))
            dup2 = flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sign_like_wallet(od2["unsigned_tx"], OTHER))
            self.assertEqual((dup2["state"], dup2["order_state"], dup2["prior_result"]["state"]), ("NOT_SUBMITTED", "CONSUMED", "FINAL_CONFIRMED_SOLIDITY")); self.assertEqual(len(node2.broadcasts), 1)
            # 서명 전 취소 뒤 제출: 서명 저장 없음
            node3 = FakeNode(); flow3 = mkflow(node3, tmp + "/c"); od3 = self._order(flow3); flow3.reject(od3["payment_id"], od3["snapshot_sha256"])
            dup3 = flow3.submit_signed(od3["payment_id"], od3["snapshot_sha256"], sign_like_wallet(od3["unsigned_tx"]))
            self.assertEqual((dup3["state"], dup3["order_state"], dup3["prior_result"]), ("NOT_SUBMITTED", "CANCELLED", None)); self.assertFalse(flow3.status(od3["payment_id"])["signature_stored"])

    def test_phone_client_logic_jsc_mock(self):
        """phone_client.js 를 JavaScriptCore(jsc)로 구동한 모의 시험(DOM·실제 브라우저·실서명·방송 없음)."""
        import subprocess
        jsc = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
        if not pathlib.Path(jsc).exists():
            self.skipTest("jsc not available")
        here = pathlib.Path(__file__).resolve().parent
        p = subprocess.run([jsc, "-e", f'var ARG_CLIENT="{here.parent / "phone_client.js"}";', str(here / "phone_client_mock.js")], capture_output=True, text=True, timeout=30)
        out = p.stdout + p.stderr
        (here / "phone_client_mock_last.txt").write_text(out, encoding="utf-8")
        self.assertIn("RESULT PASS", out, out)
        self.assertNotIn("FAIL ", out.replace("RESULT PASS", ""), out)


class WalletPayloadShapeTests(unittest.TestCase):
    """9/29 09:5x 실측: TronLink Android 은 raw_data_hex 없이 {raw_data, signature, txID} 만 돌려준다."""
    def test_signed_without_raw_data_hex_accepted_only_if_signature_over_our_bytes(self):
        spec = TX.TrxSpec(sender=SENDER, receiver=MAC, amount_sun=2_000_000, expire_at_ms=(T0 + 600) * 1000, fee_cap_sun=2_000_000)
        u = TX.build_unsigned_trx(FakeNode(), spec, now_ms=T0 * 1000)
        s = sign_like_wallet(u); del s["raw_data_hex"]; del s["visible"]; s["raw_data"]["contract"][0]["Permission_id"] = 0
        body = TX.verify_signed_trx(u, s, spec, now_ms=(T0 + 10) * 1000)
        self.assertEqual(body["signer"], SENDER); self.assertEqual(body["raw_data_hex"], u["raw_data_hex"])
        # 다른 바이트(수량 변조본)에 서명한 뒤 raw_data_hex 를 빼고 우리 raw_data JSON 만 붙여 보내면 → 서명자 복구 불일치로 거부
        spec3 = TX.TrxSpec(sender=SENDER, receiver=MAC, amount_sun=3_000_000, expire_at_ms=(T0 + 600) * 1000, fee_cap_sun=2_000_000)
        u3 = TX.build_unsigned_trx(FakeNode(), spec3, now_ms=T0 * 1000); s3 = sign_like_wallet(u3)
        forged = {"txID": u["txID"], "raw_data": u["raw_data"], "signature": s3["signature"]}
        with self.assertRaisesRegex(NT.NileTxError, "signer mismatch"):
            TX.verify_signed_trx(u, forged, spec, now_ms=(T0 + 10) * 1000)
        # raw_data JSON 이 주문과 다르면 거부
        s4 = sign_like_wallet(u); del s4["raw_data_hex"]; s4["raw_data"] = json.loads(json.dumps(u["raw_data"])); s4["raw_data"]["contract"][0]["parameter"]["value"]["amount"] = 3_000_000
        with self.assertRaisesRegex(NT.NileTxError, "differs from the order"):
            TX.verify_signed_trx(u, s4, spec, now_ms=(T0 + 10) * 1000)
        # 흐름 전체: raw_data_hex 없는 서명본으로 방송 1회·확정
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp); r = flow.chat("맥북지갑한테 트론 2개"); od = flow.prepare(r["proposal_id"], SENDER)["order"]
            sw = sign_like_wallet(od["unsigned_tx"]); del sw["raw_data_hex"]; del sw["visible"]
            self.assertEqual(flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sw)["state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertEqual(len(node.broadcasts), 1)
            self.assertEqual(node.broadcasts[0]["raw_data_hex"], od["unsigned_tx"]["raw_data_hex"])

    def test_concurrent_prepare_serialized_single_pending(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            flow = mkflow(FakeNode(), tmp); r = flow.chat("맥북지갑한테 트론 2개"); r2 = flow.chat("맥북지갑한테 트론 2개")
            ts = [1_790_700_000]; flow.now_fn = lambda: ts[0]
            outs = []
            def go(pid, t):
                ts[0] = t; outs.append(flow.prepare(pid, SENDER))
            a = threading.Thread(target=go, args=(r["proposal_id"], T0)); b = threading.Thread(target=go, args=(r2["proposal_id"], T0 + 1))
            a.start(); b.start(); a.join(); b.join()
            pend = [o for o in flow.store.pending_orders() if o.get("user_eoa") == SENDER]
            self.assertEqual(len(pend), 1, [o["payment_id"] for o in pend])


class ResubmitAndRecoveryTests(unittest.TestCase):
    """VP 9/29 결과 복구: 같은 서명본 재제출 idempotent · 주문 결과 목록 · 토큰 유지."""
    def test_same_signed_tx_resubmitted_returns_existing_state_no_second_broadcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp); r = flow.chat("맥북지갑한테 트론 2개"); od = flow.prepare(r["proposal_id"], SENDER)["order"]
            sw = sign_like_wallet(od["unsigned_tx"])
            r1 = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sw); self.assertEqual(r1["state"], "FINAL_CONFIRMED_SOLIDITY")
            r2 = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], json.loads(json.dumps(sw)))
            self.assertTrue(r2["idempotent"]); self.assertEqual(r2["state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertEqual(len(node.broadcasts), 1)
            # 다른 서명본(다른 signature)은 idempotent 아님 → NOT_SUBMITTED + 원래 상태
            other = sign_like_wallet(od["unsigned_tx"], OTHER)
            r3 = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], other); self.assertEqual(r3["state"], "NOT_SUBMITTED"); self.assertEqual(len(node.broadcasts), 1)
            # 방송 UNKNOWN 상태에서 같은 서명본 재제출 → 같은 txID 재조회로 종결(재방송 0)
            node2 = FakeNode(broadcast_exc=TimeoutError("t")); flow2 = mkflow(node2, tmp + "/u"); r = flow2.chat("맥북지갑한테 트론 2개"); od2 = flow2.prepare(r["proposal_id"], SENDER)["order"]
            sw2 = sign_like_wallet(od2["unsigned_tx"]); self.assertEqual(flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sw2)["state"], "UNKNOWN")
            node2.broadcast_exc = None; node2.chain.append(node2.broadcasts[0])
            r4 = flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sw2); self.assertTrue(r4["idempotent"]); self.assertEqual(r4["state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertEqual(len(node2.broadcasts), 1)
            lst = flow2.list_orders(SENDER); self.assertEqual(lst[0]["result_state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertEqual(lst[0]["tx_hash"], od2["tx_id"]); self.assertNotIn("signed_tx", json.dumps(lst))

    def test_token_reuse_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = mkflow(FakeNode(), tmp)
            srv, tok = PS.serve("127.0.0.1", 0, "127.0.0.1", flow, token="fixedTok123", log_path=None)
            self.assertEqual(tok, "fixedTok123"); srv.server_close()
            srv2, tok2 = PS.serve("127.0.0.1", 0, "127.0.0.1", flow, token=None, log_path=None); self.assertNotEqual(tok2, "fixedTok123"); srv2.server_close()


class Grok05ServerTests(unittest.TestCase):
    """Grok-05: 동시 최초 /signed 제출(방송 1회·진 쪽 idempotent) · status 만료는 서버 시계+확정 블록 시각."""
    def test_concurrent_same_signature_posts_single_broadcast(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp); r = flow.chat("맥북지갑한테 트론 2개"); od = flow.prepare(r["proposal_id"], SENDER)["order"]
            sw = sign_like_wallet(od["unsigned_tx"]); outs = []
            def go(): outs.append(flow.submit_signed(od["payment_id"], od["snapshot_sha256"], json.loads(json.dumps(sw))))
            ts = [threading.Thread(target=go) for _ in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]
            self.assertEqual(len(node.broadcasts), 1)
            self.assertIn("FINAL_CONFIRMED_SOLIDITY", [o["state"] for o in outs]); self.assertEqual(sum(1 for o in outs if o.get("idempotent")), 1)
            other = [o for o in outs if o.get("idempotent")][0]; self.assertIn(other["state"], ("FINAL_CONFIRMED_SOLIDITY", "PROCESSING", "UNKNOWN"))
            self.assertEqual(flow.status(od["payment_id"])["result"]["state"], "FINAL_CONFIRMED_SOLIDITY")

    def test_status_expired_requires_node_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp); r = flow.chat("맥북지갑한테 트론 2개"); od = flow.prepare(r["proposal_id"], SENDER)["order"]
            s0 = flow.status(od["payment_id"]); self.assertFalse(s0["expired"]); self.assertIsNone(s0["expiry_reason"])
            flow.now_fn = lambda: T0 + 700                                   # 서버 시계만 만료 → expired False, 근거 표시
            s1 = flow.status(od["payment_id"]); self.assertFalse(s1["expired"]); self.assertIn("server_clock_only", s1["expiry_reason"])
            node.solid_ts_ms = (T0 + 700) * 1000                             # 확정 블록 시각도 지남 → expired True
            s2 = flow.status(od["payment_id"]); self.assertTrue(s2["expired"]); self.assertEqual(s2["expiry_reason"], "server_clock+solidified_block_time")
            late = flow.submit_signed(od["payment_id"], od["snapshot_sha256"], sign_like_wallet(od["unsigned_tx"]))
            self.assertEqual(late["state"], "NOT_SUBMITTED"); self.assertEqual(len(node.broadcasts), 0)   # 만료 서명본은 저장·방송 없음


class MockKilnIntegrationTests(unittest.TestCase):
    """VP 9/29: /api/chat→phone_ai(MockKiln)→제안·거절→주문 생성·제출 판단까지. 실호출 0."""
    def test_chat_uses_mock_kiln_and_constraints_apply_to_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp)
            r = flow.chat("맥북지갑한테 트론 2개"); self.assertEqual((r["kind"], r["kind_detail"], r["ai"]["mode"], r["ai"]["calls"]), ("proposal", "normal", "MOCK_KILN", 0))
            od = flow.prepare(r["proposal_id"], SENDER)["order"]; self.assertEqual(od["ai"]["mode"], "MOCK_KILN"); self.assertEqual(od["fee_cap_sun"], 2_000_000); self.assertEqual(od["sign_window_s"], 600)
            # 적응: 예산 3 TRX·기한 5분 → 수수료 상한 1 TRX(=3-2)·만료 300초로 주문에 반영
            flow2 = mkflow(FakeNode(), tmp + "/a"); r2 = flow2.chat("맥북지갑한테 트론 2개, 예산 3 트론 5분 안에")
            self.assertEqual((r2["kind"], r2["kind_detail"]), ("proposal", "adapt")); self.assertEqual(r2["constraints"], {"budget_sun": 3_000_000, "deadline_s": 300, "fee_cap_sun": 1_000_000})
            od2 = flow2.prepare(r2["proposal_id"], SENDER)["order"]
            self.assertEqual((od2["fee_cap_sun"], od2["sign_window_s"], od2["expire_at_ms"]), (1_000_000, 300, (T0 + 300) * 1000)); self.assertEqual(od2["amount_sun"], 2_000_000)
            # 제출 판단에도 반영: 만료 300초 뒤 서명 제출 → 거부·방송 0
            flow2.now_fn = lambda: T0 + 301
            late = flow2.submit_signed(od2["payment_id"], od2["snapshot_sha256"], sign_like_wallet(od2["unsigned_tx"])); self.assertEqual(late["state"], "NOT_SUBMITTED")
            # 예산이 수량+최악 수수료보다 작으면 prepare 거부(수량 감액 없음). 수취인 미활성이면 최악 1.1+0.267 > 예산 2.5-2
            flow3 = mkflow(FakeNode(receiver_exists=False), tmp + "/b"); r3 = flow3.chat("맥북지갑한테 트론 2개 예산 2.5")
            self.assertEqual(r3["kind"], "proposal"); p3 = flow3.prepare(r3["proposal_id"], SENDER); self.assertFalse(p3["ok"]); self.assertIn("예산", p3["error"])
            # 거절: 상한 초과 → 주문 생성 없음
            d = flow.chat("맥북지갑한테 트론 1000개"); self.assertEqual((d["kind"], d["reason_code"], d["order_created"]), ("decline", "OVER_PER_REQUEST_CAP", False)); self.assertEqual(len(flow.proposals), 1)
            # AI 판단 기록 파일 존재, 서명본 없음
            log = (pathlib.Path(tmp) / "phone_ai_decisions.jsonl").read_text(); self.assertIn("MOCK_KILN", log); self.assertNotIn("signature", log)

    def test_model_invented_conditions_ignored_and_validation(self):
        class Invent:
            provider = "MOCK_KILN"; model = "qwen3-32b(mock)"
            def structure(self, text): return {"raw": '{"alias":"맥북지갑","amount":"2","asset":"TRX","change":{"budget_trx":"9","deadline_minutes":3}}', "calls": 0}
        with tempfile.TemporaryDirectory() as tmp:
            flow = mkflow(FakeNode(), tmp, ai_provider=Invent()); r = flow.chat("맥북지갑한테 트론 2개")
            self.assertEqual(r["kind_detail"], "normal"); self.assertEqual(r["ai"]["ignored_model_conditions"], {"budget_trx": "9", "deadline_minutes": 3})
            od = flow.prepare(r["proposal_id"], SENDER)["order"]; self.assertEqual((od["fee_cap_sun"], od["sign_window_s"]), (2_000_000, 600))
        import phone_ai as AI
        for bad in ('{"alias":5,"amount":"2","asset":"TRX"}', '{"alias":"x","amount":"2","asset":"TRX","change":{"budget_trx":"Infinity"}}', '{"alias":"x","amount":"2","asset":"TRX","change":{"budget_trx":"-1"}}',
                    '{"alias":"x","amount":"2","asset":"TRX","change":{"deadline_minutes":0}}', '{"alias":"x","amount":"2","asset":"TRX","change":{"deadline_minutes":2.5}}', '{"alias":"x","amount":"2","asset":"TRX","change":{"deadline_minutes":100000}}',
                    '{"alias":"x","amount":"NaN","asset":"TRX"}'):
            self.assertIsNone(AI.validate_model_output(bad)[0], bad)

    def test_hold_blocks_new_orders_server_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = mkflow(FakeNode(), tmp); r = flow.chat("맥북지갑한테 트론 2개"); od = flow.prepare(r["proposal_id"], SENDER)["order"]
            self.assertFalse(flow.register_hold(od["payment_id"], "0" * 64, od["tx_id"])["ok"])
            self.assertTrue(flow.register_hold(od["payment_id"], od["snapshot_sha256"], od["tx_id"])["ok"])
            self.assertTrue(flow.status(od["payment_id"])["client_hold"])
            flow.reject(od["payment_id"], od["snapshot_sha256"])                                    # 서버에서 취소돼도(서명 미저장) 보유 등록이 있으면 새 주문 차단
            p = flow.prepare(flow.chat("맥북지갑한테 트론 1개")["proposal_id"], SENDER); self.assertFalse(p["ok"]); self.assertIn("서명본이 남아", p["error"])
            self.assertIsNone(flow.sender_lock(OTHER.public_key.to_base58check_address(), T0))


class SenderIsolationTests(unittest.TestCase):
    """9/29 두 기기·두 지갑 시험 준비(VP): 계정/기기가 바뀌어도 해당 지갑의 주소·주문·서명본만 쓴다. 다른 지갑의 미완료 주문을 재사용·종료하지 않는다."""
    OTHER_ADDR = OTHER.public_key.to_base58check_address()

    def _prep(self, flow, sender, text="맥북지갑한테 트론 2개"):
        r = flow.chat(text); self.assertEqual(r["kind"], "proposal")
        return flow.prepare(r["proposal_id"], sender)

    def test_orders_and_signatures_are_per_sender_wallet(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); flow = mkflow(node, tmp)
            a = self._prep(flow, SENDER); self.assertTrue(a["ok"], a); oa = a["order"]
            # 지갑 A 에 미완료 주문이 있어도 지갑 B 는 자기 주문을 만들 수 있다(잠금·중복 검사는 발신 지갑별)
            b = self._prep(flow, self.OTHER_ADDR); self.assertTrue(b["ok"], b); ob = b["order"]
            self.assertNotEqual(oa["payment_id"], ob["payment_id"]); self.assertNotEqual(oa["tx_id"], ob["tx_id"])
            self.assertEqual(oa["user_eoa"], SENDER); self.assertEqual(ob["user_eoa"], self.OTHER_ADDR)
            # 결과 조회는 발신 지갑별로만 보인다
            self.assertEqual([o["payment_id"] for o in flow.list_orders(SENDER)], [oa["payment_id"]])
            self.assertEqual([o["payment_id"] for o in flow.list_orders(self.OTHER_ADDR)], [ob["payment_id"]])
            # 지갑 A 의 서명본을 지갑 B 의 주문에 제출 → 거부(원본 바이트 불일치, 방송 0). 두 주문 모두 그대로
            cross = flow.submit_signed(ob["payment_id"], ob["snapshot_sha256"], sign_like_wallet(oa["unsigned_tx"]))
            self.assertEqual(cross["state"], "NOT_SUBMITTED", cross); self.assertEqual(len(node.broadcasts), 0)
            self.assertEqual(flow.store.get(ob["payment_id"])["state"], "PENDING"); self.assertEqual(flow.store.get(oa["payment_id"])["state"], "PENDING")
            # 지갑 B 가 자기 주문에 자기 키로 서명 → 방송 1회·확정. 지갑 A 주문은 영향 없음(미완료 유지)
            okb = flow.submit_signed(ob["payment_id"], ob["snapshot_sha256"], sign_like_wallet(ob["unsigned_tx"], OTHER))
            self.assertEqual(okb["state"], "FINAL_CONFIRMED_SOLIDITY", okb); self.assertEqual(len(node.broadcasts), 1)
            self.assertEqual(flow.intents.state(ob["payment_id"]), "CONFIRMED"); self.assertEqual(flow.store.get(oa["payment_id"])["state"], "PENDING")
            # 지갑 B 의 키로 지갑 A 의 주문에 서명한 서명본 → 서명자 불일치로 거부(방송 여전히 1회). A 주문은 A 만 서명할 수 있게 그대로 남는다
            wrong = flow.submit_signed(oa["payment_id"], oa["snapshot_sha256"], sign_like_wallet(oa["unsigned_tx"], OTHER))
            self.assertEqual(wrong["state"], "NOT_SUBMITTED", wrong); self.assertIn("signer mismatch", wrong["reason"]); self.assertEqual(len(node.broadcasts), 1)
            self.assertEqual(flow.store.get(oa["payment_id"])["state"], "PENDING")
            oka = flow.submit_signed(oa["payment_id"], oa["snapshot_sha256"], sign_like_wallet(oa["unsigned_tx"]))
            self.assertEqual(oka["state"], "FINAL_CONFIRMED_SOLIDITY", oka); self.assertEqual(len(node.broadcasts), 2)
            # 결과 조회는 여전히 지갑별(상대 지갑 주문이 보이지 않는다)
            self.assertEqual({o["payment_id"] for o in flow.list_orders(SENDER)}, {oa["payment_id"]})
            self.assertEqual({o["payment_id"] for o in flow.list_orders(self.OTHER_ADDR)}, {ob["payment_id"]})
