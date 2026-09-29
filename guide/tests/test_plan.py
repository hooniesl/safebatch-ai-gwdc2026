"""안내 계획 검사 — LLM 없이. 9/28 2차 검수 반례(1.5만원·음수·부정문·반대 방향·Bitget·length 응답·범위 밖 안내 충돌) 포함."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import plan as P  # noqa: E402

FULL = "업비트에서 산 USDT 50만원어치를 바이낸스 내 계정으로 트론 네트워크로 보내고 싶어요"


def intent(text):
    rb = P.parse_rules_based(text)
    return {k: rb[k] for k in P.INTENT_KEYS}, rb["_conflicts"]


class RulesData(unittest.TestCase):
    def test_rules_have_required_fields_and_v1_scope(self):
        r = P.load_rules()
        for rid, rule in r["rules"].items():
            for k in ("kind", "applies_to", "condition", "doc_date", "effective_from", "checked_at", "source_url", "confidence", "limits"):
                self.assertIn(k, rule, f"{rid} missing {k}")
            self.assertIn(rule["kind"], ("current", "past", "future", "unverified", "partial"))
        self.assertEqual(r["scope_v1"]["to"], "binance"); self.assertTrue(r["scope_v1"]["own_account_only"])
        self.assertTrue(all(c["auto_action"] is False for c in r["earn_cards"]))
        self.assertIn("as_of_note", r["rules"]["r_fee"])

    def test_future_rule_not_in_steps(self):
        r = P.load_rules()
        used = {rid for s in r["steps"] for rid in s["rules"]}
        self.assertNotIn("r_travel_rule_future", used)


class Parser(unittest.TestCase):
    def test_full_sentence(self):
        i, c = intent(FULL)
        self.assertEqual(i, {"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500_000, "own_account": True})
        self.assertEqual(c, [])

    def test_decimal_and_sign_amounts(self):
        self.assertEqual(intent("업비트에서 1.5만원어치 USDT")[0]["amount_krw"], 15_000)
        self.assertEqual(intent("업비트에서 500,000원어치")[0]["amount_krw"], 500_000)
        for bad in ("업비트에서 -150만원", "업비트에서 0원", "업비트에서 1.23원"):
            i, c = intent(bad)
            self.assertIsNone(i["amount_krw"]); self.assertTrue(any(x.startswith("amount_krw") for x in c), bad)

    def test_negation_and_conflict_on_own_account(self):
        self.assertIs(intent("내 계정이 아니라 친구 계정으로")[0]["own_account"], False)
        self.assertIs(intent("친구 계정으로 보내줘")[0]["own_account"], False)
        i, c = intent("내 계정이랑 친구 계정 둘 다")
        self.assertIsNone(i["own_account"]); self.assertTrue(any(x.startswith("own_account") for x in c))

    def test_reverse_direction_is_not_mapped_to_upbit(self):
        i, _ = intent("바이낸스에서 업비트로 USDT 보내고 싶어요")
        self.assertEqual((i["from"], i["to"]), ("binance", "upbit"))
        self.assertEqual(P.build_plan(i)["status"], "unsupported")

    def test_missing_fields_stay_none(self):
        i, _ = intent("코인 좀 보내고 싶어요")
        self.assertTrue(all(v is None for v in i.values()))


class Validation(unittest.TestCase):
    def test_same_validation_for_all_paths(self):
        self.assertIsNone(P.validate_intent({"from": "upbit", "to": "coinbase"})[0])
        self.assertIsNone(P.validate_intent({"from": "upbit", "rule_override": 1})[0])
        self.assertIsNone(P.validate_intent({"amount_krw": -5})[0])
        self.assertIsNone(P.validate_intent({"amount_krw": 1.5})[0])
        self.assertIsNone(P.validate_intent({"own_account": "true"})[0])
        ok, _ = P.validate_intent({"from": "upbit", "to": "binance", "asset": "USDT", "network": "KAIA", "amount_krw": 10, "own_account": True})
        self.assertIsNotNone(ok)

    def test_merge_conflict_is_not_resolved_by_rules_priority(self):
        rb = P.parse_rules_based("업비트에서 바이낸스로 USDT 50만원 내 계정")
        merged, conf = P.merge_intents(rb, {"from": "upbit", "to": "bitget", "asset": "USDT", "network": "TRON", "amount_krw": 500_000, "own_account": True})
        self.assertIsNone(merged["to"]); self.assertTrue(any(x.startswith("to:") for x in conf))
        self.assertEqual(merged["network"], "TRON")      # 빈 칸은 보완


class Plan(unittest.TestCase):
    def test_ready_unverified_when_step_has_partial_conditions(self):
        i, c = intent(FULL)
        p = P.build_plan(i, current_step="s4", conflicts=c)
        self.assertEqual(p["status"], "ready_unverified"); self.assertIn("r_fee", p["unverified_condition_ids"])
        self.assertEqual(p["next_step"]["id"], "s5"); self.assertIn("확인", p["current_step"]["do_now"])

    def test_ready_when_all_conditions_current(self):
        i, c = intent(FULL)
        p = P.build_plan(i, current_step="s2", conflicts=c)
        self.assertEqual(p["status"], "ready"); self.assertEqual(p["next_step"]["id"], "s3")

    def test_unsupported_hides_next_step_and_withdrawal_instruction(self):
        i, c = intent("업비트에서 바이낸스로 USDT 300만원. 친구 계정으로")
        p = P.build_plan(i, current_step="s4", conflicts=c)
        self.assertEqual(p["status"], "unsupported"); self.assertIsNone(p["next_step"])
        self.assertNotIn("출금에서", p["current_step"]["do_now"]); self.assertIn("타인 계정", " ".join(p["unsupported"]))
        self.assertNotIn("불가", " ".join(p["unsupported"]).replace("판단하지 않습니다", ""))   # 법적 불가 주장 없음

    def test_other_destination_is_out_of_scope_v1(self):
        i, c = intent("업비트에서 비트겟 내 계정으로 USDT 10만원 트론")
        p = P.build_plan(i, conflicts=c)
        self.assertEqual(p["status"], "unsupported"); self.assertIn("Binance", " ".join(p["unsupported"]))

    def test_need_confirm_hides_next_step(self):
        i, c = intent("업비트에서 USDT 보내고 싶어요")
        p = P.build_plan(i, current_step="s4", conflicts=c)
        self.assertEqual(p["status"], "need_confirm"); self.assertIsNone(p["next_step"])
        self.assertEqual({n["field"] for n in p["need_confirm"]}, {"to", "own_account", "amount_krw", "network"})

    def test_conflict_becomes_question_with_reason(self):
        i, c = intent("업비트에서 바이낸스로 -150만원 내 계정")
        p = P.build_plan(i, conflicts=c)
        q = next(n for n in p["need_confirm"] if n["field"] == "amount_krw")
        self.assertIsNotNone(q["why"])

    def test_authority_note_and_plan_id(self):
        i, c = intent(FULL)
        p = P.build_plan(i, conflicts=c)
        self.assertIn("권한이 아닙니다", p["authority"])
        self.assertEqual(P.plan_id_for(i), P.plan_id_for(dict(i)))


class KilnOutcome(unittest.TestCase):
    def test_length_or_invalid_output_is_failure_not_success(self):
        import plan as P2
        class FakeKiln:
            def __init__(self, res): self.res = res
            def chat(self, *a, **k): return self.res
        for res, expect in (({"ok": True, "http": 200, "finish_reason": "length", "content": "", "usage": {}}, "LENGTH_TRUNCATED"),
                            ({"ok": True, "http": 200, "finish_reason": "stop", "content": "not json", "usage": {}}, "INVALID_JSON"),
                            ({"ok": False, "http": 500, "error": "boom"}, "HTTP_ERROR"),
                            ({"ok": True, "http": 200, "finish_reason": "stop", "content": '{"from":"upbit","to":"binance","asset":"USDT","network":null,"amount_krw":500000,"own_account":true}', "usage": {}}, "VALIDATED")):
            sys.modules["kiln_client"] = FakeKiln(res)  # type: ignore
            r = P2.structure_intent("업비트에서 바이낸스 내 계정으로 USDT 50만원", use_kiln=True)
            self.assertEqual(r["kiln"]["outcome"], expect, res)
            self.assertEqual(r["kiln"]["used"], expect == "VALIDATED")
            self.assertEqual(r["source"], "kiln" if expect == "VALIDATED" else "rules_after_kiln_fail")
        sys.modules.pop("kiln_client", None)


if __name__ == "__main__":
    unittest.main()


class OwnAccountWithExchangeName(unittest.TestCase):
    """9/29 부사장 비교 화면 수정 1: '내 Binance 계정' = 본인 진술(True). '내 친구 계정' 은 여전히 타인(False)."""
    def test_my_exchange_account_is_user_stated_own(self):
        self.assertIs(P.parse_rules_based("업비트에서 산 USDT 50만원어치를 내 Binance 계정으로 트론 네트워크로 보내고 싶어요")["own_account"], True)
        self.assertIs(P.parse_rules_based("제 바이낸스 계정으로")["own_account"], True)
        self.assertIs(P.parse_rules_based("내 친구 계정으로 보내줘")["own_account"], False)
        self.assertIn("사용자 진술", P.humanize_intent({"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True})["받을 곳 계정 명의"])


class ScreenExplanation(unittest.TestCase):
    """9/29 사장 비교 결과 반영: 단계마다 '이 화면은 무엇 / 주소 역할 / 다음 행동 하나 / 꼭 볼 것' 이 있고, 예시 화면은 거래소 조작 버튼처럼 동작하지 않는다."""
    def test_every_step_has_screen_block_and_s3_explains_address(self):
        for sid in ("s1", "s2", "s3", "s4", "s5"):
            p = P.build_plan({"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True}, current_step=sid)
            sc = p["current_step"]["screen"]; self.assertTrue(sc["purpose"] and sc["next_one"] and sc["key_conditions"], sid)
        p3 = P.build_plan({"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True}, current_step="s3")
        sc = p3["current_step"]["screen"]; self.assertIn("받는 사람 주소", sc["address_role"]); self.assertGreaterEqual(len(sc["key_conditions"]), 3)
        self.assertNotIn("업비트", sc["next_one"]); self.assertIn("업비트", sc["after_this"])                 # 9/29 VP: 다음 행동 하나 = 지금 할 일만, 이후 순서는 별도
        joined = " ".join(sc["key_conditions"])
        self.assertIn("본인 계정 간 이동만 안내", joined); self.assertNotIn("본인 명의여야", joined)              # 거래소 일반 규정과 구분
        self.assertNotIn("일반 거래소 출금만", joined); self.assertIn("입금 방식 조건", joined)                    # 원문보다 넓은 단정 제거
        self.assertIn("보장되지는 않습니다", joined)                                                          # 최소 수량 = 보장 아님

    def test_index_mock_screen_is_not_actionable(self):
        import pathlib
        h = pathlib.Path(__file__).resolve().parents[1].joinpath("index.html").read_text(encoding="utf-8")
        self.assertIn("합성 예시", h); self.assertIn("이 안내에서는 동작하지 않음", h)
        mock = h[h.index("const mockS3"):h.index("const st = p.stage")]
        self.assertNotIn("<button", mock); self.assertNotIn("onclick", mock)


class AdvisorWording20260929(unittest.TestCase):
    """9/29 Kimi·Grok 채택분: 3줄 요약 최상단, 네트워크 조건 경고 표시, 원화 참고값=USDT 개수 아님, 버튼 라벨 결과 중심."""
    def test_summary_and_warning_and_krw_note(self):
        p = P.build_plan({"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True}, current_step="s3")
        sc = p["current_step"]["screen"]; self.assertEqual(len(sc["summary"]), 3); self.assertIn("받는 사람 주소", sc["summary"][0]); self.assertIn("복사", sc["summary"][1])
        self.assertTrue(any(k.startswith("⚠") and "네트워크" in k for k in sc["key_conditions"]))
        self.assertIn("USDT 개수는 아직 정하지 않았습니다", p["intent_ko"]["송금할 코인 가치(원화 환산 참고값)"])
        import pathlib as _p
        h = _p.Path(__file__).resolve().parents[1].joinpath("index.html").read_text(encoding="utf-8")
        self.assertIn("이 화면 설명 듣기 (AI)", h); self.assertIn("거래소 원문 규칙만 보기", h); self.assertIn("나중에 할 일 보기", h); self.assertIn("실제로 1회 호출", h)
        s = _p.Path(__file__).resolve().parents[1].joinpath("sign.html").read_text(encoding="utf-8")
        for k in ("Nile 테스트넷 자기 지갑 이동입니다. 업비트 출금·바이낸스 입금이 아닙니다", "서명할 수량:", "메인넷 TRC20 이 아닙니다", "AI는 이유를 설명만 했습니다", "아직 전송·입금 완료가 아닙니다"):
            self.assertIn(k, s)


class StageInference(unittest.TestCase):
    """9/29 VP: 단계 선택을 먼저 요구하지 않는다. 쉬운 예/아니오 답으로 단계 결정, 이미 입력한 정보(산 USDT)는 재질문 없음, 답에 따라 다음 행동이 바뀐다."""
    def test_hint_skips_has_usdt_and_asks_deposit_screen(self):
        st = P.infer_step({}, {"has_usdt": True}); self.assertEqual(st["question"]["id"], "opened_deposit"); self.assertEqual(st["step"], "s3"); self.assertFalse(st["certain"])
        st = P.infer_step({}, {"has_usdt": False}); self.assertEqual(st["question"]["id"], "has_usdt")
    def test_answers_change_step_and_next_action(self):
        self.assertEqual(P.infer_step({"opened_deposit": False}, {"has_usdt": True})["step"], "s3")
        st = P.infer_step({"opened_deposit": True}, {"has_usdt": True}); self.assertEqual((st["step"], st["question"]["id"]), ("s4", "withdraw_requested"))
        self.assertEqual(P.infer_step({"opened_deposit": True, "withdraw_requested": True}, {"has_usdt": True})["step"], "s5")
        self.assertEqual(P.infer_step({"has_usdt": False, "has_krw": True})["step"], "s2"); self.assertEqual(P.infer_step({"has_usdt": False, "has_krw": False})["step"], "s1")
        i = {"from": "upbit", "to": "binance", "asset": "USDT", "network": "TRON", "amount_krw": 500000, "own_account": True}
        n3 = P.build_plan(i, current_step="s3")["current_step"]["screen"]["next_one"]; n4 = P.build_plan(i, current_step="s4")["current_step"]["screen"]["next_one"]
        self.assertNotEqual(n3, n4); self.assertIn("복사", n3); self.assertIn("출금", n4)
        self.assertTrue(all(P.build_plan(i, current_step=s)["current_step"]["screen"]["why_check"] for s in ("s1", "s2", "s3", "s4", "s5")))
    def test_index_first_card_and_no_upfront_step(self):
        import pathlib as _p
        h = _p.Path(__file__).resolve().parents[1].joinpath("index.html").read_text(encoding="utf-8")
        for k in ("① 지금 할 행동 하나", "② 먼저 확인할 조건", "③ 확인이 필요한 이유", "한 가지만 여쭙니다", "상세 설정(단계를 직접 고르기", "규칙 결과(AI 호출 없음)", "실제 AI 호출 결과(Kiln)"):
            self.assertIn(k, h)
        self.assertIn("거래소 화면을 읽거나 상태를 확인하지 않습니다", h)
