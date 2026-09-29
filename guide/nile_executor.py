"""일반 Nile USDT(TRC20) 실행 진입점 — 사장 선택 9/28 '일반 Nile 로 Furiosa A 시연 우선'. GasFree 경로(executor.py)는 보존.

흐름: 안내 계획(UserRules path=nile_trc20) → SafeBatch 정책 원장(배치·revision·**전체 확인 지문**·사람 확인 승인) → 미서명 거래(prepare 아티팩트의
원본 바이트 그대로; nile_tx 독립 디코더 대조) → 주문(PENDING, kind=nile_trc20) → 사람이 TronLink 로 `tronWeb.trx.sign` (방송 아님) → 서버가 서명 거래 대조
(raw 바이트 동일·txID·서명자 EOA 복구) → intent SIGNED → **reserve_broadcast(txID)** (계정 단위 잠금·fsync) → broadcasttransaction →
같은 txID 조회 → 영수증 대조(Transfer 로그·총 fee≤총상한·energy_fee≤fee_limit·net_fee≤bandwidth_max·solidity) → CONFIRMED / ACCEPTED / FAILED / REJECTED / UNKNOWN.

9/28 VP 링크 검수 반영:
1) AI: run_flow 가 meta.ok==True 인 모델 결과만 승인/실행으로 잇는다. 실제 안내 화면의 검증된 Kiln 결과를 `kiln_choose_from_record` 로 이어받거나(AI_CARRIED),
   실호출(AI_LIVE), 또는 명시적 `manual_tech_check`(A 시연 집계 제외). 자동 전환 없음.
2) 확인 지문: `confirm_digest(...)` = network/chain·발신/수신·계약·정확 수량·USDT 예산/최소/모드·TRX 총상한·Energy 한도·Bandwidth 최대·**절대 기한**·revision·
   guide_plan_id·AI 기록 참조·raw_data_hex·txID. prepare 아티팩트에 기록 → execute 는 같은 아티팩트(원본 바이트)로만 진행하고 사람이 읽은 지문과 재계산 지문이
   다르면 중단. 정책 승인(display_digest) 에도 이 extra 를 결합한다.
3) 비용: 사용자 TRX 총상한(rules.trx_fee_cap_sun) ≥ Energy fee_limit + Bandwidth 최대(무료 대역폭 소진 가정, **과금 바이트**×대역폭 단가). 견적 재조회에서
   단가/시뮬레이션이 바뀌면 중단. 영수증 총 fee/energy_fee/net_fee/net_usage 를 각 기준과 대조.
4) 대역폭 과금 바이트(9/28 부사장 8차 검수 반영): 전송 바이트(raw 214 + 서명 67 = 281) 가 아니라 java-tron BandwidthProcessor 규칙 **전송 바이트 + 64(MAX_RESULT_SIZE_IN_TX)
   × 계약 수 = 345B** 를 prepare 견적·서명 전 화면·방송 직전 재견적·영수증 대조 네 곳에 같은 함수(nile_tx.charged_bandwidth_bytes)로 쓴다. 총상한 5 TRX 는 그대로,
   Energy fee_limit = 5,000,000 − 345,000 = 4,655,000 sun 으로 줄어든다(옛 285B 산식은 4,715,000 + 345,000 = 5,060,000 > 상한).
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))

from safebatch.flow import run_batch, approve_batch  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch.intent_log import IntentLog, SCOPE_NILE_TRC20  # noqa: E402
import demo_flows as D  # noqa: E402
import plan as P  # noqa: E402
from executor import ApprovalLedger, UsedSet  # noqa: E402

QUOTE_TTL_S = 120
MAX_SIGN_WINDOW_S = 24 * 3600 - 60          # TRON 만료 상한(24h) 안. 실제 만료 = 사용자 절대 기한(아티팩트)
ENERGY_MARGIN_NUM, ENERGY_MARGIN_DEN = 13, 10
SIG_OVERHEAD_BYTES = NT.signed_tx_size_bytes("00" * NT.RAW_LEN_TRANSFER, 1) - NT.RAW_LEN_TRANSFER   # 서명 1개 protobuf 프레이밍(raw 211B → 전송 281B)
BANDWIDTH_BYTES_TRANSFER = NT.charged_bandwidth_bytes("00" * NT.RAW_LEN_TRANSFER, 1, 1)                # 과금 기준 345B = 전송 281 + 64(MAX_RESULT_SIZE_IN_TX)
KILN_LOG = HERE / "logs" / "kiln_calls.jsonl"


class NileQuote:
    """노드 실조회 스냅샷: 잔액·에너지 견적·에너지/대역폭 단가·현재 시각. 실행 직전 재조회와 비교한다."""
    def __init__(self, *, trx_sun: int, usdt_units: int, energy_est: int, energy_price_sun: int, bandwidth_price_sun: int, would_succeed: bool,
                 free_net_left: int, quoted_at: int, sender: str, receiver: str, amount_units: int, raw_len_bytes: int):
        self.trx_sun, self.usdt_units, self.energy_est = int(trx_sun), int(usdt_units), int(energy_est)
        self.energy_price_sun, self.bandwidth_price_sun, self.would_succeed = int(energy_price_sun), int(bandwidth_price_sun), bool(would_succeed)
        self.free_net_left, self.quoted_at = int(free_net_left), int(quoted_at)
        self.sender, self.receiver, self.amount_units, self.raw_len_bytes = sender, receiver, int(amount_units), int(raw_len_bytes)

    @property
    def est_energy_sun(self) -> int:
        """예상 Energy 비용(sun): 에너지×단가×1.3 여유(에너지 스테이킹 0 가정 → 전액 소각)."""
        return (self.energy_est * self.energy_price_sun * ENERGY_MARGIN_NUM + ENERGY_MARGIN_DEN - 1) // ENERGY_MARGIN_DEN

    @property
    def tx_size_bytes(self) -> int:
        """전송(직렬화) 바이트: 서명 1개 가정(raw 211 → 281). 과금 기준이 아니다."""
        return NT.signed_tx_size_bytes("00" * self.raw_len_bytes, 1)

    @property
    def bandwidth_bytes(self) -> int:
        """과금 대역폭 바이트 = 전송 바이트 + 64(MAX_RESULT_SIZE_IN_TX) × 계약 1개 (java-tron BandwidthProcessor). 방송 직전 실측(bandwidth_bytes)과 대조."""
        return NT.charged_bandwidth_bytes("00" * self.raw_len_bytes, 1, 1)

    @property
    def bandwidth_max_sun(self) -> int:
        """무료 대역폭이 그 시점에 소진됐다고 가정한 최대 Bandwidth 소각(sun) = **과금 바이트** × 대역폭 단가."""
        return self.bandwidth_bytes * self.bandwidth_price_sun

    @property
    def expected_total_sun(self) -> int:
        """지금 조회 기준 예상 총비용: Energy 예상 + (무료 대역폭이 과금 바이트에 못 미치면 Bandwidth)."""
        return self.est_energy_sun + (0 if self.free_net_left >= self.bandwidth_bytes else self.bandwidth_max_sun)

    def trx_quote(self) -> dict:
        return {"est_energy_sun": self.est_energy_sun, "bandwidth_max_sun": self.bandwidth_max_sun, "balance_sun": self.trx_sun}

    def key(self) -> tuple:
        return (self.sender, self.receiver, self.amount_units, self.energy_price_sun, self.bandwidth_price_sun, self.would_succeed, self.est_energy_sun)

    def to_dict(self) -> dict:
        return {"trx_sun": self.trx_sun, "usdt_units": self.usdt_units, "energy_est": self.energy_est, "energy_price_sun": self.energy_price_sun,
                "bandwidth_price_sun": self.bandwidth_price_sun, "tx_size_bytes": self.tx_size_bytes, "bandwidth_bytes": self.bandwidth_bytes,
                "bandwidth_rule": NT.BANDWIDTH_RULE, "est_energy_sun": self.est_energy_sun,
                "bandwidth_max_sun": self.bandwidth_max_sun, "expected_total_sun": self.expected_total_sun, "would_succeed": self.would_succeed,
                "free_net_left": self.free_net_left, "quoted_at": self.quoted_at}


def quote_from_node(node: NT.NileNode, sender: str, receiver: str, amount_units: int, now_fn=time.time, raw_len_bytes: int | None = None) -> NileQuote:
    snap = NT.account_snapshot(node, sender)
    est = NT.estimate_transfer_energy(node, sender, receiver, amount_units)
    prices = NT.chain_prices(node)
    if snap["usdt_units"] < amount_units:
        raise RuntimeError(f"USDT balance {snap['usdt_units']} below amount {amount_units}")
    if not est["would_succeed"]:
        raise RuntimeError(f"transfer simulation would fail: {est.get('message')}")
    if raw_len_bytes is None:                                  # 이 고정 거래(transfer 68B data)의 raw 길이: 실측 211B
        raw_len_bytes = NT.RAW_LEN_TRANSFER
    return NileQuote(trx_sun=snap["trx_sun"], usdt_units=snap["usdt_units"], energy_est=est["energy_used"], energy_price_sun=prices["energy_sun"],
                     bandwidth_price_sun=prices["bandwidth_sun"], would_succeed=est["would_succeed"], free_net_left=snap["free_net_limit"] - snap["free_net_used"],
                     quoted_at=int(now_fn()), sender=sender, receiver=receiver, amount_units=amount_units, raw_len_bytes=raw_len_bytes)


# ── 확인 지문(사람이 본 전체 조건 ↔ 실제 서명 대상) ─────────────────────────────
def confirm_payload(rules: D.UserRules, plan: D.Plan, sender: str, unsigned: dict | None, revision: int, ai_record_ref: str | None) -> dict:
    return {"network": "nile", "chain_id": NT.NILE_CHAIN_ID, "sender": sender, "receiver": plan.receiver, "token": NT.NILE_USDT,
            "amount_units": plan.value_units, "usdt_fee_units": plan.fee_units, "usdt_budget_units": rules.budget_total_units,
            "min_receive_units": rules.min_receive_units, "mode": rules.mode, "trx_total_cap_sun": rules.trx_fee_cap_sun,
            "energy_fee_limit_sun": plan.trx_fee_limit_sun, "bandwidth_max_sun": plan.trx_bandwidth_max_sun, "trx_total_max_sun": plan.trx_total_max_sun,
            "bandwidth_rule": NT.BANDWIDTH_RULE,
            "deadline_ts": rules.deadline_ts, "revision": revision, "guide_plan_id": rules.guide_plan_id, "ai_record_ref": ai_record_ref,
            "raw_data_hex": (unsigned or {}).get("raw_data_hex"), "tx_id": (unsigned or {}).get("txID")}


def confirm_digest(*a, **kw) -> str:
    return hashlib.sha256(json.dumps(confirm_payload(*a, **kw), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ── 이전 flow 의 검증된 Kiln 선택 이어받기(새 호출 없음) ─────────────────────────
def kiln_choose_from_flow(flow_rec: dict, *, artifact: dict, kiln_log: pathlib.Path = KILN_LOG, intent_log: IntentLog | None = None, order_store=None,
                          expected_model: str = "qwen3-32b"):
    """실행 단계 결함(예: 9/29 revision 지문 불일치)으로 **서명 전에** 끝난 flow 의 검증된 Kiln 결과를 새 호출 없이 이어받는다(AI_CARRIED).
    거부 조건(9/29 VP 검수 반영):
      ① 이전 flow 가 서명 전에 끝났는가: executed False · execution.state NOT_SUBMITTED · order 없음 · 그 payment_id 의 intent 가 SIGNED/SUBMITTED/ACCEPTED/CONFIRMED/UNKNOWN/FAILED 이 아님 ·
         주문 저장소에 서명/소비/격리 상태 없음 → 하나라도 어긋나면 거부.
      ② 실제 성공한 호출과 연결: kiln_calls.jsonl 에 같은 flow_id 의 기록이 ok True·http 200·model==지정 모델 이고, request_id 가 있으면 일치(없으면 call_id 를 식별자로 쓰고 새로 만들지 않음),
         보존된 원응답(flow.kiln.raw)을 다시 파싱한 선택 index·이유가 flow.chosen 과 같아야 한다.
      ③ 규칙 digest·후보 plan digest·아티팩트 확인 지문이 지금과 같아야 한다."""
    def choose(cands, rules):
        fr = flow_rec or {}; k = fr.get("kiln") or {}; ch = fr.get("chosen") or {}
        meta = {"ok": False, "carried": True, "reused_from_flow": fr.get("flow_id"), "model": k.get("model"), "request_id": k.get("request_id"), "call_id": None,
                "usage": k.get("usage"), "why": None, "additional_calls": 0}
        def fail(w):
            meta["why"] = w; return None, meta
        # ① 서명 전 종료 강제
        ex = fr.get("execution") or {}
        if fr.get("executed") is not False or (ex.get("state") != "NOT_SUBMITTED") or fr.get("order"):
            return fail(f"prior flow did not end before signing (executed={fr.get('executed')}, state={ex.get('state')}, order={'yes' if fr.get('order') else 'no'})")
        pid = (ex.get("detail") or {}).get("payment_id")
        if intent_log is not None and pid:
            st = intent_log.state(pid)
            if st in ("SIGNED", "SUBMITTED", "ACCEPTED", "CONFIRMED", "UNKNOWN", "FAILED", "REJECTED"):
                return fail(f"prior payment {pid} has signing/submission history ({st})")
        if order_store is not None and pid:
            od = order_store.get(pid)
            if od and od.get("state") in ("SIGNED", "SIGNED_VERIFIED", "CONSUMED", "QUARANTINED", "SIGNED_REFUSED_NOT_BROADCAST"):
                return fail(f"prior order {pid} is {od.get('state')}")
        # ② 실제 성공 호출과 연결
        if k.get("ok") is not True or k.get("validation") != "ok" or not ch:
            return fail("prior flow kiln result not ok/validated")
        try:
            entries = [json.loads(l) for l in kiln_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            entries = []
        hits = [e for e in entries if e.get("flow_id") == fr.get("flow_id") and e.get("ok") is True and e.get("http") == 200 and e.get("model") == expected_model]
        if k.get("request_id"):
            hits = [e for e in hits if e.get("request_id") == k.get("request_id")]
        if len(hits) != 1:
            return fail(f"prior call not uniquely found in kiln log (ok/http200/model={expected_model}/request_id): {len(hits)}")
        meta["call_id"] = hits[0].get("call_id"); meta["call_ts"] = hits[0].get("ts")
        if k.get("model") != expected_model:
            return fail(f"prior flow model {k.get('model')} != {expected_model}")
        parsed = P.extract_json(k.get("raw") or "")
        if not isinstance(parsed, dict) or parsed.get("choice_index") != ch.get("choice_index") or str(parsed.get("reason", ""))[:300] != str(ch.get("reason", ""))[:300]:
            return fail("preserved raw response does not reproduce the recorded choice/reason")
        # ③ 같은 규칙·계획·지문
        if fr.get("rules_digest") != rules.digest():
            return fail("rules digest differs from prior flow")
        idx = ch.get("choice_index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not cands or not (0 <= idx < len(cands)) or ch.get("plan_digest") != cands[idx].digest():
            return fail("plan digest differs from prior flow")
        if fr.get("confirm_sha256") != artifact.get("confirm_sha256"):
            return fail("artifact confirm digest differs from prior flow")
        meta["ok"] = True
        return {"choice_index": idx, "reason": str(ch.get("reason") or "")[:300]}, meta
    return choose


# ── AI 결과 이어받기 ──────────────────────────────────────────────────────────
def kiln_choose_from_record(record: dict, *, kiln_log: pathlib.Path = KILN_LOG, expect_flow_id: str | None = None):
    """안내 화면(/api/plan)이 저장한 실제 Kiln 결과(guide/logs/plan_results/*.json)를 검증된 기록으로 이어받는 kiln_choose 콜백.
    조건: kiln.outcome VALIDATED·used, ai_work.ai_status used/partially_used, 다음 행동이 '계획 제안'(AI 설명 사용), request_id 가 kiln_calls.jsonl 에 존재,
    record.plan_id == rules.guide_plan_id. 하나라도 어긋나면 meta.ok False → run_flow 는 PLAN_ONLY 로 끝낸다(수동 전환 없음)."""
    def choose(cands, rules):
        k = (record or {}).get("kiln") or {}
        aw = (record or {}).get("ai_work") or {}
        meta = {"ok": False, "carried": True, "model": k.get("model"), "request_id": k.get("request_id"), "flow_id": record.get("flow_id"), "why": None,
                "model_role": "intent structuring + next-action proposal in the guide flow; the transfer candidate is computed by code (single candidate), not chosen by the model"}
        why = None
        if record.get("plan_id") != rules.guide_plan_id:
            why = "record plan_id != rules.guide_plan_id"
        elif expect_flow_id is not None and record.get("flow_id") != expect_flow_id:
            why = f"record flow_id {record.get('flow_id')} != execution flow_id {expect_flow_id}"           # Grok-02#2: 같은 flow 로만 이어받는다
        elif k.get("outcome") != "VALIDATED" or not k.get("used"):
            why = f"kiln outcome {k.get('outcome')} / used {k.get('used')}"
        elif aw.get("ai_status") != "used":                                                                 # partially_used 는 성공으로 보지 않는다
            why = f"ai_work status {aw.get('ai_status')} (only 'used' carries)"
        elif not (aw.get("raw_ai") or {}).get("next_action") == "propose_plan":
            why = "model raw next_action is not propose_plan"
        else:
            item = next((i for i in aw.get("items", []) if i.get("action") == "다음 행동 제안"), None)
            if not item or not str(item.get("by", "")).startswith("AI(qwen3-32b)"):
                why = "no AI-backed propose_plan item"
            elif not _request_id_logged(k.get("request_id"), kiln_log):
                why = "request_id not found in kiln_calls.jsonl"
        if why:
            meta["why"] = why
            return None, meta
        meta["ok"] = True
        summ = next((i.get("summary") for i in aw.get("items", []) if i.get("action") == "다음 행동 제안"), "")
        return {"choice_index": 0, "reason": str(summ or "")[:300]}, meta
    return choose


def _request_id_logged(request_id, kiln_log: pathlib.Path) -> bool:
    if not request_id or not pathlib.Path(kiln_log).exists():
        return False
    for line in pathlib.Path(kiln_log).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if request_id in (row.get("request_id"), row.get("call_id")):
            return True
    return False


def manual_tech_check_choose(cands, rules):
    """명시적 'AI 미사용 기술 검사' 모드: A 시연 성공으로 집계되지 않는다(run_flow.a_demo_eligible False)."""
    return {"choice_index": 0, "reason": "AI 미사용 기술 검사(단일 후보)"}, {"ok": True, "manual_tech_check": True, "model": None}


# ── 주문 ──────────────────────────────────────────────────────────────────
def build_ai_work(ai_info: dict | None, plan: D.Plan, rules: D.UserRules, ai_mode: str | None) -> dict:
    """서명 화면용 'AI가 한 일 / 코드가 검사한 일' 구분(9/29 VP: 같은 목표·흐름에서 사용자 요청→Kiln 해석·제안→코드 검증→사람 확인→실행)."""
    info = dict(ai_info or {})
    mode = info.get("mode") or ai_mode or "-"
    if mode == "AI_LIVE":
        ai = {"mode": mode, "did": "사용자 목표·조건과 코드가 만든 후보(1개)를 읽고, 규칙에 맞는지 설명하는 한 문장을 냈다(후보 선택 index 포함). 금액·주소·수수료·기한은 바꿀 수 없다.",
              "model": info.get("model"), "request_id": info.get("request_id"), "choice_index": info.get("choice_index"), "reason": info.get("reason"),
              "usage": info.get("usage"), "flow_id": info.get("flow_id"), "not_did": "후보를 만들지 않았고 경로를 최적화하지 않았다(후보 1개). 승인·서명·방송을 하지 않는다."}
    elif mode == "AI_CARRIED":
        ai = {"mode": mode, "did": ("이전 flow 에서 이 조건·후보에 대해 실제로 낸 Kiln 판단(선택 index·이유)을 이어받았다(추가 호출 없음; 같은 규칙·계획·지문일 때만)." if str(info.get("record_ref") or "").startswith("flow:")
                                    else "안내 화면에서 검증된 Kiln 결과(요청 구조화·다음 행동 제안)를 이어받았다(추가 호출 없음)."),
              "record_ref": info.get("record_ref"), "model": info.get("model"), "request_id": info.get("request_id"), "choice_index": info.get("choice_index"), "reason": info.get("reason")}
    elif mode == "MANUAL_TECH_CHECK":
        ai = {"mode": mode, "did": "AI 없음 — 수동 기술 검사(사람이 조건을 읽고 확인). A 시연으로 집계하지 않는다."}
    else:
        ai = {"mode": mode, "did": "기록 없음"}
    code = ["사용자 규칙(수량·예산·허용 수취인·기한·TRX 총상한)으로 후보 계획 계산(코드, 모델 무관)",
            "모델 출력 형식 검증: choice_index 범위·허용 키만(금액/주소 필드 거부)",
            "계획 재검증: 수량=요청, 수취인∈허용목록, USDT 수수료 0, Energy 한도+Bandwidth 최대=총상한≤사용자 상한",
            "미서명 거래 독립 디코드 대조(20항목)·확인 지문(전체 조건+raw+txID)·정책 승인 결합",
            "서명 뒤: raw 바이트 동일·txID·서명자 EOA 복구=보내는 계정, 방송 직전 재견적, 영수증 4기준 대조"]
    return {"ai": ai, "code": code, "human": "사람 확인 = 확인 지문을 읽고 같을 때만 승인 · TronLink 서명 1회(기존 허용 범위)"}


def make_nile_order(*, plan: D.Plan, rules: D.UserRules, sender: str, unsigned: dict, quote: NileQuote, approval_id: str,
                    batch_id: str, revision: int, csv_sha256: str, payment_id: str, row_no: int, memo: str, expire_at_ms: int,
                    confirm_sha256: str | None = None, ai_mode: str | None = None, ai_work: dict | None = None, change: dict | None = None) -> dict:
    return {"kind": "nile_trc20", "payment_id": payment_id, "batch_id": batch_id, "revision": revision, "csv_sha256": csv_sha256,
            "goal": rules.goal, "ai_work": ai_work or build_ai_work(None, plan, rules, ai_mode), "change": change,
            "approval_id": approval_id, "row_no": row_no, "memo": memo, "network": "nile", "chain_id": NT.NILE_CHAIN_ID,
            "user_eoa": sender, "receiver": plan.receiver, "token": NT.NILE_USDT, "token_symbol": "USDT", "token_decimal": 6,
            "amount_units": plan.value_units, "trx_total_cap_sun": rules.trx_fee_cap_sun, "trx_fee_limit_sun": plan.trx_fee_limit_sun,
            "trx_bandwidth_max_sun": plan.trx_bandwidth_max_sun, "trx_total_max_sun": plan.trx_total_max_sun, "trx_est_sun": plan.trx_est_sun,
            "trx_expected_total_sun": quote.expected_total_sun, "energy_est": quote.energy_est, "energy_price_sun": quote.energy_price_sun,
            "bandwidth_price_sun": quote.bandwidth_price_sun, "tx_size_bytes": quote.tx_size_bytes, "bandwidth_bytes": quote.bandwidth_bytes,
            "bandwidth_rule": NT.BANDWIDTH_RULE, "expire_at_ms": expire_at_ms,
            "user_deadline_ts": rules.deadline_ts, "tx_id": unsigned["txID"], "confirm_sha256": confirm_sha256, "ai_mode": ai_mode,
            "unsigned_tx": {"txID": unsigned["txID"], "raw_data": unsigned["raw_data"], "raw_data_hex": unsigned["raw_data_hex"], "visible": False},
            "checks": unsigned.get("checks"), "snapshot_sha256": order_snapshot(unsigned, approval_id, csv_sha256, payment_id),
            "notice": "Nile 테스트넷 · 테스트 자금. 실제 거래소 입금이 아닙니다. 이 화면은 서명만 하며 전송(방송)은 서버가 원장 예약 뒤 1회 합니다."}


def order_snapshot(unsigned: dict, approval_id: str, csv_sha256: str, payment_id: str) -> str:
    payload = {"raw_data_hex": unsigned["raw_data_hex"], "txID": unsigned["txID"], "approval_id": approval_id, "csv_sha256": csv_sha256, "payment_id": payment_id}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def spec_from_order(od: dict) -> NT.TransferSpec:
    return NT.TransferSpec(sender=od["user_eoa"], receiver=od["receiver"], token=od["token"], amount_units=int(od["amount_units"]),
                           fee_limit_sun=int(od["trx_fee_limit_sun"]), expire_at_ms=int(od["expire_at_ms"]))


# ── 훅 ────────────────────────────────────────────────────────────────────
def make_nile_hooks(*, policy, batch_id: str, intent_log: IntentLog, approvals: ApprovalLedger, node: NT.NileNode, sender: str,
                    quote_fn, human_confirm, human_sign, now_fn=time.time, quote_ttl_s: int = QUOTE_TTL_S, receipt_polls: int = 6,
                    receipt_wait_s: float = 3.0, sleep_fn=time.sleep, artifact: dict | None = None, ai_record_ref: str | None = None, on_refused=None,
                    ai_info: dict | None = None):
    """run_flow 용 approve/execute. artifact(prepare 산출물)가 있으면 그 원본 미서명 바이트·기한·지문으로만 진행한다(재작성 없음).
    human_confirm(summary)->str|None: summary["confirm_sha256"] 가 사람이 읽은 지문과 같을 때만 이름을 돌려줘야 한다."""
    state = {"quote": None, "payment_id": None, "first_quote_key": None, "revision": None}

    def fresh_quote() -> NileQuote:
        q = quote_fn()
        if int(now_fn()) - q.quoted_at > quote_ttl_s:
            raise RuntimeError("quote expired")
        state["quote"] = q
        return q

    def _unsigned_for(plan: D.Plan, rules: D.UserRules, t_now: int):
        """아티팩트가 있으면 그 원본 바이트를 그대로(사양 재검증), 없으면 새로 작성(수동/검사 경로)."""
        expire_at_ms = int(rules.deadline_ts) * 1000
        spec = NT.TransferSpec(sender=sender, receiver=plan.receiver, token=NT.NILE_USDT, amount_units=plan.value_units,
                               fee_limit_sun=plan.trx_fee_limit_sun, expire_at_ms=expire_at_ms)
        if artifact is not None:
            u = dict(artifact["unsigned"])
            u["checks"] = NT.verify_unsigned(u, spec, now_ms=t_now * 1000, max_expiry_ms=MAX_SIGN_WINDOW_S * 1000 + 60_000)
            return u, spec, expire_at_ms
        u = NT.build_unsigned(node, spec, now_ms=t_now * 1000)
        return u, spec, expire_at_ms

    def approve(plan: D.Plan, rules: D.UserRules, ctx: dict):
        q_, r_ = divmod(int(plan.value_units), 10 ** 6)
        amount_text = f"{q_}.{r_:06d}"
        buf = io.StringIO(); wr = csv.writer(buf, lineterminator="\n")
        wr.writerow(["recipient", "amount", "memo"]); wr.writerow([plan.receiver, amount_text, f"{rules.goal[:60]} plan={rules.guide_plan_id}"])
        res = run_batch(buf.getvalue(), policy, batch_id)
        state["rules"] = rules
        if res["stage"] != "AWAITING_HUMAN_APPROVAL" and res["batch_registration"] != "REPLAY":
            return None
        row = next((r for r in res["rows"] if r["decision"] in ("ACCEPTED_FOR_APPROVAL", "REPLAY")), None)
        if row is None:
            return None
        state["payment_id"] = row["payment_id"]; state["revision"] = res["revision"]; state["csv_sha256"] = res["csv_sha256"]; state["row"] = row
        # 사람이 본 전체 조건 = confirm_payload(아티팩트 원본 바이트 포함) → 정책 display_digest 의 extra 로 결합
        unsigned = artifact["unsigned"] if artifact is not None else None
        if artifact is not None:
            a_plan = D.Plan(**artifact["plan"]); a_rules = D.UserRules(**{**artifact["rules"], "allowlist": tuple(artifact["rules"]["allowlist"])})
            if a_plan != plan or a_rules != rules or artifact.get("sender") != sender:
                return None                                   # 아티팩트와 현재 계획/규칙이 다르면 승인 없음
        extra = confirm_payload(rules, plan, sender, unsigned, ctx["revision"], ai_record_ref)
        digest = policy.display_digest(batch_id, res["revision"], "nile", extra)
        cdig = hashlib.sha256(json.dumps(extra, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        summary = {**extra, "path": "nile_trc20", "asset": plan.asset, "display_digest": digest, "confirm_sha256": cdig, "ai_mode": ctx.get("ai_mode")}
        who = human_confirm(summary)
        if not who:
            return None
        ap_res = approve_batch(policy, batch_id, res["revision"], res["csv_sha256"], confirmed_by=who, displayed_sha256=digest, extra=extra)
        if ap_res["outcome"] not in ("APPROVED_HUMAN_CONFIRMED", "APPROVAL_REPLAY"):
            return None
        ap = D.Approval(approval_id=ap_res["approval"]["approval_id"] + f"@{ctx['flow_id']}", flow_id=ctx["flow_id"], revision=ctx["revision"],
                        rules_digest=ctx["rules_digest"], plan_digest=ctx["plan_digest"], approved_by=who, approved_at=int(now_fn()))
        approvals.record(ap)
        state["policy_approval_id"] = ap_res["approval"]["approval_id"]; state["extra"] = extra; state["confirm_sha256"] = cdig; state["ai_mode"] = ctx.get("ai_mode")
        state["confirm_revision"] = ctx["revision"]          # 9/29 수정: 지문 revision = 사람이 본 것(아티팩트/flow revision). 정책 배치 revision(state["revision"])과 별개
        return ap

    def _prior(pid: str):
        prior = intent_log.state(pid) if pid else None
        cur = intent_log.current(pid) or {}
        if prior in ("SUBMITTED", "UNKNOWN"):
            return {"state": "UNKNOWN", "reason": f"already reserved/broadcast earlier ({prior}); no rebroadcast; resolve by txID", "payment_id": pid, "tx_hash": cur.get("tx_hash")}
        if prior in ("ACCEPTED", "CONFIRMED"):
            return {"state": prior, "reason": f"already {prior} earlier; no resend", "payment_id": pid, "tx_hash": cur.get("tx_hash")}
        if prior == "FAILED":
            return {"state": "FAILED", "reason": "prior attempt reached the chain and was recorded FAILED; no resend; cause/chain result must be checked before any new attempt",
                    "payment_id": pid, "prior": {k: cur.get(k) for k in ("tx_hash", "reason", "ts", "max_fee")}}
        if prior == "REJECTED":
            return {"state": "NOT_SUBMITTED", "reason": "prior broadcast was rejected by the node at validation (not on chain); this payment_id stays closed; new revision required", "payment_id": pid}
        if prior == "CANCELLED":
            return {"state": "NOT_SUBMITTED", "reason": "prior intent cancelled before broadcast; this payment_id stays closed", "payment_id": pid}
        return None

    def execute(plan: D.Plan, ap: D.Approval):
        pid = state.get("payment_id")
        pr = _prior(pid)
        if pr:
            return pr
        if plan.path != "nile_trc20" or plan.fee_units != 0:
            return {"state": "NOT_SUBMITTED", "reason": "plan is not a nile_trc20 plan"}
        rules = state["rules"]
        # 실행 직전 정책 승인 재검증: 승인에 결합된 extra(사람이 본 전체 조건)가 지금 계산한 것과 같아야 한다
        apr = policy.approvals.get(state.get("policy_approval_id")) or {}
        if apr.get("mode") != "HUMAN_CONFIRMED" or apr.get("displayed_sha256") != policy.display_digest(batch_id, state["revision"], "nile", state.get("extra")):
            return {"state": "NOT_SUBMITTED", "reason": "approval no longer matches the confirmed conditions (display/extra digest)", "payment_id": pid}
        try:
            q = fresh_quote()
        except Exception as e:
            return {"state": "NOT_SUBMITTED", "reason": f"requote failed: {e}"[:200]}
        if state["first_quote_key"] is None:
            state["first_quote_key"] = q.key()
        if q.key() != state["first_quote_key"]:
            return {"state": "NOT_SUBMITTED", "reason": "quote changed since plan (parties/amount/energy or bandwidth price/simulation)"}
        # 비용 재검: 예상 Energy ≤ fee_limit, fee_limit + bandwidth_max ≤ 총상한, 잔액 ≥ 총상한, USDT 잔액 ≥ 수량
        if q.est_energy_sun > plan.trx_fee_limit_sun or q.bandwidth_max_sun > plan.trx_bandwidth_max_sun \
                or plan.trx_fee_limit_sun + plan.trx_bandwidth_max_sun > rules.trx_fee_cap_sun:
            return {"state": "NOT_SUBMITTED", "reason": f"TRX cost bounds changed: energy est {q.est_energy_sun} / fee_limit {plan.trx_fee_limit_sun}, bandwidth max {q.bandwidth_max_sun} / {plan.trx_bandwidth_max_sun}, cap {rules.trx_fee_cap_sun} (USDT amount not reduced)"}
        if q.trx_sun < rules.trx_fee_cap_sun or q.usdt_units < plan.value_units:
            return {"state": "NOT_SUBMITTED", "reason": "balance below TRX total cap or USDT amount at execution time"}
        t_now = int(now_fn())
        if int(rules.deadline_ts) - t_now <= 0:
            return {"state": "NOT_SUBMITTED", "reason": "user deadline passed before order build"}
        try:
            unsigned, spec, expire_at_ms = _unsigned_for(plan, rules, t_now)
        except Exception as e:
            return {"state": "NOT_SUBMITTED", "reason": f"unsigned verify/build failed: {e}"[:200]}
        # 아티팩트 지문 = 사람이 확인한 지문 = 지금 서명 대상. 아티팩트 없는 경로(수동/검사)는 승인 때와 같이 바이트 없이 조건만 결합된다.
        cdig_now = confirm_digest(rules, plan, sender, unsigned if artifact is not None else None, state.get("confirm_revision", state["revision"]), ai_record_ref)
        if state.get("confirm_sha256") != cdig_now or (artifact is not None and artifact.get("confirm_sha256") != cdig_now):
            return {"state": "NOT_SUBMITTED", "reason": "confirm digest mismatch between human-confirmed summary, artifact and signing target", "payment_id": pid}
        order = make_nile_order(plan=plan, rules=rules, sender=sender, unsigned=unsigned, quote=q, approval_id=state["policy_approval_id"],
                                batch_id=batch_id, revision=state["revision"], csv_sha256=state["csv_sha256"], payment_id=pid,
                                row_no=state["row"].get("row_no", 1), memo=state["row"].get("memo", ""), expire_at_ms=expire_at_ms,
                                confirm_sha256=cdig_now, ai_mode=state.get("ai_mode"), ai_work=build_ai_work(ai_info, plan, rules, state.get("ai_mode")),
                                change=(ai_info or {}).get("change"))
        state["order"] = order
        if intent_log.state(pid) is None:
            intent_log.append(pid, "DRAFTED", user=sender, batch_id=batch_id, revision=state["revision"], csv_sha256=state["csv_sha256"],
                              row_no=order["row_no"], memo=order["memo"], approval_id=order["approval_id"], receiver=plan.receiver,
                              value=plan.value_units, max_fee=rules.trx_fee_cap_sun, tx_hash=unsigned["txID"], scope=SCOPE_NILE_TRC20)
        if intent_log.state(pid) == "DRAFTED":
            intent_log.append(pid, "AWAITING_HUMAN")
        signed = human_sign(order)
        if not signed or (isinstance(signed, dict) and signed.get("refused")):
            return {"state": "NOT_SUBMITTED", "reason": (signed or {}).get("refused") if isinstance(signed, dict) else "not signed", "payment_id": pid, "tx_hash": unsigned["txID"]}
        t_signed = int(now_fn())
        if t_signed >= int(rules.deadline_ts):
            return {"state": "NOT_SUBMITTED", "reason": "user deadline passed while waiting for wallet signature", "payment_id": pid}
        def refuse(reason: str):
            if on_refused is not None:
                try:
                    on_refused(order, reason)                                   # 소비된 서명을 방송 금지 상태로 격리(Grok-02#4)
                except Exception as e:
                    sys.stderr.write(f"[nile_executor] quarantine failed for {pid}: {e}\n")
            if intent_log.state(pid) in ("AWAITING_HUMAN", "SIGNED"):
                _safe_append(intent_log, pid, "CANCELLED", reason=f"refused before broadcast: {reason}"[:200])
            return {"state": "NOT_SUBMITTED", "reason": reason[:200], "payment_id": pid, "tx_hash": unsigned["txID"], "signature_quarantined": on_refused is not None}
        try:
            body = NT.verify_signed(unsigned, signed.get("signed_tx") if isinstance(signed, dict) else None, spec, now_ms=t_signed * 1000,
                                    max_expiry_ms=MAX_SIGN_WINDOW_S * 1000 + 60_000)
        except NT.NileTxError as e:
            return refuse(f"signature refused: {e}")
        # Grok-02#1: 방송 직전 재견적 — 서명본 실측 **과금 바이트**(전송+64×계약)×현재 대역폭 단가, 현재 에너지 단가·시뮬레이션·잔액·기한을 계획 한도와 다시 대조
        try:
            q2 = quote_fn()
        except Exception as e:
            return refuse(f"pre-broadcast requote failed: {e}")
        signed_size = int(body["signed_size_bytes"]); bw_bytes = int(body["bandwidth_bytes"])
        bw_now = bw_bytes * q2.bandwidth_price_sun
        if (q2.key()[:6] != state["first_quote_key"][:6] or q2.est_energy_sun > plan.trx_fee_limit_sun or bw_now > plan.trx_bandwidth_max_sun
                or plan.trx_fee_limit_sun + bw_now > rules.trx_fee_cap_sun or q2.trx_sun < rules.trx_fee_cap_sun or q2.usdt_units < plan.value_units
                or int(now_fn()) >= int(rules.deadline_ts)):
            return refuse(f"pre-broadcast bounds violated: energy est {q2.est_energy_sun}/{plan.trx_fee_limit_sun}, bandwidth {bw_bytes}B(tx {signed_size}B+{NT.MAX_RESULT_SIZE_IN_TX}B)×{q2.bandwidth_price_sun}={bw_now}/{plan.trx_bandwidth_max_sun}, cap {rules.trx_fee_cap_sun}, balance {q2.trx_sun}")
        try:
            intent_log.append(pid, "SIGNED")
        except Exception as e:
            return _conflict(intent_log, pid, unsigned["txID"], f"cannot mark SIGNED: {e}")
        ok, why = intent_log.reserve_broadcast(pid, sender, unsigned["txID"], batch_id=batch_id, revision=state["revision"])
        if not ok:
            return _conflict(intent_log, pid, unsigned["txID"], f"blocked before broadcast: {why}")
        bcast = {"raw_data": body["raw_data"], "raw_data_hex": body["raw_data_hex"], "signature": body["signature"], "txID": body["txID"], "visible": False}
        try:
            r = node.broadcast(bcast)
        except Exception as e:
            _safe_append(intent_log, pid, "UNKNOWN", reason=f"broadcast transport: {e}"[:200])
            return {"state": "UNKNOWN", "reason": f"broadcast transport error: {e}"[:200], "payment_id": pid, "tx_hash": unsigned["txID"]}
        cls, detail = NT.classify_broadcast(r)
        if cls == "REJECTED":
            # Grok-02#3: REJECTED 로 기록하기 전에 같은 txID 가 체인에 없는지 확인(있으면 UNKNOWN 으로 두고 조회로 종결)
            try:
                seen = NT.reconcile_receipt(unsigned, spec, NT.fetch_receipt(node, unsigned["txID"]))
            except Exception as e:
                seen = {"verdict": "QUERY_ERROR", "error": str(e)[:120]}
            if seen.get("verdict") != "NOT_FOUND":
                _safe_append(intent_log, pid, "UNKNOWN", reason=f"node said {detail} but txID lookup returned {seen.get('verdict')}"[:200])
                return {"state": "UNKNOWN", "reason": f"node reported rejection but txID may exist on chain ({seen.get('verdict')}); resolve by txID", "payment_id": pid, "tx_hash": unsigned["txID"]}
            _safe_append(intent_log, pid, "REJECTED", reason=detail)
            return {"state": "REJECTED", "reason": f"node rejected at validation (not on chain, txID lookup NOT_FOUND): {detail}", "payment_id": pid, "tx_hash": unsigned["txID"]}
        if cls == "UNKNOWN":
            _safe_append(intent_log, pid, "UNKNOWN", reason=detail)
            return {"state": "UNKNOWN", "reason": f"broadcast result unknown: {detail}; resolve by txID only", "payment_id": pid, "tx_hash": unsigned["txID"]}
        _safe_append(intent_log, pid, "ACCEPTED", reason="broadcast accepted by node")
        last = None
        for _ in range(int(receipt_polls)):
            sleep_fn(receipt_wait_s)
            try:
                rec = NT.fetch_receipt(node, unsigned["txID"])
            except Exception as e:
                last = {"verdict": "QUERY_ERROR", "error": str(e)[:160]}
                continue
            last = NT.reconcile_receipt(unsigned, spec, rec, total_cap_sun=rules.trx_fee_cap_sun, bandwidth_max_sun=plan.trx_bandwidth_max_sun, bandwidth_bytes=bw_bytes)
            if last["verdict"] in ("CONFIRMED", "FAILED", "MISMATCH"):
                break
        return _finish(intent_log, pid, unsigned["txID"], last)

    return approve, execute, state


def _conflict(intent_log, pid, txid, reason):
    """SIGNED 전이/방송 예약 충돌: 같은 지급이 이미 열려 있거나(다른 실행기·재시작) 다른 열린 지급이 있는 경우 — 이미 제출된 것을 '미제출' 로 부르지 않는다(Grok-02#3)."""
    st = intent_log.state(pid); cur = intent_log.current(pid) or {}
    if st in ("SUBMITTED", "UNKNOWN", "ACCEPTED"):
        return {"state": "UNKNOWN", "reason": f"{reason}; this payment is already open ({st}) — resolve by stored txID, no rebroadcast"[:220], "payment_id": pid, "tx_hash": cur.get("tx_hash") or txid}
    if st == "CONFIRMED":
        return {"state": "CONFIRMED", "reason": f"{reason}; already CONFIRMED"[:200], "payment_id": pid, "tx_hash": cur.get("tx_hash")}
    return {"state": "NOT_SUBMITTED", "reason": f"{reason} (this payment_id not broadcast; account may have another open payment — resolve it first)"[:220], "payment_id": pid, "tx_hash": txid}


def _safe_append(intent_log, pid, st, **fields):
    try:
        intent_log.append(pid, st, **fields)
    except Exception as e:
        sys.stderr.write(f"[nile_executor] intent {st} append failed for {pid}: {e}\n")


def _finish(intent_log, pid, txid, rec):
    v = (rec or {}).get("verdict")
    if v == "CONFIRMED":
        _safe_append(intent_log, pid, "CONFIRMED", tx_hash=txid)
        return {"state": "CONFIRMED", "outcome": "FINAL_CONFIRMED_SOLIDITY", "tx_hash": txid, "payment_id": pid, "receipt": rec}
    if v == "FAILED":
        _safe_append(intent_log, pid, "FAILED", tx_hash=txid, reason=f"contractRet={rec.get('contract_ret')} receipt={rec.get('receipt_result')}")
        return {"state": "FAILED", "outcome": "ONCHAIN_FAILED", "tx_hash": txid, "payment_id": pid, "receipt": rec}
    if v == "MISMATCH":                                    # Grok-02#1: 체인에 있으나 내용/비용 불일치 → '실행됨' 으로 집계하지 않는다(원장 ACCEPTED 유지, 재방송 차단)
        return {"state": "MISMATCH", "outcome": "ONCHAIN_MISMATCH_NEEDS_HUMAN", "tx_hash": txid, "payment_id": pid, "receipt": rec}
    return {"state": "ACCEPTED", "outcome": f"ACCEPTED_{v or 'PENDING'}", "tx_hash": txid, "payment_id": pid, "receipt": rec}


def resolve_by_txid(node: NT.NileNode, intent_log: IntentLog, order: dict) -> dict:
    pid = order["payment_id"]
    st = intent_log.state(pid)
    if st not in ("UNKNOWN", "ACCEPTED", "SUBMITTED"):
        return {"outcome": f"NOT_OPEN_{st}"}
    stored = (intent_log.current(pid) or {}).get("tx_hash")
    if not stored or stored != order["tx_id"]:
        return {"outcome": "REFUSED_TXID_MISMATCH", "stored": stored}
    rec = NT.reconcile_receipt(order["unsigned_tx"], spec_from_order(order), NT.fetch_receipt(node, stored),
                               total_cap_sun=int(order.get("trx_total_cap_sun") or order["trx_fee_limit_sun"]), bandwidth_max_sun=int(order.get("trx_bandwidth_max_sun") or 0),
                               bandwidth_bytes=order.get("bandwidth_bytes"))
    if rec["verdict"] == "NOT_FOUND":
        return {"outcome": "STILL_UNKNOWN_NOT_FOUND", "note": "not on chain yet or dropped after expiration; do NOT rebuild/rebroadcast automatically", "receipt": rec}
    out = _finish(intent_log, pid, stored, rec)
    out["outcome_resolve"] = rec["verdict"]
    return out


def run_nile_flow(rules: D.UserRules, *, policy, batch_id, intent_log, approvals, node, sender, quote_fn, human_confirm, human_sign,
                  kiln_choose, flow_id=None, revision=1, now_fn=time.time, artifact=None, ai_record_ref=None, **hook_kw):
    try:
        q = quote_fn()
    except Exception as e:
        return {"flow_id": flow_id, "revision": revision, "outcome": "DECLINED", "reason": f"no quote: {e}"[:200], "executed": False, "path": rules.path}
    approve, execute, state = make_nile_hooks(policy=policy, batch_id=batch_id, intent_log=intent_log, approvals=approvals, node=node, sender=sender,
                                              quote_fn=quote_fn, human_confirm=human_confirm, human_sign=human_sign, now_fn=now_fn,
                                              artifact=artifact, ai_record_ref=ai_record_ref, **hook_kw)
    state["first_quote_key"] = q.key()
    rec = D.run_flow(rules, 0, int(now_fn()), kiln_choose=kiln_choose, approve=approve, execute=execute, flow_id=flow_id, revision=revision,
                     used_approvals=UsedSet(approvals), now_fn=now_fn, trx_quote=q.trx_quote())
    rec["quote"] = q.to_dict()
    rec["order"] = state.get("order")
    rec["confirm_sha256"] = state.get("confirm_sha256")
    return rec
