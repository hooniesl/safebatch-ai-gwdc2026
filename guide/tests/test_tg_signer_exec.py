"""VP_REVIEW_TG_ONLY_SIGNER §1(한도 사전 원자 예약)·§2(승인/복구 단일 실행 경로) + A안 LocalKeySigner 모의 검사. 실제 서명 주체·네트워크·실호출 0.
서명·방송·예약 건수는 MockSigner.sign_count / FakeNode.broadcasts / 원장 항목으로 입증한다."""
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE.parents[3]))
from phone_flow import PhoneFlow  # noqa: E402
from phone_intent import IntentStore  # noqa: E402
import phone_signer as SG  # noqa: E402
from order_store import OrderStore  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402
from test_phone_flow import FakeNode, CONTACTS, SENDER, MAC, T0, PHONE  # noqa: E402

USER = "100000001"   # placeholder Telegram user id (tests only)


class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t


def mkflow(node, tmp, clock):
    root = pathlib.Path(tmp)
    return PhoneFlow(node=node, store=OrderStore(root / "pending"), intents=IntentLog(root / "logs" / "phone_intents.jsonl"), results_dir=root / "logs" / "phone_results",
                     contacts=CONTACTS, now_fn=clock, sleep_fn=lambda s: None, receipt_polls=2, poll_s=0)


def setup(tmp, node=None, signer=None, daily=10_000_000, init_ledger=True):
    clock = Clock(); node = node or FakeNode(); flow = mkflow(node, tmp, clock); ext = IntentStore(pathlib.Path(tmp) / "ext", now_fn=clock)
    signer = signer or SG.MockSigner(PHONE)
    policy = SG.SignerPolicy(allowed_receivers={MAC}, daily_total_max_sun=daily, stop_file=pathlib.Path(tmp) / "stop")
    ledger = SG.LimitLedger(pathlib.Path(tmp) / "ledger.jsonl", now_fn=clock)
    if init_ledger:
        ledger.init("test")
    return clock, node, flow, ext, signer, policy, ledger


def make_awaiting(ext, flow, text="맥북지갑한테 트론 2개 보내", key="tg:1:1"):
    rec, _ = ext.create(flow, text=text, sender=SENDER, source="telegram", external_key=key, external_user=USER, sender_label="아이폰")
    r = ext.prepare_bg(flow, intent_id=rec["intent_id"]); assert r["ok"], r
    return ext.get(rec["intent_id"])


def approve(ext, flow, signer, policy, ledger, rec, key="tg:%s:2" % USER):
    return ext.approve(flow, signer, policy, intent_id=rec["intent_id"], approval_key=key, fingerprint=rec["fingerprint"], external_user=USER, ledger=ledger)


def order_of(ext, flow, rec):
    return flow.store.get(ext.get(rec["intent_id"])["payment_id"])["order"]


