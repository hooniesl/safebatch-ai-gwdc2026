"""SafeBatch AI · GasFree Open API 클라이언트 (Nile 테스트넷 기본).

- 인증: docs.gasfree.io "API Authentication" — 헤더 Timestamp(초), Authorization: "ApiKey {key}:{sig}",
  sig = base64(HMAC-SHA256(secret, f"{METHOD}{PATH}{TIMESTAMP}")). PATH 는 '/nile/api/v1/...' 전체.
- 키는 환경변수 또는 .env 파일에서 읽고 절대 출력·로그하지 않는다.
- 표준 라이브러리만 사용(urllib). 재시도는 하지 않는다(응답 유실은 호출자가 UNKNOWN 으로 보관).
- 이 모듈은 서명(지갑 개인키)·송금을 하지 않는다. submit 은 사람이 서명한 데이터를 그대로 전달할 뿐이다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

NILE_BASE = "https://open-test.gasfree.io"
NILE_PREFIX = "/nile"
MAINNET_BASE = "https://open.gasfree.io"       # 사용 금지(테스트 범위 밖). 상수만 둔다.
MAINNET_PREFIX = "/tron"

DEFAULT_ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"


class GasFreeError(Exception):
    pass


def load_keys(env_path: pathlib.Path | None = None, env: str = "nile") -> tuple[str, str]:
    """환경별 키. 9/28 진단으로 기존 GASFREE_API_KEY/SECRET 은 **mainnet 앱 키**로 확인됐다.
    Nile 경로는 GASFREE_NILE_API_KEY / GASFREE_NILE_API_SECRET 만 읽는다(없으면 실패 — mainnet 값으로 대체하지 않는다).
    값은 반환만 하고 어디에도 출력하지 않는다. 기존 mainnet 값은 덮어쓰지 않는다."""
    if env != "nile":
        raise GasFreeError("only nile credentials are loaded by this client")
    names = ("GASFREE_NILE_API_KEY", "GASFREE_NILE_API_SECRET")
    key = os.environ.get(names[0], "")
    secret = os.environ.get(names[1], "")
    if key and secret:
        return key, secret
    p = env_path or DEFAULT_ENV
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == names[0] and not key:
                key = v
            elif k == names[1] and not secret:
                secret = v
    if not key or not secret:
        raise GasFreeError("GASFREE_NILE_API_KEY/SECRET 없음 — Nile 테스트넷용 키가 아직 저장되지 않았다(기존 GASFREE_API_KEY 는 mainnet 키). 값은 출력하지 않는다")
    return key, secret


def sign(secret: str, method: str, path: str, timestamp: int) -> str:
    msg = f"{method}{path}{timestamp}".encode("utf-8")
    return base64.b64encode(hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()).decode("utf-8")


class GasFreeClient:
    def __init__(self, base: str = NILE_BASE, prefix: str = NILE_PREFIX, keys: tuple[str, str] | None = None,
                 timeout: int = 30, transport: str = "curl"):
        """transport: 'curl'(기본) 또는 'urllib'.
        9/28 실측: macOS 시스템 python3.9 의 LibreSSL 2.8.3 은 open-test.gasfree.io 와 TLS 협상 실패
        (tlsv1 alert protocol version). curl 8.7(LibreSSL 3.3.6)은 접속된다 → 기본을 curl 로 둔다.
        헤더는 임시 파일(0600)로 전달해 프로세스 인자에 키가 노출되지 않게 한다."""
        base = base.rstrip("/")
        prefix = "/" + prefix.strip("/")
        if base != NILE_BASE or prefix != NILE_PREFIX:   # 9/28 GPT#4: 문자열 변형(끝 슬래시·/tron)으로 우회 불가
            raise GasFreeError(f"only Nile testnet is allowed ({NILE_BASE}{NILE_PREFIX}); got {base}{prefix}")
        self.base = base
        self.prefix = prefix
        self.key, self.secret = keys or load_keys()
        self.timeout = timeout
        self.transport = transport
        self.calls: list[dict] = []   # 비밀값 없는 호출 기록(시각·메서드·경로·HTTP·code)

    def _curl(self, method: str, url: str, headers: dict, data: bytes | None) -> tuple[int, str]:
        import subprocess, tempfile, os
        fd, hpath = tempfile.mkstemp(prefix=".gf_hdr_", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for k, v in headers.items():
                    fh.write(f"{k}: {v}\n")
            os.chmod(hpath, 0o600)
            cmd = ["curl", "-sS", "--max-time", str(self.timeout), "-X", method, "-H", f"@{hpath}",
                   "-w", "\n__HTTP__%{http_code}", url]
            if data is not None:
                cmd += ["--data-binary", "@-"]
            p = subprocess.run(cmd, input=data, capture_output=True, timeout=self.timeout + 5)
        finally:
            try:
                os.unlink(hpath)
            except OSError:
                pass
        if p.returncode != 0:
            raise GasFreeError(f"curl exit {p.returncode}: {p.stderr.decode('utf-8', 'ignore')[:200]}")
        out = p.stdout.decode("utf-8", "ignore")
        body, _, code = out.rpartition("\n__HTTP__")
        return int(code.strip() or 0), body

    # ── 저수준 ────────────────────────────────────────────────────────────
    def _request(self, method: str, api_path: str, body: dict | None = None) -> dict:
        path = f"{self.prefix}{api_path}"
        ts = int(time.time())
        headers = {
            "Timestamp": str(ts),
            "Authorization": f"ApiKey {self.key}:{sign(self.secret, method, path, ts)}",
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8") if body is not None else None
        rec = {"ts": ts, "method": method, "path": path, "http": None, "code": None, "reason": None, "elapsed_ms": None,
               "transport": self.transport}
        t0 = time.time()
        try:
            if self.transport == "curl":
                rec["http"], raw = self._curl(method, self.base + path, headers, data)
            else:
                req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode("utf-8", "ignore")
                    rec["http"] = r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            rec["http"] = e.code
        except Exception as e:  # 네트워크/timeout — 성공 여부 미확인(UNKNOWN)
            rec["elapsed_ms"] = int((time.time() - t0) * 1000)
            rec["error"] = f"{type(e).__name__}: {e}"
            self.calls.append(rec)
            raise GasFreeError(f"transport error (UNKNOWN outcome): {rec['error']}")
        rec["elapsed_ms"] = int((time.time() - t0) * 1000)
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {"_raw": raw[:500]}
        rec["code"] = parsed.get("code") if isinstance(parsed, dict) else None
        rec["reason"] = parsed.get("reason") if isinstance(parsed, dict) else None
        self.calls.append(rec)
        return {"http": rec["http"], "body": parsed, "record": rec}

    # ── 읽기 전용 ─────────────────────────────────────────────────────────
    def tokens(self) -> dict:
        return self._request("GET", "/api/v1/config/token/all")

    def providers(self) -> dict:
        return self._request("GET", "/api/v1/config/provider/all")

    def address(self, account_address: str) -> dict:
        return self._request("GET", f"/api/v1/address/{account_address}")

    def trace(self, trace_id: str) -> dict:
        return self._request("GET", f"/api/v1/gasfree/{trace_id}")

    # ── 제출(사람 서명 완료 데이터만) ───────────────────────────────────────
    def submit(self, signed_authorization: dict) -> dict:
        """사람이 서명한 permit authorization 을 그대로 제출한다. 서명 생성은 하지 않는다."""
        return self._request("POST", "/api/v1/gasfree/submit", body=signed_authorization)


def ok(resp: dict) -> bool:
    """HTTP 200 이고 본문 code 도 200 일 때만 성공. 둘 중 하나만 보고 성공이라 하지 않는다."""
    return resp.get("http") == 200 and isinstance(resp.get("body"), dict) and resp["body"].get("code") == 200
