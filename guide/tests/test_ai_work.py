"""'AI가 도와드린 일' 카드 검사 — 합성 모델 응답(실호출 없음). VP_AI_WORK_EXPERIENCE 9/28:
허용되지 않은 next_action·없는 근거 id·주소/숫자/보장 문장·코드 판정과 모순 → 사용 안 함(확인 필요, 규칙 표시). 실행 권한은 늘지 않는다."""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import plan as P  # noqa: E402
import ai_work as AW  # noqa: E402

TEXT_FULL = "업비트에서 산 USDT 50만원어치를 바이낸스 내 계정으로 트론 네트워크로 보내고 싶어요"
TEXT_VAGUE = "업비트에서 산 코인을 해외로 보내고 싶어요"


def fake_chat(reply: dict, finish="stop", ok=True):
    calls = []
    def chat(messages, *, flow_id, max_tokens=700, **kw):
        calls.append({"messages": messages, "flow_id": flow_id})
        return {"ok": ok, "http": 200 if ok else 500, "content": json.dumps(reply, ensure_ascii=False), "usage": {"prompt_tokens": 300, "completion_tokens": 80},
                "model": "qwen3-32b", "request_id": "fake", "elapsed_ms": 5, "error": None if ok else "boom", "finish_reason": finish}
    chat.calls = calls
    return chat


def run(text, reply, change=None, answers=None, **kw):
    chat = fake_chat(reply, **kw)
    si = P.structure_intent(text, use_kiln=True, chat_fn=chat, change_request=change)
    intent = dict(si["intent"])
    for k, v in (answers or {}).items():
        intent[k] = v
    pl = P.build_plan(intent, conflicts=list(si["conflicts"]))
    return pl, si, AW.work_card(pl, si, si.get("ai_work_raw"), change), chat


class SingleCallAndPrompt(unittest.TestCase):
    def test_one_call_carries_intent_and_work_and_prompt_has_evidence_ids(self):
        reply = {"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True,
                 "ai": {"next_action": "propose_plan", "question_field": None, "evidence_ids": ["r_krw_delay"], "summary_ko": "원화 입금 후 출금 지연 조건이 적용됩니다. 먼저 원화를 준비하세요.", "change": None}}
        pl, si, card, chat = run(TEXT_FULL, reply)
        self.assertEqual(len(chat.calls), 1)
        self.assertIn("r_krw_delay", chat.calls[0]["messages"][1]["content"]); self.assertIn("EVIDENCE", chat.calls[0]["messages"][1]["content"])
        self.assertEqual(si["kiln"]["outcome"], "VALIDATED"); self.assertTrue(si["kiln"]["used"])
        self.assertEqual(card["ai_status"], "used"); self.assertEqual(card["rejected_reasons"], [])
        steps = [i["step"] for i in card["items"]]
        self.assertEqual(steps, ["요청 이해", "맞춤 안내 · 다음 행동", "진행/결과"])
        self.assertEqual(card["items"][1]["evidence"][0]["id"], "r_krw_delay"); self.assertIn("AI(qwen3-32b)", card["items"][1]["by"])
        self.assertTrue(all(u["by"] == "AI+규칙 일치" for u in card["items"][0]["understood"]))


