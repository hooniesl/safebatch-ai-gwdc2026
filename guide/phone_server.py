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
import phone_chat as PC  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402

MAX_BODY = 64 * 1024
PENDING = HERE / "pending"
LOGS = HERE / "logs"


def make_handler(flow: PhoneFlow, token: str, allowed_hosts: set[str], log_path: pathlib.Path | None):
    PAGE = (HERE / "phone.html").read_text(encoding="utf-8").replace("__SB_BASE__", f"/p/{token}")
    prefix = f"/p/{token}"

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
            if not u.path.startswith(prefix + "/") and u.path != prefix:
                return None
            return u.path[len(prefix):] or "/"

        def do_GET(self):
            why = self._host_ok()
            if why:
                return self._j(403, {"ok": False, "error": why})
            r = self._route()
            if r is None:
                return self._j(404, {"error": "not found"})
            if r in ("/", ""):
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
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
            if r == "/api/chat":
                return self._j(200, flow.chat(str(req.get("text") or "")[:300]))
            if r == "/api/order/prepare":
                res = flow.prepare(str(req.get("proposal_id") or ""), str(req.get("sender") or ""), confirm_resend=bool(req.get("confirm_resend", False)))
                return self._j(200 if res.get("ok") else 409, res)
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
            line = "%s %s - %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), self.address_string(), (fmt % args).replace(token, "<token>"))
            sys.stderr.write(line)
            if log_path:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(line)

    return H


def build_flow(node: NT.NileNode | None = None, pending: pathlib.Path = PENDING, logs: pathlib.Path = LOGS, **kw) -> PhoneFlow:
    node = node or NT.NileNode()
    return PhoneFlow(node=node, store=OrderStore(pending), intents=IntentLog(logs / "phone_intents.jsonl"), results_dir=logs / "phone_results", **kw)


def serve(bind: str, port: int, advertise: str, flow: PhoneFlow, token: str | None = None, log_path: pathlib.Path | None = None) -> tuple[ThreadingHTTPServer, str]:
    token = token or secrets.token_urlsafe(9)
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    for h in str(advertise).split(","):                     # 쉼표로 여러 호스트(LAN IP:포트, Tailscale HTTPS 호스트(포트 없음=443))
        h = h.strip()
        if not h:
            continue
        allowed.add(h if ":" in h or h.endswith(".ts.net") else f"{h}:{port}")
        if h.endswith(".ts.net"):
            allowed.add(f"{h}:443")
    srv = ThreadingHTTPServer((bind, port), make_handler(flow, token, allowed, log_path))
    return srv, token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = 같은 Wi-Fi(LAN) 시험. 인터넷 공개 아님.")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--advertise", default="127.0.0.1", help="휴대폰이 접속할 맥북 LAN IP")
    ap.add_argument("--rotate-token", action="store_true", help="경로 토큰을 새로 만든다(기본: guide/logs/phone_token.txt 의 기존 토큰 유지 → 재기동 뒤에도 같은 URL 로 결과 조회 가능)")
    ap.add_argument("--ai", choices=["mock", "kiln-live"], default="mock",
                    help="mock=MockKiln(실호출 0, 기본). kiln-live=사장 승인 범위의 BudgetedKiln(logs/kiln_budget_r2_20260929.json 상한·중단 공유, 실패 시 후속 호출 중단, 규칙 폴백은 성공 아님)")
    a = ap.parse_args()
    LOGS.mkdir(parents=True, exist_ok=True)
    import phone_ai as AI
    provider = AI.BudgetedKiln(flow_id="phone_server") if a.ai == "kiln-live" else None
    flow = build_flow(ai_provider=provider)
    tok_file = LOGS / "phone_token.txt"                   # 0600. 재기동 시 재사용(VP 9/29: 토큰 변경으로 결과를 못 보는 문제). 인증 제거·전체 공개 아님.
    token = None
    if tok_file.exists() and not a.rotate_token:
        token = tok_file.read_text(encoding="utf-8").strip() or None
    srv, token = serve(a.host, a.port, a.advertise, flow, token=token, log_path=LOGS / "phone_server.log")
    tok_file.write_text(token, encoding="utf-8"); os.chmod(tok_file, 0o600)
    hosts = [h.strip() for h in str(a.advertise).split(",") if h.strip()]
    first = hosts[0] if hosts else "127.0.0.1"
    url = f"https://{first}/p/{token}/" if first.endswith(".ts.net") else f"http://{first}:{a.port}/p/{token}/"     # Tailscale Serve 사설 HTTPS 는 443
    extra = [(f"https://{h}/p/{token}/" if h.endswith(".ts.net") else f"http://{h}:{a.port}/p/{token}/") for h in hosts[1:]]
    sess = LOGS / "phone_session.json"
    sess.write_text(json.dumps({"url": url, "extra_urls": extra, "bind": a.host, "port": a.port, "pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                "ai_mode": PC.AI_MODE, "ai_provider": (provider.provider if provider else "MOCK_KILN"), "network": "nile"}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(sess, 0o600)
    sys.stderr.write(f"[phone_server] listening on {a.host}:{a.port} · session file {sess} (URL with token inside)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[phone_server] stopped by KeyboardInterrupt\n")


if __name__ == "__main__":
    main()
