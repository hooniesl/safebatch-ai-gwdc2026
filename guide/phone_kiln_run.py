"""Kiln qwen3-32b 휴대폰 흐름 실호출 러너 R2 — 사장 승인(2026-09-29 "응 상한 승인할께": 추가 최대 4회·이번 추가분 $0.01 상한·재시도 없음·CLI+서버 합산) 범위에서만 실행한다.
요청서: prep_20260914/KILN_REQUEST_PHONE_4CALLS_R2_20260929.md
  1단계  python3 guide/phone_kiln_run.py --stage format          KilnLive 직접 1회(형식 검증). 통과해야 2단계.
  2단계  python3 guide/phone_kiln_run.py --stage server --url http://127.0.0.1:8791/p/<token>   실행 중인 phone_server(KilnLive 주입) /api/chat 에 정상·적응·거절 3문장 POST(휴대폰 실제 입력 경로).
예산은 phone_ai.KilnBudget(logs/kiln_budget_r2_20260929.json) 한 파일로 CLI·서버가 함께 집계한다. 실패(형식·의미·기록·비용 미확인·통신)면 그 자리에서 멈춘다. 규칙 폴백은 성공으로 집계하지 않는다."""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import phone_ai as AI  # noqa: E402
import phone_chat as PC  # noqa: E402

FORMAT = ("format_check", "맥북지갑한테 트론 2개", {"kind": "proposal", "kind_detail": "normal", "amount_trx": "2"})
SERVER_FLOWS = [
    ("normal", "맥북지갑한테 트론 2개 보내줘", {"kind": "proposal", "kind_detail": "normal", "amount_trx": "2"}),
    ("adapt", "맥북지갑한테 트론 2개, 예산 3 트론 5분 안에", {"kind": "proposal", "kind_detail": "adapt", "amount_trx": "2", "constraints": {"budget_sun": 3_000_000, "deadline_s": 300, "fee_cap_sun": 1_000_000}}),
    ("decline", "맥북지갑한테 트론 1000개", {"kind": "decline", "reason_code": "OVER_PER_REQUEST_CAP", "order_created": False}),
]
OUT = HERE / "logs" / "kiln_phone_runs.jsonl"
PRICE_IN, PRICE_OUT = 0.10, 0.10          # USD / 1M tokens — 참고 추정 단가. 판정에는 쓰지 않는다(서버 usage.cost 만 '확인됨').


def est_cost(usage: dict | None):
    """서버 usage.cost 가 숫자면 그 값, 아니면 None(0 으로 두지 않는다 — VP 9/29). 토큰 단가 추정은 estimate_only() 로 따로."""
    if not usage or not isinstance(usage, dict):
        return None
    c = usage.get("cost")
    if isinstance(c, (int, float)) and not isinstance(c, bool):
        return float(c)
    return None


def estimate_only(usage: dict | None) -> float | None:
    if not usage:
        return None
    return (float(usage.get("prompt_tokens") or 0) * PRICE_IN + float(usage.get("completion_tokens") or 0) * PRICE_OUT) / 1_000_000


def judge(r: dict, expect: dict) -> tuple[bool, str]:
    """형식(도구 호출)·기록·비용 + 의미(기대 결과) 판정. 어느 하나라도 아니면 실패."""
    ai = r.get("ai") or {}
    if ai.get("mode") != "KILN_LIVE":
        return False, f"not live (mode={ai.get('mode')})"
    if ai.get("calls") != 1:
        return False, f"calls={ai.get('calls')} (budget gate refused?) error={ai.get('error')}"
    if ai.get("error") or ai.get("fallback"):
        return False, f"error/fallback: {ai.get('error') or ai.get('fallback')}"
    if ai.get("source") != "tool_calls":
        return False, f"source={ai.get('source')}"
    if ai.get("raw_saved") is not True:
        return False, f"raw not saved: {ai.get('raw_error')}"
    if ai.get("cost_status") != "server_usage" or not isinstance(ai.get("cost_usd"), (int, float)):
        return False, f"cost unknown ({ai.get('cost_status')})"
    for k in ("kind", "kind_detail", "reason_code", "order_created"):
        if k in expect and r.get(k) != expect[k]:
            return False, f"{k}={r.get(k)!r} expected {expect[k]!r}"
    if "amount_trx" in expect and Decimal(str((r.get("proposal") or {}).get("amount_trx"))) != Decimal(expect["amount_trx"]):
        return False, f"amount_trx={(r.get('proposal') or {}).get('amount_trx')} expected {expect['amount_trx']}"
    if "constraints" in expect and r.get("constraints") != expect["constraints"]:
        return False, f"constraints={r.get('constraints')} expected {expect['constraints']}"
    if ai.get("ignored_model_conditions"):
        return False, f"model invented conditions {ai.get('ignored_model_conditions')}"
    return True, ""


def record(stage: str, name: str, text: str, r: dict, ok: bool, why: str, elapsed_ms: int, budget_state: dict | None) -> dict:
    ai = r.get("ai") or {}
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "round": "R2", "stage": stage, "flow": name, "text": text, "kind": r.get("kind"), "kind_detail": r.get("kind_detail"),
           "reason_code": r.get("reason_code"), "ai_mode": ai.get("mode"), "model": ai.get("model"), "calls": ai.get("calls"), "usage": ai.get("usage"), "request_id": ai.get("request_id"),
           "error": ai.get("error"), "fallback": ai.get("fallback"), "ignored_model_conditions": ai.get("ignored_model_conditions"), "constraints": r.get("constraints"),
           "source": ai.get("source"), "tool_calls_n": ai.get("tool_calls_n"), "finish_reason": ai.get("finish_reason"), "call_id": ai.get("call_id"), "raw_file": ai.get("raw_file"),
           "raw_saved": ai.get("raw_saved"), "raw_error": ai.get("raw_error"), "cost_usd": ai.get("cost_usd"), "cost_status": ai.get("cost_status"), "diagnostic": ai.get("diagnostic"),
           "proposal": {k: v for k, v in (r.get("proposal") or {}).items() if k in ("alias", "address", "amount_trx")}, "elapsed_ms": elapsed_ms,
           "ai_success": bool(ok), "fail_reason": why or None, "budget": budget_state}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps({k: rec[k] for k in ("stage", "flow", "kind", "kind_detail", "reason_code", "source", "tool_calls_n", "cost_usd", "cost_status", "raw_saved", "ai_success", "fail_reason", "budget")}, ensure_ascii=False))
    return rec


