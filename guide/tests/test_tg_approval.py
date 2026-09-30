"""Telegram '승인' 답장만으로 송금 (VP_TELEGRAM_ONLY_APPROVAL_20260929 §1~§7) 모의 인수시험. 네트워크·실제 Telegram·실지갑·실호출 0.
한 문장 → 백그라운드 준비(지갑 연결 없음) → 짧은 요약(URL 없음) → '승인' 답장 → 모의 signer 서명 1 → 방송 1 → 같은 txID 확정 → 완료 회신.
검증: 중복 승인·동시 승인·다른 사용자·잘못된 답장·여러 대기·만료·조건 변경(지문)·응답 유실(UNKNOWN → 새 거래 없음)·알림 실패 복구·전달/음성 승인 거부·취소·정책 한도·서명 주체 미정."""
import json
import pathlib
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE.parents[3]))
from phone_flow import PhoneFlow  # noqa: E402
from phone_intent import IntentStore, IntentConflict  # noqa: E402
import phone_signer as SG  # noqa: E402
from order_store import OrderStore  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402
from test_phone_flow import FakeNode, CONTACTS, SENDER, MAC, T0, PHONE  # noqa: E402
import gwdc_tg_adapter as TG  # noqa: E402

IPHONE = SENDER
ANDROID = "TJ1aFHjZsTyDtpUkwHXkPEC8Ay4w6ixHFY"
WALLETS = {"default": None, "wallets": [{"label": "아이폰", "address": IPHONE, "words": ["아이폰", "아이폰으로"]}, {"label": "안드로이드", "address": ANDROID, "words": ["안드로이드", "안드로이드로"]}]}
TEXT = "맥북지갑한테 트론 2개 보내 트론링크앱 사용해서"
USER = "100000001"   # placeholder Telegram user id (tests only)


class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t


def mkflow(node, tmp, clock):
    root = pathlib.Path(tmp)
    return PhoneFlow(node=node, store=OrderStore(root / "pending"), intents=IntentLog(root / "logs" / "phone_intents.jsonl"), results_dir=root / "logs" / "phone_results",
                     contacts=CONTACTS, now_fn=clock, sleep_fn=lambda s: None, receipt_polls=2, poll_s=0)


class FakeServer:
    """어댑터 ↔ 서버 경계를 메모리에서 재현(봇 키 검사는 HTTP 검사에서). 실제 IntentStore·flow·signer·policy 사용."""
    def __init__(self, flow, ext, signer, policy, ledger=None):
        self.flow, self.ext, self.signer, self.policy, self.ledger = flow, ext, signer, policy, ledger; self.public_origin = "https://mac.example.ts.net"; self.base = "http://127.0.0.1:1"
    def create_intent(self, **b):
        try:
            rec, created = self.ext.create(self.flow, text=b["text"], sender=b.get("sender"), source=b["source"], external_key=b["external_key"], external_user=b["external_user"], sender_label=b.get("sender_label"))
        except IntentConflict as e:
            return 409, {"ok": False, "error": str(e), "conflict": True}
        return 200, {"ok": True, "created": created, "intent": self.ext.view(rec, self.flow), "approval_path": f"/a/{rec['token']}", "summary": self.ext.summary_line(rec)}
    def prepare_bg(self, intent_id):
        r = self.ext.prepare_bg(self.flow, intent_id=intent_id); return (200 if r.get("ok") else 409), r
    def approve(self, **b):
        r = self.ext.approve(self.flow, self.signer, self.policy, intent_id=b["intent_id"], approval_key=b["approval_key"], fingerprint=b["fingerprint"], external_user=b["external_user"], summary_msg_id=b.get("summary_msg_id"), ledger=self.ledger); return (200 if r.get("ok") else 409), r
    def pending(self, external_user):
        return 200, {"ok": True, "pending": [self.ext.view(x, self.flow) for x in self.ext.pending_for_user(self.flow, external_user)]}
    def intent_view_by_id(self, intent_id):
        rec = self.ext.get(intent_id); return (200, {"ok": True, "intent": self.ext.view(rec, self.flow)}) if rec else (404, {"ok": False})
    def intent_view(self, token):
        rec = self.ext.by_token(token); return (200, {"ok": True, "intent": self.ext.view(rec, self.flow)}) if rec else (404, {"ok": False})
    def approval_url(self, p): return self.public_origin + p


