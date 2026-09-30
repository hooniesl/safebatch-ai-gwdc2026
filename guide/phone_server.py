"""휴대폰용 서버(9/29 첫 시제품): 휴대폰 TronLink 앱의 DApp 브라우저에서 접속 → 대화 → 확인 → 휴대폰 서명 → 맥북 검증·방송 → 결과.

실행: python3 guide/phone_server.py --host 0.0.0.0 --port 8791 --advertise 192.168.0.251
- 기본은 127.0.0.1 만 듣는다. --host 0.0.0.0 은 같은 Wi-Fi(LAN) 시험용이며 인터넷 공개가 아니다(포트포워딩·터널 없음).
- 모든 경로는 실행마다 새로 만드는 경로 토큰 /p/<token>/ 뒤에만 있다. 토큰 없는 요청은 404. 토큰·URL 은 guide/logs/phone_session.json(0600) 에만 쓴다.
- 요청 경계: Host 허용 목록(advertise/127.0.0.1/localhost:포트), POST 는 JSON + 64KB 이하, Origin 이 있으면 허용 호스트와 동일해야 한다.
- 기존 안내 서버(8765, PID 유지)와 별개 프로세스. 포트 8765 는 다른 프로세스가 *:8765 로 듣고 있어 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
from order_store import OrderStore  # noqa: E402
from phone_flow import PhoneFlow  # noqa: E402
from phone_intent import IntentStore, IntentConflict  # noqa: E402
import phone_signer as SG  # noqa: E402
import phone_chat as PC  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402

MAX_BODY = 64 * 1024
PAGE_VERSION = "2026-09-29.tg2"      # 링크 페이지/서버 버전(증거 표기용). 변경 시 올린다
BOT_KEY_HEADER = "X-SB-Bot-Key"
PENDING = HERE / "pending"
LOGS = HERE / "logs"


def make_handler(flow: PhoneFlow, token: str, allowed_hosts: set[str], log_path: pathlib.Path | None, intents_ext: IntentStore | None = None, bot_key: str | None = None, allowed_senders: set[str] | None = None, signer=None, policy=None, ledger=None):
    signer = signer or SG.NoSigner()
    policy = policy or SG.SignerPolicy(allowed_receivers=set())
    if getattr(signer, "kind", "none") == "local" and (not policy.is_one_shot_trial() or signer.address not in (policy.allowed_senders or set())):
        raise ValueError("local signer requires an explicit one-shot trial policy bound to its address")
    RAW_PAGE = (HERE / "phone.html").read_text(encoding="utf-8").replace("__SB_VERSION__", PAGE_VERSION)
    PAGE = RAW_PAGE.replace("__SB_BASE__", f"/p/{token}")
    prefix = f"/p/{token}"
    ext = intents_ext or IntentStore(LOGS / "phone_ext_intents", now_fn=flow.now_fn)
    MASK_RE = __import__("re").compile(r"(/)[A-Za-z0-9_-]{16,64}(?=/|\s|\?|$)|(token=)[A-Za-z0-9_-]+")     # 경로 안 토큰 모양 세그먼트(승인 토큰 등)와 token= 값은 전부 마스킹(리뷰 9/29 22:3x)
    import secrets as _secrets
    def bot_key_ok(h) -> bool:                     # 봇 전용 인증(어댑터만 아는 파일 키). 페이지·링크에는 절대 넣지 않는다. loopback 출발지만으로 판정하지 않는다
        return bool(bot_key) and _secrets.compare_digest(str(h.headers.get(BOT_KEY_HEADER) or ""), bot_key)

    class H(BaseHTTPRequestHandler):
        server_version = "SafeBatchPhone/0.1"

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers(); self.wfile.write(body)

        def _j(self, code: int, obj: dict):
            return self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _host_ok(self) -> str:
            host = (self.headers.get("Host") or "")
            if host not in allowed_hosts:
                return f"host not allowed: {host}"
            origin = self.headers.get("Origin")
            if origin is not None and origin not in ({f"http://{h}" for h in allowed_hosts} | {f"https://{h}" for h in allowed_hosts} | {f"https://{h[:-4]}" for h in allowed_hosts if h.endswith(":443")}):
                return f"origin not allowed: {origin}"
            return ""

        def _route(self) -> str | None:
            u = urlparse(self.path)
            self._scope = "session"; self._intent = None
            if u.path.startswith(prefix + "/") or u.path == prefix:
                return u.path[len(prefix):] or "/"
            m = __import__("re").match(r"^/a/([A-Za-z0-9_-]{16,64})(/.*)?$", u.path)     # 승인 링크: 그 intent 범위만(공통 세션 토큰 미배포)
            if m:
                rec = ext.by_token(m.group(1))
                if not rec:
                    return "__expired__"
                self._scope = "approval"; self._intent = rec; self._abase = f"/a/{m.group(1)}"
                return m.group(2) or "/"
            return None

        def do_GET(self):
            why = self._host_ok()
            if why:
                return self._j(403, {"ok": False, "error": why})
            r = self._route()
            if r is None:
                return self._j(404, {"error": "not found"})
            if r == "__expired__":
                return self._send(404, RAW_PAGE.replace("__SB_BASE__", "/a/expired").replace("__SB_INTENT__", "expired").encode("utf-8"), "text/html; charset=utf-8")
            if self._scope == "approval":
                if r in ("/", ""):
                    return self._send(200, RAW_PAGE.replace("__SB_BASE__", self._abase).replace("__SB_INTENT__", self._intent["token"]).encode("utf-8"), "text/html; charset=utf-8")
                if r == "/api/intent":
                    return self._j(200, {"ok": True, "intent": ext.view(self._intent, flow)})
                if r == "/api/order/status":
                    q = parse_qs(urlparse(self.path).query); pid = (q.get("payment_id") or [""])[0]
                    if not ext.order_allowed(self._intent, pid):
                        return self._j(403, {"ok": False, "error": "이 승인 링크의 주문이 아닙니다"})
                    return self._j(200, flow.status(pid))
                if r == "/api/orders":                      # 링크 범위: 그 요청의 발신 지갑 기록만(다른 지갑 조회 불가)
                    q = parse_qs(urlparse(self.path).query); snd = (q.get("sender") or [""])[0]
                    if not self._intent.get("sender") or flow._norm_sender(snd) != flow._norm_sender(self._intent["sender"]):
                        return self._j(200, {"orders": []})
                    return self._j(200, {"orders": flow.list_orders(snd)})
                if r not in ("/phone_client.js", "/api/health", "/api/contacts"):
                    return self._j(404, {"error": "not found"})
            if r in ("/", ""):
                return self._send(200, PAGE.replace("__SB_INTENT__", "").encode("utf-8"), "text/html; charset=utf-8")
            if r == "/phone_client.js":
                return self._send(200, (HERE / "phone_client.js").read_bytes(), "application/javascript; charset=utf-8")
            if r == "/api/health":
                return self._j(200, {"ok": True, "ai_mode": PC.AI_MODE, "ai_provider": getattr(flow.ai_provider, "provider", None), "network": "nile", "server_time": int(time.time())})
            if r == "/api/contacts":
                cs = [{"alias": c.get("alias"), "aliases": c.get("aliases") or [], "network": c.get("network"), "confirmed": bool(c.get("confirmed_by_owner") and c.get("address")),
                       "address": c.get("address") if c.get("confirmed_by_owner") else None, "note": c.get("note")} for c in (flow.contacts if flow.contacts is not None else PC.load_contacts())]
                return self._j(200, {"contacts": cs})
            if r == "/api/order/status":
                q = parse_qs(urlparse(self.path).query)
                pid = (q.get("payment_id") or [""])[0]
                return self._j(200, flow.status(pid))
            if r == "/api/orders":                          # 연결된 지갑의 기존 주문 결과 조회(읽기 전용, 토큰 경로 안에서만, 전체 공개 아님)
                q = parse_qs(urlparse(self.path).query)
                return self._j(200, {"orders": flow.list_orders((q.get("sender") or [""])[0])})
            return self._j(404, {"error": "not found"})

        def do_POST(self):
            why = self._host_ok()
            if why:
                return self._j(403, {"ok": False, "error": why})
            r = self._route()
            if r is None:
                return self._j(404, {"error": "not found"})
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._j(403, {"ok": False, "error": "content-type must be application/json"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._j(400, {"ok": False, "error": "bad content-length"})
            if n <= 0 or n > MAX_BODY:
                return self._j(403, {"ok": False, "error": "body size"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._j(400, {"ok": False, "error": "bad json"})
            if not isinstance(req, dict):
                return self._j(400, {"ok": False, "error": "object required"})
            if self._scope == "approval":
                rec = self._intent
                if r == "/api/intent/prepare":
                    res = ext.prepare(flow, token=rec["token"], sender=str(req.get("sender") or ""), confirm_resend=bool(req.get("confirm_resend", False)))
                    return self._j(200 if res.get("ok") else 409, res)
                pid = str(req.get("payment_id") or "")
                if r in ("/api/order/signed", "/api/order/hold", "/api/order/reject"):
                    if not ext.order_allowed(rec, pid):
                        return self._j(403, {"ok": False, "error": "이 승인 링크의 주문이 아닙니다"})
                    if rec.get("test_mode") and r != "/api/order/reject":
                        return self._j(409, {"ok": False, "state": "NOT_SUBMITTED", "reason": "시험 모드: 서명본 제출·보관 등록 불가(전송 없음)", "test_mode": True, "payment_id": pid})
                else:
                    return self._j(404, {"error": "not found"})        # 링크 범위에서 chat·일반 prepare·intent 생성 불가
            if r == "/api/chat":
                return self._j(200, flow.chat(str(req.get("text") or "")[:300]))
            if r == "/api/intent/create":                  # 어댑터(같은 기기) 전용: 봇 키 필수. source=test 는 시험 모드 강제(모의 제공자·서명 불가)
                if not bot_key_ok(self):
                    return self._j(403, {"ok": False, "error": "bot key required"})
                src = str(req.get("source") or "")
                if src not in ("telegram", "test"):
                    return self._j(403, {"ok": False, "error": "unknown source"})
                if not str(req.get("external_key") or "") or not str(req.get("external_user") or ""):
                    return self._j(400, {"ok": False, "error": "external_key and external_user required"})
                snd = str(req.get("sender")) if req.get("sender") else None
                if snd and allowed_senders is not None and flow._norm_sender(snd) not in {flow._norm_sender(x) for x in allowed_senders}:
                    return self._j(403, {"ok": False, "error": "sender not in registered wallets"})     # 서버 쪽 발신 지갑 허용 목록(보안 리뷰 9/29: 봇 키 유출 시 임의 지갑 intent 방지)
                try:
                    rec, created = ext.create(flow, text=str(req.get("text") or "")[:300], sender=(str(req.get("sender")) if req.get("sender") else None), source=src,
                                              external_key=str(req.get("external_key")), external_user=str(req.get("external_user")), sender_label=(str(req.get("sender_label")) if req.get("sender_label") else None),
                                              test_mode=(src == "test") or bool(req.get("test_mode", False)))
                except IntentConflict as e:
                    return self._j(409, {"ok": False, "error": f"conflict: {e}", "conflict": True})
                return self._j(200, {"ok": True, "created": created, "intent": ext.view(rec, flow), "approval_path": f"/a/{rec['token']}", "summary": ext.summary_line(rec)})
            if r == "/api/order/prepare":
                res = flow.prepare(str(req.get("proposal_id") or ""), str(req.get("sender") or ""), confirm_resend=bool(req.get("confirm_resend", False)))
                return self._j(200 if res.get("ok") else 409, res)
            if r == "/api/intent/prepare_bg":                  # 어댑터 전용(봇 키): 지갑 연결 없이 서버가 미서명 거래 고정 → 승인 대기
                if not bot_key_ok(self):
                    return self._j(403, {"ok": False, "error": "bot key required"})
                res = ext.prepare_bg(flow, intent_id=str(req.get("intent_id") or ""))
                return self._j(200 if res.get("ok") else 409, res)
            if r == "/api/intent/approve":                     # 어댑터 전용(봇 키): 승인 메시지 키+지문으로 1회 소비 → 서명 → 방송 → 조회
                if not bot_key_ok(self):
                    return self._j(403, {"ok": False, "error": "bot key required"})
                res = ext.approve(flow, signer, policy, intent_id=str(req.get("intent_id") or ""), approval_key=str(req.get("approval_key") or ""), fingerprint=str(req.get("fingerprint") or ""),
                                  external_user=str(req.get("external_user") or ""), summary_msg_id=req.get("summary_msg_id"), ledger=ledger)
                return self._j(200 if res.get("ok") else 409, res)
            if r == "/api/intent/view":
                if not bot_key_ok(self):
                    return self._j(403, {"ok": False, "error": "bot key required"})
                rec = ext.get(str(req.get("intent_id") or ""))
                return self._j(200, {"ok": True, "intent": ext.view(rec, flow)}) if rec else self._j(404, {"ok": False, "error": "intent not found"})
            if r == "/api/intent/pending":
                if not bot_key_ok(self):
                    return self._j(403, {"ok": False, "error": "bot key required"})
                return self._j(200, {"ok": True, "pending": [ext.view(x, flow) for x in ext.pending_for_user(flow, str(req.get("external_user") or ""))], "signer": getattr(signer, "kind", "none")})
            if r == "/api/order/signed":
                st = req.get("signed_tx")
                if not isinstance(st, dict):
                    return self._j(400, {"ok": False, "error": "signed_tx object required"})
                res = flow.submit_signed(str(req.get("payment_id") or ""), str(req.get("snapshot_sha256") or ""), st)
                return self._j(200, {"ok": res.get("state") not in ("NOT_SUBMITTED",), **res})
            if r == "/api/order/hold":
                res = flow.register_hold(str(req.get("payment_id") or ""), str(req.get("snapshot_sha256") or ""), str(req.get("tx_hash") or ""))
                return self._j(200 if res.get("ok") else 409, res)
            if r == "/api/order/reject":
                res = flow.reject(str(req.get("payment_id") or ""), str(req.get("snapshot_sha256") or ""))
                return self._j(200 if res.get("ok") else 409, res)
            return self._j(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            line = "%s %s - %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), self.address_string(), MASK_RE.sub(lambda m: (m.group(1) or m.group(2)) + "<masked>", (fmt % args).replace(token, "<token>")))
            sys.stderr.write(line)
            if log_path:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(line)

    return H


def build_flow(node: NT.NileNode | None = None, pending: pathlib.Path = PENDING, logs: pathlib.Path = LOGS, **kw) -> PhoneFlow:
    node = node or NT.NileNode()
    return PhoneFlow(node=node, store=OrderStore(pending), intents=IntentLog(logs / "phone_intents.jsonl"), results_dir=logs / "phone_results", **kw)


def load_or_make_bot_key(path: pathlib.Path) -> str:
    """어댑터↔서버 봇 키(0600). 페이지·링크·로그에 넣지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    k = secrets.token_urlsafe(32); path.write_text(k, encoding="utf-8"); os.chmod(path, 0o600); return k