class LedgerTests(unittest.TestCase):
    def test_reserve_atomic_over_cap_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp, daily=4_500_000)     # 2 TRX+2 TRX 상한 = 4 TRX 비용 → 두 건이면 초과
            r1 = make_awaiting(ext, flow, key="k1"); o1 = order_of(ext, flow, r1)
            o2 = {**o1, "payment_id": "phone_trx_other", "tx_id": "b" * 64}
            outs = []
            def go(o): outs.append(ledger.reserve(o, policy))
            ths = [threading.Thread(target=go, args=(o,)) for o in (o1, o2, o1, o2)]; [t.start() for t in ths]; [t.join() for t in ths]
            ok_new = [o for o in outs if o.get("ok") and not o.get("existing")]; ok_existing = [o for o in outs if o.get("ok") and o.get("existing")]; refused = [o for o in outs if not o.get("ok")]
            self.assertEqual(len(ok_new), 1, outs); self.assertEqual(len(ok_existing), 1, "같은 주문 재예약 → 기존 항목(중복 차감 없음)"); self.assertEqual(len(refused), 2, "합계가 상한을 넘는 다른 주문은 거부")
            lines = [json.loads(l) for l in (pathlib.Path(tmp) / "ledger.jsonl").read_text().splitlines()]; self.assertEqual(len(lines), 1)
            self.assertEqual(ledger.state_of(lines[0]["payment_id"])["state"], "RESERVED")

    def test_uninitialized_and_corrupt_ledger_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp, init_ledger=False)
            r = make_awaiting(ext, flow)
            out = approve(ext, flow, signer, policy, ledger, r); self.assertFalse(out["ok"]); self.assertEqual(out["state"], "LIMIT_UNKNOWN"); self.assertEqual(signer.sign_count, 0); self.assertEqual(len(node.broadcasts), 0)
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp)
            (pathlib.Path(tmp) / "ledger.jsonl").write_text('{"payment_id": "x", "state": "RESERVED", "cost_sun": 1}\nnot json\n', encoding="utf-8")
            r = make_awaiting(ext, flow)
            out = approve(ext, flow, signer, policy, ledger, r); self.assertEqual(out["state"], "LIMIT_UNKNOWN"); self.assertIn("corrupt", out["error"]); self.assertEqual(signer.sign_count, 0)

    def test_reservation_survives_day_change_and_consume_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp, daily=4_500_000)
            r = make_awaiting(ext, flow); o = order_of(ext, flow, r)
            ledger.reserve(o, policy)
            t_saved = clock.t; clock.t += 86400 * 2
            cur = ledger._read(); self.assertEqual(ledger.used_sun(cur, SG._kst_day(clock.t)), o["amount_sun"] + o["fee_cap_sun"], "미종결 예약은 날짜가 바뀌어도 합산")
            clock.t = t_saved
            # 방송 성공 뒤 원장 소비 기록 전 중단 흉내: consume 이 실패해도 예약은 남고 실행 결과는 기록
            orig = ledger.consume
            calls = {"n": 0}
            def failing_consume(pid, reason="broadcast attempted"):
                calls["n"] += 1; raise SG.LedgerError("disk gone")
            ledger.consume = failing_consume
            out = approve(ext, flow, signer, policy, ledger, r); self.assertTrue(out["ok"]); self.assertEqual(out["state"], "SUBMITTED"); self.assertEqual(len(node.broadcasts), 1)
            self.assertIn("reservation kept", ext.get(r["intent_id"])["exec"]["ledger_note"]); self.assertEqual(ledger.state_of(o["payment_id"])["state"], "RESERVED", "소비 기록 실패 → 예약 유지(소실 없음)")
            ledger.consume = orig; ledger.consume(o["payment_id"]); self.assertEqual(ledger.state_of(o["payment_id"])["state"], "CONSUMED")
            res = ext.resume_approved(flow, signer, policy, intent_id=r["intent_id"], ledger=ledger); self.assertTrue(res["ok"]); self.assertEqual(len(node.broadcasts), 1); self.assertEqual(signer.sign_count, 1, "같은 주문 복구 → 새 예약·재서명 없음")
            self.assertEqual(len([l for l in (pathlib.Path(tmp) / "ledger.jsonl").read_text().splitlines() if l]), 2, "예약 1 + 소비 1, 복구로 새 예약 없음")


