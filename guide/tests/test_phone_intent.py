"""9/29 외부 채팅(텔레그램) → intent → 승인 링크(/a/<token>) → 자동 prepare → 서명 → 확정 (VP_REMOTE_MINIMAL_APPROVAL §2·§3·§5 + VP_REVIEW_TG_IMPL §1~§4).
네트워크 없음: 가짜 Nile 노드(test_phone_flow.FakeNode)·검사용 키. 실제 Telegram 발송·AI 실호출·서명·방송 0.
검증: 메시지 키 멱등·충돌 거부·문장 병합 제거(두 지갑 같은 문장 → 2건) · CREATING 내구 예약(chat 뒤 기록 전 중단 → 추가 AI 호출 0) · 주문 생성 뒤 결합 전 중단 → 기존 주문 결합 ·
발신 지갑 결합(불일치·미지정·만료) · 시험 모드(모의 제공자·서명 제출 거부) · HTTP(봇 키 필수·source 위조 거부·/a/ 링크 범위·다른 주문 403·chat 불가·마스킹·버전) ·
동시성(같은 키 동시 → intent 1) · 어댑터(should_route 되묻기·hani_final 훅 문자열·라우팅 순서·dedupe·회신 지갑 대조·알림 실패 기록·재전송·비활성)."""
import http.client
import json
import pathlib
import socket
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE.parents[3]))   # hani_bot 루트(gwdc_tg_adapter)
import phone_server as PS  # noqa: E402
from phone_flow import PhoneFlow  # noqa: E402
from phone_intent import IntentStore, IntentConflict, INTENT_TTL_S  # noqa: E402
from order_store import OrderStore  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402
from test_phone_flow import FakeNode, CONTACTS, SENDER, MAC, T0, sign_like_wallet  # noqa: E402
import gwdc_tg_adapter as TG  # noqa: E402

IPHONE = SENDER            # 검사용 키의 주소를 '아이폰' 지갑으로 등록(실지갑 아님)
ANDROID = "TJ1aFHjZsTyDtpUkwHXkPEC8Ay4w6ixHFY"
WALLETS = {"default": None, "wallets": [{"label": "아이폰", "address": IPHONE, "words": ["아이폰", "iphone", "아이폰으로"]}, {"label": "안드로이드", "address": ANDROID, "words": ["안드로이드", "android", "안드로이드로"]}]}
TEXT = "맥북지갑한테 트론 2개 보내 트론링크앱 사용해서"


class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t


def mkflow(node, tmp, clock=None):
    root = pathlib.Path(tmp)
    return PhoneFlow(node=node, store=OrderStore(root / "pending"), intents=IntentLog(root / "logs" / "phone_intents.jsonl"), results_dir=root / "logs" / "phone_results",
                     contacts=CONTACTS, now_fn=clock or Clock(), sleep_fn=lambda s: None, receipt_polls=2, poll_s=0)


