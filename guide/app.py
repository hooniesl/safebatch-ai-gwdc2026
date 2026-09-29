#!/usr/bin/env python3
"""안내 화면 + 서명 화면 서버(표준 라이브러리). 실행: python3 guide/app.py --port 8765
GET  /               한국어 안내 화면 · GET /sign 서명 화면
POST /api/plan       계획(JSON) · GET /api/cards
GET  /api/order/pending          대기 주문(있으면 1건)
POST /api/order/signed           서명본 저장(서버 원본 주문과 domain/types/message 동일 + 서명자 EOA 복구 == 주문 user + 기한)
POST /api/order/reject           주문별 취소(payment_id + snapshot 지정)
요청 경계(9/28 부사장 검수 3): 상태 변경/유료 POST 는 Content-Type application/json + 세션 토큰 헤더(X-SafeBatch-Token, 서버 기동 시 생성·페이지에 주입)
+ Origin/Sec-Fetch-Site 가 같은 출처일 때만. 본문 64KB 상한. LLM 은 목적문 구조화만 하며 규칙·문구·URL 은 데이터 파일이 고정한다.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import plan as P  # noqa: E402
import ai_work as AW  # noqa: E402
from order_store import OrderStore  # noqa: E402

PENDING = HERE / "pending"
MAX_BODY = 64 * 1024
SESSION_TOKEN = secrets.token_urlsafe(24)   # 프로세스마다 새로 생성; 페이지에만 주입


def make_handler(store: OrderStore, token: str, port: int):
    PAGE = (HERE / "index.html").read_text(encoding="utf-8").replace("__SB_TOKEN__", token)
    SIGN = (HERE / "sign.html").read_text(encoding="utf-8").replace("__SB_TOKEN__", token)
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _j(self, code: int, obj: dict):
            return self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/sign":
                return self._send(200, SIGN.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/api/order/pending":
                p = store.pending_orders()
                return self._j(200, {"order": p[0] if p else None, "pending_count": len(p)})
            if self.path == "/api/health":
                return self._j(200, {"ok": True})
            if self.path == "/api/cards":
                r = P.load_rules()
                return self._j(200, {"glossary": r["menu_glossary"], "earn_cards": r["earn_cards"]})
            return self._j(404, {"error": "not found"})

        # ── 요청 경계 ──────────────────────────────────────────────────
        def _boundary(self) -> str:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return "content-type must be application/json"
            if (self.headers.get("X-SafeBatch-Token") or "") != token:
                return "missing or wrong session token"
            origin = self.headers.get("Origin")
            sfs = self.headers.get("Sec-Fetch-Site")
            if origin is not None and origin not in allowed_origins:
                return f"origin not allowed: {origin}"
            if sfs is not None and sfs not in ("same-origin", "none"):
                return f"cross-site request: {sfs}"
            host = (self.headers.get("Host") or "")
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                return f"host not allowed: {host}"
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return "bad content-length"
            if n <= 0 or n > MAX_BODY:
                return "body size"
            return ""

        def do_POST(self):
            why = self._boundary()
            if why:
                return self._j(403, {"ok": False, "error": why})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._j(400, {"ok": False, "error": "bad json"})
            if not isinstance(req, dict):
                return self._j(400, {"ok": False, "error": "object required"})

            if self.path == "/api/order/signed":
                pid, snap = str(req.get("payment_id") or ""), str(req.get("snapshot_sha256") or "")
                ok, msg = store.store_signature(pid, snap, req, now=int(time.time()))
                return self._j(200 if ok else 409, {"ok": ok, "detail": msg if ok else None, "error": None if ok else msg,
                                                     "signer": msg if ok else None})
            if self.path == "/api/order/reject":
                pid, snap = str(req.get("payment_id") or ""), str(req.get("snapshot_sha256") or "")
                ok, msg = store.cancel(pid, snap)
                return self._j(200 if ok else 409, {"ok": ok, "detail": msg if ok else None, "error": None if ok else msg})
            if self.path != "/api/plan":
                return self._j(404, {"error": "not found"})

            text = str(req.get("text") or "")[:800]
            answers = req.get("answers") or {}
            use_kiln = bool(req.get("use_kiln", False))          # 버튼으로만 켜진다(진입/새로고침만으로 호출 없음)
            change_request = (str(req.get("change_request") or "")[:300] or None)
            si = P.structure_intent(text, use_kiln=use_kiln, change_request=change_request)
            intent = dict(si["intent"])
            conflicts = list(si["conflicts"])
            ok_answers, why2 = P.validate_intent({k: answers.get(k) for k in P.INTENT_KEYS})
            if ok_answers:
                for k in P.INTENT_KEYS:
                    if intent.get(k) is None and ok_answers[k] is not None:
                        intent[k] = ok_answers[k]
                        conflicts = [c for c in conflicts if not c.startswith(k + ":")]
            elif answers:
                conflicts.append(f"answers: 화면 입력이 검증을 통과하지 못함({why2})")
            stage_answers = req.get("stage_answers") if isinstance(req.get("stage_answers"), dict) else {}
            stage = P.infer_step(stage_answers, si.get("hints"))
            step = str(req.get("step")) if req.get("step") else stage["step"]          # 명시 step 은 '상세 설정'에서만 온다
            pl = P.build_plan(intent, current_step=step, conflicts=conflicts, hints=si.get("hints"))
            pl["stage"] = {**stage, "step_used": step, "note": P.load_rules().get("stage_note")}
            pl["intent_source"] = si["source"]; pl["kiln"] = si["kiln"]; pl["flow_id"] = si["flow_id"]
            pl["ai_work"] = AW.work_card(pl, si, si.get("ai_work_raw"), change_request)
            if use_kiln:                                          # 실제 AI 결과를 보존 → 실행 흐름이 검증된 기록으로 이어받는다(두 번째 호출 없음)
                d = HERE / "logs" / "plan_results"; d.mkdir(parents=True, exist_ok=True)
                (d / f"{si['flow_id']}.json").write_text(json.dumps({"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "user_text": text, "answers": answers,
                                                                     "change_request": change_request, **pl}, ensure_ascii=False, indent=1), encoding="utf-8")
                pl["ai_record_path"] = str(d / f"{si['flow_id']}.json")
            return self._j(200, pl)

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return H


def serve(port: int, store: OrderStore | None = None, token: str | None = None):
    store = store or OrderStore(PENDING)
    token = token or SESSION_TOKEN
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, token, port))
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = serve(a.port)
    print(f"guide server http://127.0.0.1:{a.port}/  (session token issued in-page; Ctrl-C to stop)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
