"""SafeBatch AI · 지급 intent 원장 (append-only JSONL) + nonce 잠금.

Kimi 1회 자문(9/28) 채택 항목 ①: timeout/응답 유실 뒤 새 nonce 로 재제출해 이중 지급하는 사고를 막는다.
- 한 지급(payment_id)마다 상태 전이를 **추가만** 한다(수정·삭제 없음): DRAFTED → AWAITING_HUMAN → SIGNED → SUBMITTED
  → UNKNOWN | ACCEPTED → CONFIRMED | FAILED | REJECTED. 마지막 행이 현재 상태다.
- `nonce_locked(user, nonce)`: 같은 EOA 의 nonce 가 SUBMITTED/UNKNOWN/ACCEPTED 로 열려 있으면 True.
- `can_submit(payment_id, user, nonce)`: 같은 지급이 이미 SUBMITTED/UNKNOWN/ACCEPTED/CONFIRMED 이면 거부(재제출 금지),
  다른 지급이 그 nonce 를 열어 두었으면 거부. 상태 조회로 CONFIRMED/FAILED 가 확정되기 전에는 풀리지 않는다.
- 표준 라이브러리만. 파일이 없으면 빈 원장. 손상 행은 세어서 보고하고 조용히 버리지 않는다.
"""
from __future__ import annotations

import datetime
import json
import pathlib

SCOPE_NILE = "nile:3448148188:THQGuFzL87ZqhxkgqYEryRAd7gqFqL5rdc"   # network:chainId:GasFreeController
SCOPE_NILE_TRC20 = "nile:3448148188:trc20:TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"   # 일반 TRC20 전송(사장 선택 9/28 주경로)
OPEN_STATES = {"SUBMITTED", "UNKNOWN", "ACCEPTED"}
NO_RESUBMIT_STATES = OPEN_STATES | {"CONFIRMED"}
TERMINAL = {"CONFIRMED", "FAILED", "REJECTED", "CANCELLED"}
ALLOWED = {
    None: {"DRAFTED"},
    "DRAFTED": {"AWAITING_HUMAN", "CANCELLED"},
    "AWAITING_HUMAN": {"SIGNED", "CANCELLED"},
    "SIGNED": {"SUBMITTED", "CANCELLED"},
    "SUBMITTED": {"UNKNOWN", "ACCEPTED", "REJECTED"},
    "UNKNOWN": {"ACCEPTED", "CONFIRMED", "FAILED", "REJECTED", "UNKNOWN"},
    "ACCEPTED": {"CONFIRMED", "FAILED", "ACCEPTED", "UNKNOWN"},     # 9/29 VP: 블록에 있던 거래가 조회에서 사라지면 UNKNOWN 으로 되돌려 안전 종결 절차로
}


class IntentError(Exception):
    pass