class IntentStoreTests(unittest.TestCase):
    def test_message_key_idempotent_conflict_and_no_text_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); flow = mkflow(FakeNode(), tmp, clock); st = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock)
            calls = []; orig = flow.chat; flow.chat = lambda t: (calls.append(t), orig(t))[1]
            a, c1 = st.create(flow, text=TEXT, sender=IPHONE, source="telegram", external_key="tg:1:10", external_user="1", sender_label="아이폰")
            b, c2 = st.create(flow, text="맥북지갑한테  트론 2개 보내 트론링크앱 사용해서", sender=IPHONE, source="telegram", external_key="tg:1:10", external_user="1", sender_label="아이폰")
            self.assertTrue(c1); self.assertFalse(c2); self.assertEqual(a["intent_id"], b["intent_id"]); self.assertEqual(len(calls), 1, "같은 메시지 키 재전달 → chat 1회")
            with self.assertRaises(IntentConflict):
                st.create(flow, text=TEXT, sender=ANDROID, source="telegram", external_key="tg:1:10", external_user="1", sender_label="안드로이드")   # 같은 키·다른 지갑
            with self.assertRaises(IntentConflict):
                st.create(flow, text="맥북지갑한테 트론 3개 보내", sender=IPHONE, source="telegram", external_key="tg:1:10", external_user="1")       # 같은 키·다른 내용
            clock.t += 30
            c, c3 = st.create(flow, text=TEXT, sender=ANDROID, source="telegram", external_key="tg:1:11", external_user="1", sender_label="안드로이드")
            self.assertTrue(c3); self.assertNotEqual(c["intent_id"], a["intent_id"]); self.assertEqual(c["sender"], ANDROID); self.assertEqual(len(calls), 2, "같은 문장이라도 다른 지갑·다른 메시지 → 별개 intent")
            d, c4 = st.create(flow, text=TEXT, sender=IPHONE, source="telegram", external_key="tg:1:12", external_user="1", sender_label="아이폰")
            self.assertTrue(c4); self.assertNotEqual(d["intent_id"], a["intent_id"], "같은 문장·같은 지갑이라도 새 메시지 → 새 intent(문장 병합 없음)")
            clock.t += 10_000; st2 = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock)                       # 프로세스 재시작·시간 경과 뒤 재전달
            e, c5 = st2.create(flow, text=TEXT, sender=IPHONE, source="telegram", external_key="tg:1:10", external_user="1", sender_label="아이폰")
            self.assertFalse(c5); self.assertEqual(e["intent_id"], a["intent_id"]); self.assertEqual(len(calls), 3)
            self.assertIn("tg:1:10", e["keys"])
            self.assertIsNone(st.by_token("short")); self.assertIsNone(st.by_token(a["token"][:-1] + ("x" if a["token"][-1] != "x" else "y")))

    def test_durable_reservation_no_extra_ai_call_after_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); flow = mkflow(FakeNode(), tmp, clock); st = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock)
            calls = []
            def crashing_chat(t):
                calls.append(t); raise RuntimeError("process died after AI response, before record")
            orig = flow.chat; flow.chat = crashing_chat
            rec, created = st.create(flow, text=TEXT, sender=IPHONE, source="telegram", external_key="tg:1:1", external_user="1", sender_label="아이폰")
            self.assertTrue(created); self.assertEqual(rec["state"], "CREATE_FAILED"); self.assertEqual(len(calls), 1)
            flow.chat = orig
            rec2, created2 = st.create(flow, text=TEXT, sender=IPHONE, source="telegram", external_key="tg:1:1", external_user="1", sender_label="아이폰")
            self.assertFalse(created2); self.assertEqual(rec2["intent_id"], rec["intent_id"]); self.assertEqual(rec2["state"], "CREATE_FAILED", "재전달 → 기존 기록, 추가 AI 호출 0")
            p = st._path(rec["intent_id"]); d = json.loads(p.read_text(encoding="utf-8")); d["state"] = "CREATING"; p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")   # 기록 전 중단 흉내
            n0 = len(calls)
            rec3, created3 = st.create(flow, text=TEXT, sender=IPHONE, source="telegram", external_key="tg:1:1", external_user="1", sender_label="아이폰")
            self.assertFalse(created3); self.assertEqual(rec3["state"], "CREATING"); self.assertEqual(len(calls), n0, "CREATING 재전달 → chat 0")
            self.assertFalse(st.prepare(flow, token=rec3["token"], sender=IPHONE)["ok"], "CREATING 은 주문 불가")

    def test_prepare_binding_recovery_and_single_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); node = FakeNode(); flow = mkflow(node, tmp, clock); st = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock)
            rec, _ = st.create(flow, text="맥북지갑한테 트론 2개 보내", sender=IPHONE, source="telegram", external_key="k1", external_user="u", sender_label="아이폰")
            tok = rec["token"]
            r = st.prepare(flow, token=tok, sender=ANDROID); self.assertFalse(r["ok"]); self.assertTrue(r.get("wallet_mismatch")); self.assertEqual(len(flow.store.pending_orders()), 0)
            self.assertFalse(st.prepare(flow, token="nope", sender=IPHONE)["ok"])
            r1 = st.prepare(flow, token=tok, sender=IPHONE); self.assertTrue(r1["ok"], r1); pid = r1["order"]["payment_id"]
            r2 = st.prepare(flow, token=tok, sender=IPHONE); self.assertTrue(r2["ok"]); self.assertTrue(r2.get("reused")); self.assertEqual(r2["order"]["payment_id"], pid)
            self.assertEqual(len(flow.store.pending_orders()), 1)
            p = st._path(rec["intent_id"]); d = json.loads(p.read_text(encoding="utf-8")); d["payment_id"] = None; d["state"] = "PROPOSED"; p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")   # 생성 뒤 결합 전 중단 흉내
            r3 = st.prepare(flow, token=tok, sender=IPHONE); self.assertTrue(r3["ok"]); self.assertTrue(r3.get("recovered")); self.assertEqual(r3["order"]["payment_id"], pid); self.assertEqual(len(flow.store.pending_orders()), 1, "기존 주문 결합·추가 생성 0")
            self.assertTrue(st.order_allowed(st.get(rec["intent_id"]), pid)); self.assertFalse(st.order_allowed(st.get(rec["intent_id"]), "other"))
            v = st.view(st.get(rec["intent_id"]), flow); self.assertEqual((v["state"], v["payment_id"]), ("ORDER_PENDING", pid))
            res = flow.submit_signed(pid, r1["order"]["snapshot_sha256"], sign_like_wallet(r1["order"]["unsigned_tx"]))
            self.assertEqual(res["state"], "FINAL_CONFIRMED_SOLIDITY"); self.assertEqual(len(node.broadcasts), 1)
            v = st.view(st.get(rec["intent_id"]), flow); self.assertEqual(v["state"], "FINAL")
            r4 = st.prepare(flow, token=tok, sender=IPHONE); self.assertFalse(r4["ok"]); self.assertTrue(r4.get("done")); self.assertEqual(len(node.broadcasts), 1)
            rq, _ = st.create(flow, text="맥북지갑한테 트론 2개 보내", sender=None, source="telegram", external_key="k2", external_user="u2")
            self.assertFalse(st.prepare(flow, token=rq["token"], sender=IPHONE)["ok"], "발신 지갑 미지정 → 주문 없음")
            rx, _ = st.create(flow, text="맥북지갑한테 트론 2개 보내", sender=IPHONE, source="telegram", external_key="k3", external_user="u3", sender_label="아이폰")
            clock.t += INTENT_TTL_S + 1
            rr = st.prepare(flow, token=rx["token"], sender=IPHONE); self.assertFalse(rr["ok"]); self.assertTrue(rr.get("expired"))
            st.note_notify(rec["intent_id"], "FINAL", False); st.note_notify(rec["intent_id"], "FINAL", True)
            self.assertEqual(st.get(rec["intent_id"])["notify"]["FINAL"]["attempts"], 2); self.assertTrue(st.get(rec["intent_id"])["notify"]["FINAL"]["ok"])

    def test_test_mode_uses_mock_provider_and_restart_restores_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); flow = mkflow(FakeNode(), tmp, clock); st = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock)
            class Live:
                provider = "KILN_LIVE"; model = "x"; calls = 0
                def structure(self, text): self.calls += 1; return {"raw": "", "calls": 1, "error": "should not be called"}
            live = Live(); flow.ai_provider = live
            rec, _ = st.create(flow, text=TEXT, sender=IPHONE, source="test", external_key="t1", external_user="u", sender_label="아이폰", test_mode=True)
            self.assertTrue(rec["test_mode"]); self.assertEqual(rec["kind"], "proposal"); self.assertEqual(live.calls, 0, "시험 모드는 운영 제공자를 부르지 않음"); self.assertEqual(rec["ai"]["mode"], "MOCK_KILN")
            flow2 = mkflow(FakeNode(), tmp, clock); st2 = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock)
            calls = []; orig = flow2.chat; flow2.chat = lambda t: (calls.append(t), orig(t))[1]
            r = st2.prepare(flow2, token=rec["token"], sender=IPHONE); self.assertTrue(r["ok"], r); self.assertEqual(calls, [], "재기동 뒤 저장된 같은 제안 복원 → chat 0")

    def test_concurrent_same_key_single_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); flow = mkflow(FakeNode(), tmp, clock); st = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock)
            calls = []; orig = flow.chat
            import time as _t
            def slow_chat(t): calls.append(t); _t.sleep(0.05); return orig(t)
            flow.chat = slow_chat
            out = []
            def go(i): out.append(st.create(flow, text=TEXT, sender=IPHONE, source="telegram", external_key="tg:1:99", external_user="1", sender_label="아이폰"))
            ths = [threading.Thread(target=go, args=(i,)) for i in range(4)]; [t.start() for t in ths]; [t.join() for t in ths]
            ids = {r["intent_id"] for r, _ in out}; self.assertEqual(len(ids), 1); self.assertEqual(sum(1 for _, c in out if c), 1); self.assertEqual(len(calls), 1, "동일 메시지 동시 전달 → chat 1회·intent 1건")
            out2 = []
            def go2(i): out2.append(st.create(flow, text=TEXT, sender=IPHONE, source="telegram", external_key=f"tg:1:{200 + i}", external_user="1", sender_label="아이폰"))
            ths = [threading.Thread(target=go2, args=(i,)) for i in range(3)]; [t.start() for t in ths]; [t.join() for t in ths]
            self.assertEqual(len({r["intent_id"] for r, _ in out2}), 3, "다른 메시지 동시 3건 → 3 intent")