class SingleExecutionTests(unittest.TestCase):
    def test_slow_signer_with_concurrent_resume_signs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            signer = SG.MockSigner(PHONE, delay_fn=lambda: time.sleep(0.3))
            clock, node, flow, ext, signer, policy, ledger = setup(tmp, signer=signer)
            r = make_awaiting(ext, flow); outs = {}
            def do_approve(): outs["a"] = approve(ext, flow, signer, policy, ledger, r)
            def do_resume(): time.sleep(0.1); outs["r1"] = ext.resume_approved(flow, signer, policy, intent_id=r["intent_id"], ledger=ledger); outs["r2"] = ext.resume_approved(flow, signer, policy, intent_id=r["intent_id"], ledger=ledger)
            ta = threading.Thread(target=do_approve); tr = threading.Thread(target=do_resume); ta.start(); tr.start(); ta.join(); tr.join()
            self.assertTrue(outs["a"]["ok"]); self.assertTrue(outs["r1"].get("busy") or outs["r1"].get("status_only") or outs["r1"]["state"] == "SUBMITTED", outs["r1"])
            self.assertEqual(signer.sign_count, 1); self.assertEqual(len(node.broadcasts), 1, "느린 서명 중 복구 동시 호출 → 서명 1·방송 1")
            self.assertEqual(len([l for l in (pathlib.Path(tmp) / "ledger.jsonl").read_text().splitlines() if l]), 2, "예약 1·소비 1")

    def test_two_recoveries_race_after_signed_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp)
            r = make_awaiting(ext, flow); o = order_of(ext, flow, r)
            signed = signer.sign(o["unsigned_tx"], o["user_eoa"]); n0 = signer.sign_count
            ledger.reserve(o, policy)
            p = ext._path(r["intent_id"]); d = json.loads(p.read_text(encoding="utf-8"))
            d["approval"] = {"key": "tg:%s:2" % USER, "t": clock.t, "state": "APPROVED"}; d["awaiting_approval"] = False
            d["exec"] = {"stage": "SIGNED", "signed_tx": signed, "owner": "deadtoken", "pid": 999999, "attempts": 1}; p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            outs = []
            def go(): outs.append(ext.resume_approved(flow, signer, policy, intent_id=r["intent_id"], ledger=ledger))
            ths = [threading.Thread(target=go) for _ in range(2)]; [t.start() for t in ths]; [t.join() for t in ths]
            self.assertEqual(len(node.broadcasts), 1, "두 복구 경합 → 방송 1"); self.assertEqual(signer.sign_count, n0, "보관 서명본 재사용, 재서명 0")
            self.assertTrue(any(o.get("ok") for o in outs), outs)

    def test_interrupted_during_signing_goes_to_investigate(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp)
            r = make_awaiting(ext, flow); o = order_of(ext, flow, r); ledger.reserve(o, policy)
            p = ext._path(r["intent_id"]); d = json.loads(p.read_text(encoding="utf-8"))
            d["approval"] = {"key": "tg:%s:2" % USER, "t": clock.t, "state": "APPROVED"}; d["awaiting_approval"] = False
            d["exec"] = {"stage": "SIGNING", "owner": "deadtoken", "pid": 999999, "attempts": 1}; p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            out = ext.resume_approved(flow, signer, policy, intent_id=r["intent_id"], ledger=ledger)
            self.assertFalse(out["ok"]); self.assertEqual(out["state"], "INVESTIGATE"); self.assertEqual(signer.sign_count, 0); self.assertEqual(len(node.broadcasts), 0, "서명 중 중단 → 재서명·방송 없이 조사 상태")
            self.assertEqual(ledger.state_of(o["payment_id"])["state"], "RESERVED", "결과 불명 → 예약 유지")
            again = ext.resume_approved(flow, signer, policy, intent_id=r["intent_id"], ledger=ledger); self.assertEqual(again["state"], "INVESTIGATE"); self.assertEqual(signer.sign_count, 0)

    def test_submit_response_lost_is_investigate_or_status_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp)
            r = make_awaiting(ext, flow); o = order_of(ext, flow, r); signed = signer.sign(o["unsigned_tx"], o["user_eoa"]); ledger.reserve(o, policy)
            p = ext._path(r["intent_id"]); d = json.loads(p.read_text(encoding="utf-8"))
            d["approval"] = {"key": "tg:%s:2" % USER, "t": clock.t, "state": "APPROVED"}; d["awaiting_approval"] = False
            d["exec"] = {"stage": "SUBMITTING", "signed_tx": signed, "owner": "dead", "pid": 999999, "attempts": 1}; p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            out = ext.resume_approved(flow, signer, policy, intent_id=r["intent_id"], ledger=ledger)
            self.assertFalse(out["ok"]); self.assertEqual(out["state"], "INVESTIGATE"); self.assertEqual(len(node.broadcasts), 0)
            self.assertEqual(ledger.state_of(o["payment_id"])["state"], "RESERVED", "방송 여부 불명 → 보관 서명본도 재제출하지 않음")
            # 이미 제출된 경우(서명 저장됨) 의 응답 유실 → 조회만
            clock.t += 5
            r2 = make_awaiting(ext, flow, text="맥북지갑한테 트론 1개 보내", key="tg:1:9"); o2 = order_of(ext, flow, r2); signed2 = signer.sign(o2["unsigned_tx"], o2["user_eoa"]); ledger.reserve(o2, policy)
            flow.submit_signed(o2["payment_id"], o2["snapshot_sha256"], signed2); nb = len(node.broadcasts)
            p2 = ext._path(r2["intent_id"]); d2 = json.loads(p2.read_text(encoding="utf-8"))
            d2["approval"] = {"key": "tg:%s:3" % USER, "t": clock.t, "state": "APPROVED"}; d2["awaiting_approval"] = False
            d2["exec"] = {"stage": "SUBMITTING", "signed_tx": signed2, "owner": "dead", "pid": 999999, "attempts": 1}; p2.write_text(json.dumps(d2, ensure_ascii=False), encoding="utf-8")
            out2 = ext.resume_approved(flow, signer, policy, intent_id=r2["intent_id"], ledger=ledger)
            self.assertTrue(out2["ok"]); self.assertEqual(len(node.broadcasts), nb, "서명 저장돼 있음 → 재제출 없이 조회만"); self.assertTrue(ext.get(r2["intent_id"])["exec"].get("status_only"))

    def test_policy_refused_and_sign_failed_release_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp)
            (pathlib.Path(tmp) / "stop").write_text("stop")
            r = make_awaiting(ext, flow); out = approve(ext, flow, signer, policy, ledger, r); self.assertEqual(out["state"], "POLICY_REFUSED"); self.assertEqual(signer.sign_count, 0)
            self.assertIsNone(ledger.state_of(order_of(ext, flow, r)["payment_id"]), "정책 거부 → 예약 없음")
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp, signer=SG.NoSigner())
            r = make_awaiting(ext, flow); out = approve(ext, flow, signer, policy, ledger, r); self.assertEqual(out["state"], "SIGN_FAILED"); self.assertEqual(len(node.broadcasts), 0)
            self.assertEqual(ledger.state_of(order_of(ext, flow, r)["payment_id"])["state"], "RELEASED", "서명본 없음이 확실 → 예약 해제")


