"""휴대폰 대화 입력 → 송금 제안/질문 (9/29 첫 시제품, 규칙 기반 모의 응답).

- AI 표시: 이 모듈은 **MOCK_RULES**(규칙 기반 모의) 이며 실제 AI(Kiln qwen3-32b) 호출이 아니다. Kiln 예약 잔여 0 → 호출하지 않는다.
  실제 모델 연결 자리는 structure_with_kiln() 이며 지금은 None 을 돌려준다(호출 0).
- '영훈이한테 트론 2개' = 수취인 별칭 '영훈', 자산 TRX, 수량 2 (2 TRX). 예시일 뿐 실제 송금 명령이 아니다.
- 미등록·동명이인·주소 미확인 별칭은 질문한다. 주소를 추측하거나 만들어내지 않는다.
- 이 시제품은 Nile 테스트넷 TRX 전송만 다룬다. USDT 등 다른 자산은 질문으로 돌려보낸다.
"""
from __future__ import annotations

import json
import pathlib
import re
from decimal import Decimal, InvalidOperation

HERE = pathlib.Path(__file__).resolve().parent
CONTACTS_PATH = HERE / "phone_contacts.json"
AI_MODE = "MOCK_RULES"
AI_NOTE = "규칙 기반 모의 응답 · 실제 AI(Kiln qwen3-32b) 호출 아님(예약 잔여 0)"
MAX_TRX_PER_REQUEST = Decimal("100")           # 시제품 상한(테스트넷). 넘으면 질문.
PARTICLES = ("한테", "에게", "께", "으로", "로", "의")   # "의": 음성 인식이 "맥북 지갑의 트론 2개" 처럼 적는 경우(9/29 사장 실측) — 등록 별칭과 맞을 때만 수취인으로 본다
ASSET_TRX = re.compile(r"(트론|tron|trx|티알엑스)", re.I)
ASSET_OTHER = re.compile(r"(usdt|테더|usdd|btc|비트코인|eth|이더)", re.I)
AMT_AFTER = re.compile(r"(트론|tron|trx|티알엑스)\s*(\d+(?:\.\d+)?)\s*(개|트론|trx)?", re.I)
AMT_BEFORE = re.compile(r"(\d+(?:\.\d+)?)\s*(개|트론|trx|tron)", re.I)


def load_contacts(path: pathlib.Path | None = None) -> list[dict]:
    p = path or CONTACTS_PATH
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    return list(d.get("contacts") or [])


def _alias_candidates(token: str) -> list[str]:
    token = token.strip()
    out = [token]
    if len(token) >= 2 and token.endswith("이"):
        out.append(token[:-1])
    return out


def find_alias(contacts: list[dict], token: str) -> list[dict]:
    cands = _alias_candidates(token)
    hits = []
    for c in contacts:
        names = [c.get("alias") or ""] + list(c.get("aliases") or [])
        if any(n and n in cands for n in names):
            hits.append(c)
    return hits


def _strict_alias_token(text: str) -> str | None:
    for m in re.finditer(r"([가-힣A-Za-z0-9_\-]+?)(한테|에게|께|으로|로|의)(\s|$|,)", text):
        tok = m.group(1)
        if ASSET_TRX.search(tok) or re.fullmatch(r"\d+(\.\d+)?", tok):
            continue
        return tok
    return None