def run_format(budget: AI.KilnBudget) -> bool:
    name, text, expect = FORMAT
    ok_gate, why = budget.can_call()
    if not ok_gate:
        print(json.dumps({"stage": "format", "skipped": why}, ensure_ascii=False)); return False
    prov = AI.BudgetedKiln(budget=budget, flow_id=f"phone_r2_{name}")
    t0 = time.time(); r = AI.decide(text, PC.load_contacts(), provider=prov); ms = int((time.time() - t0) * 1000)
    ok, why = judge(r, expect)
    if not ok:
        budget.halt(f"format judge: {why[:150]}")          # 형식/의미 판정 실패 → 후속 중단(횟수는 예약 시점에 이미 소비)
    d = budget.load()
    record("format", name, text, r, ok, why, ms, {k: d.get(k) for k in ("used", "max_calls", "cost_usd", "unknown_cost_calls", "halted")})
    return ok


def post_chat(url: str, text: str, timeout: int = 60) -> tuple[int, dict | None, str | None]:
    """휴대폰과 같은 경로: /api/chat 에 JSON POST(curl). Host 는 URL 의 호스트 그대로."""
    p = subprocess.run(["curl", "-sS", "--max-time", str(timeout), "-H", "Content-Type: application/json", "-w", "\n__HTTP__%{http_code}",
                        "-X", "POST", url.rstrip("/") + "/api/chat", "--data-binary", "@-"], input=json.dumps({"text": text}).encode("utf-8"), capture_output=True, timeout=timeout + 5)
    if p.returncode != 0:
        return 0, None, f"curl exit {p.returncode}: {p.stderr.decode('utf-8', 'ignore')[:200]}"
    out = p.stdout.decode("utf-8", "ignore"); body, _, code = out.rpartition("\n__HTTP__")
    try:
        return int(code.strip() or 0), json.loads(body), None
    except Exception as e:                                  # noqa: BLE001
        return int(code.strip() or 0), None, f"non-JSON: {body[:200]} ({e})"


def run_server(budget: AI.KilnBudget, url: str, only: str = "") -> bool:
    d = budget.load()
    if d.get("halted") or int(d.get("used", 0)) < 1:
        print(json.dumps({"stage": "server", "skipped": "format stage not passed" if int(d.get("used", 0)) < 1 else f"halted: {d.get('halted')}"}, ensure_ascii=False)); return False
    all_ok = True
    for name, text, expect in SERVER_FLOWS:
        if only and name not in only.split(","):
            continue
        ok_gate, why = budget.can_call()
        if not ok_gate:
            print(json.dumps({"stage": "server", "flow": name, "skipped": why}, ensure_ascii=False)); return False
        t0 = time.time(); http, r, err = post_chat(url, text); ms = int((time.time() - t0) * 1000)
        if err or http != 200 or not isinstance(r, dict):
            why = f"transport: http={http} {err or ''}".strip()
            budget.halt(f"server {name}: {why[:150]}"); d = budget.load()      # 통신 오류: 서버 쪽 예약이 이미 횟수를 소비했을 수 있음. 중단만 표시
            record("server", name, text, {"ai": {"mode": "KILN_LIVE", "calls": None, "error": why}}, False, why, ms, {k: d.get(k) for k in ("used", "max_calls", "cost_usd", "unknown_cost_calls", "halted")})
            return False
        ok, why = judge(r, expect)
        if not ok:
            budget.halt(f"server {name} judge: {why[:150]}")
        d = budget.load()
        record("server", name, text, r, ok, why, ms, {k: d.get(k) for k in ("used", "max_calls", "cost_usd", "unknown_cost_calls", "halted")})
        if not ok:
            return False
    return all_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["format", "server", "init"], required=True, help="init: 승인 원문과 함께 집계 파일 생성(없을 때만). 집계 파일 없음은 새 승인이 아니다")
    ap.add_argument("--approval", default="", help="init 단계: 사장 승인 원문")
    ap.add_argument("--url", default="", help="server 단계: http(s)://host[:port]/p/<token>")
    ap.add_argument("--only", default="")
    ap.add_argument("--max-calls", type=int, default=4); ap.add_argument("--max-usd", type=float, default=0.01)
    a = ap.parse_args()
    budget = AI.KilnBudget(max_calls=a.max_calls, max_usd=a.max_usd)
    if a.stage == "init":
        if not a.approval:
            sys.exit("--approval '<사장 승인 원문>' 필요")
        d = budget.init(a.approval); print(json.dumps({"stage": "init", "path": str(budget.path), "used": d.get("used"), "approval": d.get("approval")}, ensure_ascii=False)); return
    if a.stage == "format":
        ok = run_format(budget)
    else:
        if not a.url:
            sys.exit("--url 필요")
        ok = run_server(budget, a.url, a.only)
    d = budget.load()
    print(json.dumps({"stage": a.stage, "passed": ok, "budget": {k: d.get(k) for k in ("used", "max_calls", "cost_usd", "unknown_cost_calls", "halted")}}, ensure_ascii=False))
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
