"""SafeBatch AI · 승인된 1건 지급 → GasFree 전송 주문서 → (사람 서명) → 제출 → 추적 → 대사.

이 모듈은 **서명하지 않는다**(개인키 없음). 하는 일:
1. build_order: 로컬 승인(approve_batch) 결과 + GasFree address 조회 결과로 PermitTransfer 메시지(typed data)를 만든다.
   - 서명 직전 재검증: 현재 revision·CSV 해시·승인 ID 일치, 수신자/금액이 승인 행과 일치, allowSubmit, 토큰 지원,
     가용 잔액(assets 잔액 − frozen) ≥ value + maxFee, deadline 은 짧게(기본 10분).
   - maxFee = transferFee (+ activateFee, 계정 미활성일 때). 사람 화면에 네트워크·토큰·수수료·합계를 그대로 보여준다.
   - requestId 는 payment_id 기반 uuid5 → 같은 지급의 재제출은 같은 requestId (중복 지급 추적).
2. verify_signed: 사람이 서명해 돌려준 데이터가 주문서(메시지)와 바이트 단위로 같은지 확인한 뒤에만 제출을 허용한다.
3. submit_and_track: 제출 → 응답 유실이면 UNKNOWN 으로 보관(재송금 금지) → trace 로 상태 조회.
4. reconcile: trace 결과(txnHash·txnAmount·txnTotalFee·state)를 원본 행·메모·승인 내용과 대사한다.

공식 근거: docs.gasfree.io §3.1(chainId 3448148188, verifyingContract THQGuFzL87ZqhxkgqYEryRAd7gqFqL5rdc — Nile),
§3.2 PermitTransfer 필드, §5 address/submit/trace 필드. 필드명은 문서의 camelCase 와 예시의 snake_case(allow_submit) 둘 다 읽는다.
"""
from __future__ import annotations

import dataclasses
import json
import time
import uuid

from .csvcheck import DECIMALS as CSV_DECIMALS, is_tron_address

