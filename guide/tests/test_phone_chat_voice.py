"""9/29 사장 음성 입력 실측 '맥북 지갑의 트론 2개' 대응(VP 검수 A/B/C 반영): 등록 별칭 띄어쓰기·조사 관용, 왼쪽 경계, 수취인 모호성 우선 판정, 재확인 문구. Kiln 호출 0."""
import unittest
import phone_chat as PC
import phone_ai as AI

MAC = "TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz"
IPH = "TQNXymgxq4j5grpQFcTMSkhjXHNDsTW3mb"
C = [
    {"alias": "맥북지갑", "aliases": ["맥북", "테스트지갑", "MacBook지갑"], "address": MAC, "network": "nile", "confirmed_by_owner": True, "note": "n"},
    {"alias": "아이폰지갑", "aliases": ["아이폰"], "address": IPH, "network": "nile", "confirmed_by_owner": True, "note": "i"},   # 사장 확인된 두 번째 수취인(서로 다른 주소)
    {"alias": "민수", "address": None, "network": "nile", "confirmed_by_owner": False, "note": "a"},
    {"alias": "민수", "address": None, "network": "nile", "confirmed_by_owner": False, "note": "b"},
    {"alias": "지연", "address": None, "network": "nile", "confirmed_by_owner": False, "note": "c"},
]


def no_proposal(tc, r):
    tc.assertEqual(r["kind"], "question"); tc.assertNotIn("proposal", r); tc.assertFalse(r.get("order_created"))


class ObservedVoiceText(unittest.TestCase):
    def test_observed_spaced_alias_with_possessive_is_proposal_with_confirm_note(self):
        r = PC.parse_request("맥북 지갑의 트론 2개", C)
        self.assertEqual(r["kind"], "proposal"); self.assertEqual(r["proposal"]["alias"], "맥북지갑"); self.assertEqual(r["proposal"]["amount_trx"], "2")
        self.assertEqual(r["understood"]["alias_match"], "space_or_particle_tolerant")
        self.assertIn("이해했습니다", r["confirm_note"]); self.assertIn("맥북 지갑의 트론 2개", r["confirm_note"]); self.assertIn("아니면 진행하지 마세요", r["confirm_note"])
        self.assertTrue(r["next"].startswith(r["confirm_note"]))

    def test_spaced_alias_with_normal_particle(self):
        r = PC.parse_request("맥북 지갑한테 트론 2개", C); self.assertEqual(r["kind"], "proposal"); self.assertEqual(r["proposal"]["alias"], "맥북지갑"); self.assertIn("confirm_note", r)

    def test_exact_alias_has_no_confirm_note(self):
        r = PC.parse_request("맥북지갑한테 트론 2개", C)
        self.assertEqual(r["kind"], "proposal"); self.assertNotIn("alias_match", r["understood"]); self.assertNotIn("confirm_note", r); self.assertNotIn("이해했습니다", r["next"])

    def test_object_first_word_order_still_ok(self):
        r = PC.parse_request("트론 2개를 맥북 지갑한테 보내줘", C); self.assertEqual(r["kind"], "proposal"); self.assertEqual(r["proposal"]["alias"], "맥북지갑")

    def test_korean_numeral_still_asks_amount(self):
        r = PC.parse_request("맥북지갑한테 트론 두 개", C); no_proposal(self, r); self.assertIn("몇 TRX", r["question"])


class IphoneVoiceText(unittest.TestCase):   # 9/29 17:5x 사장 아이폰 실측 "MacBook 지갑에 트론 2개" — 명시 등록 별칭(MacBook지갑)으로만 연결, 조사 '에'
    def test_observed_iphone_text_is_proposal_with_confirm_note(self):
        r = PC.parse_request("MacBook 지갑에 트론 2개", C)
        self.assertEqual(r["kind"], "proposal"); self.assertEqual(r["proposal"]["alias"], "맥북지갑"); self.assertEqual(r["proposal"]["amount_trx"], "2")
        self.assertEqual(r["understood"]["alias_match"], "space_or_particle_tolerant"); self.assertIn("MacBook 지갑에 트론 2개", r["confirm_note"])

    def test_case_insensitive_latin_alias(self):
        for t in ("macbook 지갑에 트론 2개", "MACBOOK지갑한테 트론 2개", "MacBook지갑에 트론 2개"):
            r = PC.parse_request(t, C); self.assertEqual(r["kind"], "proposal", t); self.assertEqual(r["proposal"]["alias"], "맥북지갑", t)

    def test_unregistered_latin_word_is_not_guessed(self):
        r = PC.parse_request("MacBook에 트론 2개", C); no_proposal(self, r); self.assertEqual(r.get("reason"), "UNREGISTERED_ALIAS")
        r = PC.parse_request("Mac 지갑에 트론 2개", C); no_proposal(self, r)

    def test_particle_e_alone_does_not_create_recipient(self):
        r = PC.parse_request("집에 가서 트론 2개", C); no_proposal(self, r)                       # '집' 미등록 → 질문, 추측 없음
        r = PC.parse_request("맥북지갑한테 트론 2개 집에 가서", C); self.assertEqual(r["kind"], "proposal")   # '에' 앞 미등록 단어는 두 번째 수취인으로 세지 않음

    def test_without_explicit_alias_registration_it_asks(self):
        c2 = [dict(C[0], aliases=["맥북", "테스트지갑"])] + C[1:]
        r = PC.parse_request("MacBook 지갑에 트론 2개", c2); no_proposal(self, r)


