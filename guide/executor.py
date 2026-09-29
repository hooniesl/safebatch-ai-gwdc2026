"""실제 실행 진입점(안내 계획 → SafeBatch 코어 → GasFree). 9/28 20:15 부사장 검수 3항목 연결.

- 견적(quote): `quote_fn()` 이 provider 수수료·nonce·allowSubmit·잔액을 **현재 시각과 함께** 돌려준다. 최초 계획과
  실행 직전 재견적을 비교해 수수료/nonce/allowSubmit 이 달라지면 실행하지 않는다(견적 유효기간 QUOTE_TTL_S).
- 시각: `now_fn` 기본값은 실제 시계(time.time). 승인 대기 중 기한 경과는 실행 직전에 걸린다.
- 영속 원장: 승인·제출 상태는 safebatch.intent_log.IntentLog(JSONL, fsync, flock) 에 남는다. 같은 지급(payment_id)의
  재전달·재시작·동시 요청은 IntentLog.reserve_submit 이 막는다. 승인 기록은 approvals JSONL 에 append 되고 재사용은 거부.
- 응답 유실: 제출 콜백이 예외/None 을 내면 `UNKNOWN` 으로 영속(재전송 금지), 조회로만 해소(gasfree_order.resolve_unknown).
  제출 전에 끝난 경우만 NOT_SUBMITTED.
- 이 모듈은 서명하지 않는다. 서명은 사람(TronLink)이 하고, verify_signed 가 주문서와 대조한다.
"""
from __future__ import annotations

import csv
import dataclasses
import fcntl
import io
import json
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from safebatch.flow import run_batch, approve_batch  # noqa: E402
from safebatch.policy import PaymentPolicy  # noqa: E402
from safebatch import gasfree_order as go  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402
from safebatch import tip712  # noqa: E402
import demo_flows as D  # noqa: E402

QUOTE_TTL_S = 120
LOG_DIR = HERE / "logs"


class Quote:
    """provider 조회 결과 스냅샷(불변 dict)."""
    def __init__(self, address_info: dict, tokens_info: list, provider_address: str, quoted_at: int, fee_units: int):
        self.address_info = address_info
        self.tokens_info = tokens_info
        self.provider_address = provider_address
        self.quoted_at = int(quoted_at)
        self.fee_units = int(fee_units)

    def key(self) -> tuple:
        d = (self.address_info.get("data") or self.address_info)
        return (self.fee_units, d.get("nonce"), d.get("allowSubmit", d.get("allow_submit")), self.provider_address)


def quote_from_client(client, owner_eoa: str, token_symbol: str = "USDT", now_fn=time.time) -> Quote:
    """GasFree 실조회로 견적 생성. 실패는 예외(호출자가 '견적 없음' 으로 처리)."""
    t = client.tokens(); p = client.providers(); a = client.address(owner_eoa)
    if not (go_ok(t) and go_ok(p) and go_ok(a)):
        raise RuntimeError("quote failed: provider responses not ok")
    tokens = (t["body"].get("data") or {}).get("tokens") or []
    provs = (p["body"].get("data") or {}).get("providers") or []
    d = a["body"].get("data") or {}
    asset = next((x for x in (d.get("assets") or []) if x.get("tokenSymbol") == token_symbol), None)
    tok = next((x for x in tokens if x.get("symbol") == token_symbol), None)
    if asset is None or tok is None or not provs:
        raise RuntimeError("quote failed: token/asset/provider missing")
    fee = int(asset["transferFee"]) + (0 if d.get("active") else int(asset["activateFee"]))
    return Quote(a["body"], tokens, provs[0]["address"], int(now_fn()), fee)


def go_ok(r):
    return r.get("http") == 200 and isinstance(r.get("body"), dict) and r["body"].get("code") == 200