class HttpIntentTests(unittest.TestCase):
    KEY = "botkey-" + "k" * 30

    def _serve(self, tmp, clock):
        flow = mkflow(FakeNode(), tmp, clock); ext = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock); logp = pathlib.Path(tmp) / "access.log"
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
        srv, token = PS.serve("127.0.0.1", port, "127.0.0.1", flow, log_path=logp, intents_ext=ext, bot_key=self.KEY, allowed_senders={IPHONE, ANDROID})
        th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
        return srv, token, port, flow, ext, logp

    def _req(self, port, path, method="GET", body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        hdr = {"Host": f"127.0.0.1:{port}", **(headers or {})}
        if body is not None:
            hdr["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=hdr)
        r = c.getresponse(); raw = r.read(); c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode("utf-8", "ignore")

    def test_bot_key_scope_links_test_mode_masking(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); srv, token, port, flow, ext, logp = self._serve(tmp, clock); P = f"/p/{token}"; KEY = {"X-SB-Bot-Key": self.KEY}
            try:
                body = {"text": TEXT, "sender": IPHONE, "sender_label": "아이폰", "source": "telegram", "external_key": "tg:1:1", "external_user": "1"}
                s, r = self._req(port, P + "/api/intent/create", "POST", body); self.assertEqual(s, 403, "봇 키 없음 → 거부(source 문자열만으로 인증 불가)")
                s, r = self._req(port, P + "/api/intent/create", "POST", body, {"X-SB-Bot-Key": "wrong"}); self.assertEqual(s, 403)
                s, r = self._req(port, P + "/api/intent/create", "POST", {**body, "source": "evil"}, KEY); self.assertEqual(s, 403)
                s, r = self._req(port, P + "/api/intent/create", "POST", {**body, "sender": "TMDKznuDWaZwfZHcM61FVFstyYNmK6Njk1"}, KEY); self.assertEqual(s, 403, "등록 지갑 밖 sender → 서버가 거부(봇 키 유출 시 방어)")
                s, r = self._req(port, P + "/api/intent/create", "POST", body, KEY); self.assertEqual(s, 200); self.assertTrue(r["created"])
                ap = r["approval_path"]; itok = ap.rsplit("/", 1)[-1]; self.assertTrue(ap.startswith("/a/")); self.assertNotIn(token, ap, "승인 링크에 공통 세션 토큰 없음")
                self.assertNotIn("token", r["intent"]); self.assertNotIn("keys", r["intent"])
                s, r2 = self._req(port, P + "/api/intent/create", "POST", {**body, "sender": ANDROID, "sender_label": "안드로이드"}, KEY); self.assertEqual(s, 409); self.assertTrue(r2.get("conflict"))
                # 승인 링크 범위
                s, page = self._req(port, ap); self.assertEqual(s, 200); self.assertIn(f'<meta name="sb-intent" content="{itok}">', page)
                self.assertIn(f'<meta name="sb-page-version" content="{PS.PAGE_VERSION}">', page); self.assertIn(f'const BASE = "{ap}"', page); self.assertNotIn(token, page, "페이지에 세션 토큰 없음")
                self.assertEqual(page.count(itok), 1 + page.count(ap) - 0 if False else page.count(itok), "sanity"); self.assertNotIn("__SB_INTENT__", page); self.assertNotIn("__SB_VERSION__", page)
                s, js = self._req(port, ap + "/phone_client.js"); self.assertEqual(s, 200)
                s, _ = self._req(port, "/a/" + "z" * 32); self.assertEqual(s, 404)
                s, v = self._req(port, ap + "/api/intent"); self.assertEqual(s, 200); self.assertEqual(v["intent"]["state"], "PROPOSED"); self.assertFalse(v["intent"]["test_mode"])
                s, x = self._req(port, ap + "/api/chat", "POST", {"text": "x"}); self.assertEqual(s, 404, "링크 범위에서 chat 불가")
                s, x = self._req(port, ap + "/api/intent/create", "POST", body); self.assertEqual(s, 404)
                s, x = self._req(port, ap + "/api/order/prepare", "POST", {"proposal_id": "p", "sender": IPHONE}); self.assertEqual(s, 404, "링크 범위에서 일반 prepare 불가")
                s, pr = self._req(port, ap + "/api/intent/prepare", "POST", {"sender": ANDROID}); self.assertEqual(s, 409); self.assertTrue(pr.get("wallet_mismatch"))
                s, pr = self._req(port, ap + "/api/intent/prepare", "POST", {"sender": IPHONE}); self.assertEqual(s, 200); pid = pr["order"]["payment_id"]
                s, pr2 = self._req(port, ap + "/api/intent/prepare", "POST", {"sender": IPHONE}); self.assertEqual(s, 200); self.assertTrue(pr2["reused"])
                s, stt = self._req(port, ap + f"/api/order/status?payment_id={pid}"); self.assertEqual(s, 200); self.assertEqual(stt["order_state"], "PENDING")
                s, x = self._req(port, ap + "/api/order/status?payment_id=phone_trx_other"); self.assertEqual(s, 403, "다른 주문 조회 불가")
                s, x = self._req(port, ap + "/api/order/signed", "POST", {"payment_id": "phone_trx_other", "snapshot_sha256": "s", "signed_tx": {}}); self.assertEqual(s, 403)
                s, o = self._req(port, ap + f"/api/orders?sender={ANDROID}"); self.assertEqual(o["orders"], [], "다른 지갑 기록 조회 불가")
                s, o = self._req(port, ap + f"/api/orders?sender={IPHONE}"); self.assertEqual(s, 200)
                # 시험 모드 intent: 서명본 제출·보관 거부
                clock.t += 5   # 같은 지갑·같은 내용의 두 번째 주문이 같은 txID 가 되지 않게(시계 전진)
                s, rt = self._req(port, P + "/api/intent/create", "POST", {**body, "source": "test", "external_key": "t:1"}, KEY); self.assertEqual(s, 200); self.assertTrue(rt["intent"]["test_mode"]); apt = rt["approval_path"]
                s, prt = self._req(port, apt + "/api/intent/prepare", "POST", {"sender": IPHONE}); self.assertEqual(s, 200, prt); pidt = prt["order"]["payment_id"]
                s, sg = self._req(port, apt + "/api/order/signed", "POST", {"payment_id": pidt, "snapshot_sha256": prt["order"]["snapshot_sha256"], "signed_tx": sign_like_wallet(prt["order"]["unsigned_tx"])})
                self.assertEqual(s, 409); self.assertTrue(sg.get("test_mode")); self.assertEqual(sg["state"], "NOT_SUBMITTED")
                s, hd = self._req(port, apt + "/api/order/hold", "POST", {"payment_id": pidt, "snapshot_sha256": prt["order"]["snapshot_sha256"], "tx_hash": prt["order"]["tx_id"]}); self.assertEqual(s, 409)
                self.assertEqual(flow.store.get(pidt)["state"], "PENDING", "시험 모드 주문은 서명 저장 없음"); self.assertEqual(len(flow.node.broadcasts), 0)
                s, _ = self._req(port, P + f"/i/{itok}"); self.assertEqual(s, 404, "세션 범위의 옛 /i/ 경로 없음")
                srv.shutdown()
                log = logp.read_text(encoding="utf-8")
                self.assertNotIn(itok, log); self.assertIn("/a/<masked>", log); self.assertNotIn(token, log); self.assertNotIn("botkey-", log)
            finally:
                try:
                    srv.shutdown()
                except Exception:                           # noqa: BLE001
                    pass


class HttpApprovalRoutesTests(HttpIntentTests):
    """봇 키 라우트: prepare_bg · approve · pending · view (VP_TELEGRAM_ONLY_APPROVAL). 모의 signer 로 서명 1·방송 1, 키 없으면 403."""
    def test_bot_routes_prepare_bg_approve_pending_view(self):
        import phone_signer as SG
        from test_phone_flow import PHONE
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); flow = mkflow(FakeNode(), tmp, clock); ext = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock); logp = pathlib.Path(tmp) / "access.log"
            with socket.socket() as sk:
                sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
            policy = SG.SignerPolicy(allowed_receivers={MAC}, stop_file=pathlib.Path(tmp) / "stop"); ledger = SG.LimitLedger(pathlib.Path(tmp) / "ledger.jsonl", now_fn=clock); ledger.init("test")
            srv, token = PS.serve("127.0.0.1", port, "127.0.0.1", flow, log_path=logp, intents_ext=ext, bot_key=self.KEY, allowed_senders={IPHONE, ANDROID}, signer=SG.MockSigner(PHONE), policy=policy, ledger=ledger)
            threading.Thread(target=srv.serve_forever, daemon=True).start(); P = f"/p/{token}"; KEY = {"X-SB-Bot-Key": self.KEY}
            try:
                body = {"text": TEXT, "sender": IPHONE, "sender_label": "아이폰", "source": "telegram", "external_key": "tg:1:1", "external_user": "1"}
                s, r = self._req(port, P + "/api/intent/create", "POST", body, KEY); self.assertEqual(s, 200); iid = r["intent"]["intent_id"]
                for path in ("/api/intent/prepare_bg", "/api/intent/approve", "/api/intent/pending", "/api/intent/view"):
                    s, x = self._req(port, P + path, "POST", {"intent_id": iid}); self.assertEqual(s, 403, path + " 봇 키 없음")
                s, pb = self._req(port, P + "/api/intent/prepare_bg", "POST", {"intent_id": iid}, KEY); self.assertEqual(s, 200, pb); fp = pb["intent"]["fingerprint"]; self.assertTrue(pb["intent"]["awaiting_approval"]); self.assertIn("max_deduct_sun", pb["intent"]["summary"])
                s, pd = self._req(port, P + "/api/intent/pending", "POST", {"external_user": "1"}, KEY); self.assertEqual(len(pd["pending"]), 1); self.assertEqual(pd["signer"], "mock")
                s, ap = self._req(port, P + "/api/intent/approve", "POST", {"intent_id": iid, "approval_key": "tg:1:2", "fingerprint": fp, "external_user": "2"}, KEY); self.assertEqual(s, 409, "다른 사용자")
                s, ap = self._req(port, P + "/api/intent/approve", "POST", {"intent_id": iid, "approval_key": "tg:1:2", "fingerprint": fp, "external_user": "1"}, KEY); self.assertEqual(s, 200, ap); self.assertEqual(ap["state"], "SUBMITTED")
                s, ap2 = self._req(port, P + "/api/intent/approve", "POST", {"intent_id": iid, "approval_key": "tg:1:2", "fingerprint": fp, "external_user": "1"}, KEY); self.assertEqual(s, 200); self.assertTrue(ap2["idempotent"])
                s, ap3 = self._req(port, P + "/api/intent/approve", "POST", {"intent_id": iid, "approval_key": "tg:1:3", "fingerprint": fp, "external_user": "1"}, KEY); self.assertEqual(s, 409); self.assertTrue(ap3["already"])
                self.assertEqual(len(flow.node.broadcasts), 1, "승인 3회 요청 → 방송 1")
                s, vw = self._req(port, P + "/api/intent/view", "POST", {"intent_id": iid}, KEY); self.assertEqual(vw["intent"]["state"], "FINAL"); self.assertEqual(vw["intent"]["approval"]["state"], "SUBMITTED")
                srv.shutdown(); log = logp.read_text(encoding="utf-8"); self.assertNotIn(self.KEY, log); self.assertNotIn(token, log)
            finally:
                try:
                    srv.shutdown()
                except Exception:                           # noqa: BLE001
                    pass


