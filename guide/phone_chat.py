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
PARTICLES = ("한테", "에게", "께", "으로", "로", "의", "에")   # "의": 음성 인식이 "맥북 지갑의 트론 2개" 처럼 적는 경우(9/29 사장 실측) — 등록 별칭과 맞을 때만 수취인으로 본다
ASSET_TRX = re.compile(r"(트론|tron|trx|티알엑스)", re.I)
ASSET_OTHER = re.compile(r"(usdt|테더|usdd|btc|비트코인|eth|이더)", re.I)
AMT_AFTER = re.compile(r"(트론|tron|trx|티알엑스)\s*(\d+(?:\.\d+)?)\s*(개|트론|trx)?", re.I)
AMT_BEFORE = re.compile(r"(\d+(?:\.\d+)?)\s*(개|트론|trx|tron)", re.I)

_KOREAN_COUNTS = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3,
                  "네": 4, "넷": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8,
                  "아홉": 9, "열": 10}
_COUNT_RE = re.compile(r"(?<![가-힣A-Za-z0-9])(" + "|".join(_KOREAN_COUNTS) + r")\s*(개|TRX|트론)(?=$|\s|[.,!?]|를|만|씩|은|는)", re.I)


def normalize_transfer_text(text: str, *, amount_reply: bool = False) -> str:
    """명시적 TRX 문맥에서 단위가 붙은 한글 정수만 변환한다. 원문은 호출자가 보존한다."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if ASSET_TRX.search(t) or amount_reply:
        def replace_count(match):
            if re.search(r"(?:스물|서른|마흔|쉰|예순|일흔|여든|아흔|열|십|백|천)\s*$", t[:match.start()]):
                return match.group(0)
            return f"{_KOREAN_COUNTS[match.group(1)]}{match.group(2)}"
        t = _COUNT_RE.sub(replace_count, t)
    return t


def input_problem(text: str) -> tuple[str, str] | None:
    """모델 호출 전에 부정·정정·생략·체인 변경을 차단한다. 승인 판정과는 별개다."""
    if len(text) > 300:
        return "TOO_LONG", "송금할 내용만 300자 이내로 말씀해 주세요."
    if re.search(r"(?:보내|승인)\s*하지\s*(?:마|말|않)|보내\s*지\s*(?:마|말|않)|(?:송금|전송)\s*하?지\s*(?:마|말|않)|안\s*(?:보내|송금|전송|돼|되)|취소|금지|보류|멈춰|중지|do\s*not|don't|cancel", text, re.I):
        return "NEGATED", "보내지 않는 지시로 이해했습니다. 송금 요청을 만들지 않았습니다."
    if re.search(r"말고|아니라|아니[ ,]|정정|수정|대신", text):
        return "CORRECTION", "최종적으로 보낼 수량을 하나만 알려주세요(예: 3 TRX)."
    if re.search(r"아까|지난번|이전처럼|똑같이|잔액\s*전부|전액|모두|남은", text):
        return "CONTEXT_MISSING", "보낼 수량을 정확히 알려주세요(예: 2 TRX)."
    if re.search(r"씩|(?:한|두|세|네|\d+)\s*번|나눠|각각|매일|매주|반복", text):
        return "REPEATED_TRANSFER", "한 번에 한 건만 보낼 수 있습니다. 한 건으로 보낼 수량을 알려주세요."
    if re.search(r"마이너스|플러스|반\s*개|(?:개|TRX|트론)\s*반|추가|\b더\s*보내", text, re.I):
        return "INVALID_AMOUNT", "최종 수량을 양수로 정확히 알려주세요(예: 2.5 TRX)."
    if re.search(r"메인\s*넷|mainnet|shasta|샤스타|이더리움|ethereum", text, re.I):
        return "WRONG_NETWORK", "Nile 테스트넷으로 보낼 요청인지 확인해 주세요."
    if ASSET_TRX.search(text) and ASSET_OTHER.search(text):
        return "MIXED_ASSETS", "보낼 자산을 하나만 알려주세요. 이 창구는 Nile TRX만 지원합니다."
    return None


def transfer_amounts(text: str) -> list[str]:
    """겹치는 앞/뒤 단위 표기는 한 수량으로 센다. 예산·수수료 조건은 별도다."""
    spans = []
    for rx, group in ((AMT_AFTER, 2), (AMT_BEFORE, 1)):
        for m in rx.finditer(text):
            start, end = m.span(group)
            if re.match(r"\s*(?:분|초|시간|일)(?:\s|$|[.,])", text[end:]):
                continue
            if re.search(r"(?:예산|수수료|상한)\s*(?:은|는|이|가|최대)?\s*$", text[max(0, m.start()-12):m.start()]):
                continue
            if not any(start < b and end > a for a, b, _ in spans):
                spans.append((start, end, m.group(group)))
    return [num for _, _, num in sorted(spans)]


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
    cands = {_nospace(x) for x in _alias_candidates(token)}
    hits = []
    for c in contacts:
        names = [c.get("alias") or ""] + list(c.get("aliases") or [])
        if any(n and _nospace(n) in cands for n in names):
            hits.append(c)
    return hits


def _strict_alias_token(text: str) -> str | None:
    for m in re.finditer(r"([가-힣A-Za-z0-9_\-]+?)(한테|에게|께|으로|로|의|에)(\s|$|,)", text):
        tok = m.group(1)
        if ASSET_TRX.search(tok) or re.fullmatch(r"\d+(\.\d+)?", tok):
            continue
        return tok
    return None


def _nospace(s: str) -> str:
    """띄어쓰기 제거 + 영문 소문자(음성 인식 'MacBook 지갑' ↔ 등록 'MacBook지갑')."""
    return re.sub(r"\s+", "", s or "").lower()


_DROPPABLE = re.compile(r"^(\d+(?:\.\d+)?(개|트론|trx)?|트론|tron|trx|티알엑스|[가-힣A-Za-z0-9]+(을|를|은|는|이|가|도|만|에서|부터|까지))$", re.I)
_JOINER = re.compile(r"(이랑|랑|와|과|하고|및)$")
_AMOUNT_ASSET = re.compile(r"^(\d+(?:\.\d+)?(개|트론|trx)?|트론|tron|trx|티알엑스)$", re.I)   # 수취인 조사 바로 앞 단어 중 수량·자산만 무시(이름이 '이'로 끝나는 경우를 목적어로 오인하지 않음)


def _alias_map(contacts: list[dict]) -> dict[str, str]:
    """띄어쓰기 제거한 등록 이름 → 대표 별칭. 등록된 이름만(추측 없음)."""
    m: dict[str, str] = {}
    for c in contacts:
        for n in [c.get("alias") or ""] + list(c.get("aliases") or []):
            n2 = _nospace(n)
            if len(n2) >= 2:
                m[n2] = c.get("alias") or n
    return m


def _recipient_segments(text: str) -> list[str]:
    """수취인 조사(한테/에게/께/으로/로/의) 앞의 단어 묶음과, 조사가 없을 때 자산 단어(트론…) 앞 묶음."""
    segs = [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([가-힣A-Za-z0-9_\- ]+?)(한테|에게|께|으로|로|의|에)(\s|$|,)", text)]
    if not segs:
        m = re.search(r"^(.*?)\s*(트론|tron|trx|티알엑스)", text, re.I)
        if m and m.group(1).strip():
            segs.append((m.group(1).strip(), ""))
    return [x for x in segs if x[0]]


def registered_alias_in_text(text: str, contacts: list[dict]) -> tuple[str | None, str]:
    """등록 별칭을 띄어쓰기 차이만 무시해 찾는다(9/29 음성 입력 '맥북 지갑의 트론 2개'). 이름의 **왼쪽 경계**를 본다:
    조사 앞 단어 묶음의 뒤쪽 k 단어를 이어 붙인 것이 등록 이름과 **정확히** 같아야 하고, 남는 앞 단어는 수량·자산·목적어(…을/를) 뿐이어야 한다.
    '다른맥북지갑' 처럼 등록 이름을 뒤에 품은 다른 말은 승격하지 않는다(NONE). 앞에 설명할 수 없는 말이 남으면 UNCLEAR.
    서로 다른 등록 수취인이 둘 이상 나오면 AMBIGUOUS. 반환 (대표 별칭 또는 None, OK|NONE|UNCLEAR|AMBIGUOUS)."""
    amap = _alias_map(contacts)
    found: list[str] = []
    unclear = False
    unknown_recipients = 0                                        # 명시적 수취인 조사(한테/에게/께) 앞에 등록되지 않은 이름이 있는 묶음 수
    for seg, particle in _recipient_segments(text):
        words = seg.split()
        matched_here = None
        for k in range(1, len(words) + 1):
            tail_ns = _nospace("".join(words[-k:]))
            if tail_ns in amap:
                matched_here = (amap[tail_ns], words[:-k]); break
        if not matched_here:
            if particle in ("한테", "에게", "께") and any(not _AMOUNT_ASSET.match(w) for w in words):
                unknown_recipients += 1
            continue
        alias, lead = matched_here
        # 앞에 남은 단어: 수량·자산·목적어는 무시, 다른 등록 이름(+이랑/와/과…)은 추가 수취인, 그 밖은 불명확
        for w in lead:
            if _DROPPABLE.match(w):
                continue
            w2 = _nospace(_JOINER.sub("", w))
            if w2 in amap:
                if amap[w2] not in found: found.append(amap[w2])
                continue
            unclear = True
        if alias not in found:
            found.append(alias)
    if len(found) > 1 or (found and unknown_recipients):          # 등록 수취인 + 다른 수취인(미등록 포함)이 함께 지정되면 첫 수취인으로 제안하지 않는다(VP 9/29 A)
        return None, "AMBIGUOUS"
    if unclear:
        return None, "UNCLEAR"
    if not found:
        return None, "NONE"
    return found[0], "OK"


def _extract_alias_token(text: str, contacts: list[dict] | None = None) -> str | None:
    """1) 수취인이 둘 이상/불명확하면 None(질문 경로). 2) 조사 앞 토큰이 등록 별칭이면 그대로. 3) 아니면 띄어쓰기 관용 매칭(등록 이름만)."""
    tok = _strict_alias_token(text)
    if contacts is None:
        return tok
    alias, why = registered_alias_in_text(text, contacts)
    if why in ("AMBIGUOUS", "UNCLEAR"):
        return None
    if tok and find_alias(contacts, tok):
        return tok
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
    original = (text or "").strip()
    t = normalize_transfer_text(original)
    base = {"ai": {"mode": AI_MODE, "note": AI_NOTE}, "text": original, "normalized_text": t, "understood": {}}
    problem = input_problem(original)
    if problem:
        return {**base, "kind": "question", "reason": problem[0], "question": problem[1]}
    if not t:
        return {**base, "kind": "question", "question": "무엇을 도와드릴까요? 예: '맥북지갑한테 트론 2개 보내줘'(예시)"}
    if ASSET_OTHER.search(t) and not ASSET_TRX.search(t):
        return {**base, "kind": "question", "question": "이 시제품은 Nile 테스트넷 TRX(트론)만 보낼 수 있습니다. TRX 로 보내시겠어요?"}
    if not ASSET_TRX.search(t):
        return {**base, "kind": "question", "question": "무엇을 보낼까요? 지금은 테스트넷 TRX(트론)만 가능합니다. 예: '…한테 트론 2개'"}
    base["understood"]["asset"] = "TRX"
    _scan_alias, scan_why = registered_alias_in_text(t, contacts)
    if scan_why == "AMBIGUOUS":
        return {**base, "kind": "question", "question": "받는 사람이 둘 이상으로 읽힙니다. 등록된 이름 하나만 말씀해 주세요.", "reason": "AMBIGUOUS_ALIAS"}
    amounts = transfer_amounts(t)
    if len(amounts) > 1:
        return {**base, "kind": "question", "reason": "MULTIPLE_AMOUNTS", "question": "수량이 여러 개입니다. 최종 수량 하나만 알려주세요(예: 2 TRX)."}
    # 부호·쉼표·지수·과도한 소수 자릿수를 일부만 읽어 수량을 바꾸지 않는다.
    if re.search(r"[-+−]\s*\d|\d[,.]\d+[,.]\d|\d[,/]\d|\d[eE][+-]?\d|\d+\.\d{7,}|\d+\s*점", t):
        return {**base, "kind": "question", "reason": "INVALID_AMOUNT", "question": "수량은 양수, 소수점 여섯 자리 이내의 TRX로 알려주세요."
                }
    amt = Decimal(amounts[0]) if amounts else None
    if amt is None:
        return {**base, "kind": "question", "reason": "MISSING_AMOUNT", "question": "몇 TRX 를 보낼까요? 숫자로 알려주세요(예: 트론 2개)."}
    if amt <= 0 or amt > MAX_TRX_PER_REQUEST:
        return {**base, "kind": "question", "reason": "INVALID_AMOUNT", "question": f"수량은 0 보다 크고 {MAX_TRX_PER_REQUEST} TRX 이하로만 받습니다(시제품 상한). 다시 알려주세요."}
    base["understood"]["amount_trx"] = str(amt.normalize()) if amt != amt.to_integral() else str(int(amt))
    # 모든 제안 경로에서 수취인 모호성을 먼저 판정한다(VP 9/29 A): 서로 다른 등록 수취인 둘 이상 → 질문, 이름 경계 불명확 → 질문
    if scan_why == "UNCLEAR":
        return {**base, "kind": "question", "question": "받는 사람 이름이 어디까지인지 분명하지 않습니다. 등록된 이름 그대로 말씀해 주세요(예: '맥북지갑한테').", "reason": "UNCLEAR_ALIAS"}
    strict = _strict_alias_token(t)
    tok = _extract_alias_token(t, contacts)
    tolerant = bool(tok) and _nospace(strict or "") != _nospace(tok)   # 띄어쓰기/조사 차이를 무시해 등록 별칭으로 맞춘 경우 → 재확인 문구
    if not tok:
        return {**base, "kind": "question", "reason": "MISSING_ALIAS", "question": "누구에게 보낼까요? 등록된 이름(별칭)으로 알려주세요(예: '맥북지갑한테')."}
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
    confirm_note = (f"받는 사람을 등록된 '{c['alias']}' 으로 이해했습니다(입력 문장: '{t}'). 아니면 진행하지 마세요." if tolerant else None)
    if confirm_note:
        base["confirm_note"] = confirm_note                      # 화면(정상·폴백·적응 모두)에 그대로 표시된다(phone.html, textContent)
    return {**base, "kind": "proposal",
            "proposal": {"alias": c["alias"], "address": c["address"], "network": "nile", "asset": "TRX",
                         "amount_trx": base["understood"]["amount_trx"], "amount_sun": amount_sun,
                         "address_confirmed_at": c.get("confirmed_at"), "address_note": c.get("note")},
            "next": ((confirm_note + " ") if confirm_note else "") + "휴대폰에서 아래 내용을 확인한 뒤 [서명] 을 누르면 지갑 앱이 서명을 요청합니다. 서명 전에는 아무것도 보내지 않습니다."}


def structure_with_kiln(text: str) -> None:
    """실제 모델 연결 자리. 9/29 Kiln 예약 잔여 0 → 호출하지 않는다(None). 호출 승인이 생기면 kiln_client 를 여기서 쓴다."""
    return None
