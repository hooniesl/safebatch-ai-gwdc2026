"""'AI가 도와드린 일' 카드 — 모델(qwen3-32b)이 실제로 한 일과 규칙 코드가 한 일을 구분해 보여준다(VP_AI_WORK_EXPERIENCE 9/28).

모델이 낼 수 있는 것(한 호출에서 구조화와 함께): {"next_action": ask|propose_plan|explain_decline, "question_field": <누락 필드>,
"evidence_ids": [<적용 조건 id>], "summary_ko": <짧은 한국어>, "change": {"mode": exact_amount|max_within_budget|null, "note": ..}}.
서버 검증(validate_ai_work): 코드 판정(plan.status)이 허용한 행동만, 실제 존재하는 근거 id 만, 질문은 서버가 누락으로 판정한 필드만,
설명에 주소·URL·큰 숫자·보장 표현이 있으면 사용하지 않는다(확인 필요). 조건 변경 해석은 사람이 확인해야 적용된다(승인 아님).
모델이 통과하지 못하면 규칙 코드의 결과로 표시하고 그 사실을 카드에 적는다. 결과 표시에 추가 호출은 없다.
"""
from __future__ import annotations

import re

ACTIONS = ("ask", "propose_plan", "explain_decline")
MODES = ("exact_amount", "max_within_budget")
STATUS_ACTIONS = {"unsupported": ("explain_decline",), "need_confirm": ("ask",), "ready_unverified": ("propose_plan",), "ready": ("propose_plan",)}
ACTION_KO = {"ask": "확인 질문", "propose_plan": "다음 행동 제안", "explain_decline": "멈춘 이유 설명"}
FIELD_KO = {"from": "출금 거래소", "to": "입금 거래소", "asset": "자산", "network": "네트워크", "amount_krw": "금액(원)", "own_account": "본인 계정 여부"}
STATE_KO = {"unsupported": "거절(안내 범위 밖)", "need_confirm": "확인 필요", "ready_unverified": "계획 제안(사용자 직접 확인 조건 있음)", "ready": "계획 제안"}
_ADDR = re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")
_URL = re.compile(r"https?://|www\.", re.I)
_BIGNUM = re.compile(r"\d[\d,]{3,}")
_GUARANTEE = re.compile(r"보장|무조건|확실히|반드시 성공|수익")

AI_WORK_PROMPT = (
    'Also include a key "ai" in the same JSON: {"next_action": one of ["ask","propose_plan","explain_decline"], '
    '"question_field": one of ["from","to","asset","network","amount_krw","own_account"] or null, "evidence_ids": [ids from the EVIDENCE list], '
    '"summary_ko": one or two short Korean sentences, "change": {"mode": "exact_amount"|"max_within_budget"|null, "note": short Korean} or null}. '
    "Rules: if any of from/to/asset/own_account/amount_krw/network is unknown, next_action must be \"ask\" and question_field the most important unknown "
    "(order: own_account, to, asset, network, amount_krw, from). If to is not binance or own_account is false or asset is not USDT, next_action must be "
    "\"explain_decline\". Otherwise \"propose_plan\". summary_ko must only restate facts from the EVIDENCE list and the user's text: no addresses, no URLs, "
    "no new numbers, no guarantees, no exchange policies not in EVIDENCE. Fill \"change\" only when a CHANGE_REQUEST is given: mode is "
    "max_within_budget if the user allows reducing the amount within budget, exact_amount if the amount must stay exact, null if unclear.")


def allowed_actions(plan: dict) -> tuple[str, ...]:
    return STATUS_ACTIONS.get(plan.get("status"), ())


def evidence_for_prompt(plan_rules: dict) -> list[dict]:
    """모델에 주는 제한 문맥: 조건 id·대상·한 줄 조건(서버 데이터). URL/숫자는 그대로 주되 모델은 복사 금지."""
    return [{"id": rid, "applies_to": r["applies_to"], "condition": r["condition"][:160], "kind": r["kind"]} for rid, r in plan_rules["rules"].items()]


