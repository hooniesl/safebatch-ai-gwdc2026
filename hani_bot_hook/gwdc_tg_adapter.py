#!/usr/bin/env python3
"""GWDC 텔레그램 음성/텍스트 → 요약 → 텍스트 승인 답장 → 서버 서명·방송·확정 회신.

로컬 STT 결과도 원래 Telegram 메시지 ID로 직접 접수한다. 음성·전달 메시지는 승인이 되지 않는다.
TronLink 링크 함수는 기존 시제품 호환용이며 현재 승인 요약에는 링크를 넣지 않는다.

경계(권한 확대 아님):
- dispatch_message가 관리자 개인 대화·실제 발신자를 확인한다. 되묻기는 발신 지갑·수량·수취인 한 항목씩 받는다.
- 서버 intent 생성은 봇 전용 키(guide/logs/bot_key.txt, 0600)로만 가능하다. 키는 회신·링크·로그에 넣지 않는다.
- 발신 지갑은 guide/phone_wallets.json 의 등록 단어로만 정한다. 없고 default 도 없으면 한 번 되묻는다(임의 대체 없음).
- 멱등은 메시지 키(chat_id+message_id)로만. 같은 키에 다른 내용이면 서버가 충돌로 거부한다. 회신의 지갑은 서버가 돌려준 intent 의 지갑이다.
- 이 어댑터는 서명·방송을 하지 않는다. 명령 수신은 승인이 아니다. 회신 실패는 송금과 분리해 기록하고 같은 결과 알림만 재시도한다(재송금 없음).
- 인스턴스는 모듈 단일(_adapter). 상태 파일은 fcntl 잠금 + 원자 교체. ENABLED 파일이 없으면 접수하지 않고 안내만 한다.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import pathlib
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from AI_CONTEST.gwdc_2026.guide import phone_chat as PC

GUIDE = pathlib.Path(os.environ.get("SB_GUIDE_DIR") or "/Users/djl/Desktop/hani_bot/AI_CONTEST/gwdc_2026/guide")
ENABLE_FILE = GUIDE / "logs" / "tg_adapter_enabled"          # 존재할 때만 접수(사장 실기 승인 뒤 전무가 만든다)
STATE_FILE = GUIDE / "logs" / "tg_adapter_state.json"        # 되묻기 대기·처리한 메시지 키·알림 기록
WALLETS_FILE = GUIDE / "phone_wallets.json"
SESSION_FILE = GUIDE / "logs" / "phone_session.json"
BOT_KEY_FILE = GUIDE / "logs" / "bot_key.txt"
PENDING_TTL_S = 600
WATCH_S = 20 * 60
NOTIFY_MAX_ATTEMPTS = 3
IN_PROGRESS_STALE_S = 120   # 처리 중 표시가 이보다 오래되면 중단으로 보고 재시도 허용(서버 멱등)

TRIGGER_RE = re.compile(r"(트론링크|tronlink|트론\s*링크)|((트론|trx)[^\n]{0,40}(보내|송금|전송))|((보내|송금|전송)[^\n]{0,40}(트론|trx))", re.I)
WALLET_ONLY_RE = re.compile(r"^\s*(아이폰|iphone|안드로이드|android|노트20|노트|갤럭시)\s*(으로|로|에서|지갑)?\s*[.!]?\s*$", re.I)
APPROVE_RE = re.compile(r"^\s*(승인|approve|approved|ok\s*승인|승인\s*(합니다|해|해요|할게|할께))\s*[.!]?\s*$", re.I)   # 정확한 승인 문구만. '승인하지 마' 등은 불일치
CANCEL_RE = re.compile(r"^\s*(취소|승인\s*취소|cancel)\s*[.!]?\s*$", re.I)


def matches(text: str) -> bool:
    return bool(text) and bool(TRIGGER_RE.search(text) or (
        PC.ASSET_TRX.search(text) and re.search(r"지갑|한테|에게|\d\s*(?:개|TRX)|두\s*개", text, re.I)))


def _load_json(p: pathlib.Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def enabled() -> bool:
    return ENABLE_FILE.exists() or os.environ.get("SB_TG_ENABLED") == "1"


def _wallet_mentions(text: str, wallets: dict):
    """등록 이름 전체 + 발신 조사만. '아이폰한테'를 보내는 지갑으로 쓰지 않는다."""
    found = []
    for wl in wallets.get("wallets", []):
        names = set(wl.get("words", [])) | {wl.get("label", "")}
        names = {re.sub(r"(?:으로|에서|로)$", "", n).strip() for n in names if n}
        for name in sorted(names, key=len, reverse=True):
            pat = r"\s*".join(re.escape(c) for c in re.sub(r"\s+", "", name))
            for m in re.finditer(r"(?<![\w가-힣])" + pat + r"(?:\s*지갑)?(?:\s*(?:으로|에서|로))?(?=$|[\s,!.?])", text, re.I):
                if not re.match(r"\s*(?:한테|에게|께|에|의)(?:\s|$)", text[m.end():]):
                    found.append((m.start(), m.end(), wl))
    return found


def resolve_wallet(text: str, wallets: dict | None = None) -> dict | None:
    """등록 발신자만 선택한다. 모호하거나 알 수 없는 발신자는 default로 대체하지 않는다."""
    w = wallets or _load_json(WALLETS_FILE, {"wallets": []})
    mentions = _wallet_mentions(text or "", w)
    hits = list({wl["address"]: wl for _, _, wl in mentions}.values())
    remaining = strip_wallet_words(text or "", w)
    if re.search(r"[가-힣A-Za-z]+\s*(?:으로|에서|로)(?:\s|$)", remaining):
        return None
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return None
    d = w.get("default")
    for wl in w.get("wallets", []):
        if d and wl.get("label") == d:
            return wl
    return None


def strip_wallet_words(text: str, wallets: dict | None = None) -> str:
    """발신 이름·조사 전체만 제거한다. 수취인 이름과 부분어는 보존한다."""
    w = wallets or _load_json(WALLETS_FILE, {"wallets": []}); t = text
    spans = _wallet_mentions(text, w)
    for start, end, _ in sorted(spans, reverse=True, key=lambda x: (x[0], x[1])):
        # 겹치는 등록 동의어를 여러 번 삭제하지 않는다.
        if any(a <= start and b >= end and (a, b) != (start, end) for a, b, _ in spans):
            continue
        t = t[:start] + " " * (end-start) + t[end:]
    return re.sub(r"\s+", " ", t).strip()


def tronlink_deeplink(url: str) -> str:
    """TronLink 공식 딥링크(docs.tronlink.org/mobile/deeplink, 9/29 확인). 실제 앱 열림은 실기로만 확인한다."""
    param = json.dumps({"url": url, "action": "open", "protocol": "TronLink", "version": "1.0"}, separators=(",", ":"))
    return "tronlinkoutside://pull.activity?param=" + urllib.parse.quote(param, safe="")


class Server:
    """휴대폰 서버(같은 기기) 호출. 봇 키는 파일에서만 읽고 어떤 출력에도 넣지 않는다. 오류 문구에 URL 을 넣지 않는다."""
    def __init__(self, base: str | None = None, bot_key: str | None = None, public_url: str | None = None, http=None):
        sess = _load_json(SESSION_FILE, {})
        self.base = base or f"http://127.0.0.1:{int(sess.get('port') or 8791)}"
        self.bot_key = bot_key if bot_key is not None else (BOT_KEY_FILE.read_text(encoding="utf-8").strip() if BOT_KEY_FILE.exists() else "")
        u = public_url if public_url is not None else (sess.get("url") or "")
        m = re.match(r"^(https?://[^/]+)", u or "")
        self.public_origin = m.group(1) if m else ""            # https://<ts.net> (세션 토큰 경로 제외)
        self.session_path = sess.get("url", "")
        self.http = http or self._http
        self._session_token = None
        m2 = re.search(r"/p/([A-Za-z0-9_-]+)/?$", self.session_path or "")
        self._session_token = m2.group(1) if m2 else None

    def _http(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        if not self._session_token:
            return 0, {"ok": False, "error": "session path unknown"}
        url = f"{self.base}/p/{self._session_token}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"X-SB-Bot-Key": self.bot_key}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8") or "{}")
            except (OSError, ValueError):
                return e.code, {"ok": False, "error": f"http {e.code}"}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {"ok": False, "error": f"server unreachable: {type(e).__name__}"}

    def create_intent(self, **body) -> tuple[int, dict]:
        return self.http("POST", "/api/intent/create", body)

    def intent_view_by_id(self, intent_id: str) -> tuple[int, dict]:
        return self.http("POST", "/api/intent/view", {"intent_id": intent_id})

    def intent_view(self, approval_token: str) -> tuple[int, dict]:
        """승인 링크 범위 조회(봇 키 불필요)."""
        url = f"{self.base}/a/{approval_token}/api/intent"
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {"ok": False, "error": f"http {e.code}"}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {"ok": False, "error": f"server unreachable: {type(e).__name__}"}

    def prepare_bg(self, intent_id: str) -> tuple[int, dict]:
        return self.http("POST", "/api/intent/prepare_bg", {"intent_id": intent_id})

    def approve(self, **body) -> tuple[int, dict]:
        return self.http("POST", "/api/intent/approve", body)

    def pending(self, external_user: str) -> tuple[int, dict]:
        return self.http("POST", "/api/intent/pending", {"external_user": external_user})

    def approval_url(self, approval_path: str) -> str:
        # approval_path = /a/<intent_token> → 공개 HTTPS(Tailscale Serve, tailnet 한정) 기준. 세션 토큰은 포함하지 않는다
        return (self.public_origin + approval_path) if self.public_origin else (self.base + approval_path)


def fmt_summary(v: dict) -> str:
    """승인 대기 요약(§1): 보내는 지갑 / 받는 별칭·주소 끝자리 / 금액 / Nile / 예상 수수료·최대 차감 / 승인 만료. URL·딥링크·AI 로그 없음."""
    sm = v.get("summary") or {}; rcv = sm.get("receiver") or ""
    return "\n".join([
        "🧾 승인 대기 · 이 메시지에 '승인' 이라고 답장하면 서명·전송됩니다(승인 전엔 아무것도 보내지 않음)",
        f"보내는 지갑: {sm.get('sender_label') or ''} …{str(sm.get('sender') or '')[-6:]}",
        f"받는 곳: {sm.get('receiver_alias')} (…{rcv[-6:]}) · 전체 주소: {rcv}",
        f"금액: {sm.get('amount_trx')} TRX · Nile 테스트넷",
        f"예상 최대 수수료 {int(sm.get('worst_fee_sun') or 0)/1e6:g} TRX (상한 {int(sm.get('fee_cap_sun') or 0)/1e6:g}) · 최대 차감 {int(sm.get('max_deduct_sun') or 0)/1e6:g} TRX",
        f"승인 만료: {time.strftime('%H:%M:%S', time.localtime(int(sm.get('expire_at') or 0)))} · 취소하려면 '취소'",
        f"요청 번호: {v.get('intent_id')}",
    ])


def _fmt_state(v: dict) -> str:
    st = v.get("state"); res = (v.get("result") or {}); rc = res.get("receipt") or {}; p = v.get("proposal") or {}
    if st == "FINAL":
        fee = (rc.get("fee_sun") or 0) / 1e6 if (rc.get("fee_known") or rc.get("fee_field_present")) else None
        return f"✅ 송금 완료(확정) · {p.get('amount_trx')} TRX → {p.get('alias')} · txID {str(v.get('tx_hash') or '')[:16]}… · 블록 {rc.get('block_number')} · 실제 수수료 {fee if fee is not None else '미확인'} TRX"
    if st == "ORDER_PENDING":
        if v.get("awaiting_approval"):
            return "🧾 미서명 거래 준비됨 · 요약 메시지에 '승인'이라고 답장하면 진행합니다."
        return "🧾 미서명 거래 준비됨 · TronLink 에서 내용을 확인하고 승인(서명)하면 보내집니다. 링크를 연 것만으로는 보내지지 않습니다."
    if st == "ORDER_SIGNED":
        return "✍️ 서명 접수 · 맥북이 검증·전송 뒤 같은 txID 확정을 조회 중"
    if st == "UNKNOWN":
        return f"⚠️ 결과 확인 필요 · 다시 보내지 마세요. 같은 txID {str(v.get('tx_hash') or '')[:16]}… 만 계속 조회합니다"
    if st == "FAILED":
        return f"❌ 실패/만료 · {res.get('state')} · 새로 보내려면 채팅에서 다시 요청하세요"
    if st == "ORDER_CANCELLED":
        return "↩️ 취소됨(서명 전) · 아무것도 보내지지 않았습니다"
    if st == "ORDER_EXPIRED":
        return "⌛ 미서명 거래가 만료됐습니다(전송 없음). 다시 보내려면 채팅에서 새로 요청하세요"
    return f"상태 {st}"


class _FileLock:
    def __init__(self, path: pathlib.Path):
        self.path = path; self.fd = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600); fcntl.flock(self.fd, fcntl.LOCK_EX); return self
    def __exit__(self, *a):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)
        except OSError:
            pass


class Adapter:
    def __init__(self, server: Server | None = None, send_fn=None, now_fn=time.time, wallets: dict | None = None, enabled_fn=enabled,
                 watch: bool = True, sleep_fn=time.sleep, state_file: pathlib.Path | None = None, contacts=None):
        self.server = server or Server(); self.send_fn = send_fn
        self.now_fn = now_fn; self.wallets = wallets; self.enabled_fn = enabled_fn; self.watch = watch; self.sleep_fn = sleep_fn
        self.state_file = state_file or STATE_FILE
        self._tlock = threading.RLock()
        self._watching: set[str] = set()
        self.contacts = contacts
        self.sent: list[tuple[str, str]] = []                # 검사용: (chat_id, text) — 실제 발송 성공분만

    # ── 상태 파일(잠금·원자 교체) ─────────────────────────────────────────
    def _lock(self):
        return _FileLock(self.state_file.with_suffix(".lock"))

    def _load(self) -> dict:
        st = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
        if not isinstance(st, dict) or any(k in st and not isinstance(st[k], dict) for k in ("seen", "pending", "notify", "awaiting", "tracking")):
            raise ValueError("invalid adapter state")
        st.setdefault("seen", {}); st.setdefault("pending", {}); st.setdefault("notify", {}); st.setdefault("awaiting", {})
        st.setdefault("tracking", {})
        return st

    def _save(self, st: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(f".{self.state_file.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(st, out, ensure_ascii=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(tmp, self.state_file)
        fd = os.open(self.state_file.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _send(self, chat_id, text: str) -> bool:
        if not self.send_fn:
            return False
        try:
            self.send_fn(chat_id, text)
            self.sent.append((str(chat_id), text)); return True
        except Exception:                                   # noqa: BLE001 — 회신 실패는 송금과 무관. 기록만 하고 재송금하지 않는다
            return False

    # ── 라우팅 판정(hani_final 이 호출) ───────────────────────────────────
    def should_route(self, text: str, chat_id, reply_to_id=None) -> bool:
        if matches(text):
            return True
        if APPROVE_RE.match(text or "") or CANCEL_RE.match(text or ""):
            with self._tlock, self._lock():
                state = self._load()
                aw = state.get("awaiting", {}).get(str(chat_id), {})
                tracked = {k: x for k, x in state["tracking"].items() if x.get("chat_id") == str(chat_id)}
                if CANCEL_RE.match(text or "") and state["pending"].get(str(chat_id)):
                    return True
            if reply_to_id is not None and any(str(reply_to_id) in (x.get("msg_ids") or []) for x in (*aw.values(), *tracked.values())):
                return True
            return bool(aw or tracked)                           # 이미 보낸 승인은 결과 조회로만 연결한다.
        with self._tlock, self._lock():
            st = self._load()
            pend = st["pending"].get(str(chat_id))
        if pend and int(self.now_fn()) - int(pend.get("t", 0)) <= PENDING_TTL_S:
            return bool(self._clarification_value(text, pend)) or bool(PC.input_problem(text))
        if st.get("awaiting", {}).get(str(chat_id)) and PC.input_problem(text):
            return True
        return False

    def _contacts(self):
        return self.contacts if self.contacts is not None else PC.load_contacts(GUIDE / "phone_contacts.json")

    def _clarification_value(self, text, pending):
        field = pending.get("field", "sender")
        if field == "sender":
            w = self.wallets or _load_json(WALLETS_FILE, {"wallets": []})
            wl = resolve_wallet(text, {**w, "default": None})
            return wl if wl and not strip_wallet_words(text, w).strip(" .!?") else None
        if field == "amount":
            t = PC.normalize_transfer_text(text, amount_reply=True).strip(" .!?")
            m = re.fullmatch(r"(?:(?:TRX|트론)\s*)?(\d+(?:\.\d{1,6})?)\s*(?:개|TRX|트론)?", t, re.I)
            if m and 0 < PC.Decimal(m.group(1)) <= PC.MAX_TRX_PER_REQUEST:
                return m.group(1)
        if field == "alias":
            t = re.sub(r"(?:한테|에게|께|에)$", "", text.strip(" .!?"))
            hits = PC.find_alias(self._contacts(), t)
            if len(hits) == 1 and hits[0].get("confirmed_by_owner") and hits[0].get("address"):
                return hits[0]["alias"]
        return None

    def _remember_question(self, chat_id, *, text, now, field, wallet=None, slots=None, original=None):
        with self._tlock, self._lock():
            st = self._load()
            st["pending"][str(chat_id)] = {"text": text, "original": original or text, "t": now,
                "field": field, "wallet": wallet, "slots": slots or {}}
            self._save(st)

    def _clear_context(self, chat_id):
        with self._tlock, self._lock():
            st = self._load()
            st["pending"].pop(str(chat_id), None)
            st["awaiting"].pop(str(chat_id), None)
            for key, seen in st["seen"].items():
                if key.startswith(f"tg:{chat_id}:") and str(seen.get("reply") or "").startswith("🧾 승인 대기"):
                    seen["reply"] = "이 메시지의 승인 대기는 해제되었습니다. 새 송금은 실행하지 않았습니다."
            self._save(st)

    # ── 접수 ──────────────────────────────────────────────────────────────
    def handle(self, text: str, *, chat_id, message_id, reply_to_id=None, forwarded: bool = False, is_text: bool = True) -> list[str]:
        """회신 문장 목록. 접수·되묻기·승인 소비 호출만 한다(서명·방송은 서버 signer). 같은 메시지 키 재전달은 같은 회신."""
        key = f"tg:{chat_id}:{message_id}"; now = int(self.now_fn())
        if not self.enabled_fn():
            return ["현재 텔레그램 송금 접수가 꺼져 있어 접수하지 않습니다. 이 메시지로 새 요청을 만들지 않았습니다."]
        if forwarded:
            return ["전달/인용된 메시지로는 송금을 요청하거나 승인하지 않습니다. 직접 말씀해 주세요(실행 없음)."]
        self.resume_tracking(chat_id)
        with self._tlock, self._lock():
            st = self._load()
            prev = st["seen"].get(key)
            if prev and prev.get("original") is not None and prev["original"] != text:
                return ["같은 메시지 번호의 내용이 달라 거부했습니다. 원문을 확인해 새 메시지로 알려주세요(추가 접수 없음)."]
            if prev and prev.get("reply"):
                return [prev["reply"]]
            if prev and prev.get("in_progress") and now - int(prev.get("t", 0)) <= IN_PROGRESS_STALE_S:
                return ["같은 메시지를 처리 중입니다. 잠시 뒤 결과 회신을 확인하세요(추가 접수 없음)."]
            # 없거나, 처리 중 표시가 오래 남은 경우(중단 뒤 재전달) → 서버가 같은 메시지 키로 멱등 처리하므로 다시 시도한다(추가 AI 호출은 서버 CREATING 규칙이 막는다)
            st["seen"][key] = {"t": now, "reply": None, "in_progress": True, "original": text, "is_text": is_text}
            self._save(st)
        try:
            wallets = self.wallets or _load_json(WALLETS_FILE, {"wallets": []})
            with self._tlock, self._lock():
                st = self._load(); pend = st["pending"].get(str(chat_id))
            if APPROVE_RE.match(text or "") or CANCEL_RE.match(text or ""):                 # 같은 승인 메시지 재전달은 위 seen 으로 같은 회신(추가 실행 0)
                reply = self._handle_decision(text, chat_id=chat_id, message_id=message_id, reply_to_id=reply_to_id, forwarded=forwarded, is_text=is_text, key=key, now=now)
            elif pend and now - int(pend.get("t", 0)) <= PENDING_TTL_S and self._clarification_value(text, pend):
                value = self._clarification_value(text, pend)
                if pend.get("field", "sender") == "sender":
                    reply = self._create(pend["text"], value, chat_id=chat_id, key=key, now=now,
                                         original=f"{pend.get('original') or pend['text']} / 답변: {text}", is_text=is_text)
                else:
                    slots = dict(pend.get("slots") or {}); slots[pend["field"]] = value
                    clean = ((slots.get("alias") + "한테 ") if slots.get("alias") else "") + "트론 " + (slots.get("amount") or "") + " 보내"
                    reply = self._create(clean, pend["wallet"], chat_id=chat_id, key=key, now=now,
                                         original=f"{pend.get('original') or pend['text']} / 답변: {text}", is_text=is_text)
            else:
                problem = PC.input_problem(text)
                if problem and problem[0] == "NEGATED":
                    self._clear_context(chat_id)
                    reply = [problem[1] + " 기존 승인 대기도 해제했습니다."]
                else:
                    if problem and problem[0] == "CORRECTION":
                        self._clear_context(chat_id)
                    wl = resolve_wallet(text, wallets)
                    if not wl:
                        self._remember_question(chat_id, text=text, now=now, field="sender")
                        names = " / ".join(w.get("label", "") for w in wallets.get("wallets", []))
                        reply = [f"어느 지갑에서 보낼까요? ({names}) — 지갑 이름만 답해 주세요."]
                    else:
                        reply = self._create(text, wl, chat_id=chat_id, key=key, now=now, is_text=is_text)
        except Exception as e:                              # noqa: BLE001
            logging.error("gwdc_tg_adapter.handle 실패: %s", type(e).__name__)
            reply = [f"처리 결과를 확인하지 못했습니다({type(e).__name__}). 새 송금 요청을 하지 말고 기존 요청 상태를 확인해 주세요."]
        with self._tlock, self._lock():
            st = self._load(); st["seen"][key] = {"t": now, "reply": reply[0], "in_progress": False, "original": text, "is_text": is_text}
            if len(st["seen"]) > 300:
                for k in sorted(st["seen"], key=lambda k: st["seen"][k].get("t", 0))[:-300]:
                    st["seen"].pop(k, None)
            self._save(st)
        self.resend_failed_notifications()
        return reply

    def _create(self, text: str, wl: dict, *, chat_id, key: str, now: int, original=None, is_text=True) -> list[str]:
        original = original or text
        clean = PC.normalize_transfer_text(strip_wallet_words(text, self.wallets))
        parsed = PC.parse_request(clean, self._contacts())
        if parsed.get("kind") == "question":
            reason = parsed.get("reason", "")
            alias, why = PC.registered_alias_in_text(clean, self._contacts())
            amounts = PC.transfer_amounts(clean)
            slots = {"alias": alias if why == "OK" else None, "amount": amounts[0] if len(amounts) == 1 else None}
            field = "amount" if reason in ("CORRECTION", "MULTIPLE_AMOUNTS", "MISSING_AMOUNT", "INVALID_AMOUNT", "CONTEXT_MISSING") else "alias"
            if reason in ("WRONG_NETWORK", "MIXED_ASSETS", "NEGATED", "TOO_LONG", "REPEATED_TRANSFER"):
                return ["확인이 필요합니다: " + parsed["question"] + " (주문 없음)"]
            if field == "amount":
                slots["amount"] = None
            else:
                slots["alias"] = None
            self._remember_question(chat_id, text=clean, now=now, field=field, wallet=wl, slots=slots, original=original)
            return ["확인이 필요합니다: " + parsed["question"] + " (주문 없음)"]
        code, r = self.server.create_intent(text=clean, sender=wl["address"], sender_label=wl["label"], source="telegram", external_key=key, external_user=str(chat_id))
        if code == 409 and r.get("conflict"):
            return ["같은 메시지 번호로 다른 내용이 접수돼 거부했습니다(재전달이 아닌 변경). 새 메시지로 다시 요청하세요. 아무것도 보내지지 않았습니다."]
        if code != 200 or not r.get("ok"):
            return [f"접수 실패(서버 {code}): {r.get('error') or ''} — 아무것도 보내지지 않았습니다."]
        v = r["intent"]
        if v.get("state") == "CREATING":
            return ["이전 접수가 끝나지 않은 채 중단됐습니다(결과 불명). 추가 AI 호출 없이 멈췄습니다. 새 문장으로 다시 요청하세요."]
        if v.get("state") == "CREATE_FAILED":
            return ["접수 처리에 실패했습니다(해석 단계). 아무것도 보내지지 않았습니다. 잠시 뒤 새 문장으로 다시 요청하세요."]
        if v.get("kind") == "question":
            return [f"확인이 필요합니다: {v.get('question')} (주문 없음)"]
        if v.get("kind") == "decline":
            return [f"거절: {v.get('explain')} ({v.get('reason_code')}) — 주문 없음"]
        p = v.get("proposal") or {}; ai = v.get("ai") or {}
        label = v.get("sender_label") or "(미지정)"; snd = v.get("sender") or ""
        mismatch = (snd or "").strip() != (wl["address"] or "").strip()
        if mismatch:
            return [f"같은 메시지가 이미 다른 지갑({label})으로 접수돼 있습니다. 새 메시지로 다시 요청하세요(추가 실행 없음)."]
        if v.get("awaiting_approval") and v.get("summary"):                 # 재전달: 기존 요약 다시
            return [fmt_summary(v)]
        code2, pr = self.server.prepare_bg(v["intent_id"])                 # §2 백그라운드 준비(지갑 연결 없음): 거래 고정·수수료·만료
        vi = (pr or {}).get("intent") or v
        if code2 != 200 or not pr.get("ok"):
            why = str(pr.get("error") or "")[:160]
            if pr.get("duplicate_of"):
                why = "같은 지갑·수취인·수량의 송금이 최근 15분 안에 있습니다. 정말 다시 보내려면 새 메시지로 '…다시 보내' 라고 요청하세요."
            return [f"준비 실패 — 승인 대기 아님(아무것도 보내지 않음): {why}"]
        with self._tlock, self._lock():
            st = self._load(); aw = st.setdefault("awaiting", {}).setdefault(str(chat_id), {})
            aw[vi["intent_id"]] = {"fingerprint": vi.get("fingerprint"), "expires": vi.get("approval_expires_at"), "msg_ids": [], "t": now}
            st["pending"].pop(str(chat_id), None)
            aw[vi["intent_id"]]["original"] = original
            aw[vi["intent_id"]]["normalized"] = clean
            self._save(st)
        reply = fmt_summary(vi) + f"\n{'인식 원문' if not is_text else '입력 원문'}: {original[:650]}"
        return [reply]

    # ── '승인'/'취소' 답장 처리(§3·§4): LLM 판정 없음. 인증된 개인 대화 + 해당 요약 reply 또는 대기 정확히 1건 ──
    def note_sent(self, chat_id, message_id, text: str) -> None:
        """hani_final 이 보낸 회신의 메시지 ID 를 기록(승인 답장 대조용)."""
        if not text or not text.startswith("🧾 승인 대기") or message_id is None:
            return
        match = re.search(r"^요청 번호: ([a-f0-9]{12})$", text, re.M)
        if not match:
            return
        with self._tlock, self._lock():
            st = self._load(); aw = st.get("awaiting", {}).get(str(chat_id), {})
            item = aw.get(match.group(1))
            if item is not None:
                item.setdefault("msg_ids", []).append(str(message_id)); self._save(st)

    def _handle_decision(self, text: str, *, chat_id, message_id, reply_to_id, forwarded, is_text, key: str, now: int) -> list[str]:
        approve = bool(APPROVE_RE.match(text or ""))
        if forwarded or not is_text:
            return ["전달/인용된 메시지나 음성으로는 승인하지 않습니다. 요약 메시지에 직접 '승인' 이라고 답장하세요(실행 없음)."]
        with self._tlock, self._lock():
            st = self._load(); aw = dict(st.get("awaiting", {}).get(str(chat_id), {}))
            tracked = {k: x for k, x in st["tracking"].items() if x.get("chat_id") == str(chat_id)}
        if reply_to_id is not None:
            previous = [k for k, x in tracked.items() if str(reply_to_id) in (x.get("msg_ids") or [])]
            if len(previous) == 1:
                return self._tracking_reply(chat_id, previous[0], cancel=not approve)
        live = {k: x for k, x in aw.items() if now < int(x.get("expires") or 0)}
        if not live:
            self._forget_awaiting(chat_id, list(aw.keys()))
            if tracked:
                if reply_to_id is not None:
                    return ["그 답장은 조회할 요청의 요약이 아닙니다. 해당 요약에 답장해 주세요(추가 실행 없음)."]
                if len(tracked) == 1:
                    return self._tracking_reply(chat_id, next(iter(tracked)), cancel=not approve)
                return ["이미 승인 요청한 거래가 여러 건입니다. 조회할 요청의 요약에 답장해 주세요(추가 실행 없음)."]
            if not approve:
                self._clear_context(chat_id)
                return ["송금 요청 대기를 취소했습니다. 새 송금은 실행하지 않았습니다."]
            return ["승인 대기 중인 요청이 없습니다(만료 포함). 새로 요청하세요(실행 없음)."]
        target = None
        if reply_to_id is not None:
            for k, x in live.items():
                if str(reply_to_id) in (x.get("msg_ids") or []):
                    target = k
            if target is None:
                return ["그 답장은 승인 대기 요약이 아닙니다. 요약 메시지에 답장하세요(실행 없음)."]
        elif len(live) == 1:
            target = next(iter(live))
        else:
            return [f"승인 대기가 {len(live)}건입니다. 실행할 요약 메시지에 답장으로 '승인' 하세요(자동 실행 없음)."]
        if not approve:                                            # 취소: 서버 주문 취소는 하지 않고(서명 없음) 대기만 해제 → 만료로 종결
            if not self._forget_awaiting(chat_id, [target], require_untracked=True):
                return self._tracking_reply(chat_id, target, cancel=True)
            return ["승인 대기를 취소했습니다. 아무것도 보내지 않았습니다."]
        fp = live[target].get("fingerprint") or ""
        # POST 전 내구 기록. 응답이 사라져도 이 intent에는 승인 POST를 다시 보내지 않는다.
        with self._tlock, self._lock():
            state = self._load()
            previous = state["tracking"].get(target)
            current = state.get("awaiting", {}).get(str(chat_id), {}).get(target)
            if previous is None and current is not None:
                state["tracking"][target] = {"chat_id": str(chat_id), "approval_key": key,
                    "msg_ids": list(current.get("msg_ids") or []), "t": now}
                self._save(state)
        if previous is not None:
            return self._tracking_reply(chat_id, target)
        if current is None:
            return ["승인 대기 상태가 바뀌어 새 승인을 보내지 않았습니다. 기존 요청의 결과를 확인해 주세요."]
        self._forget_awaiting(chat_id, [target])
        try:
            code, r = self.server.approve(intent_id=target, approval_key=key, fingerprint=fp, external_user=str(chat_id), summary_msg_id=(str(reply_to_id) if reply_to_id is not None else None))
        except Exception as error:                            # 응답 불명: 예외 본문에는 URL·토큰이 있을 수 있다.
            logging.error("gwdc approval response unknown: %s", type(error).__name__)
            code, r = 0, {}
        r = r if isinstance(r, dict) else {}
        if r.get("already") or r.get("idempotent"):
            return self._tracking_reply(chat_id, target)
        if code == 200 and r.get("ok") is True:
            self._start_watch(chat_id, target)
            return ["✅ 승인 접수 · 같은 요청의 처리 결과를 조회합니다. 확정 결과를 확인하면 알립니다."]
        why = str(r.get("error") or "")[:160]
        if code in (400, 403, 409) and (r.get("expired") or r.get("refused") or r.get("state") in ("POLICY_REFUSED", "SIGN_FAILED")):
            with self._tlock, self._lock():
                state = self._load(); state["tracking"].pop(target, None); self._save(state)
            return [f"승인을 실행하지 못했습니다: {why} · 새로 보내지 말고 같은 요청의 결과를 확인해 주세요."]
        self._start_watch(chat_id, target)
        return ["승인 응답을 확인하지 못했습니다. 이미 처리 중일 수 있어 다시 승인하지 않고 같은 요청의 결과만 조회합니다."]

    def _tracking_reply(self, chat_id, intent_id, *, cancel=False):
        self._start_watch(chat_id, intent_id)
        if cancel:
            return ["승인 요청이 이미 전송되어 취소됐다고 확인할 수 없습니다. 추가 승인 없이 같은 요청의 결과만 조회합니다."]
        return ["새로 승인할 요청은 없습니다. 이미 처리한 승인 요청의 결과만 확인합니다(추가 승인·서명·전송 없음)."]

    def _start_watch(self, chat_id, intent_id):
        if not self.watch:
            return False
        with self._tlock, self._lock():
            state = self._load()
            tracking = state["tracking"].get(intent_id)
            if not tracking or tracking.get("chat_id") != str(chat_id) or intent_id in self._watching:
                return False
            notices = state["notify"].get(intent_id, {}).get("states", {})
            if any((notices.get(name) or {}).get("ok") for name in ("FINAL", "FAILED", "ORDER_CANCELLED", "ORDER_EXPIRED")):
                return False
            self._watching.add(intent_id)

        def run():
            try:
                self._watch(chat_id, intent_id, "")
            finally:
                with self._tlock:
                    self._watching.discard(intent_id)

        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            with self._tlock:
                self._watching.discard(intent_id)
            raise
        return True

    def resume_tracking(self, chat_id):
        """저장된 조회만 재개한다. 재시작 후 다음 관련 메시지에서 호출하며 승인 POST는 하지 않는다."""
        if not self.watch:
            return 0
        with self._tlock, self._lock():
            targets = [key for key, item in self._load()["tracking"].items() if item.get("chat_id") == str(chat_id)]
        return sum(bool(self._start_watch(chat_id, target)) for target in targets)

    def _forget_awaiting(self, chat_id, ids, *, require_untracked=False) -> bool:
        with self._tlock, self._lock():
            st = self._load(); aw = st.get("awaiting", {}).get(str(chat_id), {})
            if require_untracked and any(item in st["tracking"] for item in ids):
                return False
            for k in ids:
                aw.pop(k, None)
            for key, seen in st["seen"].items():
                reply = str(seen.get("reply") or "")
                if key.startswith(f"tg:{chat_id}:") and any(f"요청 번호: {item}\n" in reply + "\n" for item in ids):
                    seen["reply"] = "이 메시지의 승인 대기는 종료되었습니다. 기존 요청의 결과를 확인해 주세요."
            self._save(st)
        return True

    # ── 상태 반영(조회만) ─────────────────────────────────────────────────
    def _notify_once(self, intent_id: str, chat_id, state: str, text: str, token: str) -> bool:
        """watcher·재시도가 공유하는 내구 발송 예약. 미완료 claim은 만료·재시작으로 재사용하지 않는다."""
        claim = secrets.token_hex(12)
        with self._tlock, self._lock():
            st = self._load(); n = st["notify"].setdefault(intent_id, {"chat_id": str(chat_id), "token": token, "states": {}})
            cur = dict(n["states"].get(state) or {})
            if (n.get("chat_id") != str(chat_id) or cur.get("ok") or cur.get("claim")
                    or int(cur.get("attempts", 0)) >= NOTIFY_MAX_ATTEMPTS):
                return False
            cur.update({"ok": False, "claim": claim, "t": int(self.now_fn()),
                        "attempts": int(cur.get("attempts", 0)) + 1})
            n["states"][state] = cur
            self._save(st)

        # 네트워크 발송은 상태 락 밖에서 한다. 여기서 중단되면 claim을 남겨 중복 발송을 막는다.
        ok = self._send(chat_id, text)
        with self._tlock, self._lock():
            st = self._load(); cur = st["notify"][intent_id]["states"][state]
            if cur.get("claim") != claim:
                raise RuntimeError("notification delivery claim changed")
            cur.update({"ok": bool(ok), "t": int(self.now_fn())})
            cur.pop("claim", None)                          # 반환된 실패만 기존 최대 3회 정책으로 재시도 가능.
            self._save(st)
        return ok

    def _watch(self, chat_id, intent_id: str, token: str, max_polls: int | None = None) -> list[str]:
        """intent 상태 변화를 같은 대화에 알린다. 회신 실패는 기록만(재시도 ≤ NOTIFY_MAX_ATTEMPTS), 송금에 영향 없음."""
        t0 = self.now_fn(); n = 0; out = []; done_states = set()
        while self.now_fn() - t0 < WATCH_S and (max_polls is None or n < max_polls):
            self.sleep_fn(10); n += 1
            try:
                code, r = (self.server.intent_view(token) if token else self.server.intent_view_by_id(intent_id))
            except Exception as error:
                logging.error("gwdc result lookup unavailable: %s", type(error).__name__)
                continue
            if code != 200 or not isinstance(r, dict) or not r.get("ok") or not isinstance(r.get("intent"), dict):
                continue
            v = r["intent"]; st = v.get("state")
            if st in ("PROPOSED", None) or st in done_states:
                continue
            with self._tlock, self._lock():
                saved = self._load()
                tracking = saved["tracking"].get(intent_id)
                notice = saved["notify"].get(intent_id, {}).get("states", {}).get(st) or {}
                attempts = int(notice.get("attempts", 0))
            if notice.get("ok"):
                done_states.add(st)
                if st in ("FINAL", "FAILED", "ORDER_CANCELLED", "ORDER_EXPIRED"):
                    break
                continue
            if notice.get("claim") or attempts >= NOTIFY_MAX_ATTEMPTS:
                done_states.add(st)
                if st in ("FINAL", "FAILED", "ORDER_CANCELLED", "ORDER_EXPIRED"):
                    break
                continue
            msg = ("승인 요청의 처리 상태를 확인 중입니다. 다시 승인하거나 새로 보내지 마세요."
                   if tracking and st == "ORDER_PENDING" else _fmt_state(v))
            ok = self._notify_once(intent_id, chat_id, st, msg, token)
            if ok:
                done_states.add(st); out.append(msg)
            if st in ("FINAL", "FAILED", "ORDER_CANCELLED", "ORDER_EXPIRED") and ok:
                break
        return out

    def resend_failed_notifications(self, max_items: int = 5) -> int:
        """전달 실패한 종결 알림만 같은 내용으로 재전송(재송금·재조회 부작용 없음). 다음 메시지 처리 때 호출된다."""
        with self._tlock, self._lock():
            st = self._load(); items = list(st["notify"].items())
        sent = 0
        for intent_id, n in items[-50:]:
            for state, info in (n.get("states") or {}).items():
                if info.get("ok") or info.get("claim") or int(info.get("attempts", 0)) >= NOTIFY_MAX_ATTEMPTS or state not in ("FINAL", "FAILED", "UNKNOWN", "ORDER_EXPIRED"):
                    continue
                try:
                    code, r = (self.server.intent_view(n.get("token")) if n.get("token") else self.server.intent_view_by_id(intent_id))
                except Exception as error:
                    logging.error("gwdc notification lookup unavailable: %s", type(error).__name__)
                    continue
                if code != 200 or not isinstance(r, dict) or not r.get("ok") or not isinstance(r.get("intent"), dict) or r["intent"].get("state") != state:
                    continue
                ok = self._notify_once(intent_id, n.get("chat_id"), state, _fmt_state(r["intent"]), n.get("token", ""))
                sent += int(ok)
                if sent >= max_items:
                    return sent
        return sent


_adapter: Adapter | None = None
_adapter_lock = threading.Lock()


def _get(send_fn=None) -> Adapter:
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            _adapter = Adapter(send_fn=send_fn)
        elif send_fn is not None and _adapter.send_fn is None:
            _adapter.send_fn = send_fn
        return _adapter


def should_route(text: str, chat_id, reply_to_id=None) -> bool:
    """hani_final.py 진입 판정: 송금/트론링크 문장, 되묻기 대기 중인 지갑 단독 답, 또는 승인 대기 중인 '승인'/'취소' 답장."""
    # 상태 오류를 False로 삼키면 송금/승인 문장이 일반 AI 경로로 흘러간다.
    return _get().should_route(text, chat_id, reply_to_id)


def route(text: str, *, chat_id, message_id, send_fn=None, reply_to_id=None, forwarded: bool = False, is_text: bool = True) -> list[str]:
    """hani_final.py 진입점. 단일 인스턴스. 실패해도 예외를 밖으로 내지 않는다."""
    try:
        return _get(send_fn).handle(text, chat_id=chat_id, message_id=message_id, reply_to_id=reply_to_id, forwarded=forwarded, is_text=is_text)
    except Exception as e:                                  # noqa: BLE001
        logging.error("gwdc_tg_adapter.route 실패: %s", type(e).__name__)
        return [f"송금 처리 결과를 확인하지 못했습니다({type(e).__name__}). 새 요청을 보내지 말고 기존 요청 상태를 확인해 주세요."]


def dispatch_message(message, bot, admin_id, *, text=None, is_text=None) -> bool:
    """텍스트·로컬 음성이 공유하는 Telegram 경계. 권한/원본 ID를 확인해 직접 라우팅한다."""
    chat = getattr(message, "chat", None)
    sender = getattr(message, "from_user", None)
    if (not admin_id or getattr(chat, "type", None) != "private"
            or str(getattr(chat, "id", "")) != str(admin_id)
            or str(getattr(sender, "id", "")) != str(admin_id)
            or getattr(message, "message_id", None) is None):
        return False
    text = text if text is not None else getattr(message, "text", None)
    if not text or not text.strip():
        return False
    reply_to_id = getattr(getattr(message, "reply_to_message", None), "message_id", None)
    if not should_route(text, chat.id, reply_to_id):
        return False
    forwarded = any(getattr(message, attr, None) for attr in ("forward_date", "forward_origin", "forward_from", "forward_sender_name", "external_reply"))
    # content_type이 음성이면 호출자가 is_text=True를 주어도 승인이 되지 않는다.
    is_text = getattr(message, "content_type", None) == "text" and is_text is not False
    for reply in route(text, chat_id=chat.id, message_id=message.message_id, send_fn=bot.send_message,
                       reply_to_id=reply_to_id, forwarded=bool(forwarded), is_text=is_text):
        sent = bot.reply_to(message, reply)
        note_sent(chat.id, getattr(sent, "message_id", None), reply)
    return True


def note_sent(chat_id, message_id, text: str) -> None:
    try:
        _get().note_sent(chat_id, message_id, text)
    except Exception:                                       # noqa: BLE001
        logging.exception("gwdc_tg_adapter.note_sent 실패")


def handle(text: str, *, chat_id, message_id, send_fn=None) -> list[str]:   # 하위 호환
    return route(text, chat_id=chat_id, message_id=message_id, send_fn=send_fn)