class ApprovalLedger:
    """append-only 승인 원장(JSONL + flock). run_flow 의 used_approvals 대신 영속 재사용 차단."""
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)

    def _entries(self):
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def record(self, ap: D.Approval):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(dataclasses.asdict(ap), ensure_ascii=False) + "\n"); fh.flush(); os.fsync(fh.fileno())
            fcntl.flock(fh, fcntl.LOCK_UN)

    def mark_used(self, approval_id: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps({"used": approval_id, "ts": int(time.time())}) + "\n"); fh.flush(); os.fsync(fh.fileno())
            fcntl.flock(fh, fcntl.LOCK_UN)

    def used_ids(self) -> set:
        return {e["used"] for e in self._entries() if "used" in e}

    def reserve(self, approval_id: str) -> bool:
        """원자적: 잠금 안에서 used 를 다시 읽고 없을 때만 used 기록. 두 요청 중 하나만 True."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                used = {json.loads(l)["used"] for l in fh.read().splitlines() if l.strip() and "\"used\"" in l}
                if approval_id in used:
                    return False
                fh.write(json.dumps({"used": approval_id, "ts": int(time.time())}) + "\n"); fh.flush(); os.fsync(fh.fileno())
                return True
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


class UsedSet:
    """run_flow 의 used_approvals 인터페이스(in/add)를 영속 원장에 위임."""
    def __init__(self, ledger: ApprovalLedger):
        self.ledger = ledger

    def __contains__(self, aid):
        return aid in self.ledger.used_ids()

    def add(self, aid):
        self.ledger.mark_used(aid)

    def reserve(self, aid) -> bool:
        return self.ledger.reserve(aid)


def make_hooks(*, policy: PaymentPolicy, batch_id: str, intent_log: IntentLog, approvals: ApprovalLedger,
               quote_fn, client, human_confirm, human_sign, now_fn=time.time, quote_ttl_s: int = QUOTE_TTL_S):
    """run_flow 에 넣을 approve/execute 콜백. quote_fn() -> Quote. human_confirm(summary)->str|None(승인자).
    human_sign(order)->{"message","sig"} (TronLink 등)."""
    state = {"quote": None, "order": None, "payment_id": None}

    def fresh_quote() -> Quote:
        q = quote_fn()
        if int(now_fn()) - q.quoted_at > quote_ttl_s:
            raise RuntimeError("quote expired")
        state["quote"] = q
        return q

    def approve(plan: D.Plan, rules: D.UserRules, ctx: dict):
        # 1) 정책 원장에 1행 배치 등록(원본 행/메모 = 안내 plan_id)
        q_, r_ = divmod(int(plan.value_units), 10 ** 6)                      # float 없이 정확한 십진 문자열
        amount_text = f"{q_}.{r_:06d}"
        buf = io.StringIO(); wr = csv.writer(buf, lineterminator="\n")
        wr.writerow(["recipient", "amount", "memo"]); wr.writerow([plan.receiver, amount_text, f"{rules.goal[:60]} plan={rules.guide_plan_id}"])
        res = run_batch(buf.getvalue(), policy, batch_id)
        state["rules"] = rules
        if res["stage"] != "AWAITING_HUMAN_APPROVAL" and res["batch_registration"] != "REPLAY":
            return None
        row = next((r for r in res["rows"] if r["decision"] in ("ACCEPTED_FOR_APPROVAL", "REPLAY")), None)
        if row is None:
            return None
        state["payment_id"] = row["payment_id"]
        # 2) 사람 확인: 화면 지문(display_digest) 을 승인에 결합
        digest = policy.display_digest(batch_id, res["revision"])
        summary = {"receiver": plan.receiver, "value_units": plan.value_units, "fee_units": plan.fee_units,
                   "total_units": plan.total_units, "network": plan.network, "asset": plan.asset,
                   "deadline_ts": rules.deadline_ts, "display_digest": digest}
        who = human_confirm(summary)
        if not who:
            return None
        ap_res = approve_batch(policy, batch_id, res["revision"], res["csv_sha256"], confirmed_by=who, displayed_sha256=digest)
        if ap_res["outcome"] not in ("APPROVED_HUMAN_CONFIRMED", "APPROVAL_REPLAY"):
            return None
        ap = D.Approval(approval_id=ap_res["approval"]["approval_id"] + f"@{ctx['flow_id']}", flow_id=ctx["flow_id"],
                        revision=ctx["revision"], rules_digest=ctx["rules_digest"], plan_digest=ctx["plan_digest"],
                        approved_by=who, approved_at=int(now_fn()))
        approvals.record(ap)
        state["policy_approval_id"] = ap_res["approval"]["approval_id"]
        state["revision"] = res["revision"]; state["csv_sha256"] = res["csv_sha256"]; state["row"] = row
        return ap

    def execute(plan: D.Plan, ap: D.Approval):
        # 0) 같은 지급의 기존 intent 상태를 먼저 본다 — 이미 제출/확정된 것을 다시 서명받거나 '미제출' 로 부르지 않는다(GPT-02 #3)
        prior = intent_log.state(state["payment_id"]) if state.get("payment_id") else None
        if prior in ("SUBMITTED", "UNKNOWN"):
            return {"state": "UNKNOWN", "reason": f"already reserved/submitted earlier ({prior}); no resend", "payment_id": state["payment_id"]}
        if prior in ("ACCEPTED", "CONFIRMED"):
            return {"state": prior, "reason": f"already {prior} earlier; no resend", "payment_id": state["payment_id"]}
        if prior == "FAILED":                                  # 이전에 전송 시도가 있었고 실패로 기록됨 — '미제출' 이 아니다(VP 21:08 #1)
            cur = intent_log.current(state["payment_id"]) or {}
            return {"state": "FAILED", "reason": "prior attempt reached the provider/chain and was recorded FAILED; no resend; cause/chain result must be checked before any new attempt",
                    "payment_id": state["payment_id"], "prior": {k: cur.get(k) for k in ("trace_id", "tx_hash", "reason", "ts", "nonce", "max_fee")}}
        if prior in ("CANCELLED", "REJECTED"):
            return {"state": "NOT_SUBMITTED", "reason": f"prior intent state {prior} (no submission reached the provider); this payment_id stays closed", "payment_id": state["payment_id"]}
        # 3) 실행 직전 재견적(현재 시각) — 최초 견적과 핵심값이 다르면 실행하지 않음
        try:
            q = fresh_quote()
        except Exception as e:
            return {"state": "NOT_SUBMITTED", "reason": f"requote failed: {e}"}
        if state.get("first_quote_key") is None:
            state["first_quote_key"] = q.key()
        if q.key() != state["first_quote_key"] or q.fee_units != plan.fee_units:
            return {"state": "NOT_SUBMITTED", "reason": "quote changed since plan (fee/nonce/allowSubmit/provider)"}
        # 4) 주문서(서명 직전 재검증 9종+) → 사람 서명 → 서명본 대조
        try:
            t_now = int(now_fn())
            left = int(state["rules"].deadline_ts) - t_now                       # 사용자 절대 기한을 그대로 서명 기한으로
            if left <= 0:
                return {"state": "NOT_SUBMITTED", "reason": "user deadline passed before order build"}
            order = go.build_order(policy=policy, batch_id=batch_id, revision=state["revision"], csv_sha256=state["csv_sha256"],
                                   approval_id=state["policy_approval_id"], payment_id=state["payment_id"], row=state["row"],
                                   address_info=q.address_info, tokens_info=q.tokens_info, provider_address=q.provider_address,
                                   now=t_now, deadline_secs=min(600, left))
            if int(order.message["deadline"]) > int(state["rules"].deadline_ts):
                return {"state": "NOT_SUBMITTED", "reason": "order deadline exceeds user deadline"}
        except go.OrderError as e:
            return {"state": "NOT_SUBMITTED", "reason": f"order refused: {e}"}
        go.record_order_intents(intent_log, order)
        signed = human_sign(order)
        if not signed or (isinstance(signed, dict) and signed.get("refused")):
            return {"state": "NOT_SUBMITTED", "reason": (signed or {}).get("refused") if isinstance(signed, dict) else "not signed"}
        # 실행기 자체 서명자 검증(어떤 human_sign 연결이든): 서명자 EOA 복구 == 주문 user (9/28 GPT-02 조건 2)
        try:
            signer = tip712.recover_signer_tron(order.domain, order.types, "PermitTransfer", order.message, str(signed.get("sig") or ""))
        except Exception as e:
            return {"state": "NOT_SUBMITTED", "reason": f"signer recovery failed: {e}"[:200]}
        if signer != order.user_eoa:
            return {"state": "NOT_SUBMITTED", "reason": "signer mismatch (recovered EOA != order user)"}
        t_signed = int(now_fn())
        if t_signed >= int(state["rules"].deadline_ts):                          # 지갑 서명 대기 중 사용자 기한 만료
            return {"state": "NOT_SUBMITTED", "reason": "user deadline passed while waiting for wallet signature"}
        try:
            body = go.verify_signed(order, signed, now=t_signed)
        except go.OrderError as e:
            return {"state": "NOT_SUBMITTED", "reason": f"signature refused: {e}"}
        try:
            intent_log.append(order.payment_id, "SIGNED")
        except Exception as e:                                   # 전이 불가(예: 취소됨) → 제출 전 종료
            return {"state": "NOT_SUBMITTED", "reason": f"cannot mark SIGNED: {e}"[:200], "payment_id": order.payment_id}
        # 5) 제출(영속 예약·fsync) — 응답 유실/예외는 UNKNOWN 으로 남김(재전송 금지)
        state_before = intent_log.state(order.payment_id)      # 제출 예약 전 상태(SIGNED 이어야 함)
        try:
            out = go.submit_and_track(client, order, body, poll=1, sleep_fn=lambda s: None, intent_log=intent_log,
                                      policy=policy, now=int(now_fn()))
        except Exception as e:
            st_now = intent_log.state(order.payment_id)
            # 예약(SUBMITTED) 이후의 어떤 예외도 '미제출' 로 단정하지 않는다(9/28 GPT-02 #3). 원장에 SIGNED 그대로면 예약 전 실패.
            if st_now == state_before == "SIGNED":
                return {"state": "NOT_SUBMITTED", "reason": f"pre-reservation failure: {e}"[:200]}
            if st_now in ("SUBMITTED", "ACCEPTED"):
                try:
                    intent_log.append(order.payment_id, "UNKNOWN", reason=f"exception after submit: {e}"[:200])
                except Exception as e2:  # 원장 기록 실패도 숨기지 않는다(상태는 SUBMITTED/ACCEPTED 로 남아 재전송은 계속 차단됨)
                    sys.stderr.write(f"[executor] intent UNKNOWN append failed for {order.payment_id}: {e2}\n")
            return {"state": {"CONFIRMED": "CONFIRMED"}.get(st_now, "UNKNOWN"), "reason": f"exception after reservation: {e}"[:200],
                    "payment_id": order.payment_id}
        oc = out["outcome"]
        if oc == "BLOCKED_BY_INTENT_LOG":                       # 차단 사유를 기존 상태로 구조화(이미 제출된 것을 '미제출' 로 부르지 않음)
            prior = intent_log.state(order.payment_id)
            if prior in ("SUBMITTED", "UNKNOWN"):
                return {"state": "UNKNOWN", "reason": f"already reserved/submitted earlier ({prior}); no resend", "payment_id": order.payment_id}
            if prior in ("ACCEPTED", "CONFIRMED"):
                return {"state": prior, "reason": f"already {prior} earlier; no resend", "payment_id": order.payment_id}
            return {"state": "NOT_SUBMITTED", "reason": f"blocked before reservation: {out['submit'].get('blocked')}", "payment_id": order.payment_id}
        st = ("CONFIRMED" if oc == "FINAL_SUCCEED" else "ACCEPTED" if oc.startswith("ACCEPTED") or oc.startswith("PENDING")
              or oc == "FINAL_SUCCEED_UNRECONCILED" else "UNKNOWN" if oc.startswith("UNKNOWN") else
              "REJECTED" if oc == "REJECTED_BY_PROVIDER" else "UNKNOWN")
        return {"state": st, "outcome": oc, "trace_id": out.get("trace_id"), "payment_id": order.payment_id,
                "tx_hash": (out.get("trace") or {}).get("txnHash") if isinstance(out.get("trace"), dict) else None}

    return approve, execute, state


def run_real_flow(rules: D.UserRules, *, policy, batch_id, intent_log, approvals, quote_fn, client, human_confirm,
                  human_sign, kiln_choose, flow_id=None, revision=1, now_fn=time.time):
    """실경로 1흐름: 실제 시계·실견적·영속 원장. 견적 실패면 후보 없음(거절)."""
    try:
        q = quote_fn()
    except Exception as e:
        return {"flow_id": flow_id, "revision": revision, "outcome": "DECLINED", "reason": f"no quote: {e}", "executed": False}
    approve, execute, state = make_hooks(policy=policy, batch_id=batch_id, intent_log=intent_log, approvals=approvals,
                                         quote_fn=quote_fn, client=client, human_confirm=human_confirm, human_sign=human_sign, now_fn=now_fn)
    state["first_quote_key"] = q.key()
    return D.run_flow(rules, q.fee_units, int(now_fn()), kiln_choose=kiln_choose, approve=approve, execute=execute,
                      flow_id=flow_id, revision=revision, used_approvals=UsedSet(approvals), now_fn=now_fn)
