"""SafeBatch AI · 지급 전 정책 검사 + 배치 revision·로컬 모의 승인 (외부 접속·서명·송금 없음).

행 단위 검사 순서(check_payment):
 1. 같은 payment_id 재요청 → 내용 해시 동일이면 기존 기록 반환(REPLAY, 두 번째 처리 없음),
    다르면 CONFLICT(기존 기록 불변).
 2. 수신자 허용목록 검사 → 아니면 BLOCKED_RECIPIENT_NOT_ALLOWED.
 3. 지급액 + 수수료 예약액 이 남은 예산을 넘으면 BLOCKED_BUDGET_EXCEEDED_INCL_FEE.
 4. 통과하면 예산을 예약하고 ACCEPTED_FOR_APPROVAL 로 기록한다(사람 확인·서명 대기, 전송 아님).

배치·revision(C29 로컬 구현):
 - register_batch: batch_id 최초 등록 시 revision 1. 같은 batch_id 다른 CSV 는 BATCH_CONFLICT.
 - supersede_revision: 명시적 수정 경로에서만 호출. 이전 revision 의 승인 대기·모의 승인 행을 SUPERSEDED 로
   바꾸고 그 예약액만 해제하며, 이전 revision 의 승인을 INVALIDATED 로 만든다. 기록은 삭제하지 않는다.
 - approve: 로컬 모의 승인. (batch_id, revision, csv_sha256) 가 현재 revision 과 정확히 일치할 때만 유효.
   오래된 revision·해시 불일치·무효 CSV revision 은 거부한다. 서명·전송은 하지 않는다.

모든 금액은 정수 최소단위. 수수료(fee_units)는 사람이 준 상한 추정값이며 실제 수수료가 아니다.
Decision 은 frozen 이며 현재 상태는 record_state 로 따로 관리한다.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field, asdict

PENDING = "PENDING_APPROVAL"
APPROVED = "APPROVED_LOCAL_MOCK"
SUPERSEDED = "SUPERSEDED"


def content_hash(recipient: str, amount_units: int, memo: str) -> str:
    payload = json.dumps(
        {"recipient": recipient, "amount_units": amount_units, "memo": memo},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Decision:
    """불변 기록. 반환된 객체의 필드를 바꾸려 하면 FrozenInstanceError 가 나며 원장·로그·예약액은 변하지 않는다."""
    seq: int
    payment_id: str
    outcome: str  # ACCEPTED_FOR_APPROVAL | REPLAY | CONFLICT | BLOCKED_RECIPIENT_NOT_ALLOWED | BLOCKED_BUDGET_EXCEEDED_INCL_FEE | SUPERSEDED_BY_REVISION
    reason: str
    recipient: str
    amount_units: int
    fee_units: int
    content_sha256: str
    budget_remaining_before: int
    budget_remaining_after: int
    row_no: int | None = None
    memo: str = ""
    stopped: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PaymentPolicy:
    budget_units: int            # 배치 총예산(지급액+수수료 포함)
    fee_units: int               # 건당 수수료 예약액(사람이 준 상한 추정, 실제 fee 아님)
    allowlist: frozenset[str]    # 사전 등록 수신자
    reserved_units: int = 0
    records: dict[str, Decision] = field(default_factory=dict)  # payment_id -> 첫 결정(불변)
    decision_log: list[Decision] = field(default_factory=list)
    batches: dict[str, str] = field(default_factory=dict)  # batch_id -> 현재 revision 의 CSV sha256 (변경 입력 차단용)
    batch_log: list[dict] = field(default_factory=list)   # 배치 단위 등록/충돌/수정 기록
    revisions: dict[str, list[dict]] = field(default_factory=dict)  # batch_id -> [{revision, csv_sha256, status, payment_ids, request_id, supersedes}]
    record_state: dict[str, str] = field(default_factory=dict)      # payment_id -> PENDING_APPROVAL | APPROVED_LOCAL_MOCK | SUPERSEDED
    approvals: dict[str, dict] = field(default_factory=dict)        # approval_id -> 승인 기록(VALID | INVALIDATED)
    approval_log: list[dict] = field(default_factory=list)
    revision_requests: dict[tuple, dict] = field(default_factory=dict)  # (batch_id, request_id) -> {csv_sha256, revision}

    def __post_init__(self) -> None:
        if not isinstance(self.budget_units, int) or not isinstance(self.fee_units, int):
            raise TypeError("budget_units/fee_units must be int (no floats)")
        if self.budget_units < 0 or self.fee_units < 0:
            raise ValueError("negative budget/fee")

    @property
    def remaining_units(self) -> int:
        return self.budget_units - self.reserved_units

    # ---------- 행 단위 정책 ----------
    def check_payment(self, payment_id: str, recipient: str, amount_units: int,
                      memo: str = "", row_no: int | None = None) -> Decision:
        if not isinstance(amount_units, int) or isinstance(amount_units, bool) or amount_units <= 0:
            raise TypeError("amount_units must be a positive int")
        h = content_hash(recipient, amount_units, memo)
        before = self.remaining_units

        prior = self.records.get(payment_id)
        if prior is not None:
            if prior.content_sha256 == h:
                self._log(payment_id, "REPLAY",
                          f"same payment_id and same content; returning first decision seq={prior.seq}; no second processing",
                          recipient, amount_units, h, before, before, row_no, memo, stopped=True)
                return prior  # 기존 업무 기록 그대로 반환
            self._log(payment_id, "CONFLICT",
                      f"same payment_id with different content (first seq={prior.seq}); existing record unchanged",
                      recipient, amount_units, h, before, before, row_no, memo, stopped=True)
            return self.decision_log[-1]

        if recipient not in self.allowlist:
            d = self._log(payment_id, "BLOCKED_RECIPIENT_NOT_ALLOWED",
                          "recipient is not in the pre-registered allowlist", recipient, amount_units, h,
                          before, before, row_no, memo, stopped=True)
            self.records[payment_id] = d
            return d

        need = amount_units + self.fee_units
        if need > before:
            d = self._log(payment_id, "BLOCKED_BUDGET_EXCEEDED_INCL_FEE",
                          f"amount+fee={need} exceeds remaining budget={before} (fee included)",
                          recipient, amount_units, h, before, before, row_no, memo, stopped=True)
            self.records[payment_id] = d
            return d

        self.reserved_units += need
        d = self._log(payment_id, "ACCEPTED_FOR_APPROVAL",
                      "policy passed; budget reserved incl. fee; awaiting human confirmation and signature (not sent)",
                      recipient, amount_units, h, before, self.remaining_units, row_no, memo, stopped=False)
        self.records[payment_id] = d
        self.record_state[payment_id] = PENDING
        return d

    def _log(self, payment_id, outcome, reason, recipient, amount_units, h, before, after,
             row_no, memo, stopped) -> Decision:
        d = Decision(seq=len(self.decision_log) + 1, payment_id=payment_id, outcome=outcome, reason=reason,
                     recipient=recipient, amount_units=amount_units, fee_units=self.fee_units,
                     content_sha256=h, budget_remaining_before=before, budget_remaining_after=after,
                     row_no=row_no, memo=memo, stopped=stopped)
        self.decision_log.append(d)
        return d

    def export_log(self) -> list[dict]:
        return [d.to_dict() for d in self.decision_log]  # 사본(dict); 원본 Decision 은 frozen

    def record_of(self, payment_id: str) -> Decision | None:
        return self.records.get(payment_id)

    def pending_records(self) -> list[Decision]:
        return [self.records[pid] for pid, st in self.record_state.items() if st == PENDING]

    def approved_records(self) -> list[Decision]:
        return [self.records[pid] for pid, st in self.record_state.items() if st == APPROVED]

    # ---------- 배치·revision ----------
    def register_batch(self, batch_id: str, csv_sha256: str) -> str:
        """최초 등록이면 REGISTERED(revision 1), 같은 해시면 SAME, 다른 해시면 BATCH_CONFLICT (기존 기록·예약 불변)."""
        first = self.batches.get(batch_id)
        if first is None:
            self.batches[batch_id] = csv_sha256
            self.revisions[batch_id] = [{"revision": 1, "csv_sha256": csv_sha256, "status": "ACTIVE",
                                         "payment_ids": [], "request_id": None, "supersedes": None}]
            outcome = "REGISTERED"
        elif first == csv_sha256:
            outcome = "SAME"
        else:
            outcome = "BATCH_CONFLICT"
        self._batch_log(batch_id, csv_sha256, outcome)
        return outcome

    def _batch_log(self, batch_id: str, csv_sha256: str, outcome: str) -> None:
        revs = self.revisions.get(batch_id) or []
        self.batch_log.append({"seq": len(self.batch_log) + 1, "batch_id": batch_id, "csv_sha256": csv_sha256,
                               "first_csv_sha256": revs[0]["csv_sha256"] if revs else csv_sha256,
                               "outcome": outcome, "revision": revs[-1]["revision"] if revs else None})

    def current_revision(self, batch_id: str) -> dict | None:
        revs = self.revisions.get(batch_id)
        return revs[-1] if revs else None

    def attach_payment(self, batch_id: str, payment_id: str) -> None:
        cur = self.current_revision(batch_id)
        if cur is not None and payment_id not in cur["payment_ids"]:
            cur["payment_ids"].append(payment_id)

    def supersede_revision(self, batch_id: str, new_csv_sha256: str, request_id: str, new_status: str) -> dict:
        """명시적 수정 접수. 이전 revision 의 대기/모의승인 행을 SUPERSEDED 로 바꾸고 그 예약액만 해제, 승인을 무효화한다."""
        cur = self.current_revision(batch_id)
        if cur is None:
            raise KeyError(batch_id)
        new_no = cur["revision"] + 1
        released = 0
        for pid in cur["payment_ids"]:
            if self.record_state.get(pid) in (PENDING, APPROVED):
                d = self.records[pid]
                need = d.amount_units + d.fee_units
                before = self.remaining_units
                self.reserved_units -= need
                released += need
                self.record_state[pid] = SUPERSEDED
                self._log(pid, "SUPERSEDED_BY_REVISION",
                          f"revision {cur['revision']} superseded by revision {new_no} (request {request_id}); reservation released",
                          d.recipient, d.amount_units, d.content_sha256, before, self.remaining_units, d.row_no, d.memo,
                          stopped=True)
        invalidated = []
        for aid, ap in self.approvals.items():
            if ap["batch_id"] == batch_id and ap["revision"] == cur["revision"] and ap["status"] == "VALID":
                ap["status"] = "INVALIDATED"
                ap["invalidated_by_revision"] = new_no
                invalidated.append(aid)
                self.approval_log.append({"seq": len(self.approval_log) + 1, "approval_id": aid,
                                          "outcome": "INVALIDATED", "reason": f"superseded by revision {new_no}"})
        cur["status"] = "SUPERSEDED"
        self.revisions[batch_id].append({"revision": new_no, "csv_sha256": new_csv_sha256, "status": new_status,
                                         "payment_ids": [], "request_id": request_id, "supersedes": cur["revision"]})
        self.batches[batch_id] = new_csv_sha256
        self._batch_log(batch_id, new_csv_sha256, "REVISED" if new_status == "ACTIVE" else "REVISED_INVALID_CSV")
        return {"revision": new_no, "superseded_revision": cur["revision"], "released_units": released,
                "invalidated_approvals": invalidated}

    # ---------- 로컬 모의 승인 ----------
    def display_digest(self, batch_id: str, revision: int, network: str = "nile", extra: dict | None = None) -> str | None:
        """사람 확인 화면에 보여준 내용의 지문: 그 revision 의 지급 후보 행(행번호·수신자·금액·메모)+수수료 예약액+예산+
        CSV 해시+네트워크(+extra: 발신 EOA·TRX 총상한·에너지 한도·절대 기한·토큰·raw tx/txID 등 사람이 본 전체 조건, 9/28 VP 링크 검수 2).
        승인 시 저장하고 서명 직전 같은 extra 로 재계산해 같아야 한다(9/28 R1)."""
        cur = self.current_revision(batch_id)
        if cur is None or cur["revision"] != revision:
            return None
        rows = []
        for pid in cur["payment_ids"]:
            rec = self.records.get(pid)
            if rec is None or self.record_state.get(pid) not in (PENDING, APPROVED):
                continue
            rows.append([pid, rec.row_no, rec.recipient, rec.amount_units, rec.memo])
        rows.sort()
        payload = {"batch_id": batch_id, "revision": revision, "csv_sha256": cur["csv_sha256"], "network": network,
                   "fee_units_per_payment": self.fee_units, "budget_units": self.budget_units, "rows": rows}
        if extra is not None:
            payload["extra"] = extra
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    def approve(self, batch_id: str, revision: int, csv_sha256: str, confirmed_by: str | None = None,
                network: str = "nile", displayed_sha256: str | None = None, extra: dict | None = None) -> dict:
        """(batch_id, revision, csv_sha256) 가 현재 revision 과 정확히 일치할 때만 유효한 승인. 서명·전송 없음.
        confirmed_by 가 있으면 mode=HUMAN_CONFIRMED 로 기록하고 display_digest 를 함께 고정한다(사람이 본 화면과 결합).
        없으면 LOCAL_MOCK — 실제 제출 경로(build_order 기본값)에서는 거부된다."""
        cur = self.current_revision(batch_id)
        approval = None
        if cur is None:
            outcome, reason = "REJECTED_UNKNOWN_BATCH", "batch not registered"
        elif revision != cur["revision"]:
            outcome, reason = "REJECTED_STALE_REVISION", f"revision {revision} is not current (current={cur['revision']})"
        elif csv_sha256 != cur["csv_sha256"]:
            outcome, reason = "REJECTED_HASH_MISMATCH", "csv_sha256 does not match the current revision"
        elif cur["status"] != "ACTIVE":
            outcome, reason = "REJECTED_INVALID_CSV", f"revision status is {cur['status']}"
        elif confirmed_by and displayed_sha256 is not None \
                and displayed_sha256 != self.display_digest(batch_id, revision, network, extra):
            # 9/28 GPT#1: UI 가 실제로 보여준 화면 지문을 받아 현재 내용과 대조한다(화면 이후 변경 → 승인 거절)
            outcome, reason = "REJECTED_DISPLAY_MISMATCH", "content shown to the human differs from current rows/fees"
        else:
            aid = f"{batch_id}:r{revision}:approval"
            existing = self.approvals.get(aid)
            if existing is not None and existing["status"] == "VALID":
                outcome, reason, approval = "APPROVAL_REPLAY", "valid approval already exists for this revision", existing
            else:
                pids = [pid for pid in cur["payment_ids"] if self.record_state.get(pid) == PENDING]
                for pid in pids:
                    self.record_state[pid] = APPROVED
                mode = "HUMAN_CONFIRMED" if confirmed_by else "LOCAL_MOCK"
                approval = {"approval_id": aid, "batch_id": batch_id, "revision": revision, "csv_sha256": csv_sha256,
                            "status": "VALID", "mode": mode, "payment_ids": pids, "sent": False, "signed": False,
                            "confirmed_by": confirmed_by, "network": network,
                            "displayed_sha256": self.display_digest(batch_id, revision, network, extra),
                            "displayed_extra": copy.deepcopy(extra) if extra is not None else None}
                self.approvals[aid] = approval
                outcome = "APPROVED_HUMAN_CONFIRMED" if confirmed_by else "APPROVED_LOCAL_MOCK"
                reason = f"{len(pids)} pending rows marked approved ({mode}, nothing sent)"
        self.approval_log.append({"seq": len(self.approval_log) + 1, "batch_id": batch_id, "revision": revision,
                                  "csv_sha256": csv_sha256, "outcome": outcome, "reason": reason})
        return {"outcome": outcome, "reason": reason, "batch_id": batch_id, "revision": revision,
                "csv_sha256": csv_sha256, "approval": copy.deepcopy(approval) if approval else None}  # 중첩 항목까지 사본

    def is_approval_valid(self, approval_id: str, batch_id: str, revision: int, csv_sha256: str) -> bool:
        ap = self.approvals.get(approval_id)
        cur = self.current_revision(batch_id)
        return bool(ap and cur and ap["status"] == "VALID" and ap["batch_id"] == batch_id
                    and ap["revision"] == revision == cur["revision"] and ap["csv_sha256"] == csv_sha256 == cur["csv_sha256"])