class Validation(unittest.TestCase):
    def test_disallowed_action_when_server_says_need_confirm(self):
        reply = {"from": "upbit", "to": None, "asset": None, "network": None, "amount_krw": None, "own_account": None,
                 "ai": {"next_action": "propose_plan", "question_field": None, "evidence_ids": [], "summary_ko": "바로 출금하시면 됩니다.", "change": None}}
        pl, si, card, _ = run(TEXT_VAGUE, reply)
        self.assertEqual(pl["status"], "need_confirm")
        self.assertTrue(any("허용한" in r for r in card["rejected_reasons"]))
        q = [i for i in card["items"] if i["step"] == "막힌 부분 질문"][0]
        self.assertEqual(q["by"], "규칙"); self.assertIsNotNone(q["question"])               # 규칙의 질문으로 대체, 조건 검사 생략 없음
        self.assertIn(card["ai_status"], ("partially_used", "intent_only"))

    def test_question_must_be_a_server_detected_gap(self):
        reply = {"from": "upbit", "to": None, "asset": "USDT", "network": None, "amount_krw": None, "own_account": None,
                 "ai": {"next_action": "ask", "question_field": "asset", "evidence_ids": [], "summary_ko": "자산을 알려주세요.", "change": None}}
        pl, si, card, _ = run(TEXT_VAGUE + " 테더로", reply)
        self.assertTrue(any("question_field" in r for r in card["rejected_reasons"]))
        q = [i for i in card["items"] if i["step"] == "막힌 부분 질문"][0]
        self.assertEqual(q["question_field"], pl["need_confirm"][0]["field"]); self.assertGreater(len(q["all_unknown"]), 1)

    def test_unknown_evidence_address_number_guarantee_rejected(self):
        base = {"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True}
        for summ, key in (("주소 TMDKznuDWaZwfZHcM61FVFstyYNmK6Njk1 로 보내세요.", "주소/URL"), ("수수료는 30,000원입니다.", "없는 숫자"), ("이 방법이면 수익이 보장됩니다.", "보장")):
            reply = dict(base, ai={"next_action": "propose_plan", "question_field": None, "evidence_ids": ["r_fake", "r_fee"], "summary_ko": summ, "change": None})
            pl, si, card, _ = run(TEXT_FULL, reply)
            self.assertTrue(any("없는 근거 id" in r for r in card["rejected_reasons"]))
            self.assertTrue(any(key in r for r in card["rejected_reasons"]), (summ, card["rejected_reasons"]))
            it = [i for i in card["items"] if i["step"] == "맞춤 안내 · 다음 행동"][0]
            self.assertEqual(it["by"], "규칙"); self.assertEqual(it["summary"], pl["current_step"]["do_now"][:300])
            self.assertNotIn("r_fake", [e["id"] for e in it["evidence"]])

    def test_known_number_from_user_text_allowed(self):
        reply = {"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True,
                 "ai": {"next_action": "propose_plan", "question_field": None, "evidence_ids": ["r_amount_1m"], "summary_ko": "500000원 규모는 100만원 기준 아래입니다.", "change": None}}
        pl, si, card, _ = run(TEXT_FULL, reply)
        self.assertEqual(card["rejected_reasons"], [], card["rejected_reasons"])       # 500000 은 사용자 입력값, 100만원 은 '만' 표기라 큰 숫자 패턴 아님

    def test_decline_state_only_allows_explain_decline(self):
        reply = {"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": False,
                 "ai": {"next_action": "propose_plan", "question_field": None, "evidence_ids": [], "summary_ko": "진행하세요.", "change": None}}
        pl, si, card, _ = run("업비트에서 친구 바이낸스 계정으로 USDT 50만원 트론으로", reply)
        self.assertEqual(pl["status"], "unsupported")
        it = [i for i in card["items"] if i["step"] == "멈춘 이유"][0]; self.assertEqual(it["by"], "규칙"); self.assertIn("본인 명의", it["summary"])
        st = card["items"][-1]; self.assertEqual(st["state"], "unsupported")


class ChangeRequest(unittest.TestCase):
    def test_change_interpretation_needs_human_and_is_not_approval(self):
        reply = {"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True,
                 "ai": {"next_action": "propose_plan", "question_field": None, "evidence_ids": ["r_fee"], "summary_ko": "출금 수수료 조건을 확인한 뒤 진행합니다.",
                        "change": {"mode": "max_within_budget", "note": "예산 안에서 금액을 줄이는 것을 허용함"}}}
        pl, si, card, _ = run(TEXT_FULL, reply, change="예산 안에서 금액 줄여도 돼요")
        ch = [i for i in card["items"] if i["step"] == "조건 변경"][0]
        self.assertEqual(ch["interpretation"]["mode"], "max_within_budget"); self.assertTrue(ch["interpretation"]["needs_human_confirm"])
        self.assertIn("서명 승인이 아닙니다", ch["note"])

    def test_change_without_request_is_ignored_and_bad_mode_rejected(self):
        base = {"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True}
        pl, si, card, _ = run(TEXT_FULL, dict(base, ai={"next_action": "propose_plan", "evidence_ids": [], "summary_ko": "진행합니다.", "change": {"mode": "max_within_budget"}}))
        self.assertTrue(any("변경 요청이 없는데" in r for r in card["rejected_reasons"])); self.assertFalse(any(i["step"] == "조건 변경" for i in card["items"]))
        pl, si, card, _ = run(TEXT_FULL, dict(base, ai={"next_action": "propose_plan", "evidence_ids": [], "summary_ko": "진행합니다.", "change": {"mode": "double_it"}}), change="두 배로")
        self.assertIsNone([i for i in card["items"] if i["step"] == "조건 변경"][0]["interpretation"])


class Failures(unittest.TestCase):
    def test_ai_failure_shows_rules_only_and_no_execution_authority(self):
        pl, si, card, _ = run(TEXT_FULL, {"anything": 1}, ok=False)
        self.assertEqual(card["ai_status"], "unused_failed"); self.assertEqual(si["source"], "rules_after_kiln_fail")
        self.assertTrue(all("AI" not in i["by"] for i in card["items"]))
        self.assertTrue(any(x.startswith("송금·서명") and "권한 없음" in x for x in card["items"][-1]["not_yet"]))

    def test_length_truncation_is_failure(self):
        pl, si, card, _ = run(TEXT_FULL, {"from": "upbit"}, finish="length")
        self.assertEqual(si["kiln"]["outcome"], "LENGTH_TRUNCATED"); self.assertEqual(card["ai_status"], "unused_failed")

    def test_off_when_not_requested(self):
        si = P.structure_intent(TEXT_FULL, use_kiln=False)
        pl = P.build_plan(si["intent"]); card = AW.work_card(pl, si, None, None)
        self.assertEqual(card["ai_status"], "off"); self.assertEqual(card["items"][0]["by"], "규칙")

    def test_intent_only_when_ai_key_missing(self):
        pl, si, card, _ = run(TEXT_FULL, {"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True})
        self.assertEqual(card["ai_status"], "intent_only"); self.assertTrue(si["kiln"]["used"])


if __name__ == "__main__":
    unittest.main()
