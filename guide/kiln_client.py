"""Kiln(Bricksum) chat/completions 최소 클라이언트. 키는 gwdc_2026/.env 의 KILN_API_KEY 만 읽고 출력하지 않는다.
- 모델 고정 qwen3-32b(주최: 이 모델만 인정). 재시도 없음. 흐름별 usage·cost 를 JSONL 로 기록(flow_id).
- 전송은 curl(시스템 python3 TLS 문제와 무관하게 동일 경로 사용). 응답 원문은 로그에 남기되 키는 절대 남기지 않는다.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import tempfile
import time
import uuid

BASE = "https://api.bricksum.com/v1"
MODEL = "qwen3-32b"
ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = ROOT / "guide" / "logs" / "kiln_calls.jsonl"
RAW_DIR = ROOT / "guide" / "logs" / "kiln_raw"


class KilnError(Exception):
    pass


def save_raw_message(call_id: str, body: dict) -> tuple[bool, str | None]:
    """응답 본문 중 choices/usage/model/id 만 보존(요청 헤더·키는 포함하지 않는다). 반환 (저장 성공 여부, 실패 사유).
    VP 9/29 보완: 보존 실패를 숨기지 않는다 — 호출 결과 레코드에 raw_saved=false·raw_error 로 남기고 러너는 이를 실패로 본다."""
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        keep = {k: body.get(k) for k in ("id", "model", "choices", "usage", "created") if k in body}
        path = RAW_DIR / f"{call_id}.json"
        path.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
        if not path.exists() or path.stat().st_size == 0:
            return False, "written file missing or empty"
        return True, None
    except Exception as e:                                  # noqa: BLE001 — 보존 실패가 호출 자체를 예외로 깨뜨리지는 않되, 결과에 드러낸다
        return False, f"{type(e).__name__}: {e}"[:200]


def load_key() -> str:
    key = os.environ.get("KILN_API_KEY", "")
    if not key:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("KILN_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise KilnError("KILN_API_KEY 없음(.env) — 값은 출력하지 않는다")
    return key


def chat(messages: list[dict], *, flow_id: str, tools: list | None = None, tool_choice=None, max_tokens: int = 600,
         temperature: float = 0.0, timeout: int = 90) -> dict:
    """1회 호출. 반환 {"ok", "http", "content", "tool_calls", "usage", "model", "request_id", "elapsed_ms", "error"}."""
    key = load_key()
    body = {"model": MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if tools:
        body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
    fd, hp = tempfile.mkstemp(prefix=".kiln_hdr_", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write(f"Authorization: Bearer {key}\nContent-Type: application/json\n")
    os.chmod(hp, 0o600)
    t0 = time.time()
    rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "flow_id": flow_id,
           "call_id": uuid.uuid4().hex[:8], "model_requested": MODEL, "tools": bool(tools)}
    try:
        p = subprocess.run(["curl", "-sS", "--max-time", str(timeout), "-H", f"@{hp}", "-w", "\n__HTTP__%{http_code}",
                            "-D", "-", "-X", "POST", BASE + "/chat/completions", "--data-binary", "@-"],
                           input=json.dumps(body).encode("utf-8"), capture_output=True, timeout=timeout + 5)
    finally:
        os.unlink(hp)
    rec["elapsed_ms"] = int((time.time() - t0) * 1000)
    out = p.stdout.decode("utf-8", "ignore")
    raw, _, code = out.rpartition("\n__HTTP__")
    http = int(code.strip() or 0)
    headers, _, payload = raw.partition("\r\n\r\n")
    if "\r\n\r\n" in payload:  # 100-continue 등 이중 헤더
        headers, _, payload = payload.partition("\r\n\r\n")
    req_id = None
    for line in headers.splitlines():
        if line.lower().startswith(("x-request-id:", "x-generation-id:", "x-bricksum-request-id:")):
            req_id = line.split(":", 1)[1].strip()
    res = {"ok": False, "http": http, "content": None, "tool_calls": None, "reasoning_content": None, "usage": None, "model": None,
           "request_id": req_id, "elapsed_ms": rec["elapsed_ms"], "error": None, "call_id": rec["call_id"],
           "raw_saved": False, "raw_error": "no JSON body to save", "raw_file": None, "cost_usd": None, "cost_status": "unknown"}
    if p.returncode != 0:
        res["error"] = f"curl exit {p.returncode}: {p.stderr.decode('utf-8', 'ignore')[:200]}"
    else:
        try:
            j = json.loads(payload)
        except json.JSONDecodeError:
            j = None
            res["error"] = f"non-JSON body http={http}: {payload[:200]}"
        if isinstance(j, dict):
            if http == 200 and j.get("choices"):
                msg = j["choices"][0].get("message") or {}
                res.update({"ok": True, "content": msg.get("content"), "tool_calls": msg.get("tool_calls"),
                            "reasoning_content": msg.get("reasoning_content") or msg.get("reasoning"),
                            "usage": j.get("usage"), "model": j.get("model"),
                            "finish_reason": j["choices"][0].get("finish_reason")})
            else:
                res["error"] = json.dumps(j.get("error", j), ensure_ascii=False)[:300]
            # 9/29 11:28 사후 분석 교훈: usage 만 남기면 '왜 도구 호출이 없었나'를 알 수 없다. 응답 message 원문(키 없음)을 call_id 별로 보존한다.
            ok_saved, save_err = save_raw_message(rec["call_id"], j)
            res["raw_saved"], res["raw_error"] = ok_saved, save_err
            res["raw_file"] = str((RAW_DIR / f"{rec['call_id']}.json").relative_to(ROOT)) if ok_saved else None
            # 비용: 서버 usage.cost 가 숫자일 때만 '확인됨'. 없으면 None + unknown(0 으로 두지 않는다 — VP 9/29)
            u = res.get("usage") if isinstance(res.get("usage"), dict) else None
            if u is not None and isinstance(u.get("cost"), (int, float)) and not isinstance(u.get("cost"), bool):
                res["cost_usd"], res["cost_status"] = float(u["cost"]), "server_usage"
            else:
                res["cost_usd"], res["cost_status"] = None, ("unknown(no usage.cost)" if u is not None else "unknown(no usage)")
    rec.update({k: res[k] for k in ("ok", "http", "usage", "model", "request_id", "error", "raw_saved", "raw_error", "raw_file", "cost_usd", "cost_status")})
    rec["finish_reason"] = res.get("finish_reason")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return res