def validate_ai_work(obj, plan: dict, change_request: str | None, known_numbers: set[int] | None = None) -> tuple[dict | None, list[str]]:
    """모델의 ai 객체를 코드 판정과 대조. 통과 항목만 담은 dict 또는 None + 사유 목록."""
    reasons: list[str] = []
    if not isinstance(obj, dict):
        return None, ["ai: 객체가 아님"]
    extra = set(obj) - {"next_action", "question_field", "evidence_ids", "summary_ko", "change"}
    if extra:
        return None, [f"ai: 허용되지 않은 키 {sorted(extra)}"]
    allowed = allowed_actions(plan)
    act = obj.get("next_action")
    if act not in allowed:
        reasons.append(f"next_action {act!r} 는 코드 판정({plan.get('status')})이 허용한 {list(allowed)} 가 아님")
        act = None
    need_fields = [n["field"] for n in plan.get("need_confirm", [])]
    qf = obj.get("question_field")
    if act == "ask":
        if qf not in need_fields:
            reasons.append(f"question_field {qf!r} 는 서버가 누락으로 판정한 {need_fields} 에 없음")
            qf = None
    elif qf is not None:
        reasons.append("질문이 필요 없는 상태에서 question_field 를 냄"); qf = None
    ev_ok = set(_all_rules())                             # 근거 id 는 규칙 파일에 실제로 있는 것만(이 단계 적용 여부와 무관)
    ev = obj.get("evidence_ids")
    if ev is None:
        ev = []
    if not isinstance(ev, list) or not all(isinstance(e, str) for e in ev):
        reasons.append("evidence_ids 형식 오류"); ev = []
    bad = [e for e in ev if e not in ev_ok]
    if bad:
        reasons.append(f"없는 근거 id {bad}"); ev = [e for e in ev if e in ev_ok]
    summ = obj.get("summary_ko")
    if not isinstance(summ, str) or not summ.strip():
        reasons.append("summary_ko 없음"); summ = None
    else:
        summ = summ.strip()[:300]
        nums = {int(n.replace(",", "")) for n in _BIGNUM.findall(summ) if n.replace(",", "").isdigit()}
        if _ADDR.search(summ) or _URL.search(summ):
            reasons.append("설명에 주소/URL 포함 → 사용 안 함"); summ = None
        elif nums - (known_numbers or set()):
            reasons.append(f"설명에 입력/근거에 없는 숫자 {sorted(nums - (known_numbers or set()))} → 사용 안 함"); summ = None
        elif _GUARANTEE.search(summ):
            reasons.append("설명에 보장/수익 표현 → 사용 안 함"); summ = None
    ch = obj.get("change")
    change = None
    if change_request:
        if isinstance(ch, dict) and set(ch) <= {"mode", "note"} and ch.get("mode") in (None, *MODES):
            change = {"mode": ch.get("mode"), "note": str(ch.get("note") or "")[:120], "needs_human_confirm": True}
        elif ch is not None:
            reasons.append("change 해석 형식 오류 → 사용 안 함")
    elif ch not in (None, {}):
        reasons.append("변경 요청이 없는데 change 를 냄 → 무시")
    if act is None and summ is None and qf is None and not ev and change is None:
        return None, reasons
    return {"next_action": act, "question_field": qf, "evidence_ids": ev, "summary_ko": summ, "change": change}, reasons


def rules_result(plan: dict) -> dict:
    """규칙 코드만으로 만든 같은 구조(모델 미사용/실패 시 표시용)."""
    allowed = allowed_actions(plan)
    act = allowed[0] if allowed else None
    need = plan.get("need_confirm", [])
    qf = need[0]["field"] if act == "ask" and need else None
    if act == "ask":
        summ = need[0]["question"]
    elif act == "explain_decline":
        summ = " ".join(plan.get("unsupported", []))[:300]
    else:
        summ = plan["current_step"]["do_now"][:300] if plan.get("current_step") else None
    return {"next_action": act, "question_field": qf, "evidence_ids": [c["id"] for c in plan.get("conditions", [])], "summary_ko": summ, "change": None}


