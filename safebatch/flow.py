"""SafeBatch AI · 로컬 처리 흐름: CSV 검증 → 지급 후보 → 정책 검사 → 승인 대기 기록 (+ 명시적 수정 revision).

- run_batch: 헤더·CSV 구문 오류는 REJECTED_INVALID_CSV(등록·행별 처리 없음, 기존 기록 보존).
  같은 batch_id 로 다른 CSV 가 오면 행별 처리 전 REJECTED_BATCH_CONFLICT (변경 입력 차단 유지).
  OK 행만 지급 후보. payment_id = "<batch_id>:r<revision>:<원본 행번호>". 같은 CSV 재실행은 REPLAY.
- revise_batch: 명시적 수정 경로. 기존 배치·현재 revision(base_revision)·request_id 를 요구한다.
  접수되면 이전 revision 의 승인·승인 대기를 무효화하고(이력 보존) 그 배치의 예약액만 해제한 뒤
  수정본을 CSV·정책 재검사해 통과한 행만 다시 예약·승인 대기로 둔다. 무효 CSV 도 revision 으로 접수되며
  이전 승인을 되살리지 않고 새 예약은 0 이다. 같은 request_id 재시도는 REVISION_REPLAY(중복 revision 없음).
- approve_batch: 로컬 모의 승인. (batch_id, revision, csv_sha256) 가 현재와 일치할 때만 유효.
- 흐름은 승인 대기/모의 승인에서 끝난다. 서명·전송·외부 호출은 없다.
- 응답의 decision_log·batch_log·approval_log 는 내부 기록과 독립된 사본이다.
"""
from __future__ import annotations

from .csvcheck import CheckResult, check_csv_text, format_units
from .policy import PaymentPolicy

STAGE = "AWAITING_HUMAN_APPROVAL"
STAGE_INVALID = "REJECTED_INVALID_CSV"
STAGE_CONFLICT = "REJECTED_BATCH_CONFLICT"


def _response(batch_id: str, stage: str, check: CheckResult, policy: PaymentPolicy, batch_registration: str,
              rows: list[dict], decision_counts: dict, new_pending: int, new_pending_units: int) -> dict:
    pending = policy.pending_records()
    approved = policy.approved_records()
    cur = policy.current_revision(batch_id)
    return {
        "batch_id": batch_id,
        "revision": cur["revision"] if cur else None,
        "stage": stage,
        "sent": False,
        "signed": False,
        "csv_sha256": check.content_sha256,
        "batch_registration": batch_registration,
        "header_errors": list(check.header_errors),
        "validation_counts": dict(check.counts),
        "decision_counts": decision_counts,
        "new_pending_this_run": new_pending,
        "new_pending_amount_units": new_pending_units,
        "pending_approval_total": len(pending),
        "pending_approval_amount_units": sum(d.amount_units for d in pending),
        "approved_local_mock_total": len(approved),
        "fee_units_per_payment": policy.fee_units,
        "budget_units": policy.budget_units,
        "reserved_units": policy.reserved_units,
        "remaining_units": policy.remaining_units,
        "rows": rows,
        "decision_log": policy.export_log(),               # dict 사본
        "batch_log": [dict(e) for e in policy.batch_log],     # 항목 단위 사본; 응답 수정이 내부 기록에 닿지 않음
        "approval_log": [dict(e) for e in policy.approval_log],
    }


def _process_rows(check: CheckResult, policy: PaymentPolicy, batch_id: str, revision: int):
    rows_out: list[dict] = []
    outcomes: dict[str, int] = {}
    new_pending_units = 0
    new_pending = 0
    for r in check.rows:
        item = {
            "row_no": r.row_no,
            "recipient": r.recipient,
            "amount_text": r.amount_text,
            "amount_units": r.amount_units,
            "amount": format_units(r.amount_units) if r.amount_units is not None else None,
            "memo": r.memo,
            "validation": r.status,
            "validation_reasons": list(r.reasons),
            "candidate": r.status == "OK",
            "payment_id": None,
            "decision": None,
            "decision_reason": None,
            "decision_seq": None,
            "stopped": None,
        }
        if item["candidate"]:
            pid = f"{batch_id}:r{revision}:{r.row_no}"
            before_seq = len(policy.decision_log)
            d = policy.check_payment(pid, r.recipient, r.amount_units, r.memo, row_no=r.row_no)
            logged = policy.decision_log[-1]  # 이번 호출이 남긴 로그(REPLAY 면 d 는 최초 기록)
            policy.attach_payment(batch_id, pid)
            item.update({
                "payment_id": pid,
                "decision": logged.outcome,
                "decision_reason": logged.reason,
                "decision_seq": logged.seq,
                "stopped": logged.stopped,
                "first_record_seq": d.seq,
                "record_state": policy.record_state.get(pid),
            })
            outcomes[logged.outcome] = outcomes.get(logged.outcome, 0) + 1
            if logged.outcome == "ACCEPTED_FOR_APPROVAL" and logged.seq > before_seq:
                new_pending += 1
                new_pending_units += r.amount_units
        rows_out.append(item)
    return rows_out, outcomes, new_pending, new_pending_units