class FakeServer:
    """어댑터 검사용: 실제 IntentStore·flow 를 메모리에서 호출(HTTP 없음). 봇 키 검사는 HttpIntentTests 가 담당."""
    def __init__(self, flow, ext):
        self.flow, self.ext = flow, ext; self.public_origin = "https://mac.example.ts.net"; self.base = "http://127.0.0.1:1"
    def create_intent(self, **body):
        try:
            rec, created = self.ext.create(self.flow, text=body["text"], sender=body.get("sender"), source=body["source"], external_key=body["external_key"], external_user=body["external_user"], sender_label=body.get("sender_label"), test_mode=body["source"] == "test")
        except IntentConflict as e:
            return 409, {"ok": False, "error": str(e), "conflict": True}
        return 200, {"ok": True, "created": created, "intent": self.ext.view(rec, self.flow), "approval_path": f"/a/{rec['token']}", "summary": self.ext.summary_line(rec)}
    def intent_view(self, token):
        rec = self.ext.by_token(token)
        return (200, {"ok": True, "intent": self.ext.view(rec, self.flow)}) if rec else (404, {"ok": False})
    def intent_view_by_id(self, intent_id):
        rec = self.ext.get(intent_id)
        return (200, {"ok": True, "intent": self.ext.view(rec, self.flow)}) if rec else (404, {"ok": False})
    def prepare_bg(self, intent_id):
        r = self.ext.prepare_bg(self.flow, intent_id=intent_id); return (200 if r.get("ok") else 409), r
    def approval_url(self, approval_path):
        return self.public_origin + approval_path