def work_card(plan: dict, si: dict, ai_obj, change_request: str | None = None) -> dict:
    """사용자용 카드. 각 항목에 누가 했는지(AI/규칙)와 근거를 붙인다. 표시 자체는 추가 호출 없음."""
    k = si.get("kiln") or {}
    model_used_for_intent = bool(k.get("used"))
    known = {v for v in (plan.get("intent") or {}).values() if isinstance(v, int) and not isinstance(v, bool)}
    ai, reasons = (validate_ai_work(ai_obj, plan, change_request, known) if ai_obj is not None else (None, ["모델 출력에 ai 항목 없음"] if model_used_for_intent else []))
    rb = rules_result(plan)
    if k.get("outcome") is None and si.get("source") == "rules":
        ai_status = "off"
    elif model_used_for_intent and ai and not reasons:
        ai_status = "used"
    elif model_used_for_intent and ai:
        ai_status = "partially_used"
    elif model_used_for_intent:
        ai_status = "intent_only"
    else:
        ai_status = "unused_failed"
    llm_intent = k.get("llm_intent") or {}
    rb_intent = si.get("rules_intent") or {}
    understood, unknown = [], []
    for f, v in (plan.get("intent") or {}).items():
        if v is None:
            unknown.append(FIELD_KO.get(f, f))
        else:
            src = "AI+규칙 일치" if (llm_intent.get(f) == v and rb_intent.get(f) == v) else "AI 해석" if llm_intent.get(f) == v else "규칙 해석" if rb_intent.get(f) == v else "사용자 입력"
            understood.append({"field": FIELD_KO.get(f, f), "value": (plan.get("intent_ko") or {}).get(_KO_KEY.get(f, f), v), "by": src})
    items = [{"step": "요청 이해", "by": "AI(qwen3-32b)+규칙" if model_used_for_intent else "규칙", "understood": understood, "unknown": unknown,
              "conflicts": plan.get("conflicts", []), "step_suggestion": plan.get("step_suggestion"),
              "note": "입력에 없는 계정 소유·주소·금액·네트워크는 채우지 않았습니다. 직접 고칠 수 있습니다."}]
    use_ai = ai and ai.get("next_action") is not None
    src = ai if use_ai else rb
    if src["next_action"] == "ask":
        items.append({"step": "막힌 부분 질문", "by": "AI(qwen3-32b)" if use_ai and ai.get("question_field") else "규칙",
                      "question_field": (ai.get("question_field") if use_ai else None) or rb["question_field"],
                      "question": next((n["question"] for n in plan.get("need_confirm", []) if n["field"] == ((ai.get("question_field") if use_ai else None) or rb["question_field"])), None),
                      "all_unknown": [FIELD_KO.get(n["field"], n["field"]) for n in plan.get("need_confirm", [])],
                      "note": "질문 하나만 보여도 나머지 필수 조건 검사는 그대로 수행됩니다."})
    ev_ids = (ai.get("evidence_ids") if use_ai and ai.get("evidence_ids") else rb["evidence_ids"])
    allr = _all_rules()
    ev = [{"id": rid, "applies_to": allr[rid]["applies_to"], "kind": allr[rid]["kind"], "checked_at": allr[rid]["checked_at"]} for rid in ev_ids if rid in allr]
    summary = (ai.get("summary_ko") if use_ai and ai.get("summary_ko") else rb["summary_ko"])
    items.append({"step": {"ask": "지금 필요한 것", "propose_plan": "맞춤 안내 · 다음 행동", "explain_decline": "멈춘 이유"}.get(src["next_action"], "안내"),
                  "by": "AI(qwen3-32b) 설명 · 근거는 서버 자료" if (use_ai and ai.get("summary_ko")) else "규칙", "summary": summary,
                  "action": ACTION_KO.get(src["next_action"]), "evidence": ev,
                  "single_candidate_note": "후보 계획이 하나면 '여러 경로 최적화'가 아니라 규칙 적합 설명입니다."})
    if change_request:
        ch = (ai or {}).get("change")
        items.append({"step": "조건 변경", "by": "AI(qwen3-32b) 해석 → 사람 확인 필요" if ch else "해석 못 함(사람이 직접 선택)", "request": change_request[:200],
                      "interpretation": ch, "note": "변경 답변은 서명 승인이 아닙니다. 확인하면 새 revision 으로 다시 확인받습니다."})
    items.append({"step": "진행/결과", "by": "규칙(실제 상태)", "state": plan.get("status"), "state_label": STATE_KO.get(plan.get("status"), plan.get("status")),
                  "done": ["요청 구조화(이해만)", "필수 조건 검사(안내만)", "적용 조건·근거 연결 — 전송·입금 없음"], "not_yet": ["송금·서명·입금(이 화면은 권한 없음, 아무것도 보내지 않았음)"] + (["사용자 확인 대기"] if plan.get("status") == "need_confirm" else [])})
    return {"ai_status": ai_status, "ai_status_ko": {"used": "AI 사용(검증 통과)", "partially_used": "AI 일부 사용(일부 항목은 검증 실패로 규칙 표시)", "intent_only": "AI 는 요청 구조화에만 사용(후속 제안은 규칙)",
                                                   "unused_failed": "AI 실패 → 규칙만 사용", "off": "AI 미사용(규칙만)"}[ai_status],
            "model": k.get("model"), "flow_id": si.get("flow_id"), "kiln_outcome": k.get("outcome"), "rejected_reasons": reasons, "items": items,
            "raw_ai": ai_obj if isinstance(ai_obj, dict) else None, "honesty": "이 카드는 실제로 수행된 작업만 표시하며, 시스템이 읽어 온 자료를 AI 가 실시간 조회한 것처럼 쓰지 않습니다."}


def _all_rules() -> dict:
    try:
        from . import plan as _P
    except ImportError:
        import plan as _P  # type: ignore
    return _P.load_rules()["rules"]


_KO_KEY = {"from": "코인을 보낼 곳(출금 거래소)", "to": "코인을 받을 곳(입금 거래소)", "asset": "자산", "network": "네트워크", "amount_krw": "송금할 코인 가치(원화 환산 참고값)", "own_account": "받을 곳 계정 명의"}