class LeftBoundary(unittest.TestCase):   # VP B
    def test_suffix_inside_other_word_is_not_promoted(self):
        r = PC.parse_request("다른맥북지갑한테 트론 2개", C); no_proposal(self, r); self.assertEqual(r.get("reason"), "UNREGISTERED_ALIAS")
        self.assertEqual(PC.registered_alias_in_text("다른맥북지갑한테 트론 2개", C), (None, "NONE"))

    def test_unexplained_leading_word_is_unclear_not_guessed(self):
        r = PC.parse_request("다른 맥북지갑한테 트론 2개", C); no_proposal(self, r); self.assertEqual(r.get("reason"), "UNCLEAR_ALIAS")
        self.assertEqual(PC.registered_alias_in_text("다른 맥북 지갑의 트론 2개", C), (None, "UNCLEAR"))

    def test_partial_alias_is_not_guessed(self):
        r = PC.parse_request("지갑의 트론 2개", C); no_proposal(self, r); self.assertEqual(r.get("reason"), "UNREGISTERED_ALIAS")

    def test_helper_positive_and_none(self):
        self.assertEqual(PC.registered_alias_in_text("맥북 지갑의 트론 2개", C), ("맥북지갑", "OK"))
        self.assertEqual(PC.registered_alias_in_text("테스트 지갑 트론 3개", C), ("맥북지갑", "OK"))
        self.assertEqual(PC.registered_alias_in_text("트론 2개", C), (None, "NONE"))


class TwoRecipients(unittest.TestCase):   # VP A — 서로 다른 주소로 사장 확인된 두 수취인
    def _ambiguous(self, text):
        r = PC.parse_request(text, C); no_proposal(self, r); self.assertEqual(r.get("reason"), "AMBIGUOUS_ALIAS", text)
        self.assertEqual(PC.registered_alias_in_text(text, C), (None, "AMBIGUOUS"), text)
        d = AI.decide(text, C, provider=AI.MockKiln()); self.assertEqual(d["kind"], "question", text); self.assertNotIn("proposal", d); self.assertFalse(d.get("order_created"))

    def test_two_exact_names(self):
        self._ambiguous("맥북지갑한테 트론 2개, 아이폰지갑에게 트론 2개")

    def test_two_spaced_variants(self):
        self._ambiguous("맥북 지갑의 트론 2개, 아이폰 지갑의 트론 2개")

    def test_exact_plus_variant(self):
        self._ambiguous("맥북지갑한테 트론 2개, 아이폰 지갑에게 트론 2개")

    def test_joined_names_in_one_segment(self):
        self._ambiguous("맥북지갑이랑 아이폰지갑의 트론 2개")

    def test_registered_plus_unregistered_second_recipient_asks(self):
        self._ambiguous("맥북지갑한테 트론 2개, 철이에게 트론 2개")
        self._ambiguous("철이한테 트론 2개, 맥북 지갑의 트론 2개")

    def test_single_recipient_with_other_particles_is_not_ambiguous(self):
        r = PC.parse_request("지금 바로 맥북지갑한테 트론 2개 트론으로", C); self.assertEqual(r["kind"], "proposal"); self.assertEqual(r["proposal"]["alias"], "맥북지갑")

    def test_same_name_duplicates_keep_existing_message(self):
        r = PC.parse_request("민수의 트론 2개", C); no_proposal(self, r); self.assertEqual(r.get("reason"), "AMBIGUOUS_ALIAS"); self.assertIn("같은 이름", r["question"])

    def test_unconfirmed_address_still_blocked(self):
        r = PC.parse_request("지연 의 트론 2개", C); no_proposal(self, r); self.assertEqual(r.get("reason"), "UNCONFIRMED_ADDRESS")


class MockKilnPath(unittest.TestCase):
    def test_tolerant_alias_normal_and_adapt_keep_confirm_note(self):
        r = AI.decide("맥북 지갑의 트론 2개", C, provider=AI.MockKiln())
        self.assertEqual((r["kind"], r["kind_detail"], r["proposal"]["alias"], r["ai"]["calls"]), ("proposal", "normal", "맥북지갑", 0)); self.assertIn("confirm_note", r)
        a = AI.decide("맥북 지갑의 트론 2개, 예산 3 트론 5분 안에", C, provider=AI.MockKiln())
        self.assertEqual((a["kind"], a["kind_detail"]), ("proposal", "adapt")); self.assertIn("confirm_note", a); self.assertTrue(a["next"].startswith(a["confirm_note"]))


if __name__ == "__main__":
    unittest.main()