def serve(bind: str, port: int, advertise: str, flow: PhoneFlow, token: str | None = None, log_path: pathlib.Path | None = None, intents_ext: IntentStore | None = None, bot_key: str | None = None, allowed_senders: set[str] | None = None, signer=None, policy=None, ledger=None) -> tuple[ThreadingHTTPServer, str]:
    token = token or secrets.token_urlsafe(9)
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    for h in str(advertise).split(","):                     # 쉼표로 여러 호스트(LAN IP:포트, Tailscale HTTPS 호스트(포트 없음=443))
        h = h.strip()
        if not h:
            continue
        allowed.add(h if ":" in h or h.endswith(".ts.net") else f"{h}:{port}")
        if h.endswith(".ts.net"):
            allowed.add(f"{h}:443")
    srv = ThreadingHTTPServer((bind, port), make_handler(flow, token, allowed, log_path, intents_ext, bot_key, allowed_senders, signer, policy, ledger))
    return srv, token


def local_trial_policy(path: str | None, contacts: list[dict]) -> SG.SignerPolicy:
    """Validate the explicit trial configuration before loading the signing key."""
    policy = SG.load_trial_policy(path, stop_file=LOGS / "signer_stop")
    macbook = {c.get("address") for c in contacts if c.get("alias") == "맥북지갑" and c.get("confirmed_by_owner") and c.get("network") == "nile"}
    if len(macbook) != 1 or policy.allowed_receivers != macbook:
        raise ValueError("one-shot trial receiver must match the owner-confirmed Nile MacBook wallet")
    return policy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = 같은 Wi-Fi(LAN) 시험. 인터넷 공개 아님.")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--advertise", default="127.0.0.1", help="휴대폰이 접속할 맥북 LAN IP")
    ap.add_argument("--rotate-token", action="store_true", help="경로 토큰을 새로 만든다(기본: guide/logs/phone_token.txt 의 기존 토큰 유지 → 재기동 뒤에도 같은 URL 로 결과 조회 가능)")
    ap.add_argument("--signer", choices=["none", "mock", "local"], default="none", help="none=서명 주체 미정(운영 기본). mock=검사용 키(모의 전용). local=A안 봇 지갑(SB_SIGNER_KEY_FILE·SB_SIGNER_PASS_FILE, 승인 뒤)")
    ap.add_argument("--signer-trial-policy", default=os.environ.get("SB_SIGNER_TRIAL_POLICY_FILE"),
                    help="local 전용: 명시 JSON 정책(봇 주소→등록 맥북지갑, 정확히 2 TRX·수수료 상한 2 TRX·누적 1건). 원장 초기화/승인을 만들지 않음")
    ap.add_argument("--ai", choices=["mock", "kiln-live"], default="mock",
                    help="mock=MockKiln(실호출 0, 기본). kiln-live=사장 승인 범위의 BudgetedKiln(logs/kiln_budget_r2_20260929.json 상한·중단 공유, 실패 시 후속 호출 중단, 규칙 폴백은 성공 아님)")
    a = ap.parse_args()
    # No contact-wide/default daily budget is treated as permission to use a local key.
    try:
        policy = local_trial_policy(a.signer_trial_policy, PC.load_contacts()) if a.signer == "local" else SG.SignerPolicy(allowed_receivers=set(), stop_file=LOGS / "signer_stop")
    except (OSError, ValueError, TypeError) as e:
        ap.error(str(e))
    LOGS.mkdir(parents=True, exist_ok=True)
    import phone_ai as AI
    provider = AI.BudgetedKiln(flow_id="phone_server") if a.ai == "kiln-live" else None
    flow = build_flow(ai_provider=provider)
    tok_file = LOGS / "phone_token.txt"                   # 0600. 재기동 시 재사용(VP 9/29: 토큰 변경으로 결과를 못 보는 문제). 인증 제거·전체 공개 아님.
    token = None
    if tok_file.exists() and not a.rotate_token:
        token = tok_file.read_text(encoding="utf-8").strip() or None
    try:
        wallets = json.loads((HERE / "phone_wallets.json").read_text(encoding="utf-8")).get("wallets", [])
        allowed_senders = {w["address"] for w in wallets if w.get("address")}
    except (OSError, ValueError, KeyError) as e:
        sys.stderr.write(f"[phone_server] phone_wallets.json 읽기 실패 → 외부 intent 생성 시 발신 지갑 전부 거부: {e}\n"); allowed_senders = set()
    if a.signer == "mock":
        policy = SG.SignerPolicy(allowed_receivers={c.get("address") for c in PC.load_contacts() if c.get("address") and c.get("confirmed_by_owner")}, stop_file=LOGS / "signer_stop")
    ledger = SG.LimitLedger(LOGS / "signer_ledger.jsonl")                      # 명시 초기화(.init 마커) 전에는 한도 불명 → 실행 차단
    signer = SG.build_signer(a.signer, key_file=os.environ.get("SB_SIGNER_KEY_FILE"), pass_file=os.environ.get("SB_SIGNER_PASS_FILE"))
    if a.signer == "local":
        if signer.address not in policy.allowed_senders:
            ap.error("local signer address does not match the explicit trial sender")
        allowed_senders = set(policy.allowed_senders)
    srv, token = serve(a.host, a.port, a.advertise, flow, token=token, log_path=LOGS / "phone_server.log", intents_ext=IntentStore(LOGS / "phone_ext_intents"), bot_key=load_or_make_bot_key(LOGS / "bot_key.txt"), allowed_senders=allowed_senders, signer=signer, policy=policy, ledger=ledger)
    tok_file.write_text(token, encoding="utf-8"); os.chmod(tok_file, 0o600)
    hosts = [h.strip() for h in str(a.advertise).split(",") if h.strip()]
    first = hosts[0] if hosts else "127.0.0.1"
    url = f"https://{first}/p/{token}/" if first.endswith(".ts.net") else f"http://{first}:{a.port}/p/{token}/"     # Tailscale Serve 사설 HTTPS 는 443
    extra = [(f"https://{h}/p/{token}/" if h.endswith(".ts.net") else f"http://{h}:{a.port}/p/{token}/") for h in hosts[1:]]
    sess = LOGS / "phone_session.json"
    sess.write_text(json.dumps({"url": url, "extra_urls": extra, "bind": a.host, "port": a.port, "pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                "ai_mode": PC.AI_MODE, "ai_provider": (provider.provider if provider else "MOCK_KILN"), "network": "nile", "version": PAGE_VERSION, "signer": getattr(signer, "kind", "none")}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(sess, 0o600)
    sys.stderr.write(f"[phone_server] v{PAGE_VERSION} listening on {a.host}:{a.port} · session file {sess} (URL with token inside)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[phone_server] stopped by KeyboardInterrupt\n")


if __name__ == "__main__":
    main()
