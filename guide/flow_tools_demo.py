#!/usr/bin/env python3
"""Kiln 흐름 2: 지급 없는 '조건 조회' 도구 호출 1회. 모델이 tool_calls 로 lookup_conditions(step_id) 를 요청하면
코드가 데이터 파일에서 조건을 읽어 돌려줄 준비만 한다(후속 호출 없음, 지급 없음). 결과·usage 를 JSONL 로 남긴다."""
import json, pathlib, sys, uuid
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import kiln_client, plan as P  # noqa: E402

TOOLS = [{"type": "function", "function": {
    "name": "lookup_conditions",
    "description": "Return the official conditions (rules with source URL, doc date, checked_at) that apply to a step of the Upbit→foreign-exchange USDT route. Read-only. No payment.",
    "parameters": {"type": "object", "properties": {"step_id": {"type": "string", "enum": ["s1", "s2", "s3", "s4", "s5"]}},
                   "required": ["step_id"]}}}]

flow_id = f"tools-{uuid.uuid4().hex[:8]}"
msgs = [{"role": "system", "content": "/no_think You help a Korean user follow an exchange-transfer guide. When the user asks what conditions apply to a step, call lookup_conditions with the step id. Do not invent conditions yourself."},
        {"role": "user", "content": "업비트에서 바이낸스 내 계정으로 USDT 출금 신청 단계(s4)에 어떤 조건이 적용되는지 확인해줘."}]
res = kiln_client.chat(msgs, flow_id=flow_id, tools=TOOLS, tool_choice="auto", max_tokens=500)
out = {"flow_id": flow_id, "ok": res["ok"], "http": res["http"], "model": res["model"], "usage": res["usage"],
       "request_id": res["request_id"], "elapsed_ms": res["elapsed_ms"], "error": res["error"],
       "tool_calls": res["tool_calls"], "content": (res["content"] or "")[:300], "local_lookup": None, "verdict": None}
tc = res.get("tool_calls") or []
if res["ok"] and tc:
    fn = tc[0].get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {"_raw": fn.get("arguments")}
    step = args.get("step_id")
    if fn.get("name") == "lookup_conditions" and step in ("s1", "s2", "s3", "s4", "s5"):
        rules = P.load_rules()
        st = next(s for s in rules["steps"] if s["id"] == step)
        out["local_lookup"] = {"step_id": step, "conditions": [{"id": r, "kind": rules["rules"][r]["kind"], "source_url": rules["rules"][r]["source_url"]} for r in st["rules"]]}
        out["verdict"] = "TOOL_CALL_VALID_LOCAL_LOOKUP_DONE_NO_PAYMENT"
    else:
        out["verdict"] = f"TOOL_CALL_INVALID name={fn.get('name')} args={args}"
elif res["ok"]:
    out["verdict"] = "NO_TOOL_CALL (model answered in text)"
else:
    out["verdict"] = "CALL_FAILED"
p = pathlib.Path(__file__).resolve().parent / "logs" / f"flow_tools_{flow_id}.json"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k: out[k] for k in ("flow_id", "ok", "http", "model", "usage", "verdict", "error")}, ensure_ascii=False, indent=1))
print("tool_calls:", json.dumps(tc, ensure_ascii=False)[:400])
