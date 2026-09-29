"""9/29 16:5x 사장 음성 입력 실측 '맥북 지갑의 트론 2개' 대응: 등록 별칭의 띄어쓰기·조사('의') 관용 + 불명확하면 질문. Kiln 호출 0."""
import unittest
from decimal import Decimal
import phone_chat as PC
import phone_ai as AI

C = [
    {"alias": "맥북지갑", "aliases": ["맥북", "테스트지갑"], "address": "TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz", "network": "nile", "confirmed_by_owner": True, "note": "n"},
    {"alias": "민수", "address": None, "network": "nile", "confirmed_by_owner": False, "note": "a"},
    {"alias": "민수", "address": None, "network": "nile", "confirmed_by_owner": False, "note": "b"},
    {"alias": "지연", "address": None, "network": "nile", "confirmed_by_owner": False, "note": "c"},
]


class VoiceRecognizedTextTests(unittest.TestCase):
    def test_spaced_alias_with_possessive_particle_is_proposal_with_confirm_note(self):
        r = PC.parse_request("맥북 지갑의 트론 2개", C)
        self.assertEqual(r["kind"], "proposal")
        self.assertEqual(r["proposal"]["alias"], "맥북지갑"); self.assertEqual(r["proposal"]["amount_trx"], "2")
        self.assertEqual(r["understood"]["alias_match"], "space_or_particle_tolerant")
        self.assertIn("이해했습니다", r["next"]); self.assertIn("아니면 진행하지 마세요", r["next"])

    def test_spaced_alias_with_normal_particle(self):
        r = PC.parse_request("맥북 지갑한테 트론 2개", C)
        self.assertEqual(r["kind"], "proposal"); self.assertEqual(r["proposal"]["alias"], "맥북지갑")

    def test_exact_alias_has_no_confirm_note(self):
        r = PC.parse_request("맥북지갑한테 트론 2개", C)
        self.assertEqual(r["kind"], "proposal"); self.assertNotIn("alias_match", r["understood"]); self.assertNotIn("이해했습니다", r["next"])

    def test_korean_numeral_still_asks_amount(self):
        r = PC.parse_request("맥북지갑한테 트론 두 개", C)
        self.assertEqual(r["kind"], "question"); self.assertIn("몇 TRX", r["question"])

    def test_partial_alias_is_not_guessed(self):
        r = PC.parse_request("지갑의 트론 2개", C)
        self.assertEqual(r["kind"], "question"); self.assertEqual(r.get("reason"), "UNREGISTERED_ALIAS")

    def test_duplicate_alias_with_possessive_asks(self):
        r = PC.parse_request("민수의 트론 2개", C)
        self.assertEqual(r["kind"], "question"); self.assertEqual(r.get("reason"), "AMBIGUOUS_ALIAS")

    def test_two_different_registered_names_in_text_asks(self):
        r = PC.parse_request("맥북지갑이랑 지연의 트론 2개", C)
        self.assertEqual(r["kind"], "question")

    def test_unconfirmed_address_still_blocked(self):
        r = PC.parse_request("지연 의 트론 2개", C)
        self.assertEqual(r["kind"], "question"); self.assertEqual(r.get("reason"), "UNCONFIRMED_ADDRESS")

    def test_mock_kiln_decide_path_uses_tolerant_alias(self):
        r = AI.decide("맥북 지갑의 트론 2개", C, provider=AI.MockKiln())
        self.assertEqual(r["kind"], "proposal"); self.assertEqual(r["kind_detail"], "normal"); self.assertEqual(r["proposal"]["alias"], "맥북지갑")
        self.assertEqual(r["ai"]["calls"], 0)

    def test_registered_alias_scan_helper(self):
        self.assertEqual(PC.registered_alias_in_text("맥북 지갑의 트론 2개", C), ("맥북지갑", "OK"))
        self.assertEqual(PC.registered_alias_in_text("테스트 지갑 트론 3개", C), ("맥북지갑", "OK"))
        self.assertEqual(PC.registered_alias_in_text("트론 2개", C), (None, "NONE"))
        self.assertEqual(PC.registered_alias_in_text("민수한테 트론 2개", C), (None, "AMBIGUOUS"))


if __name__ == "__main__":
    unittest.main()
