"""SafeBatch AI 로컬 흐름 연결 검사 · 합성 데이터. 사람 승인 없이 승인 대기에서 종료. 외부 호출·서명·송금 없음.

실행: cd AI_CONTEST/gwdc_2026 && python3 -m unittest safebatch.tests.test_local safebatch.tests.test_flow -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from safebatch.csvcheck import UNIT  # noqa: E402
from safebatch.flow import STAGE, approve_batch, revise_batch, run_batch  # noqa: E402
from safebatch.policy import PaymentPolicy  # noqa: E402
from safebatch.tests.test_local import A, B, C  # noqa: E402  합성 주소

FEE = 1 * UNIT


def policy() -> PaymentPolicy:
    return PaymentPolicy(budget_units=100 * UNIT, fee_units=FEE, allowlist=frozenset({A, B, C}))


class FlowCases(unittest.TestCase):
    def test_F1_single_ok_row_reserved_once_and_pending(self):
        p = policy()
        out = run_batch(f"recipient,amount,memo\n{A},10,invoice 1\n", p, "b1")
        self.assertEqual(out["stage"], STAGE)
        self.assertFalse(out["sent"]) ; self.assertFalse(out["signed"])
        row = out["rows"][0]
        self.assertEqual((row["row_no"], row["memo"], row["amount"], row["decision"]),
                         (2, "invoice 1", "10.000000", "ACCEPTED_FOR_APPROVAL"))
        self.assertTrue(row["decision_reason"])
        self.assertEqual(p.reserved_units, 10 * UNIT + FEE)  # 금액+수수료 한 번만
        self.assertEqual(out["pending_approval_total"], 1)
        self.assertEqual(len(p.decision_log), 1)
        json.dumps(out)  # 결과 JSON 직렬화 가능

    def test_F2_mixed_ok_and_error_rows(self):
        p = policy()
        text = f"recipient,amount,memo\n{A},10,ok1\n{B},abc,bad amount\n{C},5,ok2\n"
        out = run_batch(text, p, "b2")
        err = out["rows"][1]
        self.assertEqual(err["validation"], "ERROR")
        self.assertIn("AMOUNT_FORMAT", err["validation_reasons"])
        self.assertFalse(err["candidate"]) ; self.assertIsNone(err["decision"])
        self.assertEqual(err["row_no"], 3) ; self.assertEqual(err["memo"], "bad amount")
        self.assertEqual(p.reserved_units, 15 * UNIT + 2 * FEE)  # 오류행 예약 제외
        self.assertEqual(out["pending_approval_total"], 2)
        self.assertEqual(len(p.decision_log), 2)  # 오류행은 정책 검사 미진입
        self.assertEqual(out["validation_counts"], {"total_rows": 3, "OK": 2, "ERROR": 1, "DUPLICATE_SUSPECT": 0})

    def test_F3_duplicate_suspect_rows_not_candidates(self):
        p = policy()
        text = f"recipient,amount,memo\n{A},10,first\n{B},3,other\n{A},10,second\n"
        out = run_batch(text, p, "b3")
        dups = [r for r in out["rows"] if r["validation"] == "DUPLICATE_SUSPECT"]
        self.assertEqual([r["row_no"] for r in dups], [2, 4])
        self.assertTrue(all(not r["candidate"] and r["decision"] is None for r in dups))
        self.assertEqual(p.reserved_units, 3 * UNIT + FEE)  # B 만 예약
        self.assertEqual(out["pending_approval_total"], 1)
        self.assertEqual(len(p.decision_log), 1)

    def test_F4_rerun_same_request_adds_nothing(self):
        p = policy()
        text = f"recipient,amount,memo\n{A},10,invoice 1\n{B},2,invoice 2\n"
        first = run_batch(text, p, "b4")
        reserved, pending = p.reserved_units, first["pending_approval_total"]
        second = run_batch(text, p, "b4")
        self.assertEqual([r["decision"] for r in second["rows"]], ["REPLAY", "REPLAY"])
        self.assertTrue(all(r["stopped"] for r in second["rows"]))
        self.assertEqual(p.reserved_units, reserved)  # 예산 예약 추가 없음
        self.assertEqual(second["pending_approval_total"], pending)  # 승인 대기 건 추가 없음
        self.assertEqual(second["new_pending_this_run"], 0)
        self.assertEqual([r["first_record_seq"] for r in second["rows"]], [1, 2])  # 최초 기록 참조
        self.assertEqual(len(p.decision_log), 4)  # 2 ACCEPTED + 2 REPLAY 로그
        self.assertEqual(second["csv_sha256"], first["csv_sha256"])

    # --- 배치 변경 입력 차단(부사장 지시 9/15): 같은 batch_id + 다른 CSV 는 행별 처리 전 전체 거부 ---
    def _first_run(self):
        p = policy()
        text = f"recipient,amount,memo\n{A},10,invoice 1\n{B},2,invoice 2\n"
        first = run_batch(text, p, "g")
        self.assertEqual(first["batch_registration"], "REGISTERED")
        self.assertEqual(first["pending_approval_total"], 2)
        return p, text, first

    def _assert_rejected_unchanged(self, p, first, out):
        self.assertEqual(out["stage"], "REJECTED_BATCH_CONFLICT")
        self.assertEqual(out["rows"], [])  # 행별 처리 없음
        self.assertEqual(out["batch_conflict"]["first_csv_sha256"], first["csv_sha256"])
        self.assertNotEqual(out["csv_sha256"], first["csv_sha256"])
        self.assertEqual(p.reserved_units, 12 * UNIT + 2 * FEE)  # 예약 추가 없음
        self.assertEqual(out["pending_approval_total"], 2)      # 승인 대기 추가 없음
        self.assertEqual(len(p.decision_log), 2)                # 행 결정 로그 추가 없음
        self.assertEqual(p.record_of("g:r1:2").amount_units, 10 * UNIT)
        self.assertEqual(p.batch_log[-1]["outcome"], "BATCH_CONFLICT")

    def test_G1_blank_line_inserted_rejected(self):
        p, text, first = self._first_run()
        changed = text.replace(f"{A},10,invoice 1\n", f"{A},10,invoice 1\n\n")
        self._assert_rejected_unchanged(p, first, run_batch(changed, p, "g"))

    def test_G2_row_order_changed_rejected(self):
        p, text, first = self._first_run()
        changed = f"recipient,amount,memo\n{B},2,invoice 2\n{A},10,invoice 1\n"
        self._assert_rejected_unchanged(p, first, run_batch(changed, p, "g"))

    def test_G3_amount_changed_and_row_added_rejected(self):
        p, text, first = self._first_run()
        changed = f"recipient,amount,memo\n{A},11,invoice 1\n{B},2,invoice 2\n{C},3,new row\n"
        self._assert_rejected_unchanged(p, first, run_batch(changed, p, "g"))

    def test_G4_same_csv_rerun_still_replay(self):
        p, text, first = self._first_run()
        out = run_batch(text, p, "g")
        self.assertEqual(out["batch_registration"], "SAME")
        self.assertEqual([r["decision"] for r in out["rows"]], ["REPLAY", "REPLAY"])
        self.assertEqual(p.reserved_units, 12 * UNIT + 2 * FEE)
        self.assertEqual(out["pending_approval_total"], 2)

    def test_G5_header_error_input_not_registered(self):
        p = policy()
        bad = run_batch("to,amt\nx,1\n", p, "h")
        self.assertEqual(bad["batch_registration"], "NOT_REGISTERED_INVALID_CSV")
        good = run_batch(f"recipient,amount,memo\n{A},1,fixed\n", p, "h")
        self.assertEqual(good["batch_registration"], "REGISTERED")
        self.assertEqual(good["pending_approval_total"], 1)

    # --- 부사장 2차 지시(9/15): 무효 CSV 는 REJECTED_INVALID_CSV, 응답 로그는 독립 사본 ---
    def test_H1_new_invalid_csv_rejected_without_registration(self):
        p = policy()
        for bad in ("to,amt\nx,1\n", f'recipient,amount,memo\n{A},1,"open\n{B},2,x\n', "", "recipient,amount,memo,amount\n"):
            with self.subTest(bad=bad[:20]):
                out = run_batch(bad, p, "n")
                self.assertEqual(out["stage"], "REJECTED_INVALID_CSV")
                self.assertEqual(out["batch_registration"], "NOT_REGISTERED_INVALID_CSV")
                self.assertTrue(out["header_errors"])
                self.assertEqual(out["rows"], [])
        self.assertNotIn("n", p.batches)          # 신규 배치 등록 없음
        self.assertEqual(p.batch_log, [])
        self.assertEqual(p.decision_log, [])       # 행별 정책 처리 없음
        self.assertEqual(p.reserved_units, 0)

    def test_H2_invalid_csv_on_existing_batch_preserves_records(self):
        p, text, first = self._first_run()
        out = run_batch(f'recipient,amount,memo\n{A},99,"unclosed\n', p, "g")
        self.assertEqual(out["stage"], "REJECTED_INVALID_CSV")
        self.assertEqual(out["rows"], [])
        self.assertEqual(p.batches["g"], first["csv_sha256"])   # 최초 등록 유지
        self.assertEqual(len(p.batch_log), 1)                    # 등록 기록 추가 없음
        self.assertEqual(len(p.decision_log), 2)
        self.assertEqual(p.reserved_units, 12 * UNIT + 2 * FEE)
        self.assertEqual(out["pending_approval_total"], 2)
        self.assertEqual(p.record_of("g:r1:2").amount_units, 10 * UNIT)

    def test_H3_response_logs_are_independent_copies(self):
        p, text, first = self._first_run()
        # 정상 응답의 로그 변경
        first["batch_log"][0]["outcome"] = "TAMPERED"
        first["batch_log"].append({"seq": 99})
        first["decision_log"][0]["amount_units"] = 1
        first["decision_log"].clear()
        self.assertEqual(p.batch_log, [{"seq": 1, "batch_id": "g", "csv_sha256": first["csv_sha256"],
                                        "first_csv_sha256": first["csv_sha256"], "outcome": "REGISTERED", "revision": 1}])
        self.assertEqual(p.decision_log[0].amount_units, 10 * UNIT)
        self.assertEqual(len(p.decision_log), 2)
        # 거부 응답(배치 충돌)의 로그 변경
        rej = run_batch(f"recipient,amount,memo\n{B},2,invoice 2\n{A},10,invoice 1\n", p, "g")
        self.assertEqual(rej["stage"], "REJECTED_BATCH_CONFLICT")
        rej["batch_log"][-1]["outcome"] = "TAMPERED"
        rej["batch_log"].clear()
        self.assertEqual(p.batch_log[-1]["outcome"], "BATCH_CONFLICT")
        self.assertEqual(len(p.batch_log), 2)
        # 무효 CSV 거부 응답의 로그 변경
        inv = run_batch("to,amt\nx,1\n", p, "g")
        inv["batch_log"][0]["outcome"] = "TAMPERED"
        inv["decision_log"][0]["outcome"] = "TAMPERED"
        self.assertEqual(p.batch_log[0]["outcome"], "REGISTERED")
        self.assertEqual(p.decision_log[0].outcome, "ACCEPTED_FOR_APPROVAL")


class RevisionCases(unittest.TestCase):
    """C29 로컬 구현: 명시적 수정 revision·이전 승인 무효화·예약 해제/재예약. 승인은 로컬 모의."""

    def setUp(self):
        self.p = policy()
        self.text1 = f"recipient,amount,memo\n{A},10,invoice 1\n{B},2,invoice 2\n"
        self.first = run_batch(self.text1, self.p, "g")
        self.sha1 = self.first["csv_sha256"]
        self.text2 = f"recipient,amount,memo\n{A},11,invoice 1\n{B},2,invoice 2\n"
        self.assertEqual(self.p.reserved_units, 12 * UNIT + 2 * FEE)

    def test_R1_revise_before_approval_releases_and_rereserves(self):
        out = revise_batch(self.text2, self.p, "g", base_revision=1, request_id="req1")
        self.assertEqual(out["stage"], STAGE)
        self.assertEqual(out["revision"], 2)
        rr = out["revision_request"]
        self.assertEqual((rr["superseded_revision"], rr["released_units"]), (1, 12 * UNIT + 2 * FEE))
        self.assertEqual(self.p.reserved_units, 13 * UNIT + 2 * FEE)  # 이전 예약 해제 후 통과 행만 재예약
        self.assertEqual(out["pending_approval_total"], 2)
        self.assertEqual([r["payment_id"] for r in out["rows"]], ["g:r2:2", "g:r2:3"])
        self.assertEqual(self.p.record_state["g:r1:2"], "SUPERSEDED")
        self.assertEqual(self.p.record_state["g:r1:3"], "SUPERSEDED")
        self.assertEqual(self.p.record_of("g:r1:2").amount_units, 10 * UNIT)  # 이력 보존(불변)
        sup = [d for d in self.p.decision_log if d.outcome == "SUPERSEDED_BY_REVISION"]
        self.assertEqual(len(sup), 2)
        self.assertEqual(self.p.revisions["g"][0]["status"], "SUPERSEDED")
        # 일반 run_batch 의 변경 입력 차단 유지: 옛 CSV 는 충돌, 새 CSV 는 REPLAY
        self.assertEqual(run_batch(self.text1, self.p, "g")["stage"], "REJECTED_BATCH_CONFLICT")
        again = run_batch(self.text2, self.p, "g")
        self.assertEqual([r["decision"] for r in again["rows"]], ["REPLAY", "REPLAY"])
        self.assertEqual(self.p.reserved_units, 13 * UNIT + 2 * FEE)

    def test_R2_revise_after_mock_approval_invalidates_it(self):
        ap = approve_batch(self.p, "g", 1, self.sha1)
        self.assertEqual(ap["outcome"], "APPROVED_LOCAL_MOCK")
        self.assertFalse(ap["approval"]["sent"])
        aid = ap["approval"]["approval_id"]
        self.assertTrue(self.p.is_approval_valid(aid, "g", 1, self.sha1))
        self.assertEqual(self.p.record_state["g:r1:2"], "APPROVED_LOCAL_MOCK")
        out = revise_batch(self.text2, self.p, "g", base_revision=1, request_id="req1")
        self.assertEqual(out["revision_request"]["invalidated_approvals"], [aid])
        self.assertEqual(self.p.approvals[aid]["status"], "INVALIDATED")
        self.assertFalse(self.p.is_approval_valid(aid, "g", 1, self.sha1))  # 이전 승인 재사용 차단
        self.assertEqual(self.p.reserved_units, 13 * UNIT + 2 * FEE)
        self.assertEqual(out["approved_local_mock_total"], 0)
        self.assertEqual(approve_batch(self.p, "g", 1, self.sha1)["outcome"], "REJECTED_STALE_REVISION")
        self.assertEqual(approve_batch(self.p, "g", 2, self.sha1)["outcome"], "REJECTED_HASH_MISMATCH")
        ap2 = approve_batch(self.p, "g", 2, out["csv_sha256"])
        self.assertEqual(ap2["outcome"], "APPROVED_LOCAL_MOCK")
        self.assertEqual(sorted(ap2["approval"]["payment_ids"]), ["g:r2:2", "g:r2:3"])
        self.assertEqual(approve_batch(self.p, "g", 2, out["csv_sha256"])["outcome"], "APPROVAL_REPLAY")

    def test_R3_invalid_revision_releases_without_new_reservation(self):
        aid = approve_batch(self.p, "g", 1, self.sha1)["approval"]["approval_id"]
        bad = f'recipient,amount,memo\n{A},99,"unclosed\n'
        out = revise_batch(bad, self.p, "g", base_revision=1, request_id="req-bad")
        self.assertEqual(out["stage"], "REVISION_ACCEPTED_INVALID_CSV")
        self.assertEqual(out["revision"], 2)
        self.assertEqual(out["revision_request"]["released_units"], 12 * UNIT + 2 * FEE)
        self.assertEqual(self.p.reserved_units, 0)              # 새 예약 0
        self.assertEqual(out["pending_approval_total"], 0)
        self.assertEqual(self.p.approvals[aid]["status"], "INVALIDATED")  # 이전 승인 되살리지 않음
        self.assertEqual(self.p.revisions["g"][-1]["status"], "INVALID_CSV")
        self.assertEqual(approve_batch(self.p, "g", 2, out["csv_sha256"])["outcome"], "REJECTED_INVALID_CSV")
        self.assertEqual(approve_batch(self.p, "g", 1, self.sha1)["outcome"], "REJECTED_STALE_REVISION")
        fixed = revise_batch(self.text2, self.p, "g", base_revision=2, request_id="req-fix")
        self.assertEqual((fixed["revision"], fixed["pending_approval_total"]), (3, 2))
        self.assertEqual(self.p.reserved_units, 13 * UNIT + 2 * FEE)

    def test_R4_same_edit_request_retry_does_not_duplicate(self):
        out1 = revise_batch(self.text2, self.p, "g", base_revision=1, request_id="req1")
        reserved, logs = self.p.reserved_units, len(self.p.decision_log)
        out2 = revise_batch(self.text2, self.p, "g", base_revision=1, request_id="req1")
        self.assertEqual(out2["stage"], "REVISION_REPLAY")
        self.assertEqual(out2["revision_request"]["revision"], out1["revision"])
        self.assertEqual(len(self.p.revisions["g"]), 2)              # revision 중복 없음
        self.assertEqual(self.p.reserved_units, reserved)             # 예약 중복 없음
        self.assertEqual(len(self.p.decision_log), logs)
        self.assertEqual(out2["pending_approval_total"], 2)
        text3 = f"recipient,amount,memo\n{A},12,invoice 1\n{B},2,invoice 2\n"
        out3 = revise_batch(text3, self.p, "g", base_revision=2, request_id="req1")  # 같은 request_id 다른 내용
        self.assertEqual(out3["stage"], "REJECTED_REQUEST_CONFLICT")
        self.assertEqual(len(self.p.revisions["g"]), 2)
        self.assertEqual(self.p.reserved_units, reserved)

    def test_R5_stale_revision_edit_and_no_change_rejected(self):
        revise_batch(self.text2, self.p, "g", base_revision=1, request_id="req1")
        reserved, logs = self.p.reserved_units, len(self.p.decision_log)
        text3 = f"recipient,amount,memo\n{A},12,invoice 1\n{B},2,invoice 2\n"
        stale = revise_batch(text3, self.p, "g", base_revision=1, request_id="req2")
        self.assertEqual(stale["stage"], "REJECTED_STALE_REVISION")
        same = revise_batch(self.text2, self.p, "g", base_revision=2, request_id="req3")
        self.assertEqual(same["stage"], "REJECTED_NO_CHANGE")
        unknown = revise_batch(text3, self.p, "nope", base_revision=1, request_id="req4")
        self.assertEqual(unknown["stage"], "REJECTED_UNKNOWN_BATCH")
        self.assertEqual(len(self.p.revisions["g"]), 2)
        self.assertEqual(self.p.reserved_units, reserved)
        self.assertEqual(len(self.p.decision_log), logs)

    def test_R7_approval_response_payment_ids_are_independent_copies(self):
        first = approve_batch(self.p, "g", 1, self.sha1)
        replay = approve_batch(self.p, "g", 1, self.sha1)
        self.assertEqual((first["outcome"], replay["outcome"]), ("APPROVED_LOCAL_MOCK", "APPROVAL_REPLAY"))
        aid = first["approval"]["approval_id"]
        expected_ids = ["g:r1:2", "g:r1:3"]
        reserved = self.p.reserved_units
        for resp in (first, replay):
            ids = resp["approval"]["payment_ids"]
            ids.append("g:r1:99")            # 추가
            ids.remove("g:r1:2")             # 삭제
            ids[0] = "x:r1:2"                # 교체
            resp["approval"]["status"] = "TAMPERED"
            resp["approval"]["payment_ids"] = ["nothing"]
            self.assertEqual(self.p.approvals[aid]["payment_ids"], expected_ids)   # 내부 승인 대상 불변
            self.assertEqual(self.p.approvals[aid]["status"], "VALID")
            self.assertEqual({pid: self.p.record_state[pid] for pid in expected_ids},
                             {pid: "APPROVED_LOCAL_MOCK" for pid in expected_ids})  # 행 상태 불변
            self.assertNotIn("g:r1:99", self.p.record_state)
            self.assertEqual(self.p.reserved_units, reserved)                      # 예약액 불변
            again = approve_batch(self.p, "g", 1, self.sha1)                        # 재승인 결과 불변
            self.assertEqual(again["outcome"], "APPROVAL_REPLAY")
            self.assertEqual(again["approval"]["payment_ids"], expected_ids)
        self.assertIsNot(first["approval"], replay["approval"])
        self.assertTrue(self.p.is_approval_valid(aid, "g", 1, self.sha1))

    def test_R6_other_batch_reservations_preserved(self):
        other = run_batch(f"recipient,amount,memo\n{C},5,other batch\n", self.p, "x")
        self.assertEqual(self.p.reserved_units, 17 * UNIT + 3 * FEE)
        ax = approve_batch(self.p, "x", 1, other["csv_sha256"])
        self.assertEqual(ax["outcome"], "APPROVED_LOCAL_MOCK")
        out = revise_batch(self.text2, self.p, "g", base_revision=1, request_id="req1")
        self.assertEqual(out["revision_request"]["released_units"], 12 * UNIT + 2 * FEE)  # g 의 예약만 해제
        self.assertEqual(self.p.reserved_units, 18 * UNIT + 3 * FEE)  # x 5+fee 유지, g 13+2fee
        self.assertEqual(self.p.record_state["x:r1:2"], "APPROVED_LOCAL_MOCK")
        self.assertTrue(self.p.is_approval_valid(ax["approval"]["approval_id"], "x", 1, other["csv_sha256"]))
        self.assertEqual(out["approved_local_mock_total"], 1)
        json.dumps(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
