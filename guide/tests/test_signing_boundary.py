"""9/28 부사장 서명 화면 검수 3경계: ①서명자·체인·본문 검증 ②주문별 취소·늦은 서명 ③서버 요청 경계. 실제 지갑·송금 없음.
서명은 설치된 tronpy 키(합성)로 만들고, 서버는 순수 파이썬 tip712 로 복구한다(공식 벡터 미검증이므로 '자기 일관성' 검사)."""
import http.client
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import app as APP  # noqa: E402
from order_store import OrderStore  # noqa: E402
from signer import FileSigner  # noqa: E402
from safebatch import tip712 as T  # noqa: E402
from safebatch import gasfree_order as go  # noqa: E402
from tronpy.keys import PrivateKey  # noqa: E402

RECV = "TMDKznuDWaZwfZHcM61FVFstyYNmK6Njk1"
USDT = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"
PROV = "TKtWbdzEq5ss9vTS9kwRhBp5mXmBfBns3E"
OWNER = PrivateKey(bytes.fromhex("11" * 32)); OWNER_ADDR = OWNER.public_key.to_base58check_address()
OTHER = PrivateKey(bytes.fromhex("22" * 32))
NOW = 1_000_000


def order(pid="b1:r1:2", deadline=NOW + 600, user=OWNER_ADDR, value="1500000"):
    msg = {"token": USDT, "serviceProvider": PROV, "user": user, "receiver": RECV, "value": value, "maxFee": "10000000",
           "deadline": str(deadline), "version": 1, "nonce": 3}
    snap = go.snapshot_digest(go.NILE_DOMAIN, go.PERMIT_TYPES, msg, "ap-1", "c" * 64, "req-1")
    return {"payment_id": pid, "batch_id": "b1", "revision": 1, "csv_sha256": "c" * 64, "approval_id": "ap-1", "row_no": 2, "memo": "m",
            "network": "nile", "user_eoa": user, "gasfree_address": "TLGVf7MRsLG7XxBkJKy8wnCVcDnAeXYNCb", "token_symbol": "USDT",
            "token_decimal": 6, "message": msg, "domain": dict(go.NILE_DOMAIN), "types": go.PERMIT_TYPES, "request_id": "req-1",
            "fee_breakdown": {}, "available_units": 100_000_000, "created_at": NOW, "checks": [], "approval_mode": "HUMAN_CONFIRMED",
            "snapshot_sha256": snap}


def sign_with(key: PrivateKey, od: dict, msg=None, domain=None, types=None) -> dict:
    m, d, t = msg or od["message"], domain or od["domain"], types or od["types"]
    h = T.typed_data_hash(d, t, "PermitTransfer", m)
    sig = key.sign_msg_hash(h)
    sig_hex = sig.hex() if hasattr(sig, "hex") else bytes(sig).hex()
    return {"payment_id": od["payment_id"], "snapshot_sha256": od["snapshot_sha256"], "message": m, "domain": d, "types": t, "sig": sig_hex}


class Server:
    def __init__(self, tmp):
        self.store = OrderStore(pathlib.Path(tmp) / "pending")
        self.token = "tok-test"
        self.port = 18765 + (os.getpid() % 1000)
        self.srv = APP.serve(self.port, self.store, self.token)
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True); self.th.start()

    def post(self, path, body, headers=None, raw=None):
        h = {"Content-Type": "application/json", "X-SafeBatch-Token": self.token, "Host": f"127.0.0.1:{self.port}",
             "Origin": f"http://127.0.0.1:{self.port}"}
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = raw if raw is not None else json.dumps(body).encode()
        c.request("POST", path, body=data, headers=h)
        r = c.getresponse(); out = (r.status, json.loads(r.read() or b"{}")); c.close(); return out

    def stop(self):
        self.srv.shutdown(); self.srv.server_close()


class SignerVerification(unittest.TestCase):
    def test_correct_signer_accepted_wrong_signer_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = order(); store.put_pending(od)
            ok, why = store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OTHER, od), now=NOW + 1)
            self.assertFalse(ok); self.assertIn("signer mismatch", why)
            ok, who = store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OWNER, od), now=NOW + 1)
            self.assertTrue(ok, who); self.assertEqual(who, OWNER_ADDR)
            self.assertEqual(store.get(od["payment_id"])["state"], "SIGNED_VERIFIED")

    def test_domain_types_message_tamper_and_random_bytes_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = order(); store.put_pending(od)
            bad_domain = dict(od["domain"], chainId=728126428)
            for signed in (sign_with(OWNER, od, domain=bad_domain),                                   # 서명은 mainnet 도메인
                           dict(sign_with(OWNER, od), domain=bad_domain),                              # 서버 도메인과 다르다고 주장
                           dict(sign_with(OWNER, od), types={"X": []}),
                           dict(sign_with(OWNER, od), message=dict(od["message"], value="9999999")),
                           dict(sign_with(OWNER, od), sig="ab" * 65)):
                ok, why = store.store_signature(od["payment_id"], od["snapshot_sha256"], signed, now=NOW + 1)
                self.assertFalse(ok, why)
            self.assertEqual(store.get(od["payment_id"])["state"], "PENDING")

    def test_expired_signature_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = order(deadline=NOW + 30); store.put_pending(od)
            ok, why = store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OWNER, od), now=NOW + 31)
            self.assertFalse(ok); self.assertIn("deadline", why)