NILE_DOMAIN = {
    "name": "GasFreeController",
    "version": "V1.0.0",
    "chainId": 3448148188,
    "verifyingContract": "THQGuFzL87ZqhxkgqYEryRAd7gqFqL5rdc",
}
PERMIT_TYPES = {
    "PermitTransfer": [
        {"name": "token", "type": "address"},
        {"name": "serviceProvider", "type": "address"},
        {"name": "user", "type": "address"},
        {"name": "receiver", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "maxFee", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "version", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
    ]
}
SIG_VERSION = 1
DEFAULT_DEADLINE_SECS = 600
REQUEST_NS = uuid.UUID("6f2a9c1e-4b3d-4e0f-9a7b-3c2d1e0f9a8b")   # SafeBatch 고정 네임스페이스(requestId 결정론)

FINAL_OK = {"SUCCEED"}
FINAL_BAD = {"FAILED"}
PENDING = {"WAITING", "INPROGRESS", "CONFIRMING"}


class OrderError(Exception):
    pass


def _get(d: dict, *names, default=None):
    for n in names:
        if isinstance(d, dict) and n in d and d[n] is not None:
            return d[n]
    return default


@dataclasses.dataclass
class Order:
    payment_id: str
    batch_id: str
    revision: int
    csv_sha256: str
    approval_id: str
    row_no: int
    memo: str
    network: str
    user_eoa: str
    gasfree_address: str
    token_symbol: str
    token_decimal: int
    message: dict                # PermitTransfer 메시지(서명 대상)
    domain: dict
    types: dict
    request_id: str
    fee_breakdown: dict
    available_units: int
    created_at: int
    checks: list
    approval_mode: str = "UNKNOWN"
    snapshot_sha256: str = ""       # domain+types+message+approval_id+csv_sha256+request_id 지문(생성 후 변조 탐지)


    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def human_summary(self) -> str:
        m = self.message
        d = self.token_decimal
        def f(u):  # 정수 몫·나머지로 표시(float 손실 없음, 9/28 GPT#3)
            q, r = divmod(int(u), 10 ** d)
            return f"{q}.{r:0{d}d}" if d else str(q)
        return (f"[{self.network.upper()}] {self.token_symbol} 지급 1건\n"
                f" 수신자: {m['receiver']}\n 금액: {f(m['value'])} {self.token_symbol}\n"
                f" 수수료 상한: {f(m['maxFee'])} (transfer {f(self.fee_breakdown['transferFee'])}"
                f" + activate {f(self.fee_breakdown['activateFee'])})\n"
                f" 최대 지출: {f(int(m['value']) + int(m['maxFee']))}  가용: {f(self.available_units)}\n"
                f" nonce {m['nonce']} · deadline {m['deadline']} · batch {self.batch_id} r{self.revision} 행{self.row_no}\n"
                f" 메모: {self.memo}\n 승인 {self.approval_id} · csv {self.csv_sha256[:16]}…")


def snapshot_digest(domain: dict, types: dict, message: dict, approval_id: str, csv_sha256: str, request_id: str) -> str:
    import hashlib
    payload = {"domain": domain, "types": types, "message": message, "approval_id": approval_id,
               "csv_sha256": csv_sha256, "request_id": request_id}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def build_order(*, policy, batch_id: str, revision: int, csv_sha256: str, approval_id: str, payment_id: str,
                row: dict, address_info: dict, tokens_info: list, provider_address: str, token_symbol: str = "USDT",
                deadline_secs: int = DEFAULT_DEADLINE_SECS, now: int | None = None, network: str = "nile",
                allow_mock_approval: bool = False) -> Order:
    """서명 직전 재검증을 모두 통과해야 Order 를 돌려준다. 하나라도 실패하면 OrderError.
    기본값은 사람 확인 승인(HUMAN_CONFIRMED)만 허용한다. LOCAL_MOCK 승인은 allow_mock_approval=True(검사용)일 때만."""
    checks: list = []
    now = int(now if now is not None else time.time())

    def chk(name: str, cond: bool, detail: str = ""):
        checks.append({"check": name, "pass": bool(cond), "detail": detail})
        if not cond:
            raise OrderError(f"{name} 실패: {detail}")

    chk("network_is_nile", network == "nile", network)   # Nile 전용. mainnet 주문서는 만들지 않는다.

    # 1) 승인·revision·해시 재검증(로컬 정책 원장)
    cur = policy.current_revision(batch_id)
    chk("batch_known", cur is not None, batch_id)
    chk("revision_current", cur["revision"] == revision, f"current={cur['revision']} given={revision}")
    chk("csv_sha256_current", cur["csv_sha256"] == csv_sha256, "csv hash differs from current revision")
    chk("approval_valid", policy.is_approval_valid(approval_id, batch_id, revision, csv_sha256), approval_id)
    ap = policy.approvals.get(approval_id) or {}
    approval_mode = ap.get("mode", "UNKNOWN")
    chk("approval_is_human_confirmed", approval_mode == "HUMAN_CONFIRMED" or allow_mock_approval,
        f"approval mode={approval_mode}; real path requires HUMAN_CONFIRMED")
    if approval_mode == "HUMAN_CONFIRMED":
        chk("approval_network_matches", ap.get("network") == network, f"approval network={ap.get('network')}")
        chk("approval_display_bound", ap.get("displayed_sha256") is not None
            and ap.get("displayed_sha256") == policy.display_digest(batch_id, revision, network),
            "content shown at approval differs from current rows/fees (re-confirm required)")
    rec = policy.record_of(payment_id)
    chk("payment_record_exists", rec is not None, payment_id)
    chk("payment_state_approved", policy.record_state.get(payment_id) == "APPROVED_LOCAL_MOCK",
        str(policy.record_state.get(payment_id)))
    chk("row_matches_record", rec.recipient == row["recipient"] and rec.amount_units == row["amount_units"],
        "row recipient/amount differ from approved record")
    chk("payment_id_matches_row", payment_id == f"{batch_id}:r{revision}:{row['row_no']}", payment_id)

    # 2) GasFree 계정 상태
    d = _get(address_info, "data", default=address_info) or {}
    user_eoa = _get(d, "accountAddress")
    gf_addr = _get(d, "gasFreeAddress")
    active = bool(_get(d, "active", default=False))
    nonce = _get(d, "nonce")
    allow = _get(d, "allowSubmit", "allow_submit")
    chk("address_fields", user_eoa and gf_addr and nonce is not None, "accountAddress/gasFreeAddress/nonce missing")
    chk("nonce_is_uint", isinstance(nonce, int) and not isinstance(nonce, bool) and nonce >= 0, f"nonce={nonce!r}")
    for nm, addr in (("user_eoa", user_eoa), ("gasfree_address", gf_addr), ("provider_address", provider_address),
                     ("receiver", row.get("recipient"))):
        chk(f"{nm}_tron_format", is_tron_address(addr) if isinstance(addr, str) else False, str(addr))
    chk("allow_submit", allow is True, f"allowSubmit={allow}")

    # 3) 토큰 지원·수수료 — 기본값으로 채우지 않는다(9/28 R4). 필수값이 없거나 타입/부호가 틀리면 주문서 없음.
    def _uint(v, name):
        chk(f"{name}_present", v is not None, f"{name} missing in provider response")
        chk(f"{name}_is_nonneg_int", isinstance(v, int) and not isinstance(v, bool) and v >= 0, f"{name}={v!r}")
        return v

    tok = next((t for t in tokens_info if _get(t, "symbol") == token_symbol), None)
    chk("token_listed", tok is not None, token_symbol)
    chk("token_supported_explicit", _get(tok, "supported") is True, f"supported={_get(tok, 'supported')!r}")
    token_addr = _get(tok, "tokenAddress")
    chk("token_address_present", isinstance(token_addr, str) and token_addr.startswith("T") and len(token_addr) == 34, str(token_addr))
    decimal = _uint(_get(tok, "decimal"), "token_decimal")
    chk("active_is_bool", isinstance(_get(d, "active"), bool), f"active={_get(d, 'active')!r}")
    asset = next((a for a in (_get(d, "assets", default=[]) or []) if _get(a, "tokenAddress") == token_addr), None)
    chk("asset_entry_present", asset is not None, f"no asset entry for {token_symbol} in address response")
    transfer_fee = _uint(_get(asset, "transferFee"), "transferFee")
    activate_fee = 0 if active else _uint(_get(asset, "activateFee"), "activateFee")
    max_fee = transfer_fee + activate_fee
    frozen = _uint(_get(asset, "frozen"), "frozen")
    balance = _uint(_get(asset, "balance", "amount"), "balance")
    available = balance - frozen
    value = row["amount_units"]
    chk("amount_is_positive_int", isinstance(value, int) and not isinstance(value, bool) and value > 0, str(value))
    chk("sufficient_available", available >= value + max_fee, f"available={available} need={value + max_fee}")
    chk("token_decimal_matches_csv_units", decimal == CSV_DECIMALS,
        f"token decimal {decimal} != CSV amount units {CSV_DECIMALS} (amounts would be mis-scaled)")
    chk("fee_reservation_covers_max_fee", int(rec.fee_units) >= max_fee,   # 행이 예약된 당시의 불변 수수료(9/28 GPT#3)
        f"reserved fee for this row {rec.fee_units} < provider maxFee {max_fee} (re-reserve and re-approve)")

    deadline = now + int(deadline_secs)
    message = {
        "token": token_addr, "serviceProvider": provider_address, "user": user_eoa, "receiver": row["recipient"],
        "value": str(value), "maxFee": str(max_fee), "deadline": str(deadline), "version": SIG_VERSION, "nonce": int(nonce),
    }
    request_id = str(uuid.uuid5(REQUEST_NS, f"{network}:{payment_id}:{csv_sha256}"))
    domain = dict(NILE_DOMAIN)
    snap = snapshot_digest(domain, PERMIT_TYPES, message, approval_id, csv_sha256, request_id)
    return Order(payment_id=payment_id, batch_id=batch_id, revision=revision, csv_sha256=csv_sha256,
                 approval_id=approval_id, row_no=int(row["row_no"]), memo=row.get("memo", ""), network=network,
                 user_eoa=user_eoa, gasfree_address=gf_addr, token_symbol=token_symbol, token_decimal=decimal,
                 message=message, domain=domain, types=PERMIT_TYPES, request_id=request_id,
                 fee_breakdown={"transferFee": transfer_fee, "activateFee": activate_fee, "active": active},
                 available_units=available, created_at=now, checks=checks, approval_mode=approval_mode,
                 snapshot_sha256=snap)


def verify_signed(order: Order, signed: dict, now: int | None = None) -> dict:
    """사람이 서명한 뒤 돌려준 (message, sig) 가 주문서와 정확히 같을 때만 제출 본문을 만든다."""
    now = int(now if now is not None else time.time())
    # 주문서 자체가 생성 후 바뀌지 않았는지(스냅샷 지문 재계산)
    if snapshot_digest(order.domain, order.types, order.message, order.approval_id, order.csv_sha256,
                       order.request_id) != order.snapshot_sha256:
        raise OrderError("order was mutated after build (snapshot digest mismatch); rebuild from approval")
    msg = signed.get("message")
    sig = (signed.get("sig") or "").strip()
    if sig.startswith("0x"):
        sig = sig[2:]
    if json.dumps(msg, sort_keys=True) != json.dumps(order.message, sort_keys=True):
        raise OrderError("signed message differs from order message (refuse to submit)")
    # 지갑이 서명한 domain/types 가 돌아오면 Nile 도메인·PermitTransfer 타입과 같아야 한다
    if "domain" in signed and json.dumps(signed["domain"], sort_keys=True) != json.dumps(order.domain, sort_keys=True):
        raise OrderError("signed domain differs from Nile GasFreeController domain (refuse to submit)")
    if "types" in signed and json.dumps(signed["types"], sort_keys=True) != json.dumps(order.types, sort_keys=True):
        raise OrderError("signed types differ from PermitTransfer types (refuse to submit)")
    # 서명자 복구(secp256k1)는 표준 라이브러리로 하지 않는다. 서명자=EOA 검증은 지갑(TronLink)과 provider 의
    # InvalidSignatureException 에 의존하며, 이 함수의 통과는 '형식·내용 일치'이지 암호학적 검증이 아니다(9/28 R2).
    if not sig or any(c not in "0123456789abcdefABCDEF" for c in sig) or len(sig) not in (130,):
        raise OrderError("signature missing or not 65-byte hex")
    if int(order.message["deadline"]) <= now:
        raise OrderError("deadline already passed; rebuild order (new deadline needs new signature)")
    body = dict(order.message)
    body["value"] = int(body["value"]); body["maxFee"] = int(body["maxFee"]); body["deadline"] = int(body["deadline"])
    body["requestId"] = order.request_id
    body["sig"] = sig
    return body


def _intent(log, order: Order, state: str, **fields):
    if log is not None:
        log.append(order.payment_id, state, user=order.user_eoa, nonce=int(order.message["nonce"]), **fields)


def record_order_intents(log, order: Order) -> None:
    """주문서 생성 → 사람 확인 대기까지를 intent 원장에 남긴다(DRAFTED → AWAITING_HUMAN)."""
    if log is None:
        return
    if log.state(order.payment_id) is None:
        _intent(log, order, "DRAFTED", batch_id=order.batch_id, revision=order.revision, csv_sha256=order.csv_sha256,
                row_no=order.row_no, memo=order.memo, request_id=order.request_id, approval_id=order.approval_id,
                receiver=order.message["receiver"], value=order.message["value"], max_fee=order.message["maxFee"])
    if log.state(order.payment_id) == "DRAFTED":
        _intent(log, order, "AWAITING_HUMAN")


def classify_submit_response(r: dict) -> str:
    """제출 응답 분류(9/28 R5): 'ACCEPTED' | 'REJECTED'(명백한 미접수) | 'UNKNOWN'(접수 여부 불명).
    - HTTP 200 + 본문 code 200 → ACCEPTED
    - HTTP 4xx(200 포함) + JSON 본문 code 400 + reason 있음 → REJECTED (문서: 실패 응답이면 폐기, 체인 거래 없음)
    - HTTP 5xx / 502·503·504 프록시 / 본문이 JSON 아님 / code 없음·500 → UNKNOWN (서버가 접수했을 수 있음)"""
    http = r.get("http")
    body = r.get("body")
    if http == 200 and isinstance(body, dict) and body.get("code") == 200:
        return "ACCEPTED"
    if isinstance(http, int) and 400 <= http < 500 or http == 200:
        if (isinstance(body, dict) and "_raw" not in body and body.get("code") == 400
                and body.get("reason") in DOCUMENTED_PRE_SUBMIT_REJECTIONS):   # Grok#5(a): 문서에 있는 사전검증 거절만
            return "REJECTED"
    return "UNKNOWN"


# docs.gasfree.io §5 submit "Error types" — provider 가 접수 전 검증에서 폐기한다고 문서화한 사유만 REJECTED 로 본다.
DOCUMENTED_PRE_SUBMIT_REJECTIONS = {
    "ProviderAddressNotMatchException", "DeadlineExceededException", "InvalidSignatureException",
    "UnsupportedTokenException", "TooManyPendingTransferException", "VersionNotSupportedException",
    "NonceNotMatchException", "MaxFeeExceededException", "InsufficientBalanceException",
}


def expected_body(order: Order) -> dict:
    """주문서에서 제출 본문(sig 제외)을 재생성한다. 검증 뒤 본문 교체를 막는 기준값(9/28 GPT#2)."""
    b = dict(order.message)
    b["value"] = int(b["value"]); b["maxFee"] = int(b["maxFee"]); b["deadline"] = int(b["deadline"])
    b["requestId"] = order.request_id
    return b


def submit_and_track(client, order: Order, body: dict, poll: int = 0, sleep_fn=time.sleep, wait_s: float = 3.0,
                     intent_log=None, require_intent_log: bool = True, policy=None, now: int | None = None) -> dict:
    """제출 1회. 전송 오류·5xx·비JSON 은 UNKNOWN(재송금 금지). 이후 trace 로 상태 조회(poll 회).
    실제 경로는 intent_log 필수(require_intent_log=True 기본). 검사용 가짜 클라이언트만 False 로 우회한다.
    제출 직전 경계(9/28 GPT#1·#2): LOCAL_MOCK 주문은 무조건 거절, 주문서 스냅샷 재계산, 본문이 주문서에서 재생성한
    값과 동일해야 하며, policy 가 주어지면 현재 revision/해시/승인/화면 지문/지급행 상태를 다시 검증한다."""
    out = {"request_id": order.request_id, "payment_id": order.payment_id, "submit": None, "trace_id": None,
           "outcome": None, "trace": None}
    if intent_log is None and require_intent_log:
        raise OrderError("intent_log is required for real submission (persistent nonce lock)")
    if order.approval_mode != "HUMAN_CONFIRMED":
        raise OrderError(f"refuse to submit: approval mode {order.approval_mode} (HUMAN_CONFIRMED required)")
    if snapshot_digest(order.domain, order.types, order.message, order.approval_id, order.csv_sha256,
                       order.request_id) != order.snapshot_sha256:
        raise OrderError("refuse to submit: order snapshot digest mismatch")
    sig = body.get("sig") if isinstance(body, dict) else None
    if {k: v for k, v in (body or {}).items() if k != "sig"} != expected_body(order):
        raise OrderError("refuse to submit: body differs from the order-derived body (post-verify substitution)")
    if not isinstance(sig, str) or len(sig) != 130 or any(c not in "0123456789abcdefABCDEF" for c in sig):
        raise OrderError("refuse to submit: signature missing or not 65-byte hex")
    now_i = int(now if now is not None else time.time())
    if int(order.message["deadline"]) <= now_i:
        raise OrderError("refuse to submit: deadline passed")
    if policy is not None:
        ap = policy.approvals.get(order.approval_id) or {}
        if not policy.is_approval_valid(order.approval_id, order.batch_id, order.revision, order.csv_sha256):
            raise OrderError("refuse to submit: approval no longer valid (revision/hash changed or invalidated)")
        if ap.get("mode") != "HUMAN_CONFIRMED" or ap.get("displayed_sha256") != policy.display_digest(
                order.batch_id, order.revision, order.network):
            raise OrderError("refuse to submit: approval mode/displayed content no longer matches")
        if policy.record_state.get(order.payment_id) != "APPROVED_LOCAL_MOCK":
            raise OrderError(f"refuse to submit: payment state {policy.record_state.get(order.payment_id)}")
    elif require_intent_log:
        raise OrderError("policy is required for real submission (pre-submit re-verification)")
    if intent_log is not None:
        allowed, why = intent_log.reserve_submit(order.payment_id, order.user_eoa, int(order.message["nonce"]))  # 잠금+fsync
        if not allowed:
            out["outcome"] = "BLOCKED_BY_INTENT_LOG"
            out["submit"] = {"blocked": why}
            return out
    try:
        r = client.submit(body)
    except Exception as e:  # transport — 성공 여부 미확인
        out["submit"] = {"error": str(e)}
        out["outcome"] = "UNKNOWN_SUBMIT_TRANSPORT"  # 상태 조회/사람 확인 전 재송금 금지
        _intent(intent_log, order, "UNKNOWN", reason=str(e)[:200])
        return out
    body_d = r.get("body") if isinstance(r.get("body"), dict) else {}
    out["submit"] = {"http": r.get("http"), "code": body_d.get("code"), "reason": body_d.get("reason"),
                     "message": body_d.get("message")}
    cls = classify_submit_response(r)
    if cls == "REJECTED":
        out["outcome"] = "REJECTED_BY_PROVIDER"   # 문서: 실패 응답이면 폐기, 체인 거래 없음
        _intent(intent_log, order, "REJECTED", reason=str(out["submit"]["reason"]))
        return out
    if cls == "UNKNOWN":
        out["outcome"] = "UNKNOWN_SUBMIT_SERVER"   # 5xx/프록시/비JSON: 접수됐을 수 있음 → 조회 전 재제출 금지
        _intent(intent_log, order, "UNKNOWN", reason=f"http={r.get('http')} body={str(r.get('body'))[:120]}")
        return out
    data = r["body"].get("data") or {}
    out["trace_id"] = data.get("id")
    out["outcome"] = f"ACCEPTED_{data.get('state', 'WAITING')}"
    _intent(intent_log, order, "ACCEPTED", trace_id=out["trace_id"])
    for _ in range(int(poll)):
        sleep_fn(wait_s)
        try:
            t = client.trace(out["trace_id"])
        except Exception as e:
            out["trace"] = {"error": str(e)}
            break
        tb = t.get("body") if isinstance(t.get("body"), dict) else {}
        td = tb.get("data") or {}
        out["trace"] = td
        if not (t.get("http") == 200 and tb.get("code") == 200 and td.get("id") == out["trace_id"]):
            out["trace"] = {"error": "trace response not ok or id mismatch", "http": t.get("http"),
                            "code": tb.get("code"), "id": td.get("id")}
            continue                                   # 식별 안 되는 응답으로 확정하지 않는다(9/28 GPT#5)
        st = td.get("state")
        if st in FINAL_OK:
            rec = reconcile(order, td, expected_trace_id=out["trace_id"])
            out["reconcile"] = rec
            if rec["verdict"] == "RECONCILED":             # Grok#3: 대사 통과한 SUCCEED 만 CONFIRMED
                out["outcome"] = "FINAL_SUCCEED"
                _intent(intent_log, order, "CONFIRMED", tx_hash=td.get("txnHash"))
            else:
                out["outcome"] = "FINAL_SUCCEED_UNRECONCILED"   # 성공 표시이나 내용 불일치 → ACCEPTED 유지, 사람 확인
            break
        if st in FINAL_BAD:
            out["outcome"] = "FINAL_FAILED"
            _intent(intent_log, order, "FAILED")
            break
        out["outcome"] = f"PENDING_{st}"
    return out


def resolve_unknown(client, order: Order, intent_log, trace_id: str | None = None) -> dict:
    """UNKNOWN 지급의 상태를 조회로 먼저 푼다. traceId 를 모르면 사람이 GasFree 화면/제공자에 requestId 로 확인해야 하며
    여기서는 재제출하지 않는다."""
    st = intent_log.state(order.payment_id)
    if st not in ("UNKNOWN", "ACCEPTED"):                 # 열린 상태(UNKNOWN/ACCEPTED)만 조회로 종결한다
        return {"outcome": f"NOT_UNKNOWN_{st}"}
    stored = intent_log.stored_trace_id(order.payment_id)
    if not stored:
        # Grok#3: 원장에 traceId 가 없으면 외부에서 준 traceId 로 종결하지 않는다(다른 지급의 trace 오연결 방지)
        return {"outcome": "STILL_UNKNOWN_NO_TRACE_ID", "note": "resolve via provider support/requestId; do not resubmit"}
    if trace_id and trace_id != stored:
        return {"outcome": "REFUSED_TRACE_ID_MISMATCH", "stored_trace_id": stored}
    t = client.trace(stored)
    tb = t.get("body") if isinstance(t.get("body"), dict) else {}
    td = tb.get("data") or {}
    if not (t.get("http") == 200 and tb.get("code") == 200 and td.get("id") == stored):
        return {"outcome": "STILL_UNKNOWN_TRACE_NOT_OK", "http": t.get("http"), "code": tb.get("code")}
    s = td.get("state")
    if s in FINAL_OK:
        rec = reconcile(order, td, expected_trace_id=stored)
        if rec["verdict"] == "RECONCILED":
            _intent(intent_log, order, "CONFIRMED", trace_id=stored, tx_hash=td.get("txnHash"))
        else:
            return {"outcome": "SUCCEED_UNRECONCILED_STILL_UNKNOWN", "trace": td, "reconcile": rec}
    elif s in FINAL_BAD:
        _intent(intent_log, order, "FAILED", trace_id=stored)
    elif s in PENDING:
        _intent(intent_log, order, "ACCEPTED", trace_id=stored)
    return {"outcome": s or "STILL_UNKNOWN", "trace": td}


def reconcile(order: Order, trace_data: dict, expected_trace_id: str | None = None) -> dict:
    """provider trace 결과를 원본 행·메모·승인 내용과 대사. 항목별 일치/불일치와 실제 수수료를 남긴다.
    이것은 provider 응답 대사이며 독립 온체인 영수증 검증(txnHash 를 노드/스캐너에서 조회)은 별도다(9/28 R3)."""
    m = order.message
    items = {
        "state": trace_data.get("state"),
        "txn_state": trace_data.get("txnState"),
        "txn_hash": trace_data.get("txnHash"),
        "trace_id": trace_data.get("id"),
        "trace_id_match": (expected_trace_id is None) or (trace_data.get("id") == expected_trace_id),
        "user_match": trace_data.get("accountAddress") in (None, m["user"]),
        "receiver_match": trace_data.get("targetAddress") == m["receiver"],
        "token_match": trace_data.get("tokenAddress") == m["token"],
        "nonce_match": trace_data.get("nonce") == m["nonce"],
        "onchain_verified_independently": False,
        "amount_expected": int(m["value"]),
        "amount_actual": trace_data.get("txnAmount"),
        "amount_match": trace_data.get("txnAmount") is not None and int(trace_data.get("txnAmount")) == int(m["value"]),
        "fee_actual": trace_data.get("txnTotalFee"),
        "fee_within_max": (isinstance(trace_data.get("txnTotalFee"), int) and 0 <= trace_data.get("txnTotalFee") <= int(m["maxFee"])),
        "total_cost_consistent": (trace_data.get("txnTotalCost") is None or (
            isinstance(trace_data.get("txnTotalCost"), int) and isinstance(trace_data.get("txnAmount"), int)
            and isinstance(trace_data.get("txnTotalFee"), int)
            and trace_data.get("txnTotalCost") == trace_data.get("txnAmount") + trace_data.get("txnTotalFee"))),
        "total_cost_actual": trace_data.get("txnTotalCost"),
        "row_no": order.row_no, "memo": order.memo, "payment_id": order.payment_id,
        "approval_id": order.approval_id, "csv_sha256": order.csv_sha256,
    }
    succeeded = items["state"] == "SUCCEED" and items["txn_state"] in ("ON_CHAIN", "SOLIDITY")
    items["reconciled"] = bool(succeeded and items["trace_id_match"] and items["user_match"] and items["receiver_match"]
                               and items["token_match"] and items["nonce_match"] and items["amount_match"]
                               and items["fee_within_max"] and items["total_cost_consistent"] and items["txn_hash"])
    items["verdict"] = ("RECONCILED" if items["reconciled"] else
                        "PENDING" if items["state"] in PENDING else
                        "FAILED" if items["state"] == "FAILED" else "MISMATCH_OR_INCOMPLETE")
    return items