class IntentLog:
    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.bad_lines = 0

    # ── 읽기 ────────────────────────────────────────────────────────────
    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        self.bad_lines = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                self.bad_lines += 1
        return out

    def current(self, payment_id: str) -> dict | None:
        cur = None
        for e in self.entries():
            if e.get("payment_id") == payment_id:
                cur = e
        return cur

    def state(self, payment_id: str) -> str | None:
        c = self.current(payment_id)
        return c.get("state") if c else None

    def open_nonces(self, user: str) -> dict[int, str]:
        """user EOA 별 열린 nonce → payment_id."""
        latest: dict[str, dict] = {}
        for e in self.entries():
            latest[e["payment_id"]] = e
        out = {}
        for pid, e in latest.items():
            if (e.get("user") == user and e.get("state") in OPEN_STATES and e.get("nonce") is not None
                    and (e.get("scope") or SCOPE_NILE) == SCOPE_NILE):
                out[int(e["nonce"])] = pid
        return out

    def open_payments(self, user: str) -> dict[str, dict]:
        """user EOA 의 열린(SUBMITTED/UNKNOWN/ACCEPTED) 지급 전부 — 범위(GasFree/일반) 무관. 계정 단위 잠금용."""
        latest: dict[str, dict] = {}
        for e in self.entries():
            latest[e["payment_id"]] = e
        return {pid: e for pid, e in latest.items() if e.get("user") == user and e.get("state") in OPEN_STATES}

    def reserve_broadcast(self, payment_id: str, user: str, tx_id: str, scope: str = SCOPE_NILE_TRC20, **fields) -> tuple[bool, str]:
        """일반 Nile 방송 예약(원자적, 잠금+fsync): 상태가 SIGNED 이고, 같은 계정에 열린 지급이 하나도 없을 때만
        SUBMITTED(tx_hash=txID) 를 기록한다. 같은 payment_id 재예약·다른 열린 지급 존재·손상 행이면 거부."""
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path(), "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                st = self.state(payment_id)
                if self.bad_lines:
                    return False, f"intent log has {self.bad_lines} corrupt line(s); fail-closed until repaired"
                if st in NO_RESUBMIT_STATES:
                    return False, f"payment {payment_id} already {st}; query txID first, never rebroadcast a new tx"
                if st != "SIGNED":
                    return False, f"payment {payment_id} is {st}, not SIGNED"
                opened = self.open_payments(user)
                if opened:
                    return False, f"account has unresolved submission(s) {sorted(opened)}; resolve before any new broadcast"
                if not tx_id or len(tx_id) != 64:
                    return False, "tx_id must be a 64-hex txID"
                self._append_locked(payment_id, "SUBMITTED", user=user, tx_hash=tx_id, scope=scope, **fields)
                return True, "reserved"
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def stored_trace_id(self, payment_id: str) -> str | None:
        cur = self.current(payment_id) or {}
        return cur.get("trace_id")

    def nonce_locked(self, user: str, nonce: int) -> bool:
        return int(nonce) in self.open_nonces(user)

    def can_submit(self, payment_id: str, user: str, nonce: int) -> tuple[bool, str]:
        st = self.state(payment_id)                       # entries() 를 읽으며 bad_lines 갱신
        if self.bad_lines:
            return False, f"intent log has {self.bad_lines} corrupt line(s); fail-closed until repaired"   # Grok#1
        if st in NO_RESUBMIT_STATES:
            return False, f"payment {payment_id} already {st}; query status first, never resubmit"
        if st != "SIGNED":
            return False, f"payment {payment_id} is {st}, not SIGNED"
        opened = self.open_nonces(user)
        holder = opened.get(int(nonce))
        if holder and holder != payment_id:
            return False, f"nonce {nonce} held open by {holder}"
        others = {pid for pid in opened.values() if pid != payment_id} | {pid for pid in self.open_payments(user) if pid != payment_id}
        if others:                                        # Grok#5(b)+Grok-02#3: 같은 계정에 미해결 지급이 있으면(범위 무관) 새 제출 금지
            return False, f"account has unresolved submission(s) {sorted(others)}; resolve before any new submit"
        return True, "ok"

    def reserve_submit(self, payment_id: str, user: str, nonce: int, **fields) -> tuple[bool, str]:
        """원자적 예약(Grok#2): 파일 잠금 안에서 다시 읽고 can_submit 을 통과하면 SUBMITTED 를 append+fsync 한다.
        두 프로세스/스레드가 동시에 와도 하나만 True 를 받는다."""
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path(), "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                ok, why = self.can_submit(payment_id, user, nonce)
                if not ok:
                    return False, why
                self._append_locked(payment_id, "SUBMITTED", user=user, nonce=nonce, **fields)   # 같은 잠금 보유 중 기록
                return True, "reserved"
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    # ── 쓰기(추가만) ─────────────────────────────────────────────────────
    def _lock_path(self):
        return self.path.with_suffix(self.path.suffix + ".lock")

    def append(self, payment_id: str, state: str, *, user: str | None = None, nonce: int | None = None,
               **fields) -> dict:
        """모든 상태 전이는 파일 잠금 안에서 읽기→검사→기록→fsync (9/28 GPT-02 #1: 잠금 밖 전이가 SUBMITTED→SIGNED 로 되돌리는 경쟁 차단)."""
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path(), "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                return self._append_locked(payment_id, state, user=user, nonce=nonce, **fields)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _append_locked(self, payment_id: str, state: str, *, user: str | None = None, nonce: int | None = None,
                       **fields) -> dict:
        prev = self.state(payment_id)
        if state not in ALLOWED.get(prev, set()):
            raise IntentError(f"illegal transition {prev} -> {state} for {payment_id}")
        cur = self.current(payment_id) or {}
        rec = {
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "payment_id": payment_id, "state": state,
            "user": user if user is not None else cur.get("user"),
            "nonce": nonce if nonce is not None else cur.get("nonce"),
        }
        for k in ("batch_id", "revision", "csv_sha256", "row_no", "memo", "request_id", "trace_id", "tx_hash",
                  "approval_id", "receiver", "value", "max_fee", "reason"):
            if k in fields:
                rec[k] = fields[k]
            elif k in cur:
                rec[k] = cur[k]
        rec["scope"] = fields.get("scope") or cur.get("scope") or SCOPE_NILE   # Grok#5: 잠금 범위(망·컨트롤러)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import os
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())                         # Grok#1: 디스크 고정 전에는 네트워크 호출로 못 넘어간다
        return rec