def _nospace(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def registered_alias_in_text(text: str, contacts: list[dict]) -> tuple[str | None, str]:
    """띄어쓰기·조사 차이를 무시하고 **등록된** 별칭이 문장에 있는지 본다(9/29 음성 입력 '맥북 지갑의 트론 2개').
    반환 (별칭, 사유). 서로 다른 수취인이 둘 이상 맞으면 (None, "AMBIGUOUS") — 추측하지 않고 질문한다. 별칭이 문장에 없으면 (None, "NONE")."""
    t = _nospace(text)
    tail = r"(한테|에게|께|으로|로|의|트론|tron|trx|티알엑스|\d|$)"
    found: dict[str, str] = {}
    for c in contacts:
        for n in [c.get("alias") or ""] + list(c.get("aliases") or []):
            n2 = _nospace(n)
            if len(n2) >= 2 and re.search(re.escape(n2) + tail, t, re.I):
                key = (c.get("alias") or "") + "|" + (c.get("address") or "") + "|" + (c.get("note") or "")
                found[key] = c.get("alias") or n
    if not found:
        return None, "NONE"
    if len(found) > 1:
        return None, "AMBIGUOUS"
    return next(iter(found.values())), "OK"


def _extract_alias_token(text: str, contacts: list[dict] | None = None) -> str | None:
    """1) 조사 앞 토큰(엄격). 2) 그 토큰이 등록 별칭이 아니거나 없으면, 등록 별칭을 띄어쓰기 무시로 찾는다(등록된 이름만, 추측 없음)."""
    tok = _strict_alias_token(text)
    if contacts is None:
        return tok
    if tok and find_alias(contacts, tok):
        return tok
    alias, why = registered_alias_in_text(text, contacts)
    return alias if why == "OK" else tok


def _extract_amount(text: str) -> Decimal | None:
    m = AMT_AFTER.search(text) or AMT_BEFORE.search(text)
    if not m:
        return None
    num = m.group(2) if m.re is AMT_AFTER else m.group(1)
    try:
        return Decimal(num)
    except InvalidOperation:
        return None


def parse_request(text: str, contacts: list[dict] | None = None) -> dict:
    """대화 문장 → {"kind": "proposal"|"question", ...}. 항상 ai.mode=MOCK_RULES 를 붙인다."""
    contacts = load_contacts() if contacts is None else contacts
    t = (text or "").strip()[:300]
    base = {"ai": {"mode": AI_MODE, "note": AI_NOTE}, "text": t, "understood": {}}
    if not t:
        return {**base, "kind": "question", "question": "무엇을 도와드릴까요? 예: '맥북지갑한테 트론 2개 보내줘'(예시)"}
    if ASSET_OTHER.search(t) and not ASSET_TRX.search(t):
        return {**base, "kind": "question", "question": "이 시제품은 Nile 테스트넷 TRX(트론)만 보낼 수 있습니다. TRX 로 보내시겠어요?"}
    if not ASSET_TRX.search(t):
        return {**base, "kind": "question", "question": "무엇을 보낼까요? 지금은 테스트넷 TRX(트론)만 가능합니다. 예: '…한테 트론 2개'"}
    base["understood"]["asset"] = "TRX"
    amt = _extract_amount(t)
    if amt is None:
        return {**base, "kind": "question", "question": "몇 TRX 를 보낼까요? 숫자로 알려주세요(예: 트론 2개)."}
    if amt <= 0 or amt > MAX_TRX_PER_REQUEST:
        return {**base, "kind": "question", "question": f"수량은 0 보다 크고 {MAX_TRX_PER_REQUEST} TRX 이하로만 받습니다(시제품 상한). 다시 알려주세요."}
    base["understood"]["amount_trx"] = str(amt.normalize()) if amt != amt.to_integral() else str(int(amt))
    strict = _strict_alias_token(t)
    tok = _extract_alias_token(t, contacts)
    tolerant = bool(tok) and tok != strict                      # 띄어쓰기/조사 차이를 무시해 등록 별칭으로 맞춘 경우 → 제안에 재확인 문구
    if not tok:
        _, why = registered_alias_in_text(t, contacts)
        if why == "AMBIGUOUS":
            return {**base, "kind": "question", "question": "받는 사람이 둘 이상으로 읽힙니다. 등록된 이름 하나만 말씀해 주세요(예: '맥북지갑한테').", "reason": "AMBIGUOUS_ALIAS"}
        return {**base, "kind": "question", "question": "누구에게 보낼까요? 등록된 이름(별칭)으로 알려주세요(예: '맥북지갑한테')."}
    base["understood"]["alias_input"] = tok
    if tolerant:
        base["understood"]["alias_match"] = "space_or_particle_tolerant"
        base["understood"]["alias_heard"] = strict
    hits = find_alias(contacts, tok)
    if not hits:
        return {**base, "kind": "question", "question": f"'{tok}' 은(는) 등록된 수취인이 아닙니다. 주소를 추측하지 않습니다. 먼저 수취인 목록에 등록(사장 확인)한 뒤 다시 말씀해 주세요.",
                "reason": "UNREGISTERED_ALIAS"}
    if len(hits) > 1:
        opts = [f"{h.get('alias')}({h.get('note') or '설명 없음'})" for h in hits]
        return {**base, "kind": "question", "question": f"같은 이름이 {len(hits)}명 등록되어 있습니다: {', '.join(opts)}. 어느 분인지 구분되는 이름으로 알려주세요.",
                "reason": "AMBIGUOUS_ALIAS", "options": opts}
    c = hits[0]
    if not c.get("address") or not c.get("confirmed_by_owner"):
        return {**base, "kind": "question", "question": f"'{c.get('alias')}' 의 주소가 아직 사장 확인을 받지 않았습니다(미확인 주소로는 보내지 않습니다). 등록 확인 후 다시 요청해 주세요.",
                "reason": "UNCONFIRMED_ADDRESS"}
    if (c.get("network") or "nile") != "nile":
        return {**base, "kind": "question", "question": f"'{c.get('alias')}' 은(는) {c.get('network')} 주소입니다. 이 시제품은 Nile 테스트넷만 지원합니다.", "reason": "WRONG_NETWORK"}
    amount_sun = int((amt * 1_000_000).to_integral_value())
    return {**base, "kind": "proposal",
            "proposal": {"alias": c["alias"], "address": c["address"], "network": "nile", "asset": "TRX",
                         "amount_trx": base["understood"]["amount_trx"], "amount_sun": amount_sun,
                         "address_confirmed_at": c.get("confirmed_at"), "address_note": c.get("note")},
            "next": ((f"받는 사람을 등록된 '{c['alias']}' 으로 이해했습니다(입력 문장: '{t}'). 아니면 진행하지 마세요. " if tolerant else "")
                     + "휴대폰에서 아래 내용을 확인한 뒤 [서명] 을 누르면 지갑 앱이 서명을 요청합니다. 서명 전에는 아무것도 보내지 않습니다.")}


def structure_with_kiln(text: str) -> None:
    """실제 모델 연결 자리. 9/29 Kiln 예약 잔여 0 → 호출하지 않는다(None). 호출 승인이 생기면 kiln_client 를 여기서 쓴다."""
    return None