class LocalKeySignerTests(unittest.TestCase):
    def test_create_load_sign_and_wrong_passphrase(self):
        with tempfile.TemporaryDirectory() as tmp:
            kf = pathlib.Path(tmp) / "bot_wallet.json"; pf = pathlib.Path(tmp) / "bot_wallet.pass"
            addr = SG.LocalKeySigner.create(kf, pf, private_key_bytes=bytes.fromhex("33" * 32))
            self.assertEqual(addr, SENDER); self.assertEqual(oct(kf.stat().st_mode & 0o777), "0o600"); self.assertEqual(oct(pf.stat().st_mode & 0o777), "0o600")
            raw = json.loads(kf.read_text()); self.assertNotIn("33" * 32, kf.read_text(), "키 원문은 파일에 없음"); self.assertEqual(raw["kdf"], "scrypt"); self.assertEqual(raw["address"], SENDER)
            sg = SG.LocalKeySigner(kf, pf); self.assertEqual(sg.address, SENDER); self.assertEqual(sg.kind, "local")
            with tempfile.TemporaryDirectory() as t2:
                clock, node, flow, ext, _, policy, ledger = setup(t2, signer=sg)
                policy = SG.SignerPolicy(allowed_receivers={MAC}, allowed_senders={SENDER}, exact_amount_sun=2_000_000,
                                        lifetime_max_transactions=1, daily_total_max_sun=4_000_000, stop_file=pathlib.Path(t2) / "stop")
                r = make_awaiting(ext, flow); out = approve(ext, flow, sg, policy, ledger, r); self.assertTrue(out["ok"]); self.assertEqual(sg.sign_count, 1); self.assertEqual(len(node.broadcasts), 1)
            with self.assertRaises(RuntimeError):
                sg.sign({"txID": "x", "raw_data": {}, "raw_data_hex": "aa"}, MAC)                  # 봇 지갑이 아닌 sender
            pf.write_bytes(b"wrong-pass")
            with self.assertRaises(RuntimeError):
                SG.LocalKeySigner(kf, pf)
            with self.assertRaises(FileExistsError):
                SG.LocalKeySigner.create(kf, pf)


if __name__ == "__main__":
    unittest.main()
