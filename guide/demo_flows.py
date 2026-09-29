"""Furiosa A 데모 3흐름(정상 → 조건 변경 적응 → 충족 불가 거절)의 안전 골격. (9/28 20:00 부사장 검수 §2 반영)

원칙:
- 규칙(총비용 상한·최소 수취액·기한·허용 수취인·mode)은 코드가 타입·범위까지 검증하고 강제한다. 알 수 없는 mode 는 거절.
- 후보 계획은 코드가 만들고 **불변(frozen dataclass)** 으로 모델·승인·실행 경계에 전달한다. 모델은 후보 인덱스+이유만 고를 수 있다.
- AI 실패/없음 → `PLAN_ONLY_AI_UNAVAILABLE` 로 끝난다. 실행 경로에 자동 폴백하지 않는다.
- 승인은 문자열이 아니라 **승인 기록**(plan_digest·rules_digest·revision·flow_id 결합)이며 재사용·불일치·이전 revision 은 차단한다.
- 실행 직전(서명 직전)에 현재 시각으로 기한·수수료·계획을 다시 검증한다. 신뢰할 수 있는 provider 수수료(양의 정수)가 없으면 주문을 만들지 않는다.
- 실행 결과는 dict 존재가 아니라 `state` 로 판정: CONFIRMED/ACCEPTED 만 실행됨, UNKNOWN 은 재전송 금지, REJECTED/NOT_SUBMITTED 는 미실행.
- 적응은 mode=max_within_budget 일 때만. exact_amount 는 감액 없이 거절.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
import uuid

MODES = ("exact_amount", "max_within_budget")
PATHS = ("gasfree", "nile_trc20")      # nile_trc20: 일반 TRC20 전송(USDT 수수료 0, TRX 수수료 한도 별도 — 사장 선택 9/28)
EXEC_STATES = ("CONFIRMED", "ACCEPTED", "UNKNOWN", "REJECTED", "NOT_SUBMITTED", "FAILED", "MISMATCH")   # MISMATCH: 체인에 있으나 내용/비용 불일치 → 성공 아님(사람 확인)


class RulesError(ValueError):
    pass


def _uint(v, name, positive=False):
    if not isinstance(v, int) or isinstance(v, bool) or v < 0 or (positive and v <= 0):
        raise RulesError(f"{name} must be a {'positive' if positive else 'non-negative'} int, got {v!r}")
    return v


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class UserRules:
    goal: str
    guide_plan_id: str
    receiver: str
    mode: str
    amount_units: int
    budget_total_units: int
    min_receive_units: int
    deadline_ts: int
    allowlist: tuple[str, ...]
    asset: str = "USDT"
    network: str = "nile"
    path: str = "gasfree"
    trx_fee_cap_sun: int = 0            # nile_trc20 전용: 사용자 **TRX 총지출 상한**(sun; Energy+Bandwidth 포함). USDT 예산과 섞지 않는다.

    def __post_init__(self):
        if self.mode not in MODES:
            raise RulesError(f"unknown mode {self.mode!r}; allowed {MODES}")
        if self.path not in PATHS:
            raise RulesError(f"unknown path {self.path!r}; allowed {PATHS}")
        _uint(self.trx_fee_cap_sun, "trx_fee_cap_sun", positive=(self.path == "nile_trc20"))
        _uint(self.amount_units, "amount_units", positive=True)
        _uint(self.budget_total_units, "budget_total_units", positive=True)
        _uint(self.min_receive_units, "min_receive_units")
        _uint(self.deadline_ts, "deadline_ts", positive=True)
        if not isinstance(self.receiver, str) or not self.receiver:
            raise RulesError("receiver required")
        if not isinstance(self.allowlist, tuple) or not all(isinstance(a, str) for a in self.allowlist):
            raise RulesError("allowlist must be a tuple of str")
        if self.network != "nile" or self.asset != "USDT":
            raise RulesError("only nile/USDT is supported in this demo")

    def digest(self) -> str:
        return _digest(dataclasses.asdict(self))


@dataclasses.dataclass(frozen=True)
class Plan:
    kind: str
    receiver: str
    value_units: int
    fee_units: int
    total_units: int
    asset: str
    network: str
    path: str = "gasfree"
    trx_fee_limit_sun: int = 0          # nile_trc20: 거래 fee_limit = **Energy** 한도(sun) = 총상한 − Bandwidth 최대
    trx_est_sun: int = 0                # nile_trc20: 견적 시점 예상 Energy 비용(sun, 여유 포함)
    trx_bandwidth_max_sun: int = 0      # nile_trc20: 무료 대역폭 소진을 가정한 Bandwidth 최대 소각(sun)
    trx_total_max_sun: int = 0          # nile_trc20: fee_limit + bandwidth_max ≤ 사용자 총상한

    def digest(self) -> str:
        return _digest(dataclasses.asdict(self))


@dataclasses.dataclass(frozen=True)
class Approval:
    approval_id: str
    flow_id: str
    revision: int
    rules_digest: str
    plan_digest: str
    approved_by: str
    approved_at: int


def _trx_ok(rules: UserRules, trx_quote) -> tuple[bool, str, dict]:
    """nile_trc20: TRX 견적 {est_energy_sun(여유 포함), bandwidth_max_sun(무료 대역폭 소진 가정), balance_sun} 이 사용자 **총상한**·잔액 안인지.
    fee_limit(Energy 한도) = 총상한 − bandwidth_max 이며 예상 Energy 비용 이상이어야 한다. USDT 와 무관하게 판정한다."""
    if not isinstance(trx_quote, dict):
        return False, "no trusted TRX fee estimate", {}
    est, bw, bal = trx_quote.get("est_energy_sun"), trx_quote.get("bandwidth_max_sun"), trx_quote.get("balance_sun")
    for v in (est, bw, bal):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return False, "no trusted TRX fee estimate", {}
    if est <= 0 or bal <= 0:
        return False, "no trusted TRX fee estimate", {}
    cap = rules.trx_fee_cap_sun
    fee_limit = cap - bw
    if fee_limit < est:
        return False, (f"TRX total cap {cap} sun cannot cover estimated energy {est} sun + max bandwidth {bw} sun "
                       f"(USDT amount is never reduced to cover TRX)"), {}
    if bal < cap:
        return False, f"TRX balance {bal} sun below total cap {cap} sun", {}
    return True, "ok", {"trx_fee_limit_sun": fee_limit, "trx_est_sun": est, "trx_bandwidth_max_sun": bw, "trx_total_max_sun": fee_limit + bw}


def candidate_plans(rules: UserRules, provider_fee_units, now: int, trx_quote=None) -> tuple[Plan, ...]:
    """코드가 계산하는 후보(불변). 규칙 위반 후보는 만들지 않는다.
    gasfree: provider_fee_units(USDT) 가 신뢰 가능한 양의 정수여야 함. nile_trc20: USDT 수수료 0 고정 + TRX 견적이 상한·잔액 안이어야 함."""
    if rules.path == "nile_trc20":
        if provider_fee_units != 0 or isinstance(provider_fee_units, bool):
            return ()
        ok, _, trx = _trx_ok(rules, trx_quote)
        if not ok:
            return ()
        fee = 0
        extra = {"path": "nile_trc20", **trx}
    else:
        try:
            fee = _uint(provider_fee_units, "provider_fee_units", positive=True)
        except RulesError:
            return ()
        extra = {}
    if now >= rules.deadline_ts or rules.receiver not in rules.allowlist:
        return ()
    if rules.mode == "exact_amount":
        total = rules.amount_units + fee
        if total <= rules.budget_total_units:
            return (Plan("exact", rules.receiver, rules.amount_units, fee, total, rules.asset, rules.network, **extra),)
        return ()
    max_value = rules.budget_total_units - fee
    value = min(rules.amount_units, max_value)
    if value > 0 and value >= rules.min_receive_units:
        return (Plan("max_within_budget", rules.receiver, value, fee, value + fee, rules.asset, rules.network, **extra),)
    return ()


def decline_reason(rules: UserRules, provider_fee_units, now: int, trx_quote=None) -> str:
    if rules.path == "nile_trc20":
        if provider_fee_units != 0 or isinstance(provider_fee_units, bool):
            return "nile_trc20 path requires USDT fee 0 (TRX cost is separate)"
        ok, why, _ = _trx_ok(rules, trx_quote)
        if not ok:
            return why
    elif not (isinstance(provider_fee_units, int) and not isinstance(provider_fee_units, bool) and provider_fee_units > 0):
        return "no trusted provider fee quote"
    if now >= rules.deadline_ts:
        return "deadline passed"
    if rules.receiver not in rules.allowlist:
        return "receiver not permitted"
    if rules.mode == "exact_amount":
        return f"exact amount {rules.amount_units} + fee {provider_fee_units} exceeds budget {rules.budget_total_units}"
    return f"max sendable within budget below min_receive {rules.min_receive_units}"


def validate_kiln_choice(choice, cands: tuple[Plan, ...]) -> tuple[dict | None, str]:
    """Kiln 응답(JSON {"choice_index": int, "reason": str}) 을 후보 범위로만 검증. 금액 필드는 거부."""
    if not isinstance(choice, dict):
        return None, "not object"
    idx = choice.get("choice_index")
    if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(cands)):
        return None, f"choice_index out of range: {idx!r}"
    extra = set(choice) - {"choice_index", "reason"}
    if extra:
        return None, f"unexpected keys {sorted(extra)}"
    return {"choice_index": idx, "reason": str(choice.get("reason", ""))[:300]}, "ok"


def verify_approval(ap, *, flow_id: str, revision: int, rules: UserRules, plan: Plan, used: set) -> str:
    """승인 기록 검증. 통과면 '' 아니면 사유."""
    if not isinstance(ap, Approval):
        return "approval must be an Approval record"
    if ap.approval_id in used:
        return "approval already used"
    if ap.flow_id != flow_id or ap.revision != revision:
        return "approval belongs to another flow/revision"
    if ap.rules_digest != rules.digest():
        return "approval rules digest mismatch"
    if ap.plan_digest != plan.digest():
        return "approval plan digest mismatch"
    if not ap.approved_by:
        return "approval has no human"
    return ""


def classify_execution(result) -> str:
    """실행 콜백 결과의 state 만 본다. dict 존재≠성공."""
    if not isinstance(result, dict):
        return "NOT_SUBMITTED"
    st = result.get("state")
    return st if st in EXEC_STATES else "UNKNOWN"


def run_flow(rules: UserRules, provider_fee_units, now: int, *, kiln_choose=None, approve=None, execute=None,
             flow_id: str | None = None, revision: int = 1, used_approvals: set | None = None, now_fn=None, trx_quote=None) -> dict:
    """한 흐름. kiln_choose(cands, rules)->(choice_dict, meta) / approve(plan, rules, ctx)->Approval|None /
    execute(plan, approval)->{"state": ..., ...}. 실행은 AI 검증 통과 + 승인 검증 통과 + 실행 직전 재검증 뒤에만."""
    fid = flow_id or f"flow-{uuid.uuid4().hex[:8]}"
    used = used_approvals if used_approvals is not None else set()
    now_fn = now_fn or (lambda: now)
    rec = {"flow_id": fid, "revision": revision, "rules_digest": rules.digest(), "guide_plan_id": rules.guide_plan_id,
           "mode": rules.mode, "candidates": [], "kiln": None, "chosen": None, "approval": None, "executed": False,
           "execution": None, "outcome": None, "reason": None, "ts": int(time.time())}
    rec["path"] = rules.path
    cands = candidate_plans(rules, provider_fee_units, now, trx_quote)
    rec["candidates"] = [dataclasses.asdict(c) for c in cands]
    if not cands:
        rec["outcome"] = "DECLINED"; rec["reason"] = decline_reason(rules, provider_fee_units, now, trx_quote)
        return rec
    # AI 선택 — 실패/없음이면 계획 전용으로 종료(실행 폴백 없음)
    if kiln_choose is None:
        rec["outcome"] = "PLAN_ONLY_AI_UNAVAILABLE"; rec["reason"] = "no model configured"; return rec
    try:
        choice, meta = kiln_choose(cands, rules)
    except Exception as e:  # 호출 실패
        rec["kiln"] = {"ok": False, "error": str(e)[:200]}
        rec["outcome"] = "PLAN_ONLY_AI_UNAVAILABLE"; rec["reason"] = "model call failed"; return rec
    rec["kiln"] = dict(meta or {})
    # 모델 결과의 출처·성공이 확인되지 않으면(meta.ok != True) 형식이 맞아도 승인/실행으로 가지 않는다(9/28 VP 링크 검수 1)
    if not isinstance(meta, dict) or meta.get("ok") is not True:
        rec["outcome"] = "PLAN_ONLY_AI_UNAVAILABLE"; rec["reason"] = "model result not ok/unverified (no fallback to execution)"; return rec
    rec["ai_mode"] = ("MANUAL_TECH_CHECK" if meta.get("manual_tech_check") else "AI_CARRIED" if meta.get("carried") else "AI_LIVE")
    rec["a_demo_eligible"] = rec["ai_mode"] != "MANUAL_TECH_CHECK"      # 수동 기술 검사는 A 시연 성공으로 집계하지 않는다
    chosen, why = validate_kiln_choice(choice, cands)
    rec["kiln"]["validation"] = why
    if chosen is None:
        rec["outcome"] = "PLAN_ONLY_AI_UNAVAILABLE"; rec["reason"] = f"model output invalid: {why}"; return rec
    plan = cands[chosen["choice_index"]]
    rec["chosen"] = {"choice_index": chosen["choice_index"], "reason": chosen["reason"], "plan": dataclasses.asdict(plan),
                     "plan_digest": plan.digest()}
    # 코드 재검증(모델과 무관)
    if plan.total_units != plan.value_units + plan.fee_units or plan.total_units > rules.budget_total_units \
            or plan.value_units <= 0 or plan.receiver not in rules.allowlist \
            or (rules.mode == "exact_amount" and plan.value_units != rules.amount_units) \
            or (rules.mode == "max_within_budget" and plan.value_units < rules.min_receive_units) \
            or plan.path != rules.path or (rules.path == "nile_trc20" and (plan.fee_units != 0 or plan.trx_total_max_sun > rules.trx_fee_cap_sun
                                                                            or plan.trx_fee_limit_sun + plan.trx_bandwidth_max_sun != plan.trx_total_max_sun)):
        rec["outcome"] = "DECLINED"; rec["reason"] = "plan violates rules after re-check"; return rec
    if approve is None:
        rec["outcome"] = "PLANNED_AWAITING_APPROVAL"; return rec
    ap = approve(plan, rules, {"flow_id": fid, "revision": revision, "rules_digest": rules.digest(), "plan_digest": plan.digest(), "ai_mode": rec.get("ai_mode")})
    if ap is None:
        rec["outcome"] = "DECLINED"; rec["reason"] = "human did not approve"; return rec
    bad = verify_approval(ap, flow_id=fid, revision=revision, rules=rules, plan=plan, used=used)
    if bad:
        rec["outcome"] = "BLOCKED_APPROVAL_INVALID"; rec["reason"] = bad; return rec
    rec["approval"] = dataclasses.asdict(ap)
    if execute is None:
        rec["outcome"] = "APPROVED_NOT_EXECUTED"; return rec
    # 실행(서명) 직전 재검증: 현재 시각·수수료·후보 재계산과 동일성
    t_exec = int(now_fn())
    fresh = candidate_plans(rules, provider_fee_units, t_exec, trx_quote)
    if plan not in fresh:
        rec["outcome"] = "DECLINED"; rec["reason"] = "conditions changed before execution (deadline/fee/plan)"; return rec
    reserved = used.reserve(ap.approval_id) if hasattr(used, "reserve") else (ap.approval_id not in used and (used.add(ap.approval_id) or True))
    if not reserved:                                   # 동시 요청: 승인 사용 표시는 원자적으로 한 번만
        rec["outcome"] = "BLOCKED_APPROVAL_INVALID"; rec["reason"] = "approval already used (race)"; return rec
    result = execute(plan, ap)
    st = classify_execution(result)
    rec["execution"] = {"state": st, "detail": result if isinstance(result, dict) else None}
    rec["executed"] = st in ("CONFIRMED", "ACCEPTED")
    rec["outcome"] = {"CONFIRMED": "EXECUTED_CONFIRMED", "ACCEPTED": "EXECUTED_ACCEPTED_PENDING",
                      "UNKNOWN": "EXECUTION_UNKNOWN_NO_RESEND", "REJECTED": "EXECUTION_REJECTED",
                      "NOT_SUBMITTED": "NOT_SUBMITTED", "FAILED": "EXECUTION_FAILED_EARLIER_NO_RESEND", "MISMATCH": "EXECUTION_MISMATCH_NEEDS_HUMAN"}[st]
    return rec


def scenario_bundle(rules: UserRules, provider_fee_units, now: int, trx_quote=None, **hooks) -> dict:
    """정상 → USDT 예산 축소(적응 또는 거절) → 기한 경과(거절). 각 흐름은 새 revision·새 flow_id·새 승인. 승인 재사용 집합 공유.
    nile_trc20 에서도 축소 대상은 USDT 예산이며 TRX 상한은 그대로다(TRX 부족을 USDT 감액으로 풀지 않는다)."""
    used: set = set()
    bundle_id = f"bundle-{rules.digest()}"
    f1 = run_flow(rules, provider_fee_units, now, revision=1, used_approvals=used, trx_quote=trx_quote, **hooks)
    fee = provider_fee_units if isinstance(provider_fee_units, int) and not isinstance(provider_fee_units, bool) else 0
    reduced = dataclasses.replace(rules, budget_total_units=max(fee + 1, rules.budget_total_units // 2))
    f2 = run_flow(reduced, provider_fee_units, now, revision=2, used_approvals=used, trx_quote=trx_quote, **hooks)
    expired = dataclasses.replace(rules, deadline_ts=max(1, now - 1))
    f3 = run_flow(expired, provider_fee_units, now, revision=3, used_approvals=used, trx_quote=trx_quote, **hooks)
    return {"bundle_id": bundle_id, "flows": [f1, f2, f3]}