def setup(tmp, node=None, signer=None, policy=None):
    clock = Clock(); node = node or FakeNode(); flow = mkflow(node, tmp, clock); ext = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock)
    signer = signer or SG.MockSigner(PHONE)
    policy = policy or SG.SignerPolicy(allowed_receivers={MAC}, stop_file=pathlib.Path(tmp) / "stop")
    ledger = SG.LimitLedger(pathlib.Path(tmp) / "ledger.jsonl", now_fn=clock); ledger.init("test")
    fs = FakeServer(flow, ext, signer, policy, ledger); sent = []
    ad = TG.Adapter(server=fs, send_fn=lambda c, t: sent.append(t), now_fn=clock, wallets=WALLETS, enabled_fn=lambda: True, watch=False, sleep_fn=lambda s: None, state_file=pathlib.Path(tmp) / "st.json")
    return clock, node, flow, ext, fs, ad, sent


class ApprovalFlowTests(unittest.TestCase):
    def test_happy_path_one_sentence_to_final_and_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, fs, ad, sent = setup(tmp)
            self.assertTrue(ad.should_route("아이폰으로 " + TEXT, USER))
            r = ad.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]
            self.assertTrue(r.startswith("🧾 승인 대기"), r); self.assertNotIn("http", r); self.assertNotIn("tronlinkoutside", r); self.assertIn("2 TRX", r); self.assertIn(MAC, r); self.assertIn("최대 차감", r); self.assertIn("승인 만료", r)
            rec = ext.all()[0]; self.assertTrue(rec["awaiting_approval"]); self.assertEqual(flow.store.get(rec["payment_id"])["state"], "PENDING"); self.assertEqual(len(node.broadcasts), 0, "요약 단계: 서명·방송 0")
            ad.note_sent(USER, 900, r)                                                     # 봇이 보낸 요약 메시지 ID
            self.assertFalse(TG.matches("승인")); self.assertTrue(ad.should_route("승인", USER, 900)); self.assertTrue(ad.should_route("승인", USER))
            self.assertTrue(ad.should_route("승인하지 마", USER, 900), "부정은 송금 경로에서 차단하며 일반 AI로 보내지 않음")
            out = ad.handle("승인", chat_id=USER, message_id=2, reply_to_id=900)[0]
            self.assertIn("승인 접수", out); self.assertNotIn("서명 1회·전송 1회", out)
            self.assertEqual(len(node.broadcasts), 1); rec = ext.get(rec["intent_id"]); self.assertEqual(rec["approval"]["state"], "SUBMITTED"); self.assertEqual(rec["approval"]["key"], f"tg:{USER}:2")
            v = ext.view(rec, flow); self.assertEqual(v["state"], "FINAL"); self.assertEqual(v["result"]["state"], "FINAL_CONFIRMED_SOLIDITY")
            msgs = ad._watch(USER, rec["intent_id"], "", max_polls=2); self.assertTrue(any("송금 완료(확정)" in m for m in msgs), msgs)
            self.assertEqual(ad.handle("승인", chat_id=USER, message_id=2, reply_to_id=900)[0], out, "같은 승인 메시지 재전달 → 같은 회신·추가 실행 0"); self.assertEqual(len(node.broadcasts), 1)
            out2 = ad.handle("승인", chat_id=USER, message_id=3, reply_to_id=900)[0]; self.assertIn("없습니다", out2); self.assertEqual(len(node.broadcasts), 1, "두 번째 승인 → 대기 없음, 실행 0")
            self.assertEqual(json.loads((pathlib.Path(tmp) / "ledger.jsonl").read_text().splitlines()[0])["amount_sun"], 2000000)

    def test_wrong_reply_other_user_multiple_pending_forwarded_voice_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, fs, ad, sent = setup(tmp)
            r1 = ad.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]; ad.note_sent(USER, 901, r1)
            self.assertIn("전달/인용", ad.handle("승인", chat_id=USER, message_id=2, reply_to_id=901, forwarded=True)[0]); self.assertEqual(len(node.broadcasts), 0)
            self.assertIn("음성", ad.handle("승인", chat_id=USER, message_id=3, reply_to_id=901, is_text=False)[0]); self.assertEqual(len(node.broadcasts), 0)
            self.assertIn("요약이 아닙니다", ad.handle("승인", chat_id=USER, message_id=4, reply_to_id=123)[0]); self.assertEqual(len(node.broadcasts), 0, "다른 메시지에 답장 → 실행 0")
            self.assertFalse(ad.should_route("승인", "999")); self.assertEqual(ext.pending_for_user(flow, "999"), [])
            # 다른 사용자가 같은 intent 를 승인 시도(서버 검사)
            rec = ext.all()[0]; r = ext.approve(flow, fs.signer, fs.policy, intent_id=rec["intent_id"], approval_key="tg:999:1", fingerprint=rec["fingerprint"], external_user="999", ledger=fs.ledger)
            self.assertFalse(r["ok"]); self.assertTrue(r.get("refused")); self.assertEqual(len(node.broadcasts), 0)
            # 여러 대기: 두 번째 요청(다른 지갑) → 일반 '승인' 거절, 답장 승인만
            clock.t += 5
            r2 = ad.handle("안드로이드로 " + TEXT, chat_id=USER, message_id=5)[0]; self.assertTrue(r2.startswith("🧾 승인 대기"), r2); ad.note_sent(USER, 902, r2)
            self.assertEqual(len(ext.pending_for_user(flow, USER)), 2, "다른 지갑 → 대기 2건")
            self.assertIn("2건", ad.handle("승인", chat_id=USER, message_id=6)[0]); self.assertEqual(len(node.broadcasts), 0, "여러 대기 + 일반 승인 → 실행 0")
            r3 = ad.handle("아이폰으로 맥북지갑한테 트론 1개 보내", chat_id=USER, message_id=7)[0]; self.assertTrue(r3.startswith("🧾 승인 대기")); ad.note_sent(USER, 903, r3)
            self.assertEqual(len(ext.pending_for_user(flow, USER)), 2, "같은 지갑의 새 주문은 이전 PENDING 을 대체(supersede) → 아이폰 1 + 안드로이드 1")
            out = ad.handle("승인", chat_id=USER, message_id=8, reply_to_id=901)[0]      # 대체된(취소된) 첫 요약에 답장 → 실행 불가
            self.assertIn("실행하지 못했습니다", out); self.assertEqual(len(node.broadcasts), 0)
            self.assertIn("취소했습니다", ad.handle("취소", chat_id=USER, message_id=9, reply_to_id=903)[0]); self.assertEqual(len(node.broadcasts), 0)
            self.assertIn("요약이 아닙니다", ad.handle("승인", chat_id=USER, message_id=10, reply_to_id=903)[0], "취소 뒤 그 요약에 승인 → 실행 없음")

    def test_expiry_fingerprint_change_and_concurrent_approvals(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, fs, ad, sent = setup(tmp)
            r = ad.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]; ad.note_sent(USER, 901, r); rec = ext.all()[0]
            bad = ext.approve(flow, fs.signer, fs.policy, intent_id=rec["intent_id"], approval_key="tg:%s:2" % USER, fingerprint="0" * 64, external_user=USER, ledger=fs.ledger)
            self.assertFalse(bad["ok"]); self.assertTrue(bad.get("refused")); self.assertEqual(len(node.broadcasts), 0, "지문 불일치(내용 변경) → 실행 0")
            outs = []
            def go(i): outs.append(ext.approve(flow, fs.signer, fs.policy, intent_id=rec["intent_id"], approval_key=f"tg:{USER}:{10 + i}", fingerprint=rec["fingerprint"], external_user=USER, ledger=fs.ledger))
            ths = [threading.Thread(target=go, args=(i,)) for i in range(4)]; [t.start() for t in ths]; [t.join() for t in ths]
            self.assertEqual(sum(1 for o in outs if o["ok"] and not o.get("idempotent")), 1); self.assertEqual(len(node.broadcasts), 1, "동시 승인 4건 → 서명·방송 1")
            self.assertTrue(all(o.get("already") for o in outs if not o["ok"]))
            # 만료
            r2 = ad.handle("아이폰으로 맥북지갑한테 트론 1개 보내", chat_id=USER, message_id=20)[0]; self.assertTrue(r2.startswith("🧾 승인 대기"))
            rec2 = [x for x in ext.all() if x["intent_id"] != rec["intent_id"]][0]
            clock.t = int(rec2["approval_expires_at"]) + 1
            self.assertIn("없습니다", ad.handle("승인", chat_id=USER, message_id=21)[0]); self.assertEqual(len(node.broadcasts), 1, "만료 뒤 승인 → 실행 0")

    def test_unknown_result_no_new_tx_and_resume_after_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(receipt_mode="not_found")
            clock, node, flow, ext, fs, ad, sent = setup(tmp, node=node)
            r = ad.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]; ad.note_sent(USER, 901, r); rec = ext.all()[0]
            out = ad.handle("승인", chat_id=USER, message_id=2, reply_to_id=901)[0]
            self.assertEqual(len(node.broadcasts), 1)
            v = ext.view(ext.get(rec["intent_id"]), flow); self.assertIn(v["state"], ("UNKNOWN", "ORDER_SIGNED")); self.assertNotEqual(v["state"], "FINAL")
            msgs = ad._watch(USER, rec["intent_id"], "", max_polls=3); self.assertTrue(any("다시 보내지 마세요" in m or "확인 중" in m for m in msgs), msgs)
            self.assertEqual(len(node.broadcasts), 1, "응답 유실/불명 → 새 거래·재방송 0(같은 txID 조회만)")
            # 재시작 복구: APPROVED 기록 뒤 실행 전 중단 흉내 → resume 는 서명 저장 여부로 판단
            with tempfile.TemporaryDirectory() as tmp2:
                clock2, node2, flow2, ext2, fs2, ad2, _ = setup(tmp2)
                r = ad2.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]; rec2 = ext2.all()[0]
                p = ext2._path(rec2["intent_id"]); d = json.loads(p.read_text(encoding="utf-8"))
                d["approval"] = {"key": f"tg:{USER}:2", "t": clock2(), "state": "APPROVED", "result": None}; d["awaiting_approval"] = False; p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                res = ext2.resume_approved(flow2, fs2.signer, fs2.policy, intent_id=rec2["intent_id"], ledger=fs2.ledger); self.assertTrue(res["ok"]); self.assertEqual(len(node2.broadcasts), 1, "중단 복구(승인 기록만, 실행 미시작): 같은 거래 1회 실행")
                res2 = ext2.resume_approved(flow2, fs2.signer, fs2.policy, intent_id=rec2["intent_id"], ledger=fs2.ledger); self.assertEqual(len(node2.broadcasts), 1, "이미 실행됨 → 조회만"); self.assertTrue(res2.get("status_only") or res2["state"] == "SUBMITTED")
                again = ext2.approve(flow2, fs2.signer, fs2.policy, intent_id=rec2["intent_id"], approval_key=f"tg:{USER}:2", fingerprint=rec2["fingerprint"], external_user=USER, ledger=fs2.ledger)
                self.assertTrue(again.get("idempotent")); self.assertEqual(len(node2.broadcasts), 1)

    def test_policy_limits_and_no_signer(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = SG.SignerPolicy(allowed_receivers={MAC}, per_tx_max_sun=1_000_000, stop_file=pathlib.Path(tmp) / "stop")
            clock, node, flow, ext, fs, ad, sent = setup(tmp, policy=policy)
            r = ad.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]; ad.note_sent(USER, 901, r)
            out = ad.handle("승인", chat_id=USER, message_id=2, reply_to_id=901)[0]; self.assertIn("실행하지 못했습니다", out); self.assertEqual(len(node.broadcasts), 0, "건별 한도 초과 → 서명·방송 0")
            self.assertEqual(ext.all()[0]["approval"]["state"], "POLICY_REFUSED")
            with tempfile.TemporaryDirectory() as tmp2:
                clock2, node2, flow2, ext2, fs2, ad2, _ = setup(tmp2, signer=SG.NoSigner())
                r = ad2.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]; ad2.note_sent(USER, 901, r)
                out = ad2.handle("승인", chat_id=USER, message_id=2, reply_to_id=901)[0]; self.assertIn("실행하지 못했습니다", out); self.assertEqual(len(node2.broadcasts), 0, "서명 주체 미정 → 실행 0")
                self.assertEqual(ext2.all()[0]["approval"]["state"], "SIGN_FAILED")
            with tempfile.TemporaryDirectory() as tmp3:
                clock3, node3, flow3, ext3, fs3, ad3, _ = setup(tmp3); (pathlib.Path(tmp3) / "stop").write_text("stop")
                r = ad3.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]; ad3.note_sent(USER, 901, r)
                ad3.handle("승인", chat_id=USER, message_id=2, reply_to_id=901); self.assertEqual(len(node3.broadcasts), 0, "중지 파일 → 실행 0")

    def test_prepare_failure_is_not_awaiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, fs, ad, sent = setup(tmp, node=FakeNode(balance=1_000_000))
            r = ad.handle("아이폰으로 " + TEXT, chat_id=USER, message_id=1)[0]
            self.assertIn("준비 실패", r); self.assertIn("잔액 부족", r); self.assertFalse(ad.should_route("승인", USER)); self.assertEqual(ext.pending_for_user(flow, USER), [])
            self.assertIn("없습니다", ad.handle("승인", chat_id=USER, message_id=2)[0]) if ad.should_route("승인", USER) else None


if __name__ == "__main__":
    unittest.main()
