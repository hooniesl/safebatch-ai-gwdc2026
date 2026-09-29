"""휴대폰 대화의 AI 파싱 계층(9/29 부사장 지시). 승인 전 MockKiln(실호출 0) / 승인 후 KilnLive(qwen3-32b, 도구 호출 1회, 재시도 없음).

원칙
- 모델은 **의도와 부족 정보만** 구조화한다: {alias, amount, asset, change{budget_trx, deadline_minutes}, reason, missing}. 주소·최종 수량·상한·만료는 코드가 결정한다.
- 모델 출력은 스키마 검증 뒤에만 사용: 허용 키만, 주소 필드 금지, alias 는 문자열, amount/budget 은 유한한 양의 십진수(상한 있음), deadline 은 양의 정수(≤1440분).
- 모델이 **사용자가 말하지 않은 조건**(예산·기한)을 추가하면 적용하지 않는다: change 의 숫자가 사용자 문장에 그대로 나타나야 한다.
- 정확히 요청한 수량(예: 2 TRX)을 임의로 낮추지 않는다. 적응은 사용자가 명시한 예산·기한 변경에만 따르며 새 제안·재승인이 필요하고, 그 값은 주문 생성(만료·수수료 상한)에도 반영된다.
- 거절은 명백한 제약 위반(요청당 상한 초과·미지원 자산·자기 전송·예산이 수량+상한 미만)에만. 미등록 별칭은 정보 확인(질문)이며 거절 시연이 아니다.
- 규칙 파서 폴백은 실제 AI 성공으로 집계하지 않는다(ai.fallback 표시, calls 는 실제 호출 수).
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

import phone_chat as PC

ALLOWED_KEYS = {"alias", "amount", "asset", "change", "reason", "missing"}
REQUIRED_KEYS = ("alias", "amount", "asset")
CHANGE_KEYS = {"budget_trx", "deadline_minutes"}
MAX_TRX = PC.MAX_TRX_PER_REQUEST
MAX_BUDGET_TRX = Decimal("1000000")
MAX_DEADLINE_MIN = 1440
TOOL = {"type": "function", "function": {"name": "parse_transfer_request", "description": "Extract intent fields only. Never decide addresses or final amounts.",
        "parameters": {"type": "object", "properties": {
            "alias": {"type": ["string", "null"], "description": "recipient nickname as written by the user"},
            "amount": {"type": ["string", "null"], "description": "requested amount as decimal string, exactly as the user said"},
            "asset": {"type": ["string", "null"], "enum": ["TRX", "OTHER", None]},
            "change": {"type": ["object", "null"], "properties": {"budget_trx": {"type": ["string", "null"]}, "deadline_minutes": {"type": ["integer", "null"]}}, "additionalProperties": False},
            "missing": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}}, "additionalProperties": False}}}
# 9/28 실증 성공 경로(flow_tools_demo: /no_think 를 system 맨 앞, tool_choice "auto", reasoning_tokens 1, finish tool_calls)와 같은 형태로 맞춘다.
# 9/29 11:28 실패 4건은 tool_choice 를 함수명으로 강제 + /no_think 를 user 끝에 붙인 형태였고, 4건 모두 completion_tokens == reasoning_tokens,
# finish_reason stop, tool_calls 없음 → 출력 전부가 reasoning 으로 분류되어 도구 인자를 받지 못했다(저장 로그 근거; 본문 미보존이라 최종 확인은 다음 호출).
SYSTEM = ("/no_think You extract a TRON testnet transfer request from Korean text. Call the tool parse_transfer_request exactly once and output nothing else. "
          "Do not invent budget or deadline the user did not state. Do not lower or change the requested amount. Never output addresses. "
          "'트론'/'TRX' means asset TRX; USDT or others = OTHER.")
_JSON_OBJ = re.compile(r"\{.*\}", re.S)


TOOL_NAME = "parse_transfer_request"


def extract_tool_call(r: dict) -> dict:
    """Kiln 응답의 도구 호출을 **엄격히** 판정한다(VP 9/29 보완).
    성공 조건: message.tool_calls 가 정확히 1건이고 function.name == parse_transfer_request 이며 arguments 가 비어 있지 않다(문자열 또는 객체).
    반환 {"raw": 인자 JSON 문자열 또는 "", "source": "tool_calls"|"none", "tool_calls_n": n, "tool_names": [...],
          "diagnostic": {"content_json": …, "reasoning_json": …}}  — content/reasoning_content 안의 JSON 은 진단용 기록일 뿐 성공으로 쓰지 않는다."""
    out = {"raw": "", "source": "none", "tool_calls_n": 0, "tool_names": [], "diagnostic": {}}
    if not isinstance(r, dict) or not r.get("ok"):
        return out
    tcs = r.get("tool_calls") or []
    if isinstance(tcs, list):
        out["tool_calls_n"] = len(tcs)
        out["tool_names"] = [((tc.get("function") or {}).get("name") if isinstance(tc, dict) else None) for tc in tcs]
        if len(tcs) == 1 and isinstance(tcs[0], dict) and (tcs[0].get("function") or {}).get("name") == TOOL_NAME:
            args = (tcs[0].get("function") or {}).get("arguments")
            if isinstance(args, dict) and args:
                out["raw"], out["source"] = json.dumps(args, ensure_ascii=False), "tool_calls"
            elif isinstance(args, str) and args.strip():
                out["raw"], out["source"] = args, "tool_calls"
    for field, key in (("content", "content_json"), ("reasoning_content", "reasoning_json")):   # 진단용만
        s = r.get(field)
        if not isinstance(s, str) or not s.strip():
            continue
        s2 = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
        m = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", s2, re.S)
        cand = m.group(1) if m else (_JSON_OBJ.search(s2).group(0) if _JSON_OBJ.search(s2) else None)
        if cand is None:
            continue
        try:
            json.loads(cand); out["diagnostic"][key] = cand[:400]
        except Exception:                                   # noqa: BLE001
            out["diagnostic"][key + "_invalid"] = cand[:120]
    return out


def extract_tool_arguments(r: dict) -> tuple[str, str]:
    """호환용: (raw, source). 엄격 판정만 성공(source=tool_calls), 그 외 ("", "none")."""
    e = extract_tool_call(r)
    return e["raw"], e["source"]


class MockKiln:
    """규칙으로 흉내 낸 모델 응답. 실제 Kiln qwen3-32b 호출 아님."""
    provider = "MOCK_KILN"
    model = "qwen3-32b(mock)"

    def structure(self, text: str) -> dict:
        t = (text or "").strip()
        amt = PC._extract_amount(t)
        alias = PC._extract_alias_token(t, PC.load_contacts())   # 등록 별칭 띄어쓰기/조사 관용(9/29 음성 입력)
        asset = "TRX" if PC.ASSET_TRX.search(t) else ("OTHER" if PC.ASSET_OTHER.search(t) else None)
        change = {}
        m = re.search(r"예산\s*(\d+(?:\.\d+)?)", t)
        if m:
            change["budget_trx"] = m.group(1)
        m = re.search(r"(\d+)\s*분\s*(안|내|이내)", t)
        if m:
            change["deadline_minutes"] = int(m.group(1))
        missing = [k for k, v in (("alias", alias), ("amount", amt), ("asset", asset)) if not v]
        out = {"alias": alias, "amount": (str(amt.normalize()) if amt is not None else None), "asset": asset, "change": change or None,
               "reason": "모의 모델: 문장에서 별칭·수량·자산·변경 조건만 추출", "missing": missing}
        return {"provider": self.provider, "model": self.model, "raw": json.dumps(out, ensure_ascii=False), "calls": 0, "usage": None}


class KilnLive:
    """실제 Kiln qwen3-32b 도구 호출 1회(재시도 없음). 사장 승인 범위 안에서만 생성해 사용한다. 실패는 calls=1 로 집계되고 규칙 폴백은 성공이 아니다."""
    provider = "KILN_LIVE"
    model = "qwen3-32b"

    def __init__(self, chat_fn=None, flow_id: str = "phone"):
        self.chat_fn, self.flow_id = chat_fn, flow_id

    def structure(self, text: str) -> dict:
        import kiln_client as KC
        fn = self.chat_fn or KC.chat
        r = fn(self.build_messages(text), flow_id=self.flow_id, tools=[TOOL], tool_choice="auto", max_tokens=300)
        e = extract_tool_call(r)
        raw, source = e["raw"], e["source"]
        err = r.get("error")
        if not raw and not err:
            u = r.get("usage") or {}
            err = (f"no valid tool call (tool_calls={e['tool_calls_n']} names={e['tool_names']} finish={r.get('finish_reason')}, completion={u.get('completion_tokens')}, "
                   f"reasoning={(u.get('completion_tokens_details') or {}).get('reasoning_tokens')}, raw_file={r.get('raw_file') or r.get('call_id')})")
        return {"provider": self.provider, "model": r.get("model") or self.model, "raw": raw or "", "calls": 1, "usage": r.get("usage"),
                "request_id": r.get("request_id"), "http": r.get("http"), "error": err if not raw else None, "source": source,
                "finish_reason": r.get("finish_reason"), "call_id": r.get("call_id"), "tool_calls_n": e["tool_calls_n"], "diagnostic": e["diagnostic"] or None,
                "raw_saved": r.get("raw_saved"), "raw_error": r.get("raw_error"), "raw_file": r.get("raw_file"),
                "cost_usd": r.get("cost_usd"), "cost_status": r.get("cost_status")}

    @staticmethod
    def build_messages(text: str) -> list[dict]:
        """/no_think 는 system 맨 앞(9/28 성공 형태). user 문장은 그대로(300자 제한)."""
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": (text or "")[:300]}]


def _positive_decimal(v, cap: Decimal) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite() or d <= 0 or d > cap:
        return None
    return d


def validate_model_output(raw: str) -> tuple[dict | None, str]:
    try:
        o = json.loads(raw or "")
    except Exception:                                       # noqa: BLE001
        return None, "not json"
    if not isinstance(o, dict):
        return None, "not object"
    extra = set(o) - ALLOWED_KEYS
    if extra:
        return None, f"unexpected keys {sorted(extra)}"
    missing_keys = [k for k in REQUIRED_KEYS if k not in o]
    if missing_keys:                                        # VP 9/29 R2 검수: "{}"·필수 키 누락은 성공 아님(정보 부족은 키가 있고 값이 null 인 경우만)
        return None, f"missing required keys {missing_keys}"
    for k in ("address", "to", "receiver", "to_address"):
        if k in o:
            return None, "address field not allowed"
    if o.get("alias") is not None and not isinstance(o["alias"], str):
        return None, "alias must be string"
    if o.get("amount") is not None and _positive_decimal(o["amount"], MAX_BUDGET_TRX) is None:
        return None, "amount must be finite positive decimal"
    ch = o.get("change")
    if ch is not None:
        if not isinstance(ch, dict) or set(ch) - CHANGE_KEYS:
            return None, "bad change keys"
        if ch.get("budget_trx") is not None and _positive_decimal(ch["budget_trx"], MAX_BUDGET_TRX) is None:
            return None, "budget must be finite positive decimal"
        dm = ch.get("deadline_minutes")
        if dm is not None and (isinstance(dm, bool) or not isinstance(dm, int) or dm <= 0 or dm > MAX_DEADLINE_MIN):
            return None, "deadline must be positive int minutes ≤ 1440"
    if o.get("asset") not in (None, "TRX", "OTHER"):
        return None, "bad asset"
    return o, "ok"


BUDGET_CTX = re.compile(r"예산\s*(\d+(?:\.\d+)?)")
DEADLINE_CTX = re.compile(r"(\d+)\s*분")


def _same_number(a, b) -> bool:
    try:
        return Decimal(str(a)).normalize() == Decimal(str(b)).normalize()
    except Exception:                                       # noqa: BLE001
        return str(a) == str(b)


def _condition_stated(key: str, value, text: str) -> bool:
    """모델이 낸 예산·기한이 사용자 문장의 **해당 문맥**('예산 N' / 'N분')에 있는지. 수량 숫자와 같다는 이유만으로는 인정하지 않는다(VP 9/29)."""
    if value is None:
        return False
    pat = BUDGET_CTX if key == "budget_trx" else DEADLINE_CTX if key == "deadline_minutes" else None
    if pat is None:
        return False
    return any(_same_number(m.group(1), value) for m in pat.finditer(text or ""))


def _stated_in_text(value, text: str) -> bool:
    """호환용(문맥 없는 단순 포함 검사). 조건 판정에는 _condition_stated 를 쓴다."""
    return value is not None and (str(value) in (text or ""))


def outcome_of(r: dict) -> tuple[bool, str]:
    """AI 판단 결과의 최종 판정(스키마·의미·기록·비용 모두). 실호출 예약 확정에 쓴다."""
    ai = r.get("ai") or {}
    if ai.get("error"):
        return False, f"error: {ai['error']}"
    if ai.get("fallback"):
        return False, f"schema/semantic: {ai['fallback']}"
    if ai.get("ignored_model_conditions"):
        return False, f"semantic: invented conditions {ai['ignored_model_conditions']}"
    if ai.get("mode") == "KILN_LIVE":
        if ai.get("source") != "tool_calls":
            return False, f"no valid tool call (source={ai.get('source')})"
        if ai.get("raw_saved") is not True:
            return False, f"raw not saved: {ai.get('raw_error')}"
        if ai.get("cost_status") != "server_usage":
            return False, f"cost unknown: {ai.get('cost_status')}"
    return True, ""


def decide(text: str, contacts: list[dict], provider=None, fee_cap_trx: Decimal = Decimal("2")) -> dict:
    """대화 → proposal(정상|adapt) / decline / question(정보 확인) / fallback. 주소·수량·상한·만료는 코드가 정한다.
    실호출 제공자(on_outcome 보유)는 스키마·의미·기록·비용 검증이 **모두 끝난 뒤** 한 번만 예약을 확정한다(VP 9/29 R2b 불변식 2). 예외로 확정 못 하면 예약은 pending 으로 남아 후속 호출을 막는다."""
    provider = provider or MockKiln()
    notify = getattr(provider, "on_outcome", None)
    r = _decide_inner(text, contacts, provider, fee_cap_trx)
    if callable(notify) and (r.get("ai") or {}).get("calls"):
        ok, why = outcome_of(r)
        notify(ok, why, reservation=(r.get("ai") or {}).get("reservation"))
    return r


def _decide_inner(text: str, contacts: list[dict], provider, fee_cap_trx: Decimal) -> dict:
    resp = provider.structure(text)
    ai = {"mode": provider.provider, "model": resp.get("model") or provider.model, "calls": resp.get("calls", 0), "usage": resp.get("usage"),
          "note": "모의 모델 응답(실제 Kiln 호출 아님)" if provider.provider == "MOCK_KILN" else "Kiln qwen3-32b 실호출", "request_id": resp.get("request_id")}
    for k in ("source", "finish_reason", "call_id", "tool_calls_n", "diagnostic", "raw_saved", "raw_error", "raw_file", "cost_usd", "cost_status", "budget", "reservation"):
        if k in resp:                                          # 실호출 사후 분석용: 어느 필드에서 인자를 얻었나·왜 없었나·원문 보존·비용 확인 상태. None 도 그대로 남긴다(숨기지 않음)
            ai[k] = resp[k]
    if resp.get("error"):
        ai["error"] = str(resp["error"])[:200]
    o, why = validate_model_output(resp.get("raw") or "")
    if o is None:
        r = PC.parse_request(text, contacts); r["ai"] = {**ai, "fallback": f"모델 출력 검증 실패({why}) → 규칙 파서(실제 AI 성공 아님)"}; r["kind_detail"] = "fallback"; return r
    # 모델이 지어낸 조건 차단: 사용자 문장에 없는 예산·기한은 버린다(기록만) — 실호출이면 의미 실패로 통보
    change = dict(o.get("change") or {}); ignored = {}
    for k in list(change):
        if not _condition_stated(k, change[k], text):
            ignored[k] = change.pop(k)
    if ignored:
        ai["ignored_model_conditions"] = ignored
    # 정상적인 정보 부족(키는 있고 값이 null): 질문으로 구분(AI 성공, 폴백 아님). 주문 생성 없음
    lacking = [k for k in REQUIRED_KEYS if o.get(k) in (None, "")]
    if lacking:
        return {"kind": "question", "kind_detail": "info_check", "ai": ai, "text": text, "understood": o, "reason": "MISSING_INFO", "missing": lacking,
                "question": "확인이 필요합니다: " + ", ".join({"alias": "받는 사람(별칭)", "amount": "수량", "asset": "자산(TRX)"}[k] for k in lacking) + " 을 알려 주세요.", "order_created": False}
    if o.get("asset") == "OTHER":
        return {"kind": "decline", "ai": ai, "text": text, "understood": o, "reason_code": "UNSUPPORTED_ASSET",
                "explain": "이 창구는 Nile 테스트넷 TRX 만 보냅니다. 다른 자산은 처리하지 않습니다.", "order_created": False}
    amt = Decimal(str(o["amount"])) if o.get("amount") is not None else None
    if amt is not None and amt > MAX_TRX:
        return {"kind": "decline", "ai": ai, "text": text, "understood": o, "reason_code": "OVER_PER_REQUEST_CAP",
                "explain": f"요청 {amt} TRX 는 요청당 상한 {MAX_TRX} TRX 를 넘습니다. 수량을 임의로 낮추지 않고 거절합니다.", "order_created": False}
    hits = PC.find_alias(contacts, o["alias"]) if o.get("alias") else []
    if len(hits) == 1 and hits[0].get("address") and hits[0].get("confirmed_by_owner") and hits[0].get("self_wallet"):
        return {"kind": "decline", "ai": ai, "text": text, "understood": o, "reason_code": "SELF_TRANSFER",
                "explain": "보내는 지갑과 같은 주소로는 보내지 않습니다.", "order_created": False}
    base = PC.parse_request(text, contacts)
    if base["kind"] != "proposal":
        base["ai"] = ai; base["kind_detail"] = "info_check"; return base
    prop = base["proposal"]
    if amt is not None and Decimal(prop["amount_trx"]) != amt:
        ai = {**ai, "fallback": f"amount mismatch(수량 불일치): 모델 {amt} ≠ 규칙 {prop['amount_trx']} → 규칙 수량 사용(실제 AI 성공 아님)"}
    constraints = {"budget_sun": None, "deadline_s": None, "fee_cap_sun": int(fee_cap_trx * 1_000_000)}
    if change:
        budget = Decimal(str(change["budget_trx"])) if change.get("budget_trx") is not None else None
        deadline_min = int(change["deadline_minutes"]) if change.get("deadline_minutes") is not None else None
        need = Decimal(prop["amount_trx"]) + fee_cap_trx
        if budget is not None and budget < Decimal(prop["amount_trx"]):
            return {"kind": "decline", "ai": ai, "text": text, "understood": o, "reason_code": "BUDGET_BELOW_REQUEST",
                    "explain": f"예산 {budget} TRX 로는 요청 {prop['amount_trx']} TRX 를 보낼 수 없습니다. 수량을 임의로 낮추지 않습니다. 예산이나 수량을 다시 알려 주세요.", "order_created": False}
        if budget is not None:
            constraints["budget_sun"] = int(budget * 1_000_000)
            constraints["fee_cap_sun"] = int(min(fee_cap_trx, budget - Decimal(prop["amount_trx"])) * 1_000_000)   # 예산 안에서만 수수료 허용
        if deadline_min is not None:
            constraints["deadline_s"] = deadline_min * 60
        adapt = {"budget_trx": (str(budget) if budget is not None else None), "deadline_minutes": deadline_min, "amount_trx": prop["amount_trx"],
                 "fee_cap_trx": str(Decimal(constraints["fee_cap_sun"]) / 1_000_000), "need_trx_at_default_cap": str(need)}
        return {**base, "kind": "proposal", "kind_detail": "adapt", "ai": ai, "adapt": adapt, "constraints": constraints,
                "next": ((base.get("confirm_note") + " ") if base.get("confirm_note") else "") + "조건이 바뀌어 새 제안입니다. 이전 승인은 무효이며 이 내용으로 다시 확인·서명해야 합니다. 예산·기한은 주문의 수수료 상한·만료에 반영됩니다."}
    base["ai"] = ai; base["kind_detail"] = "normal"; base["constraints"] = constraints; return base


# ── 승인 예산 관리(VP 9/29 R2): CLI 러너·서버가 같은 파일을 쓴다. 실패·타임아웃도 횟수 포함, 실패 뒤 후속 호출 중단 ─────────
import os as _os
import pathlib as _pathlib
import threading as _threading
import time as _time

BUDGET_FILE = _pathlib.Path(__file__).resolve().parent / "logs" / "kiln_budget_r2_20260929.json"


class KilnBudget:
    """승인된 호출 수·비용 상한을 파일로 집계한다(VP 9/29 R2 검수 반영).
    - 호출 **전**에 reserve() 로 횟수를 영구 소비(파일에 기록)하고, 호출 뒤 settle() 로 결과·비용을 채운다. 예외·프로세스 중단 시에도 예약은 남는다(pending 항목 = 소비된 횟수).
    - 프로세스 간 잠금: fcntl.flock(<파일>.lock). CLI 러너와 서버(다중 스레드)가 같은 파일을 쓴다.
    - 집계 파일이 없으면 새 승인으로 보지 않는다 → 호출 거부. 파일은 init() 로 승인 원문과 함께 명시적으로 만든다.
    - 비용 미확인(cost_usd None)은 0 으로 두지 않고 unknown_cost_calls 로 세며 중단 사유가 된다."""
    _tlock = _threading.Lock()

    def __init__(self, path: _pathlib.Path | None = None, max_calls: int = 4, max_usd: float = 0.01, approval: str = "사장 2026-09-29 '응 상한 승인할께'"):
        self.path = _pathlib.Path(path or BUDGET_FILE); self.max_calls, self.max_usd, self.approval = int(max_calls), float(max_usd), approval

    def init(self, approval: str | None = None) -> dict:
        """승인 기록과 함께 집계 파일을 만든다(이미 있으면 그대로 반환·덮어쓰지 않음)."""
        with self._locked():
            if self.path.exists():
                return self._load_unlocked()
            d = {"approval": approval or self.approval, "max_calls": self.max_calls, "max_usd": self.max_usd, "used": 0, "cost_usd": 0.0, "unknown_cost_calls": 0, "halted": None, "calls": []}
            self._save(d); return d

    def _locked(self):
        return _BudgetLock(self)

    def _load_unlocked(self) -> dict:
        if not self.path.exists():
            return {"missing": True, "used": self.max_calls, "cost_usd": None, "halted": "budget file missing (not a fresh approval)"}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:                              # noqa: BLE001 — 깨진 집계 파일은 '판단 불가' → 호출 차단
            return {"corrupt": f"{type(e).__name__}: {e}", "used": self.max_calls, "cost_usd": None, "halted": "budget file unreadable"}

    def load(self) -> dict:
        with self._locked():
            return self._load_unlocked()

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp"); tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8"); _os.replace(tmp, self.path)

    def _check(self, d: dict) -> tuple[bool, str]:
        if d.get("missing"):
            return False, "budget file missing (init required; not treated as new approval)"
        if d.get("halted"):
            return False, f"halted: {d['halted']}"
        pend = [c.get("reservation") for c in d.get("calls", []) if c.get("pending")]
        if pend:                                            # 불변식 1: 미해결 예약(호출 중·검증 중·중단으로 남은 것)이 있으면 다음 유료 호출 불허. 시간 경과로 풀지 않는다
            return False, f"pending reservation {pend[0]} (unresolved call/verification; not cleared by restart or time)"
        if int(d.get("used", 0)) >= self.max_calls:
            return False, f"call cap reached ({d.get('used')}/{self.max_calls})"
        if d.get("cost_usd") is not None and float(d["cost_usd"]) >= self.max_usd:
            return False, f"cost cap reached (${d['cost_usd']} ≥ ${self.max_usd})"
        return True, ""

    def can_call(self) -> tuple[bool, str]:
        return self._check(self.load())

    def reserve(self, flow: str) -> tuple[str | None, str]:
        """호출 전에 횟수 1회를 영구 예약(used+1, pending 항목). 반환 (예약 id, 거부 사유)."""
        with self._locked():
            d = self._load_unlocked()
            ok, why = self._check(d)
            if not ok:
                return None, why
            rid = f"r{int(_time.time() * 1000) % 100000000:08d}{_os.getpid() % 1000:03d}{len(d.get('calls', []))}"
            d["used"] = int(d.get("used", 0)) + 1
            d.setdefault("calls", []).append({"reservation": rid, "ts": _time.strftime("%Y-%m-%dT%H:%M:%S%z"), "flow": flow, "pending": True, "ok": None, "why": None,
                                              "call_id": None, "http": None, "source": None, "cost_usd": None, "cost_status": "pending", "raw_saved": None})
            self._save(d); return rid, ""

    def settle(self, rid: str, resp: dict, ok: bool, why: str = "", final: bool = False) -> dict:
        """네트워크 단계 결과(비용·원문 보존·도구 호출 형식)를 예약에 채운다. final=False 면 pending 을 유지(스키마·의미 검증 중) → finalize() 가 확정.
        ok=False 면 즉시 halted(후속 차단). 비용은 숫자일 때만 더한다."""
        with self._locked():
            d = self._load_unlocked()
            if d.get("missing") or d.get("corrupt"):
                return d
            entry = next((c for c in d.get("calls", []) if c.get("reservation") == rid), None)
            if entry is None:                               # 예약이 없으면(파일 교체 등) 별도 항목으로 남기되 횟수는 다시 센다
                entry = {"reservation": rid, "ts": _time.strftime("%Y-%m-%dT%H:%M:%S%z"), "flow": "?", "note": "reservation not found; counted again", "pending": True}
                d.setdefault("calls", []).append(entry); d["used"] = int(d.get("used", 0)) + 1
            if not entry.get("cost_settled"):
                c = resp.get("cost_usd")
                if isinstance(c, (int, float)) and not isinstance(c, bool):
                    d["cost_usd"] = (float(d["cost_usd"]) if d.get("cost_usd") is not None else 0.0) + float(c)
                else:
                    d["unknown_cost_calls"] = int(d.get("unknown_cost_calls", 0)) + 1
                entry["cost_settled"] = True
            entry.update({"phase": "final" if (final or not ok) else "verifying", "pending": not (final or not ok), "settled_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                          "call_id": resp.get("call_id"), "http": resp.get("http"), "source": resp.get("source"), "cost_usd": resp.get("cost_usd"), "cost_status": resp.get("cost_status"),
                          "raw_saved": resp.get("raw_saved"), "ok": (ok if (final or not ok) else None), "why": (why or "")[:200]})
            if not ok and not d.get("halted"):
                d["halted"] = f"{entry.get('flow')}: {(why or '')[:160]}"
            self._save(d); return d

    def finalize(self, rid: str, ok: bool, why: str = "") -> dict:
        """스키마·의미 검증까지 끝난 뒤 예약을 확정(pending 해제). 실패면 halted 로 남긴다."""
        with self._locked():
            d = self._load_unlocked()
            if d.get("missing") or d.get("corrupt"):
                return d
            entry = next((c for c in d.get("calls", []) if c.get("reservation") == rid), None)
            if entry is None:
                d["halted"] = d.get("halted") or f"finalize: unknown reservation {rid}"; self._save(d); return d
            entry.update({"pending": False, "phase": "final", "ok": bool(ok), "why": (why or entry.get("why") or "")[:200], "finalized_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            if not ok and not d.get("halted"):
                d["halted"] = f"{entry.get('flow')}: {(why or '')[:160]}"
            self._save(d); return d

    def halt(self, why: str) -> dict:
        with self._locked():
            d = self._load_unlocked()
            if not d.get("missing") and not d.get("corrupt") and not d.get("halted"):
                d["halted"] = why[:200]; self._save(d)
            return d

    def record(self, flow: str, resp: dict, ok: bool, why: str = "") -> dict:
        """호환용(1회 집계, 즉시 확정): reserve + settle(final)."""
        rid, deny = self.reserve(flow)
        if rid is None:
            return self.load()
        return self.settle(rid, resp, ok, why, final=True)


class _BudgetLock:
    """프로세스 간(flock) + 스레드 간(threading.Lock) 잠금. with 블록 안에서만 집계 파일을 읽고 쓴다."""
    def __init__(self, b: KilnBudget):
        self.b = b; self.fh = None

    def __enter__(self):
        import fcntl
        KilnBudget._tlock.acquire()
        self.b.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(str(self.b.path) + ".lock", "a+")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        import fcntl
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN); self.fh.close()
        finally:
            KilnBudget._tlock.release()
        return False


class BudgetedKiln:
    """KilnLive 앞에 예산 관문을 둔 제공자(CLI·서버 공통).
    reserve(호출 전 영구 소비) → 실호출(예외도 정산: 실패로 기록·중단) → settle. 관문에 막히면 호출하지 않고(calls=0) 규칙 폴백 사유를 남긴다.
    한 번 실패(도구 호출 없음·기록 실패·비용 미확인·HTTP 오류·예외·스키마/의미 실패)하면 halted 로 잠가 후속 유료 호출을 멈춘다."""
    provider = "KILN_LIVE"
    model = "qwen3-32b"

    def __init__(self, budget: KilnBudget | None = None, flow_id: str = "phone_server", chat_fn=None):
        self.budget = budget or KilnBudget(); self.flow_id = flow_id; self.chat_fn = chat_fn

    def _state(self) -> dict:
        d = self.budget.load(); return {k: d.get(k) for k in ("used", "max_calls", "cost_usd", "unknown_cost_calls", "halted")}

    def structure(self, text: str) -> dict:
        rid, why = self.budget.reserve(self.flow_id)
        if rid is None:
            return {"provider": self.provider, "model": self.model, "raw": "", "calls": 0, "usage": None, "error": f"AI 미호출({why})", "source": "none", "budget": self._state()}
        resp = None
        try:
            resp = KilnLive(chat_fn=self.chat_fn, flow_id=self.flow_id).structure(text)
            good, reason = call_is_sound(resp)
        except BaseException as e:                          # noqa: BLE001 — 예외·중단도 예약을 소비하고 실패로 정산한다
            resp = {"provider": self.provider, "model": self.model, "raw": "", "calls": 1, "usage": None, "error": f"exception: {type(e).__name__}: {e}"[:200], "source": "none",
                    "cost_usd": None, "cost_status": "unknown(exception)", "raw_saved": False}
            good, reason = False, resp["error"]
        d = self.budget.settle(rid, resp, good, reason, final=False)   # 형식·기록·비용 단계. 스키마·의미 검증은 decide()→on_outcome 이 확정(그 전까지 pending 유지)
        resp["budget"] = {k: d.get(k) for k in ("used", "max_calls", "cost_usd", "unknown_cost_calls", "halted")}
        resp["reservation"] = rid
        self._last_reservation = rid
        return resp

    def on_outcome(self, ok: bool, why: str, reservation: str | None = None) -> None:
        """decide() 가 스키마·의미·기록·비용 검증을 끝낸 뒤 호출. 예약을 확정하고 실패면 후속 유료 호출을 차단한다(서버 직접 경로 포함)."""
        rid = reservation or getattr(self, "_last_reservation", None)
        if rid:
            self.budget.finalize(rid, ok, why or "")
        elif not ok:
            self.budget.halt(f"{self.flow_id}: {why}")


def call_is_sound(resp: dict) -> tuple[bool, str]:
    """형식(도구 호출 1건)·기록(원문 보존)·비용 확인이 모두 됐는지. 의미 검증은 decide() 가 한다."""
    if resp.get("error"):
        return False, str(resp["error"])
    if resp.get("source") != "tool_calls" or not resp.get("raw"):
        return False, "no valid tool call"
    if resp.get("raw_saved") is not True:
        return False, f"raw not saved: {resp.get('raw_error')}"
    if resp.get("cost_status") != "server_usage" or not isinstance(resp.get("cost_usd"), (int, float)):
        return False, f"cost unknown: {resp.get('cost_status')}"
    return True, ""