def run_batch(csv_text: str, policy: PaymentPolicy, batch_id: str) -> dict:
    check = check_csv_text(csv_text)

    if check.header_errors:  # 헤더 누락/중복, 닫히지 않은 따옴표 등 구문 오류, 빈 파일
        return _response(batch_id, STAGE_INVALID, check, policy, "NOT_REGISTERED_INVALID_CSV", [], {}, 0, 0)

    batch_outcome = policy.register_batch(batch_id, check.content_sha256)
    if batch_outcome == "BATCH_CONFLICT":
        out = _response(batch_id, STAGE_CONFLICT, check, policy, batch_outcome, [], {}, 0, 0)
        out["batch_conflict"] = {
            "first_csv_sha256": policy.revisions[batch_id][0]["csv_sha256"],
            "current_csv_sha256": policy.batches[batch_id],
            "csv_sha256": check.content_sha256,
            "reason": "same batch_id with different CSV content; rejected before per-row processing (use revise_batch for explicit edits)",
        }
        return out

    revision = policy.current_revision(batch_id)["revision"]
    rows_out, outcomes, new_pending, new_pending_units = _process_rows(check, policy, batch_id, revision)
    return _response(batch_id, STAGE, check, policy, batch_outcome, rows_out, outcomes, new_pending, new_pending_units)


def revise_batch(csv_text: str, policy: PaymentPolicy, batch_id: str, base_revision: int, request_id: str) -> dict:
    """명시적 수정. 접수 시 이전 revision 승인·대기 무효화 + 그 배치 예약 해제 + 수정본 재검사·재예약."""
    check = check_csv_text(csv_text)
    cur = policy.current_revision(batch_id)
    if cur is None:
        return _response(batch_id, "REJECTED_UNKNOWN_BATCH", check, policy, "NOT_REGISTERED_UNKNOWN_BATCH", [], {}, 0, 0)

    prev = policy.revision_requests.get((batch_id, request_id))
    if prev is not None:
        if prev["csv_sha256"] == check.content_sha256:
            out = _response(batch_id, "REVISION_REPLAY", check, policy, "REVISION_REPLAY", [], {}, 0, 0)
            out["revision_request"] = {"request_id": request_id, "revision": prev["revision"],
                                       "reason": "same request_id and same content already accepted; no new revision or reservation"}
            return out
        out = _response(batch_id, "REJECTED_REQUEST_CONFLICT", check, policy, "REJECTED_REQUEST_CONFLICT", [], {}, 0, 0)
        out["revision_request"] = {"request_id": request_id, "revision": prev["revision"],
                                   "reason": "same request_id with different content; existing revision unchanged"}
        return out

    if base_revision != cur["revision"]:
        out = _response(batch_id, "REJECTED_STALE_REVISION", check, policy, "REJECTED_STALE_REVISION", [], {}, 0, 0)
        out["revision_request"] = {"request_id": request_id, "base_revision": base_revision, "current_revision": cur["revision"],
                                   "reason": "edit targets a revision that is not current"}
        return out

    if check.content_sha256 == cur["csv_sha256"]:
        out = _response(batch_id, "REJECTED_NO_CHANGE", check, policy, "REJECTED_NO_CHANGE", [], {}, 0, 0)
        out["revision_request"] = {"request_id": request_id, "reason": "content identical to current revision; nothing to revise"}
        return out

    new_status = "INVALID_CSV" if check.header_errors else "ACTIVE"
    info = policy.supersede_revision(batch_id, check.content_sha256, request_id, new_status)
    policy.revision_requests[(batch_id, request_id)] = {"csv_sha256": check.content_sha256, "revision": info["revision"]}

    if check.header_errors:
        out = _response(batch_id, "REVISION_ACCEPTED_INVALID_CSV", check, policy, "REVISED_INVALID_CSV", [], {}, 0, 0)
    else:
        rows_out, outcomes, new_pending, new_pending_units = _process_rows(check, policy, batch_id, info["revision"])
        out = _response(batch_id, STAGE, check, policy, "REVISED", rows_out, outcomes, new_pending, new_pending_units)
    out["revision_request"] = {"request_id": request_id, "base_revision": base_revision, **info}
    return out


def approve_batch(policy: PaymentPolicy, batch_id: str, revision: int, csv_sha256: str,
                  confirmed_by: str | None = None, network: str = "nile", displayed_sha256: str | None = None, extra: dict | None = None) -> dict:
    """승인. 현재 revision·해시와 일치할 때만 유효. 서명·전송 없음.
    confirmed_by 가 있으면 사람 확인(HUMAN_CONFIRMED)으로 기록하고 화면 지문(display_digest)을 함께 고정한다.
    없으면 LOCAL_MOCK(검사용)이며 실제 주문서 생성 기본값에서 거부된다."""
    return policy.approve(batch_id, revision, csv_sha256, confirmed_by=confirmed_by, network=network,
                          displayed_sha256=displayed_sha256, extra=extra)