class PerOrderCancel(unittest.TestCase):
    def test_stale_A_cancel_does_not_touch_B(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); a = order("b1:r1:2"); b = order("b2:r1:2")
            store.put_pending(a); store.put_pending(b)
            ok, _ = store.cancel(a["payment_id"], a["snapshot_sha256"])
            self.assertTrue(ok); self.assertEqual(store.get(b["payment_id"])["state"], "PENDING")
            ok, why = store.cancel(b["payment_id"], "0" * 64)                                        # 잘못된 지문
            self.assertFalse(ok); self.assertEqual(store.get(b["payment_id"])["state"], "PENDING")

    def test_cancel_then_late_signature_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = order(); store.put_pending(od)
            store.cancel(od["payment_id"], od["snapshot_sha256"])
            ok, why = store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OWNER, od), now=NOW + 1)
            self.assertFalse(ok); self.assertIn("CANCELLED", why)
            self.assertEqual(store.consume(od["payment_id"], od["snapshot_sha256"])[0], None)

    def test_sign_then_cancel_is_withdrawn_not_unsigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = order(); store.put_pending(od)
            self.assertTrue(store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OWNER, od), now=NOW + 1)[0])
            ok, msg = store.cancel(od["payment_id"], od["snapshot_sha256"])
            self.assertTrue(ok); self.assertIn("cannot be revoked", msg); self.assertIn("NOT be submitted", msg)
            self.assertEqual(store.get(od["payment_id"])["state"], "SIGNED_THEN_WITHDRAWN")
            self.assertIsNone(store.consume(od["payment_id"], od["snapshot_sha256"])[0])                # 소비 불가

    def test_duplicate_signature_and_double_consume(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = order(); store.put_pending(od)
            self.assertTrue(store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OWNER, od), now=NOW + 1)[0])
            self.assertFalse(store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OWNER, od), now=NOW + 2)[0])
            self.assertIsNotNone(store.consume(od["payment_id"], od["snapshot_sha256"])[0])
            self.assertIsNone(store.consume(od["payment_id"], od["snapshot_sha256"])[0])


class ServerBoundary(unittest.TestCase):
    def test_http_boundary_and_state_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Server(tmp)
            try:
                od = order(deadline=int(__import__("time").time()) + 600); s.store.put_pending(od)   # 실서버는 실제 시계
                body = {"payment_id": od["payment_id"], "snapshot_sha256": od["snapshot_sha256"]}
                self.assertEqual(s.post("/api/order/reject", body, headers={"X-SafeBatch-Token": "wrong"})[0], 403)
                self.assertEqual(s.post("/api/order/reject", body, headers={"Origin": "https://evil.example"})[0], 403)
                self.assertEqual(s.post("/api/order/reject", body, headers={"Content-Type": "text/plain"})[0], 403)
                self.assertEqual(s.post("/api/order/reject", body, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
                self.assertEqual(s.post("/api/order/reject", None, raw=b"x" * (APP.MAX_BODY + 1))[0], 403)
                self.assertEqual(s.store.get(od["payment_id"])["state"], "PENDING")                     # 어느 것도 상태를 못 바꿈
                st, j = s.post("/api/order/signed", sign_with(OTHER, od)); self.assertEqual(st, 409)
                st, j = s.post("/api/order/signed", sign_with(OWNER, od)); self.assertEqual(st, 200); self.assertEqual(j["signer"], OWNER_ADDR)
                st, j = s.post("/api/order/reject", body); self.assertEqual(st, 200); self.assertIn("NOT be submitted", j["detail"])
                self.assertEqual(s.store.get(od["payment_id"])["state"], "SIGNED_THEN_WITHDRAWN")
            finally:
                s.stop()


class FileSignerPath(unittest.TestCase):
    def test_executor_consumes_verified_signature_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = order()
            clock = {"t": NOW}
            def human_signs_in_background(_):
                store.store_signature(od["payment_id"], od["snapshot_sha256"], sign_with(OWNER, od), now=clock["t"])
            fs = FileSigner(store, timeout_s=60, sleep_fn=human_signs_in_background, now_fn=lambda: clock["t"])
            signed = fs.sign(od)
            self.assertIsNotNone(signed); self.assertEqual(signed["message"], od["message"])
            self.assertEqual(store.get(od["payment_id"])["state"], "CONSUMED")

    def test_cancel_while_waiting_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderStore(pathlib.Path(tmp)); od = order()
            def cancel_in_background(_): store.cancel(od["payment_id"], od["snapshot_sha256"])
            fs = FileSigner(store, timeout_s=60, sleep_fn=cancel_in_background, now_fn=lambda: NOW)
            r = fs.sign(od); self.assertIn("cancelled", r["refused"]); self.assertEqual(store.get(od["payment_id"])["state"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()


class WalletConnectUX(unittest.TestCase):
    """9/28 부사장 지적: /sign 에 최초 지갑 연결 절차가 없었다. 연결 버튼은 계정 요청만 하고 서명 검증은 그대로여야 한다."""
    def test_sign_page_has_connect_step_and_keeps_signing_checks(self):
        import pathlib
        s = pathlib.Path(__file__).resolve().parents[1].joinpath("sign.html").read_text(encoding="utf-8")
        self.assertIn('id="connect"', s); self.assertIn("tron_requestAccounts", s); self.assertIn('id="wallet"', s)
        self.assertNotIn("sendRawTransaction(", s.split("// 서명만")[0] if "// 서명만" in s else s)      # 연결 코드가 방송을 호출하지 않는다
        for keep in ("tw.trx.sign(", "지갑 계정(", "지갑 네트워크가 Nile 이 아닙니다", "signed.raw_data_hex !== order.unsigned_tx.raw_data_hex", "/api/order/signed"):
            self.assertIn(keep, s)
        self.assertLess(s.index('id="connect"'), s.index('id="sign"'))                                    # 연결이 서명보다 먼저