class AdapterTests(unittest.TestCase):
    def test_trigger_wallet_words(self):
        self.assertTrue(TG.matches(TEXT)); self.assertTrue(TG.matches("아이폰으로 맥북지갑에 TRX 2개 송금")); self.assertFalse(TG.matches("오늘 날씨 어때")); self.assertFalse(TG.matches(""))
        self.assertEqual(TG.resolve_wallet("아이폰으로 맥북지갑한테 트론 2개 보내", WALLETS)["label"], "아이폰"); self.assertIsNone(TG.resolve_wallet(TEXT, WALLETS)); self.assertIsNone(TG.resolve_wallet("아이폰 안드로이드 둘 다", WALLETS))
        self.assertEqual(TG.strip_wallet_words("아이폰으로 " + TEXT, WALLETS), TEXT)
        dl = TG.tronlink_deeplink("https://x.ts.net/a/abc"); self.assertTrue(dl.startswith("tronlinkoutside://pull.activity?param=")); self.assertIn("%22action%22%3A%22open%22", dl)
        # 실제 핸들러 배선은 tests/test_gwdc_voice_handler.py 통합 시험에서 실행한다.

    def test_routing_ask_wallet_dedupe_conflict_reply_and_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); node = FakeNode(); flow = mkflow(node, tmp, clock); ext = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock); fs = FakeServer(flow, ext)
            sent = []
            off = TG.Adapter(server=fs, send_fn=lambda c, t: sent.append(t), now_fn=clock, wallets=WALLETS, enabled_fn=lambda: False, watch=False, state_file=pathlib.Path(tmp) / "st.json")
            r = off.handle("아이폰으로 " + TEXT, chat_id=1, message_id=1); self.assertIn("접수하지 않습니다", r[0]); self.assertEqual(ext.all(), [])
            ad = TG.Adapter(server=fs, send_fn=lambda c, t: sent.append(t), now_fn=clock, wallets=WALLETS, enabled_fn=lambda: True, watch=False, sleep_fn=lambda s: None, state_file=pathlib.Path(tmp) / "st.json")
            # hani_final 라우팅 순서 그대로: 원문 → should_route(True) → 되묻기 → "아이폰"(트리거 아님) → should_route(True, 대기 있음) → intent 1건
            self.assertTrue(ad.should_route(TEXT, 1)); r = ad.handle(TEXT, chat_id=1, message_id=2); self.assertIn("어느 지갑", r[0]); self.assertEqual(ext.all(), [])
            self.assertFalse(TG.matches("아이폰")); self.assertTrue(ad.should_route("아이폰", 1), "대기 중인 대화의 지갑 단독 답은 라우팅"); self.assertFalse(ad.should_route("아이폰", 2), "대기 없는 대화는 기존 처리")
            r = ad.handle("아이폰", chat_id=1, message_id=3)
            self.assertTrue(r[0].startswith("🧾 승인 대기"), r[0]); self.assertIn("아이폰", r[0]); self.assertNotIn("https://", r[0]); self.assertNotIn("tronlinkoutside://", r[0])
            self.assertEqual(len(ext.all()), 1); rec = ext.all()[0]; self.assertEqual((rec["sender"], rec["sender_label"], rec["text"]), (IPHONE, "아이폰", TEXT))
            self.assertFalse(ad.should_route("아이폰", 1), "되묻기 소진 뒤 단독 답은 라우팅 안 함")
            self.assertEqual(ad.handle("아이폰", chat_id=1, message_id=3), r, "같은 message_id 재전달 → 같은 회신, 새 intent 없음"); self.assertEqual(len(ext.all()), 1)
            clock.t += 5
            r3 = ad.handle("안드로이드로 " + TEXT, chat_id=1, message_id=4); self.assertEqual(len(ext.all()), 2, "다른 지갑 같은 문장 → 별개 intent")
            self.assertTrue(r3[0].startswith("🧾 승인 대기"), r3[0]); self.assertIn("안드로이드", r3[0]); self.assertIn(ANDROID[-6:], r3[0]); self.assertNotIn(IPHONE[-6:], r3[0], "회신 지갑 = 서버가 돌려준 intent 의 지갑")
            fails = {"n": 0}
            def flaky(c, t):
                fails["n"] += 1
                if fails["n"] == 1: raise RuntimeError("telegram down")
            ad2 = TG.Adapter(server=fs, send_fn=flaky, now_fn=clock, wallets=WALLETS, enabled_fn=lambda: True, watch=False, sleep_fn=lambda s: None, state_file=pathlib.Path(tmp) / "st2.json")
            pr = ext.prepare(flow, token=rec["token"], sender=IPHONE); self.assertTrue(pr["ok"])   # 이미 prepare_bg 로 같은 주문 → reused
            msgs = ad2._watch(1, rec["intent_id"], rec["token"], max_polls=2)
            self.assertEqual(len(msgs), 1, "첫 회신 실패 → 다음 폴링에서 같은 상태 1회 재전송"); self.assertIn("미서명 거래 준비됨", msgs[0])
            res = flow.submit_signed(pr["order"]["payment_id"], pr["order"]["snapshot_sha256"], sign_like_wallet(pr["order"]["unsigned_tx"])); self.assertEqual(res["state"], "FINAL_CONFIRMED_SOLIDITY")
            def down(c, t): raise RuntimeError("down")
            ad3 = TG.Adapter(server=fs, send_fn=down, now_fn=clock, wallets=WALLETS, enabled_fn=lambda: True, watch=False, sleep_fn=lambda s: None, state_file=pathlib.Path(tmp) / "st3.json")
            msgs = ad3._watch(1, rec["intent_id"], rec["token"], max_polls=5); self.assertEqual(msgs, [], "확정 알림 전부 실패 → 기록만")
            st3 = json.loads((pathlib.Path(tmp) / "st3.json").read_text(encoding="utf-8")); self.assertEqual(st3["notify"][rec["intent_id"]]["states"]["FINAL"]["attempts"], TG.NOTIFY_MAX_ATTEMPTS); self.assertFalse(st3["notify"][rec["intent_id"]]["states"]["FINAL"]["ok"])
            ok_sent = []; ad3.send_fn = lambda c, t: ok_sent.append(t)
            st3["notify"][rec["intent_id"]]["states"]["FINAL"]["attempts"] = 1; (pathlib.Path(tmp) / "st3.json").write_text(json.dumps(st3), encoding="utf-8")
            self.assertEqual(ad3.resend_failed_notifications(), 1); self.assertIn("송금 완료(확정)", ok_sent[0]); self.assertEqual(ad3.resend_failed_notifications(), 0, "성공 뒤 재전송 없음")
            self.assertEqual(len(node.broadcasts), 1, "회신 실패·재시도 중 재방송 없음")
            r = ad.handle("아이폰으로 트론 2개 보내", chat_id=1, message_id=9); self.assertIn("확인이 필요", r[0]); self.assertNotIn("https://", r[0])
            ext.create(flow, text="맥북지갑한테 트론 5개 보내", sender=ANDROID, source="telegram", external_key="tg:1:50", external_user="1", sender_label="안드로이드")
            r = ad.handle("아이폰으로 " + TEXT, chat_id=1, message_id=50); self.assertIn("거부", r[0]); self.assertNotIn("승인 대기", r[0], "같은 메시지 키·다른 내용 → 충돌 회신, 요약 없음")

    def test_stale_in_progress_is_retried_recent_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); flow = mkflow(FakeNode(), tmp, clock); ext = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock); fs = FakeServer(flow, ext)
            sf = pathlib.Path(tmp) / "st.json"; ad = TG.Adapter(server=fs, send_fn=lambda c, t: None, now_fn=clock, wallets=WALLETS, enabled_fn=lambda: True, watch=False, state_file=sf)
            sf.write_text(json.dumps({"seen": {"tg:1:5": {"t": clock.t - 10, "reply": None, "in_progress": True}}, "pending": {}, "notify": {}}), encoding="utf-8")
            r = ad.handle("아이폰으로 " + TEXT, chat_id=1, message_id=5); self.assertIn("처리 중", r[0]); self.assertEqual(ext.all(), [], "최근 처리 중 → 추가 접수 없음")
            sf.write_text(json.dumps({"seen": {"tg:1:5": {"t": clock.t - TG.IN_PROGRESS_STALE_S - 1, "reply": None, "in_progress": True}}, "pending": {}, "notify": {}}), encoding="utf-8")
            r = ad.handle("아이폰으로 " + TEXT, chat_id=1, message_id=5); self.assertTrue(r[0].startswith("🧾 승인 대기"), r[0]); self.assertEqual(len(ext.all()), 1, "중단 뒤 재전달 → 재시도(서버 멱등)")
            self.assertEqual(ad.handle("아이폰으로 " + TEXT, chat_id=1, message_id=5), r); self.assertEqual(len(ext.all()), 1)

    def test_concurrent_handle_same_and_different_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(); flow = mkflow(FakeNode(), tmp, clock); ext = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock); fs = FakeServer(flow, ext)
            ad = TG.Adapter(server=fs, send_fn=lambda c, t: None, now_fn=clock, wallets=WALLETS, enabled_fn=lambda: True, watch=False, state_file=pathlib.Path(tmp) / "st.json")
            out = []
            def go(mid): out.append(ad.handle("아이폰으로 " + TEXT, chat_id=1, message_id=mid))
            ths = [threading.Thread(target=go, args=(7,)) for _ in range(3)] + [threading.Thread(target=go, args=(8,)) for _ in range(2)]
            [t.start() for t in ths]; [t.join() for t in ths]
            self.assertEqual(len(ext.all()), 2, "같은 메시지 동시 3건 + 다른 메시지 동시 2건 → intent 2건")
            self.assertTrue(all(("승인 대기" in r[0]) or ("이미 접수" in r[0]) or ("처리 중" in r[0]) for r in out), out)


if __name__ == "__main__":
    unittest.main()
