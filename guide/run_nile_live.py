#!/usr/bin/env python3
"""일반 Nile USDT 실경로 CLI(테스트넷 전용). 서명은 TronLink(/sign 화면) 가 하고 이 CLI 는 서명하지 않는다.

  prepare  : 읽기 전용. 노드 실조회(잔액·에너지·단가) → 규칙(절대 기한) → 후보 계획(TRX 총상한 = Energy 한도 + Bandwidth 최대) → 미서명 거래 작성·독립 대조 →
             **아티팩트**(원본 raw_data_hex/txID + 사람이 볼 전체 조건 + confirm_sha256 + AI 기록 참조) 저장. 방송·서명·원장 예약 없음.
             --ai-record: 안내 화면이 저장한 실제 Kiln 결과(guide/logs/plan_results/<flow>.json)를 검증해 이어받는다(두 번째 호출 없음).
  execute  : 실제 1건. --artifact + --confirm-digest(사람이 prepare 요약에서 읽은 지문) + --confirmed-by. 아티팩트의 원본 바이트·절대 기한만 사용(재작성·연장 없음).
             AI 모드: --ai-record(AI_CARRIED) | --kiln(AI_LIVE, 승인 잔여 범위) | --manual-tech-check(AI 미사용 기술 검사, A 시연 집계 제외). 자동 전환 없음.
             주문 PENDING → /sign 서명 대기(TronLink) → 서버 검증 → 원장 예약 → 방송 1회 → 같은 txID 조회 → 영수증 대조. 결과 JSON 저장.
  resolve  : 열린 지급을 저장된 txID 로만 조회해 종결(재방송 없음).
공통: 메인넷·실자산 사용 없음(노드 URL Nile 고정). 비밀값 없음(키는 지갑에만). 조작자는 --confirmed-by 로 기록(기존 허용 주체).
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))

import demo_flows as D  # noqa: E402
import nile_executor as NX  # noqa: E402
from executor import ApprovalLedger  # noqa: E402
from order_store import OrderStore  # noqa: E402
from signer import FileSigner  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402
from safebatch.policy import PaymentPolicy  # noqa: E402

LOGS = HERE / "logs"
SENDER_DEFAULT = "TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz"      # 9/21 사장 허용 Nile 테스트 지갑(전무는 키 열람 없음)
ARTIFACT_VERSION = 3      # v3(=2): 대역폭 285B 산식 → 실행 보류. v3 부터 과금 바이트(전송+64) 기준; 옛 버전 아티팩트는 execute 가 거부한다.


def fmt6(u: int) -> str:
    q, r = divmod(int(u), 10 ** 6); return f"{q}.{r:06d}"


def make_rules(a, deadline_ts: int | None = None) -> D.UserRules:
    return D.UserRules(goal=a.goal, guide_plan_id=a.guide_plan_id, receiver=a.receiver, mode=a.mode, amount_units=a.amount_units,
                       budget_total_units=a.budget_units, min_receive_units=a.min_receive_units,
                       deadline_ts=int(deadline_ts if deadline_ts is not None else int(time.time()) + a.deadline_s),
                       allowlist=(a.receiver,), path="nile_trc20", trx_fee_cap_sun=a.trx_cap_sun)


def load_ai_record(path: str | None) -> tuple[dict | None, str | None]:
    if not path:
        return None, None
    p = pathlib.Path(path)
    rec = json.loads(p.read_text(encoding="utf-8"))
    ref = f"{p.name}:{hashlib.sha256(p.read_bytes()).hexdigest()[:16]}"
    return rec, ref


def change_block(prior: dict | None, rules: D.UserRules, user_reason: str | None) -> dict | None:
    """적응 흐름(9/29): 이전 아티팩트(rules/plan)와 새 규칙의 차이를 코드가 계산해 '전/후/이유' 로 남긴다. 금액 조정은 mode=max_within_budget 에서만 코드가 한다."""
    if prior is None:
        return None
    pr = prior.get("rules") or {}; pp = prior.get("plan") or {}
    before = {"mode": pr.get("mode"), "amount_units": pr.get("amount_units"), "budget_total_units": pr.get("budget_total_units"), "min_receive_units": pr.get("min_receive_units"),
              "deadline_ts": pr.get("deadline_ts"), "receiver": pr.get("receiver"), "trx_fee_cap_sun": pr.get("trx_fee_cap_sun"), "planned_value_units": pp.get("value_units")}
    after = {"mode": rules.mode, "amount_units": rules.amount_units, "budget_total_units": rules.budget_total_units, "min_receive_units": rules.min_receive_units,
             "deadline_ts": rules.deadline_ts, "receiver": rules.receiver, "trx_fee_cap_sun": rules.trx_fee_cap_sun}
    diff = [k for k in after if before.get(k) != after[k]]
    bt = f"{fmt6(before['amount_units'] or 0)} USDT 요청 · 예산 {fmt6(before['budget_total_units'] or 0)} · 모드 {before['mode']} · 계획 수량 {fmt6(before['planned_value_units'] or 0)}"
    at = f"{fmt6(after['amount_units'])} USDT 요청 · 예산 {fmt6(after['budget_total_units'])} · 최소 {fmt6(after['min_receive_units'])} · 모드 {after['mode']}"
    return {"prior_artifact_txid": (prior.get("unsigned") or {}).get("txID"), "prior_confirm_sha256": prior.get("confirm_sha256"), "before": before, "after": after,
            "changed_fields": diff, "user_reason": user_reason, "before_text": bt, "after_text": at,
            "adjust_rule": "금액 조정은 mode=max_within_budget(예산 안에서 금액 조절 허용) 에서만 코드가 계산한다. exact_amount 이면 조정 없이 거절된다."}


def prepare_artifact(node: NT.NileNode, rules: D.UserRules, sender: str, *, ai_record: dict | None = None, ai_record_ref: str | None = None,
                     revision: int = 1, now: int | None = None, kiln_log: pathlib.Path = NX.KILN_LOG, prior: dict | None = None, change_reason: str | None = None) -> dict:
    """읽기 전용 준비. 반환 아티팩트(dict). 후보가 없으면 decline_reason 만 담는다. prior(이전 아티팩트)가 있으면 change(전/후/이유)를 담는다."""
    now = int(now if now is not None else time.time())
    q = NX.quote_from_node(node, sender, rules.receiver, rules.amount_units, now_fn=lambda: now)
    cands = D.candidate_plans(rules, 0, now, q.trx_quote())
    art = {"artifact_version": ARTIFACT_VERSION, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "network": "nile", "chain_id": NT.NILE_CHAIN_ID,
           "sender": sender, "rules": dataclasses.asdict(rules), "quote": q.to_dict(), "revision": revision, "ai_record_ref": ai_record_ref,
           "ai_record_check": None, "plan": None, "unsigned": None, "confirm_sha256": None, "human_readable": None,
           "decline_reason": None if cands else D.decline_reason(rules, 0, now, q.trx_quote()), "change": change_block(prior, rules, change_reason),
           "note": "prepare = 읽기 전용. 서명·방송·원장 예약 없음. execute 는 이 아티팩트의 원본 바이트·절대 기한·지문만 사용한다."}
    if ai_record is not None:
        ch, meta = NX.kiln_choose_from_record(ai_record, kiln_log=kiln_log)(cands or (), rules)
        art["ai_record_check"] = {"ok": meta.get("ok"), "why": meta.get("why"), "model": meta.get("model"), "request_id": meta.get("request_id"), "flow_id": meta.get("flow_id")}
    if not cands:
        return art
    plan = cands[0]
    spec = NT.TransferSpec(sender=sender, receiver=plan.receiver, token=NT.NILE_USDT, amount_units=plan.value_units, fee_limit_sun=plan.trx_fee_limit_sun,
                           expire_at_ms=int(rules.deadline_ts) * 1000)
    unsigned = NT.build_unsigned(node, spec, now_ms=now * 1000, max_expiry_ms=NX.MAX_SIGN_WINDOW_S * 1000 + 60_000)
    # 대역폭 산정 근거 대조: 견적(raw 211B 가정)의 과금 바이트 == 실제 미서명 바이트로 계산한 과금 바이트(전송 + 64 × 계약 수). 다르면 아티팩트를 만들지 않는다.
    actual_tx = NT.signed_tx_size_bytes(unsigned["raw_data_hex"], 1)
    actual_bw = NT.charged_bandwidth_bytes(unsigned["raw_data_hex"], 1, len(unsigned["raw_data"]["contract"]))
    art["bandwidth_basis_check"] = {"pass": actual_bw == q.bandwidth_bytes, "rule": NT.BANDWIDTH_RULE, "tx_bytes_actual": actual_tx,
                                    "result_reserve_bytes": NT.MAX_RESULT_SIZE_IN_TX, "contracts": len(unsigned["raw_data"]["contract"]),
                                    "bandwidth_bytes_actual": actual_bw, "bandwidth_bytes_quoted": q.bandwidth_bytes}
    if actual_bw != q.bandwidth_bytes:
        art["decline_reason"] = f"bandwidth basis mismatch: quoted {q.bandwidth_bytes}B vs actual {actual_bw}B (no artifact)"
        return art
    art["plan"] = dataclasses.asdict(plan)
    if art.get("change"):
        art["change"]["after"]["planned_value_units"] = plan.value_units
        art["change"]["after_text"] += f" · 계획 수량 {fmt6(plan.value_units)}"
    art["unsigned"] = {k: unsigned[k] for k in ("txID", "raw_data", "raw_data_hex")}
    art["unsigned"]["ref_block_number"] = unsigned.get("ref_block_number"); art["checks"] = unsigned["checks"]
    art["confirm_payload"] = NX.confirm_payload(rules, plan, sender, unsigned, revision, ai_record_ref)
    art["confirm_sha256"] = NX.confirm_digest(rules, plan, sender, unsigned, revision, ai_record_ref)
    art["human_readable"] = {
        "네트워크": f"Nile 테스트넷 chainId {NT.NILE_CHAIN_ID} (테스트 자금, 실제 거래소 입금 아님)", "보내는 계정": sender, "받는 주소": plan.receiver,
        "자체 전송 여부": "예 — 같은 지갑으로 보내는 기술 검증(수취인 순증가 없음)" if plan.receiver == sender else "아니오",
        "토큰 계약": f"{NT.NILE_USDT} (TetherToken, Nile USDT)", "전송 수량(USDT)": f"{fmt6(plan.value_units)} USDT (최소단위 {plan.value_units})",
        "USDT 예산/최소/모드": f"{fmt6(rules.budget_total_units)} / {fmt6(rules.min_receive_units)} / {rules.mode}", "USDT 수수료": "0 (일반 전송)",
        "TRX 총지출 상한": f"{fmt6(rules.trx_fee_cap_sun)} TRX", "Energy 한도(fee_limit)": f"{fmt6(plan.trx_fee_limit_sun)} TRX (예상 Energy {fmt6(plan.trx_est_sun)} = {q.energy_est}×{q.energy_price_sun} sun×1.3)",
        "Bandwidth 최대": f"{fmt6(plan.trx_bandwidth_max_sun)} TRX (과금 {q.bandwidth_bytes} B = 전송 {q.tx_size_bytes} B + 결과 상한 {NT.MAX_RESULT_SIZE_IN_TX} B, × {q.bandwidth_price_sun} sun, 무료 대역폭 소진 가정)",
        "대역폭 산정 근거": f"{NT.BANDWIDTH_RULE}; 실제 미서명 바이트 재계산 {actual_bw} B 일치",
        "이 거래 최대 비용": f"{fmt6(plan.trx_total_max_sun)} TRX ≤ 상한", "지금 예상 총비용": f"{fmt6(q.expected_total_sun)} TRX (무료 대역폭 {q.free_net_left} B 남음)",
        "지갑 잔액(조회)": f"TRX {fmt6(q.trx_sun)} · USDT {fmt6(q.usdt_units)}", "절대 기한(=서명·전송 만료)": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rules.deadline_ts)),
        "revision": revision, "guide_plan_id": rules.guide_plan_id, "AI 기록": ai_record_ref or "없음(수동 기술 검사 또는 실호출)",
        "txID(미서명)": unsigned["txID"], "raw_data_hex": unsigned["raw_data_hex"], "서버 사전 검사": f"{sum(c['pass'] for c in unsigned['checks'])}/{len(unsigned['checks'])} 통과",
        "확인 지문(confirm_sha256)": art["confirm_sha256"],
    }
    if art.get("change"):
        art["human_readable"]["조건 변경(전 → 후 · 이유)"] = f"{art['change']['before_text']} → {art['change']['after_text']} · 이유: {change_reason or '-'} · 바뀐 항목 {art['change']['changed_fields']}"
    return art


def cmd_prepare(a):
    node = NT.NileNode()
    rules = make_rules(a)
    ai_record, ref = load_ai_record(a.ai_record)
    prior = json.loads(pathlib.Path(a.prior_artifact).read_text(encoding="utf-8")) if a.prior_artifact else None
    art = prepare_artifact(node, rules, a.sender, ai_record=ai_record, ai_record_ref=ref, revision=a.revision, prior=prior, change_reason=a.change_reason)
    if a.preview_store and art.get("unsigned"):
        store = OrderStore(pathlib.Path(a.preview_store)); plan = D.Plan(**art["plan"])
        q = NX.quote_from_node(node, a.sender, rules.receiver, rules.amount_units)
        od = NX.make_nile_order(plan=plan, rules=rules, sender=a.sender, unsigned={**art["unsigned"], "checks": art["checks"]}, quote=q, approval_id="PREVIEW-NOT-APPROVED",
                                batch_id="preview", revision=0, csv_sha256="0" * 64, payment_id=f"preview:{art['unsigned']['txID'][:12]}", row_no=1,
                                memo="미리보기(승인 전)", expire_at_ms=int(rules.deadline_ts) * 1000, confirm_sha256=art["confirm_sha256"], ai_mode="PREVIEW")
        od["notice"] = "미리보기 주문(승인 전). 실행기가 대기하지 않으므로 서명해도 방송되지 않습니다. " + od["notice"]
        store.put_pending(od); art["preview_store"] = str(a.preview_store); art["preview_payment_id"] = od["payment_id"]
    path = pathlib.Path(a.out); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(art, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: art[k] for k in ("at", "decline_reason", "ai_record_check", "human_readable")}, ensure_ascii=False, indent=1))
    print(f"saved {path}")


def cmd_execute(a):
    node = NT.NileNode()
    art = json.loads(pathlib.Path(a.artifact).read_text(encoding="utf-8"))
    if art.get("artifact_version") != ARTIFACT_VERSION or not art.get("unsigned") or not art.get("confirm_sha256"):
        print("[중단] 아티팩트 형식/내용 부족(미서명 거래·지문 없음)"); return
    if a.confirm_digest != art["confirm_sha256"]:
        print(f"[중단] 사람이 읽은 지문({a.confirm_digest[:16]}…) 이 아티팩트 지문({art['confirm_sha256'][:16]}…) 과 다릅니다 → 실행 없음"); return
    rules = D.UserRules(**{**art["rules"], "allowlist": tuple(art["rules"]["allowlist"])})     # 절대 기한 그대로(재생성·연장 없음)
    if int(time.time()) >= rules.deadline_ts:
        print("[중단] 아티팩트의 절대 기한이 지났습니다 → 새 prepare·검수 필요(자동 연장 없음)"); return
    sender = art["sender"]
    modes = [m for m, on in (("ai_record", bool(a.ai_record)), ("kiln", a.kiln), ("manual", a.manual_tech_check), ("reuse_flow", bool(a.reuse_flow))) if on]
    if len(modes) != 1:
        print("[중단] AI 모드는 --ai-record | --kiln | --manual-tech-check | --reuse-flow 중 정확히 하나"); return
    ai_record, ref = load_ai_record(a.ai_record)
    if (ref or None) != (art.get("ai_record_ref") or None):
        print(f"[중단] 아티팩트의 AI 기록 참조({art.get('ai_record_ref')}) 와 지금 준 기록({ref}) 이 다릅니다"); return
    ai_info: dict = {}                                                       # 서명 화면 'AI가 한 일' (9/29)
    if a.kiln:
        import kiln_choose as KC
        ai_info["change"] = art.get("change")
        def kiln_choose(c, r):
            ch, meta = KC.choose(c, r, flow_id=a.flow_id, context={"change": art.get("change")} if art.get("change") else None)
            ai_info.update({"mode": "AI_LIVE", "model": meta.get("model"), "request_id": meta.get("request_id"), "usage": meta.get("usage"), "flow_id": a.flow_id,
                            "choice_index": (ch or {}).get("choice_index") if isinstance(ch, dict) else None, "reason": (ch or {}).get("reason") if isinstance(ch, dict) else None,
                            "ok": meta.get("ok"), "raw": meta.get("raw")})
            return ch, meta
    elif ai_record is not None:
        ai_info["change"] = art.get("change")
        if ai_record.get("flow_id") != a.flow_id:
            print(f"[중단] --flow-id({a.flow_id}) 는 AI 기록의 flow_id({ai_record.get('flow_id')}) 와 같아야 합니다(같은 flow 로만 이어받음)"); return
        kiln_choose = NX.kiln_choose_from_record(ai_record, expect_flow_id=a.flow_id)
        ai_info.update({"mode": "AI_CARRIED", "record_ref": ref, "model": (ai_record.get("kiln") or {}).get("model"), "request_id": (ai_record.get("kiln") or {}).get("request_id")})
    elif a.reuse_flow:
        prior_flow = json.loads(pathlib.Path(a.reuse_flow).read_text(encoding="utf-8"))
        _kc = NX.kiln_choose_from_flow(prior_flow, artifact=art, intent_log=IntentLog(LOGS / "nile_intents.jsonl"), order_store=OrderStore(HERE / "pending"))
        def kiln_choose(c, r):
            ch, meta = _kc(c, r)
            ai_info.update({"request_id": meta.get("request_id") or (f"call_id:{meta.get('call_id')}" if meta.get("call_id") else None), "call_ts": meta.get("call_ts"),
                            "ok": meta.get("ok"), "why": meta.get("why")})
            return ch, meta
        ai_info.update({"mode": "AI_CARRIED", "record_ref": f"flow:{prior_flow.get('flow_id')}", "model": (prior_flow.get("kiln") or {}).get("model"),
                        "reason": (prior_flow.get("chosen") or {}).get("reason"), "choice_index": (prior_flow.get("chosen") or {}).get("choice_index"),
                        "change": art.get("change"), "additional_calls": 0})
    else:
        kiln_choose = NX.manual_tech_check_choose
        ai_info.update({"mode": "MANUAL_TECH_CHECK"})
    LOGS.mkdir(exist_ok=True)
    policy = PaymentPolicy(budget_units=rules.budget_total_units, fee_units=0, allowlist=frozenset({rules.receiver}))
    intent_log = IntentLog(LOGS / "nile_intents.jsonl"); approvals = ApprovalLedger(LOGS / "nile_approvals.jsonl")
    store = OrderStore(HERE / "pending")
    signer = FileSigner(store, timeout_s=max(60, rules.deadline_ts - int(time.time())))

    def confirm(summary):
        print("[사람 확인 요약]", json.dumps({k: summary[k] for k in ("sender", "receiver", "amount_units", "trx_total_cap_sun", "energy_fee_limit_sun", "bandwidth_max_sun", "deadline_ts", "tx_id", "confirm_sha256", "ai_mode")}, ensure_ascii=False))
        print("[AI가 한 일]", json.dumps({k: ai_info.get(k) for k in ("mode", "model", "request_id", "choice_index", "reason", "ok")}, ensure_ascii=False))
        if summary["confirm_sha256"] != a.confirm_digest:
            print("[중단] 요약 지문이 사람이 읽은 지문과 다릅니다 → 승인하지 않음"); return None
        return a.confirmed_by

    rec = NX.run_nile_flow(rules, policy=policy, batch_id=a.batch_id, intent_log=intent_log, approvals=approvals, node=node, sender=sender,
                           quote_fn=lambda: NX.quote_from_node(node, sender, rules.receiver, rules.amount_units), human_confirm=confirm,
                           human_sign=signer.sign, kiln_choose=kiln_choose, flow_id=a.flow_id, revision=art.get("revision", 1), artifact=art, ai_record_ref=ref,
                           on_refused=signer.refuse, ai_info=ai_info)
    rec["ai_info"] = ai_info
    rec["artifact"] = str(a.artifact); rec["confirmed_by"] = a.confirmed_by
    path = pathlib.Path(a.out); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: rec.get(k) for k in ("flow_id", "revision", "outcome", "reason", "executed", "ai_mode", "a_demo_eligible")}, ensure_ascii=False))
    if rec.get("execution"):
        d = rec["execution"]["detail"] or {}
        print("execution:", rec["execution"]["state"], d.get("outcome"), "tx", d.get("tx_hash"), "reason", d.get("reason"))
        human = {"CONFIRMED": "확정됨(solidity). 입금이 아니라 테스트넷 자기 지갑 이동.", "ACCEPTED": "노드 접수됨 — 아직 확정 아님. 성공이라고 보지 말고 resolve 로 같은 txID 만 조회.",
                 "UNKNOWN": "결과 불명 — 다시 보내면 두 번 보낼 수 있음. resolve 로 같은 txID 만 조회.", "NOT_SUBMITTED": "제출 안 됨(체인 기록 없음) — 새 실행 ID 로 재시도 가능.",
                 "REJECTED": "노드가 거절(체인 기록 없음). 이 실행 ID 는 닫힘.", "FAILED": "체인에서 실패로 기록됨. 재전송 없음.", "MISMATCH": "영수증이 조건과 다름 — 사람 확인 필요."}.get(rec["execution"]["state"])
        if human: print("[상태 뜻]", human)
    print(f"saved {path}")


def decline_flow(node: NT.NileNode, rules: D.UserRules, sender: str, *, flow_id: str, kiln: bool, now: int | None = None, chat_fn=None,
                 pending_dir: pathlib.Path | None = None, intents_path: pathlib.Path | None = None) -> dict:
    """거절 흐름(9/29 VP): 코드가 후보 0 을 강제 → (검사) 주문·미서명·원장 기록 없음 → Kiln 은 이유 설명만(kiln=True 일 때 1회) → 결과 기록. 주문·서명·방송 0.
    후보가 하나라도 있으면 거절 시나리오가 아니므로 **호출 전에** 중단한다."""
    now = int(now if now is not None else time.time())
    store = OrderStore(pending_dir or (HERE / "pending")); ilog = IntentLog(intents_path or (LOGS / "nile_intents.jsonl"))
    before = {"pending_count": len(store.pending_orders()), "intents_lines": _count_lines(ilog.path if hasattr(ilog, "path") else (intents_path or (LOGS / "nile_intents.jsonl")))}
    q = NX.quote_from_node(node, sender, rules.receiver, rules.amount_units, now_fn=lambda: now)
    cands = D.candidate_plans(rules, 0, now, q.trx_quote())
    rec = {"flow_id": flow_id, "kind": "decline", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "rules": dataclasses.asdict(rules), "quote": q.to_dict(),
           "candidates": [dataclasses.asdict(c) for c in cands], "code_block": None, "kiln": None, "ai_explanation": None, "order": None, "unsigned": None,
           "signed": 0, "broadcast": 0, "onchain_tx": 0, "ledger_before": before, "ledger_after": None, "outcome": None,
           "ai_work": {"code": ["규칙(수량·예산·모드·허용 수취인·기한·TRX 상한)으로 후보 계산 → 후보 0 = 지급 불가(코드 강제, 모델 무관)", "미서명 거래·주문·intent·승인 생성 없음(호출 전후 원장 대조)",
                                "모델 출력은 decision=decline 형식·허용 키만 검증하며 어떤 제안도 실행에 쓰지 않는다"],
                       "ai": None, "human": "사람 확인·서명 없음(거절이므로 서명 대상 자체가 없다)"}}
    if cands:
        rec["outcome"] = "NOT_A_DECLINE_SCENARIO"; rec["code_block"] = {"blocked": False, "why": "candidate exists → not a decline; no model call"}
        rec["ledger_after"] = {"pending_count": len(store.pending_orders()), "intents_lines": before["intents_lines"]}
        return rec
    reason = D.decline_reason(rules, 0, now, q.trx_quote())
    rec["code_block"] = {"blocked": True, "reason": reason, "enforced_by": "demo_flows.candidate_plans/decline_reason (code)",
                         "user_line": "지급하지 않았습니다. 예산 안으로 자동 감액하지 않습니다(정확한 금액 모드). 규칙을 바꾸기 전에는 서명 대상이 없습니다. 서명 0 · 방송 0."}
    if not kiln:
        rec["outcome"] = "DECLINED_BY_CODE_NO_AI_CALL"; rec["ai_work"]["ai"] = {"mode": "NONE", "did": "호출 안 함(--kiln 없음)"}
    else:
        import kiln_choose as KC
        ex, meta = KC.explain_decline(rules, reason, flow_id=flow_id, chat_fn=chat_fn)
        rec["kiln"] = dict(meta)
        if ex is None or meta.get("ok") is not True:
            rec["outcome"] = "DECLINED_BY_CODE_AI_EXPLANATION_UNAVAILABLE"; rec["ai_work"]["ai"] = {"mode": "AI_LIVE", "did": "호출했으나 응답 무효/실패 — 거절은 코드가 이미 확정", "model": meta.get("model"), "why": meta.get("validation") or meta.get("error")}
        else:
            rec["ai_explanation"] = ex
            rec["outcome"] = "DECLINED_BY_CODE_AI_EXPLAINED"
            rec["ai_work"]["ai"] = {"mode": "AI_LIVE", "did": "코드가 확정한 지급 불가 사유를 사용자에게 설명하는 한 문장을 냈다(결정 권한 없음, 금액 제안 불가)", "model": meta.get("model"),
                                    "request_id": meta.get("request_id"), "reason": ex["reason"], "usage": meta.get("usage"), "not_did": "지급 여부를 결정하지 않았고 후보·금액을 만들지 않았다"}
    rec["ledger_after"] = {"pending_count": len(store.pending_orders()), "intents_lines": _count_lines(intents_path or (LOGS / "nile_intents.jsonl"))}
    rec["ledger_unchanged"] = rec["ledger_after"] == before
    rec["a_demo_eligible"] = rec["outcome"] == "DECLINED_BY_CODE_AI_EXPLAINED"
    return rec


def _count_lines(p: pathlib.Path) -> int:
    try:
        return sum(1 for _ in open(p, encoding="utf-8"))
    except OSError:
        return 0


def cmd_decline(a):
    node = NT.NileNode(); rules = make_rules(a)
    rec = decline_flow(node, rules, a.sender, flow_id=a.flow_id, kiln=a.kiln)
    path = pathlib.Path(a.out); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: rec.get(k) for k in ("flow_id", "outcome", "code_block", "ai_explanation", "signed", "broadcast", "onchain_tx", "ledger_unchanged", "a_demo_eligible")}, ensure_ascii=False, indent=1))
    print(f"saved {path}")


def cmd_resolve(a):
    node = NT.NileNode(); intent_log = IntentLog(LOGS / "nile_intents.jsonl")
    rec = json.loads(pathlib.Path(a.flow_json).read_text(encoding="utf-8"))
    order = rec.get("order")
    if not order:
        print("no order in flow record"); return
    print(json.dumps(NX.resolve_by_txid(node, intent_log, order), ensure_ascii=False, indent=1, default=str))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--sender", default=SENDER_DEFAULT); p.add_argument("--receiver", required=True)
    p.add_argument("--amount-units", type=int, required=True, help="USDT 최소단위(6자리)"); p.add_argument("--budget-units", type=int, required=True)
    p.add_argument("--min-receive-units", type=int, default=0); p.add_argument("--mode", choices=D.MODES, default="exact_amount")
    p.add_argument("--trx-cap-sun", type=int, required=True, help="TRX 총지출 상한(sun, Energy+Bandwidth)"); p.add_argument("--deadline-s", type=int, default=3600)
    p.add_argument("--goal", default="일반 Nile USDT 테스트 전송(연습, 실제 거래소 입금 아님)"); p.add_argument("--guide-plan-id", default="fa4b723f945db52e")
    p.add_argument("--revision", type=int, default=1); p.add_argument("--ai-record", default=None, help="guide/logs/plan_results/<flow>.json")
    p.add_argument("--preview-store", default=None); p.add_argument("--out", required=True)
    p.add_argument("--prior-artifact", default=None, help="적응 흐름: 이전 아티팩트(전/후 비교)"); p.add_argument("--change-reason", default=None, help="사용자가 조건을 바꾼 이유(그대로 기록)")
    e = sub.add_parser("execute")
    e.add_argument("--artifact", required=True); e.add_argument("--confirm-digest", required=True); e.add_argument("--confirmed-by", required=True)
    e.add_argument("--batch-id", required=True); e.add_argument("--flow-id", required=True); e.add_argument("--out", required=True)
    e.add_argument("--ai-record", default=None); e.add_argument("--kiln", action="store_true"); e.add_argument("--manual-tech-check", action="store_true")
    e.add_argument("--reuse-flow", default=None, help="서명 전에 끝난 이전 flow JSON 의 검증된 Kiln 결과를 이어받음(새 호출 0, AI_CARRIED)")
    dcl = sub.add_parser("decline", help="거절 흐름: 코드 차단(후보 0) 확인 뒤 Kiln 이유 설명 1회. 주문·서명·방송 0")
    for arg, kw in (("--sender", dict(default=SENDER_DEFAULT)), ("--receiver", dict(required=True)), ("--amount-units", dict(type=int, required=True)), ("--budget-units", dict(type=int, required=True)),
                    ("--min-receive-units", dict(type=int, default=0)), ("--mode", dict(choices=D.MODES, default="exact_amount")), ("--trx-cap-sun", dict(type=int, required=True)),
                    ("--deadline-s", dict(type=int, default=3600)), ("--goal", dict(default="Nile 테스트 USDT 지급 거절 시연")), ("--guide-plan-id", dict(default="nile-decline")),
                    ("--flow-id", dict(required=True)), ("--kiln", dict(action="store_true")), ("--out", dict(required=True))):
        dcl.add_argument(arg, **kw)
    r = sub.add_parser("resolve"); r.add_argument("--flow-json", required=True)
    a = ap.parse_args()
    {"prepare": cmd_prepare, "execute": cmd_execute, "resolve": cmd_resolve, "decline": cmd_decline}[a.cmd](a)


if __name__ == "__main__":
    main()
