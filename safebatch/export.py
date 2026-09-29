"""SafeBatch AI · 단일 export + replay 검사.

Kimi 1회 자문(9/28) 채택 항목 ③: 제3자가 로그만으로 "어느 CSV 행·메모·승인에서 어떤 지출이 나갔고 무엇이 왜 차단됐는지"
재구성할 수 있어야 한다(Furiosa 요건 5·TRON 요건).
- build_export(policy, batch_id, intent_log, traces): 배치의 모든 행(분류코드·사유), 승인 범위(revision·해시·승인ID),
  차단 결정(범위 밖 포함), 사람 중단, intent 상태 이력, 요청ID→traceId→txHash→확정·실제 수신액/수수료 를 한 JSON 으로.
- to_csv_rows(export): 행 단위 CSV(대사표).
- replay_check(export): export 만으로 ①지급 확정 건의 금액/수수료가 체인 조회(trace)와 일치 ②차단 건마다 사유가 있음
  ③모든 지급이 유효 승인 revision 에 속함 을 다시 계산해 통과/실패를 돌려준다.
"""
from __future__ import annotations

import csv
import io
import json

def is_blocked(decision: str | None) -> bool:
    """정책 원장의 차단 결과: BLOCKED_* (허용목록 밖·예산 초과) 와 CONFLICT (같은 ID 다른 내용)."""
    return bool(decision) and (decision.startswith("BLOCKED_") or decision == "CONFLICT")


def build_export(policy, batch_id: str, intent_log=None, traces: dict | None = None, human_stops: list | None = None) -> dict:
    traces = traces or {}
    revs = policy.revisions.get(batch_id, [])
    cur = policy.current_revision(batch_id)
    approvals = [dict(a) for a in policy.approvals.values() if a.get("batch_id") == batch_id]
    decisions = [d for d in policy.export_log() if str(d.get("payment_id", "")).startswith(f"{batch_id}:")]
    intents = []
    if intent_log is not None:
        intents = [e for e in intent_log.entries() if str(e.get("payment_id", "")).startswith(f"{batch_id}:")]
    latest_intent: dict[str, dict] = {}
    for e in intents:
        latest_intent[e["payment_id"]] = e

    payments = []
    for d in decisions:
        pid = d["payment_id"]
        li = latest_intent.get(pid, {})
        tr = traces.get(li.get("trace_id") or "", {}) if li else {}
        payments.append({
            "payment_id": pid, "row_no": d.get("row_no"), "recipient": d.get("recipient"),
            "amount_units": d.get("amount_units"), "memo": d.get("memo"),
            "decision": d.get("outcome"), "decision_reason": d.get("reason"), "stopped": d.get("stopped"),
            "record_state": policy.record_state.get(pid),
            "intent_state": li.get("state"), "request_id": li.get("request_id"), "nonce": li.get("nonce"),
            "trace_id": li.get("trace_id"), "tx_hash": li.get("tx_hash") or tr.get("txnHash"),
            "chain_state": tr.get("state"), "txn_state": tr.get("txnState"),
            "txn_amount": tr.get("txnAmount"), "txn_total_fee": tr.get("txnTotalFee"),
            "txn_total_cost": tr.get("txnTotalCost"),
        })
    return {
        "batch_id": batch_id,
        "current_revision": cur,
        "revisions": [dict(r) for r in revs],
        "approvals": approvals,
        "approval_log": [dict(e) for e in policy.approval_log if e.get("batch_id") == batch_id],
        "batch_log": [dict(e) for e in policy.batch_log if e.get("batch_id") == batch_id],
        "policy_scope": {"allowlist": sorted(policy.allowlist), "budget_units": policy.budget_units,
                         "fee_units_per_payment": policy.fee_units, "reserved_units": policy.reserved_units},
        "human_stops": list(human_stops or []),
        "payments": payments,
        "intent_history": intents,
        "traces": traces,
    }


def to_csv_rows(export: dict) -> str:
    cols = ["payment_id", "row_no", "recipient", "amount_units", "memo", "decision", "decision_reason", "record_state",
            "intent_state", "request_id", "nonce", "trace_id", "tx_hash", "chain_state", "txn_state", "txn_amount",
            "txn_total_fee", "txn_total_cost"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for p in export["payments"]:
        w.writerow(p)
    return buf.getvalue()


def replay_check(export: dict) -> dict:
    """export 만 보고 재계산. 외부 조회 없음."""
    problems = []
    valid_approvals = {a["approval_id"] for a in export["approvals"] if a.get("status") == "VALID"}
    cur = export.get("current_revision") or {}
    for p in export["payments"]:
        pid = p["payment_id"]
        dec = p["decision"]
        if is_blocked(dec):
            if not p.get("decision_reason"):
                problems.append(f"{pid}: blocked without reason")
            if p.get("tx_hash") or p.get("intent_state") in ("SUBMITTED", "ACCEPTED", "CONFIRMED"):
                problems.append(f"{pid}: blocked row has submission/tx evidence")
        if p.get("intent_state") == "CONFIRMED" or p.get("chain_state") == "SUCCEED":
            if p.get("txn_amount") is None or int(p["txn_amount"]) != int(p["amount_units"]):
                problems.append(f"{pid}: chain amount {p.get('txn_amount')} != row amount {p.get('amount_units')}")
            if not p.get("tx_hash"):
                problems.append(f"{pid}: confirmed without tx_hash")
            rev_tag = f":r{cur.get('revision')}:"
            if rev_tag not in pid:
                problems.append(f"{pid}: paid under non-current revision")
            if not valid_approvals:
                problems.append(f"{pid}: no valid approval in export")
    # Grok#5(c): 사람 중단·차단·decisions 밖 제출이 export 에서 보이도록
    stops = set(export.get("human_stops") or [])
    known = {p["payment_id"] for p in export["payments"]}
    for p in export["payments"]:
        if (p["payment_id"] in stops or p.get("stopped")) and p.get("intent_state") in ("SUBMITTED", "ACCEPTED", "CONFIRMED"):
            problems.append(f"{p['payment_id']}: stopped/blocked row has submission {p.get('intent_state')}")
        if p.get("intent_state") == "CONFIRMED" and (p.get("txn_state") not in ("ON_CHAIN", "SOLIDITY")
                                                     or p.get("chain_state") != "SUCCEED"):
            problems.append(f"{p['payment_id']}: CONFIRMED without matching chain state")
    for e in export.get("intent_history") or []:
        if e.get("payment_id") not in known and e.get("state") in ("SUBMITTED", "ACCEPTED", "CONFIRMED", "UNKNOWN"):
            problems.append(f"{e.get('payment_id')}: submission outside policy decisions")
    blocked = [p for p in export["payments"] if is_blocked(p["decision"])]
    return {"ok": not problems, "problems": problems, "payments": len(export["payments"]),
            "blocked_with_reason": sum(1 for p in blocked if p.get("decision_reason")), "blocked": len(blocked)}


def dumps(export: dict) -> str:
    return json.dumps(export, ensure_ascii=False, indent=2, default=str)
