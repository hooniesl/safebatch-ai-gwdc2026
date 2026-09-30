"""휴대폰 서명 → 맥북 검증·방송 → 휴대폰 결과 (9/29 첫 시제품, Nile TRX 단일 서명).

구조(휴대폰 서명만 필요한 구조): 보내는 계정 = 휴대폰 지갑 자체 키. 맥북은 서명하지 않는다.
  대화(phone_chat, 규칙 모의) → 제안 → prepare(미서명 TransferContract 작성·독립 디코드 대조·견적·주문 PENDING)
  → 휴대폰 TronLink 서명(tronWeb.trx.sign, 방송 아님) → submit_signed(raw 바이트 동일·txID·서명자 복구·재견적·방송 예약(txID 잠금)
  → broadcasttransaction 1회 → 같은 txID 조회 → 영수증 대조) → status(같은 txID 재조회만, 재방송 없음).
검증 규칙: 내용이 바뀌면 새 주문(이전 PENDING 은 취소·지문 불일치로 늦은 서명 거부), 만료 후 서명 거부, 변조 raw/다른 서명자 거부,
  같은 서명 재제출·같은 계정의 미해결 방송 중 재방송 거부(OrderStore 1회 소비 + IntentLog reserve_broadcast).
보안 한계: 이 검증은 우리 서버를 거칠 때만 적용된다. 휴대폰 지갑 앱에서 직접 보내는 송금은 막지 못한다(체인 권한 변경 없음).
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
import os
import pathlib
import secrets
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
from order_store import OrderStore  # noqa: E402
import phone_chat as PC  # noqa: E402
import phone_ai as AI  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch import trx_tx as TX  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402

SIGN_WINDOW_S = 600
PROPOSAL_TTL_S = 900
DEFAULT_FEE_CAP_SUN = 2_000_000           # 2 TRX: 대역폭 최악(약 0.35 TRX) + 수취인 미활성 시 1.1 TRX 를 덮는 상한
EXPLORER = "https://nile.tronscan.org/#/transaction/"
TERMINAL_RESULTS = ("FINAL_CONFIRMED_SOLIDITY", "FAILED_ONCHAIN", "EXPIRED_NOT_ON_CHAIN")


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def norm_sender(s) -> str | None:
    """주소 표기 정규화(Grok-04 #6): base58 또는 hex(41+20바이트) → base58. 잘못된 값은 None."""
    if not isinstance(s, str):
        return None
    t = s.strip()
    if len(t) == 42 and t.lower().startswith("41") and all(c in "0123456789abcdefABCDEF" for c in t):
        from tronpy.keys import to_base58check_address
        return to_base58check_address(t.lower())
    try:
        NT.addr20(t); return t
    except Exception:                                       # noqa: BLE001
        return None


def spec_from_order(od: dict) -> TX.TrxSpec:
    return TX.TrxSpec(sender=od["user_eoa"], receiver=od["receiver"], amount_sun=int(od["amount_sun"]),
                      expire_at_ms=int(od["expire_at_ms"]), fee_cap_sun=int(od["fee_cap_sun"]))


class PhoneFlow:
    def __init__(self, *, node: NT.NileNode, store: OrderStore, intents: IntentLog, results_dir: pathlib.Path,
                 contacts: list[dict] | None = None, now_fn=time.time, sleep_fn=time.sleep, fee_cap_sun: int = DEFAULT_FEE_CAP_SUN,
                 sign_window_s: int = SIGN_WINDOW_S, receipt_polls: int = 6, poll_s: float = 3.0, auto_expiry_settle: bool = False, ai_provider=None):
        self.node, self.store, self.intents = node, store, intents
        self.ai_provider = ai_provider or AI.MockKiln()      # 승인 전 MockKiln(실호출 0). 실제 Kiln 은 사장 승인 범위 안에서 KilnLive 주입
        self.auto_expiry_settle = bool(auto_expiry_settle)   # VP 9/29: 이번 시험에서는 자동 EXPIRED_NOT_ON_CHAIN 판정·잠금 해제 비활성(과거 조회 범위 근거 미확보). UNKNOWN 유지·같은 txID 만 조회
        self.results_dir = pathlib.Path(results_dir); self.results_dir.mkdir(parents=True, exist_ok=True)
        self.contacts = contacts
        self.now_fn, self.sleep_fn = now_fn, sleep_fn
        self.fee_cap_sun, self.sign_window_s, self.receipt_polls, self.poll_s = int(fee_cap_sun), int(sign_window_s), int(receipt_polls), poll_s
        self.proposals: dict[str, dict] = {}
        import threading
        self._prepare_lock = threading.Lock()                # 9/29 실측: 주문 만들기 두 번 탭 → 동시 prepare 가 supersede 검사를 지나쳐 PENDING 2건 생성 → 직렬화

    # ── 대화 → 제안 ───────────────────────────────────────────────────────
    CARRY_WINDOW_S = 900
    carry_enabled = os.environ.get("SB_CARRY_LAST_LIVE", "0") == "1"

    def _carry_candidate(self, text: str) -> dict | None:
        """9/29 A-T 실측(화면 버튼 결함으로 새로고침 필요) 대응: **같은 문장**의 직전 Kiln 실호출 성공 결과를 15분 안에 1회만 재사용한다(추가 호출 0).
        조건(Mac 적응 흐름 AI_CARRIED 와 같은 취지): 기록된 호출이 KILN_LIVE·call_id 있음·폴백 없음·kind proposal(normal) · 900 s 이내 · 그 call_id 를 이미 재사용한 기록 없음.
        실호출 성공 수에 넣지 않는다(ai.calls=0, ai.carried_from 표시)."""
        try:
            p = self.results_dir.parent / "phone_ai_decisions.jsonl"
            if not p.exists():
                return None
            rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:                                   # noqa: BLE001
            return None
        used = {(r.get("ai") or {}).get("carried_from") for r in rows if (r.get("ai") or {}).get("carried_from")}
        now = int(self.now_fn())
        for r in reversed(rows):
            ai = r.get("ai") or {}
            if r.get("text") != text[:300] or ai.get("mode") != "KILN_LIVE" or not ai.get("call_id") or ai.get("fallback") or ai.get("carried_from"):
                continue
            if r.get("kind") != "proposal" or r.get("kind_detail") != "normal" or ai.get("call_id") in used:
                return None
            try:
                ts = int(r["t"]) if r.get("t") is not None else int(time.mktime(time.strptime(r["ts"][:19], "%Y-%m-%dT%H:%M:%S")))
            except Exception:                               # noqa: BLE001
                return None
            return r if 0 <= now - ts <= self.CARRY_WINDOW_S else None
        return None

    def chat(self, text: str) -> dict:
        return self._chat(text, self.ai_provider)

    def chat_test(self, text: str) -> dict:
        """시험 모드(VP 9/29 검수 §후속): 명시적 모의 제공자만 사용 — 운영 Kiln 예산을 소모하지 않는다. 재사용(carry)도 없다."""
        return self._chat(text, AI.MockKiln(), allow_carry=False)

    def _chat(self, text: str, provider, allow_carry: bool = True) -> dict:
        contacts = self.contacts if self.contacts is not None else PC.load_contacts()
        # VP 9/29: 일반 /api/chat 의 자동 재사용은 기본 비활성. A-T 복구(17:56) 이력은 보존. 필요 시 SB_CARRY_LAST_LIVE=1 로만 켠다(명시 복구 식별자 검증은 별도 범위)
        carry = self._carry_candidate(text) if (allow_carry and self.carry_enabled and getattr(provider, "provider", "") == "KILN_LIVE") else None
        if carry:
            r = PC.parse_request(text, contacts)
            if r.get("kind") == "proposal":
                r["kind_detail"] = "normal"; r["constraints"] = {"budget_sun": None, "deadline_s": None, "fee_cap_sun": int(self.fee_cap_sun)}
                r["ai"] = {**(carry.get("ai") or {}), "calls": 0, "carried_from": carry["ai"]["call_id"],
                           "note": f"직전 Kiln 실호출({carry['ai']['call_id']}, {carry['ts'][11:19]}) 결과를 같은 문장·15분 안에 1회 재사용 — 추가 호출 0, 실호출 성공 수에 세지 않음"}
            else:
                carry = None
        if not carry:
            r = AI.decide(text, contacts, provider=provider, fee_cap_trx=Decimal(self.fee_cap_sun) / 1_000_000)
        self._ai_log(text, r)
        if r["kind"] == "proposal":
            pid = secrets.token_hex(6)
            c = r.get("constraints") or {}
            self.proposals[pid] = {**r["proposal"], "text": r.get("text") or text, "created_at": int(self.now_fn()), "expires_at": int(self.now_fn()) + PROPOSAL_TTL_S,
                                   "budget_sun": c.get("budget_sun"), "deadline_s": c.get("deadline_s"), "fee_cap_sun": int(c.get("fee_cap_sun") or self.fee_cap_sun),
                                   "kind_detail": r.get("kind_detail"), "ai": r.get("ai")}
            r["proposal_id"] = pid
            r["fee_cap_trx"] = self.proposals[pid]["fee_cap_sun"] / 1_000_000
        return r

    def _ai_log(self, text: str, r: dict) -> None:
        """AI 계층 판단 기록(모의/실호출 구분, 폴백은 성공 아님). 서명본·키 없음."""
        try:
            d = self.results_dir.parent / "phone_ai_decisions.jsonl"
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "t": int(self.now_fn()), "text": text[:300], "kind": r.get("kind"), "kind_detail": r.get("kind_detail"), "reason_code": r.get("reason_code"),
                   "ai": r.get("ai"), "constraints": r.get("constraints"), "proposal": {k: v for k, v in (r.get("proposal") or {}).items() if k in ("alias", "address", "amount_trx")}}
            with open(d, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:                              # noqa: BLE001
            sys.stderr.write(f"[phone_flow] ai log failed: {e}\n")

    # ── 제안 → 미서명 주문 ─────────────────────────────────────────────────
    def sender_lock(self, sender: str, now: int) -> str | None:
        """Grok-04 #1 + VP 9/29: 서명본이 남은(격리·철회·소비·노드 거절) 주문은 그 자체로 유효한 송금이므로 같은 보내는 계정의 새 주문을 막는다.
        해제는 시간 경과가 아니라 **체인 근거**(확정 블록 시각 > 만료+60s 이고 solidity 조회가 2회 연속 정상 빈 응답)로만 한다. 미해결 방송(SUBMITTED/UNKNOWN/ACCEPTED)도 같다."""
        sender = self._norm_sender(sender) or sender
        opened = self.intents.open_payments(sender)
        if opened:
            for pid in sorted(opened):                     # 열린 지급은 같은 txID 조회로 종결을 시도(재방송 없음)
                self.status(pid)
            opened = self.intents.open_payments(sender)
            if opened:
                return f"이 지갑의 이전 전송이 아직 종결되지 않았습니다({', '.join(sorted(opened))}). 결과가 '완료/실패/만료'로 바뀔 때까지 새 주문을 만들 수 없습니다."
        holds = self._holds_for(sender)                     # VP 9/29: 클라이언트가 서명본을 보유(서버 미수신·취소 포함)한 주문도 체인 근거 종결 전에는 새 주문 차단
        if holds:
            return f"이 지갑의 이전 주문 {holds[0]['payment_id']} 에 대한 휴대폰 서명본이 남아 있어(서버 미전송) 체인 근거로 종결되기 전에는 새 주문을 만들 수 없습니다."
        for p in sorted(self.store.root.glob("*.json")):
            rec = json.loads(p.read_text(encoding="utf-8")); od = rec.get("order") or {}
            if od.get("kind") != "nile_trx" or self._norm_sender(od.get("user_eoa")) != sender:
                continue
            if rec.get("state") not in ("SIGNED_VERIFIED", "CONSUMED", "SIGNED_THEN_WITHDRAWN", "SIGNED_REFUSED_NOT_BROADCAST"):
                continue
            res = self._load_result(od["payment_id"]) or {}
            if res.get("state") in TERMINAL_RESULTS:
                continue                                   # 체인에 실렸거나 체인 근거로 종결된 서명
            settled = self._settle_held_signature(od, res)
            if settled and settled.get("state") in TERMINAL_RESULTS:
                continue
            return (f"이전 주문 {od['payment_id']} 의 서명이 아직 체인 근거로 종결되지 않았습니다(만료 {time.strftime('%H:%M:%S', time.localtime(int(od['expire_at_ms'])//1000))} + 60초가 "
                    "확정 블록 시각으로 지나고 확정 조회가 2회 연속 비어야 해제). 그 전에는 같은 지갑으로 새 주문을 만들지 않습니다(이중 출금 방지).")
        return None

    def register_hold(self, payment_id: str, snapshot_sha256: str, tx_hash: str) -> dict:
        """클라이언트가 지갑 서명본을 보유 중임을 서버에 등록(서명 바이트 없음). 이 주문이 체인 근거로 종결되기 전에는 같은 지갑의 새 주문을 서버도 막는다."""
        rec = self.store.get(payment_id)
        if not rec or rec["order"].get("snapshot_sha256") != snapshot_sha256 or rec["order"].get("tx_id") != tx_hash:
            return {"ok": False, "error": "order not found or snapshot/txID mismatch"}
        h = self._hold_path(payment_id)
        h.parent.mkdir(parents=True, exist_ok=True)
        h.write_text(json.dumps({"payment_id": payment_id, "tx_hash": tx_hash, "sender": rec["order"]["user_eoa"], "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                 "note": "client holds a wallet signature for this order; new orders for this sender are blocked until chain-terminal"}, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "hold": True}

    def _hold_path(self, payment_id: str) -> pathlib.Path:
        return self.results_dir / f"{payment_id}.hold.json"

    def _holds_for(self, sender: str) -> list[dict]:
        out = []
        for p in sorted(self.results_dir.glob("*.hold.json")):
            try:
                h = json.loads(p.read_text(encoding="utf-8"))
            except Exception:                               # noqa: BLE001
                continue
            if self._norm_sender(h.get("sender")) != sender:
                continue
            res = self._load_result(h["payment_id"]) or {}
            if res.get("state") in TERMINAL_RESULTS:
                continue
            out.append(h)
        return out

    def _settle_held_signature(self, od: dict, res: dict) -> dict | None:
        """보관된 서명본(방송 안 함/거절/철회)의 종결 시도: 같은 txID 를 4종 조회 → 체인에 있으면 그 결과, 없으면 만료 확정 절차."""
        pid = od["payment_id"]; txid = od["tx_id"]
        try:
            rc = TX.reconcile_receipt_trx(od["unsigned_tx"], spec_from_order(od), TX.fetch_receipt_trx(self.node, txid))
        except Exception as e:                              # noqa: BLE001
            rc = {"verdict": "LOOKUP_ERROR", "error": str(e)[:120]}
        if rc.get("verdict") in ("CONFIRMED", "ACCEPTED", "PENDING", "FAILED", "FAILED_UNCONFIRMED", "MISMATCH"):
            # 보관만 했던 서명이 체인에 나타남(다른 경로로 방송됨) → 원장에 남기고 결과로 기록
            return self._result(pid, {"state": {"CONFIRMED": "FINAL_CONFIRMED_SOLIDITY", "FAILED": "FAILED_ONCHAIN"}.get(rc["verdict"], "UNKNOWN"),
                                      "payment_id": pid, "tx_hash": txid, "receipt": rc, "explorer": EXPLORER + txid,
                                      "note": "보관만 하던 서명본이 체인에서 조회됨(이 서버는 방송하지 않았음). 수동 확인 필요."})
        return self._expiry_settle(pid, txid, od, res, rc, held=True)

    def _expiry_settle(self, pid: str, txid: str, od: dict, res: dict, rc: dict, held: bool = False) -> dict | None:
        """미송금 확정 절차(공식 근거: expiration 이후 거래는 블록에 포함되지 않음 · 최종성은 solidified 블록):
        ① 확정(solidified) 블록 시각 > 만료+60s ② solidity 2종 조회가 오류 없이 **연속 2회** 빈 응답(사이에 오류가 끼면 0 부터).
        둘 다 만족할 때만 EXPIRED_NOT_ON_CHAIN. 아니면 UNKNOWN 과 잠금 유지. 로컬 시계·미확정 최신 블록 시각은 쓰지 않는다."""
        nf = int(res.get("not_found_count") or 0)
        nf = nf + 1 if (rc.get("verdict") == "NOT_FOUND" and rc.get("solid_lookups_ok")) else 0
        solid_ms = self._solid_time_ms()
        base = {"payment_id": pid, "tx_hash": txid, "receipt": rc, "explorer": EXPLORER + txid, "not_found_count": nf, "solid_time_ms": solid_ms,
                "expire_at_ms": int(od["expire_at_ms"]), "held_signature": held}
        base["expiry_conditions_met"] = bool(rc.get("verdict") == "NOT_FOUND" and nf >= 2 and solid_ms > int(od["expire_at_ms"]) + 60_000)
        base["auto_expiry_settle"] = self.auto_expiry_settle
        if base["expiry_conditions_met"] and self.auto_expiry_settle:
            cur = self.intents.state(pid)
            if cur in ("UNKNOWN",):
                self._intent(pid, "REJECTED", reason="expired per solidified block time and absent from solidity lookups twice")
            return self._result(pid, {**base, "state": "EXPIRED_NOT_ON_CHAIN",
                                      "note": "확정 블록 시각이 만료+60초를 지났고 확정 조회가 2회 연속 비어 있음 → 체인에 포함될 수 없는 거래. 이 주문은 종결. 보내려면 새 주문."})
        if rc.get("verdict") == "NOT_FOUND":                # 조회에서 사라짐 → 성공 문구 없이 UNKNOWN(보관 서명은 SIGNATURE_HELD)
            state = "SIGNATURE_HELD" if held else "UNKNOWN"
        else:                                                # LOOKUP_ERROR → 이전 표시 유지(판단 보류)
            state = res.get("state") if res.get("state") in ("UNKNOWN", "ACCEPTED_UNCONFIRMED", "REJECTED_BY_NODE_UNCONFIRMED", "FAILED_UNCONFIRMED", "SIGNATURE_HELD") else ("SIGNATURE_HELD" if held else "UNKNOWN")
        note_extra = " 자동 미송금 종결은 꺼져 있음(수동 판단 전까지 UNKNOWN·잠금 유지)." if (base["expiry_conditions_met"] and not self.auto_expiry_settle) else ""
        return self._result(pid, {**base, "state": state, "reason": res.get("reason"),
                                  "note": "체인 근거(확정 블록 시각·확정 조회 2회) 전에는 종결하지 않음. 새 거래 재전송 없음." + note_extra + (f" 조회 오류: {rc.get('error')}" if rc.get("verdict") == "LOOKUP_ERROR" else "")})

    def recent_same_transfer(self, sender: str, receiver: str, amount_sun: int, now: int, window_s: int = 900) -> dict | None:
        """Grok-04 #3: 같은 보내는 계정·수취인·수량의 최근 결과(완료/미확정/불명)가 있으면 먼저 보여준다(응답 유실 뒤 재서명 방지)."""
        for p in sorted(self.results_dir.glob("*.json")):
            res = json.loads(p.read_text(encoding="utf-8"))
            if res.get("state") not in ("FINAL_CONFIRMED_SOLIDITY", "ACCEPTED_UNCONFIRMED", "UNKNOWN"):
                continue
            rec = self.store.get(res.get("payment_id") or p.stem); od = (rec or {}).get("order") or {}
            if self._norm_sender(od.get("user_eoa")) == self._norm_sender(sender) and od.get("receiver") == receiver and int(od.get("amount_sun") or 0) == int(amount_sun) \
                    and now - int(od.get("created_at") or 0) <= window_s:
                return res
        return None

    def prepare(self, proposal_id: str, sender: str, confirm_resend: bool = False) -> dict:
        with self._prepare_lock:
            return self._prepare(proposal_id, sender, confirm_resend)

    def _prepare(self, proposal_id: str, sender: str, confirm_resend: bool = False) -> dict:
        p = self.proposals.get(str(proposal_id or ""))
        now = int(self.now_fn())
        if not p:
            return {"ok": False, "error": "제안을 찾을 수 없습니다(새로 요청해 주세요)."}
        if now >= p["expires_at"]:
            self.proposals.pop(proposal_id, None)
            return {"ok": False, "error": "제안이 만료되었습니다. 다시 요청해 주세요."}
        try:
            NT.addr20(sender)                               # API 표기는 base58 하나만 받는다(hex 41… 은 거부). 잠금·비교는 norm_sender 로 정규화
        except Exception:                                   # noqa: BLE001
            return {"ok": False, "error": "지갑 주소 형식이 올바르지 않습니다(base58 주소만, 지갑 연결을 다시 해주세요)."}
        if sender == p["address"]:
            return {"ok": False, "error": "보내는 지갑과 받는 주소가 같습니다. 다른 수취인을 지정해 주세요."}
        lock = self.sender_lock(sender, now)
        if lock:
            return {"ok": False, "error": lock, "locked": True}
        prev = self.recent_same_transfer(sender, p["address"], int(p["amount_sun"]), now)
        if prev and not confirm_resend:
            return {"ok": False, "error": "같은 지갑에서 같은 수취인·수량의 전송 기록이 최근 15분 안에 있습니다. 아래 이전 결과를 먼저 확인하세요. 정말 한 번 더 보내려면 [그래도 다시 보내기] 를 누르세요.",
                    "duplicate_of": prev}
        # 같은 보내는 계정의 기존 PENDING 주문은 새 내용으로 대체 → 취소(늦은 서명은 지문 불일치/취소 상태로 거부)
        superseded = []
        for od in self.store.pending_orders():
            if od.get("kind") == "nile_trx" and od.get("user_eoa") == sender:
                ok, _ = self.store.cancel(od["payment_id"], od["snapshot_sha256"])
                if ok:
                    superseded.append(od["payment_id"])
                    self._intent(od["payment_id"], "CANCELLED", reason="superseded by a new phone order")
        window_s = self.sign_window_s
        if p.get("deadline_s"):
            window_s = max(60, min(self.sign_window_s, int(p["deadline_s"])))                    # 사용자 기한 → 서명·전송 만료에 반영(최소 60초)
        fee_cap = int(p.get("fee_cap_sun") or self.fee_cap_sun)
        if fee_cap <= 0:
            return {"ok": False, "error": "예산이 수량과 같거나 작아 수수료를 낼 수 없습니다. 예산이나 수량을 다시 알려 주세요."}
        expire_at_ms = (now + window_s) * 1000
        try:
            spec = TX.TrxSpec(sender=sender, receiver=p["address"], amount_sun=int(p["amount_sun"]), expire_at_ms=expire_at_ms, fee_cap_sun=fee_cap)
            unsigned = TX.build_unsigned_trx(self.node, spec, now_ms=now * 1000, max_expiry_ms=self.sign_window_s * 1000 + 60_000)
            quote = TX.quote_trx(self.node, spec, raw_len_bytes=len(unsigned["raw_data_hex"]) // 2)
        except NT.NileTxError as e:
            return {"ok": False, "error": f"거래 작성/검사 실패: {e}"[:200]}
        if p.get("budget_sun") is not None and int(p["budget_sun"]) < spec.amount_sun + quote["worst_case_fee_sun"]:
            return {"ok": False, "error": f"예산 {int(p['budget_sun'])/1e6:.6f} TRX 로는 수량 {spec.amount_sun/1e6:.6f} + 최악 수수료 {quote['worst_case_fee_sun']/1e6:.6f} TRX 를 보낼 수 없습니다(수량을 낮추지 않습니다).", "quote": quote}
        if not quote["would_succeed"]:
            why = ("보내는 지갑이 Nile 에서 아직 활성화되지 않았습니다(TRX 0)." if not quote["sender_exists"] else
                   f"잔액 부족: 잔액 {quote['balance_sun']/1e6:.6f} TRX < 보낼 {spec.amount_sun/1e6:.6f} + 최악 수수료 {quote['worst_case_fee_sun']/1e6:.6f} TRX"
                   if quote["balance_sun"] < spec.amount_sun + quote["worst_case_fee_sun"] else
                   f"예상 최악 수수료 {quote['worst_case_fee_sun']/1e6:.6f} TRX 가 상한 {spec.fee_cap_sun/1e6:.6f} TRX 를 넘습니다")
            return {"ok": False, "error": why, "quote": quote, "superseded": superseded}
        payment_id = f"phone_trx_{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}_{unsigned['txID'][:8]}"
        snapshot = _sha({"raw_data_hex": unsigned["raw_data_hex"], "txID": unsigned["txID"], "sender": sender, "receiver": p["address"],
                         "amount_sun": spec.amount_sun, "fee_cap_sun": spec.fee_cap_sun, "expire_at_ms": expire_at_ms})
        order = {
            "payment_id": payment_id, "kind": "nile_trx", "network": "nile", "chain_id": NT.NILE_CHAIN_ID,
            "user_eoa": sender, "receiver": p["address"], "receiver_alias": p["alias"], "receiver_confirmed_at": p.get("address_confirmed_at"),
            "asset": "TRX", "amount_sun": spec.amount_sun, "amount_trx": f"{spec.amount_sun/1e6:.6f}".rstrip("0").rstrip("."),
            "fee_cap_sun": spec.fee_cap_sun, "fee_cap_trx": f"{spec.fee_cap_sun/1e6:.6f}".rstrip("0").rstrip("."),
            "budget_sun": p.get("budget_sun"), "deadline_s": p.get("deadline_s"), "sign_window_s": window_s, "kind_detail": p.get("kind_detail"),
            "expire_at_ms": expire_at_ms, "created_at": now, "request_text": p["text"], "proposal_id": proposal_id,
            "unsigned_tx": {"txID": unsigned["txID"], "raw_data": unsigned["raw_data"], "raw_data_hex": unsigned["raw_data_hex"], "visible": False},
            "tx_id": unsigned["txID"], "checks": unsigned["checks"], "ref_block_number": unsigned.get("ref_block_number"), "quote": quote,
            "snapshot_sha256": snapshot,
            "ai": p.get("ai") or {"mode": PC.AI_MODE, "note": PC.AI_NOTE},
            "signing": {"who": "휴대폰 지갑(보내는 계정) 1회 서명", "mac_signs": False, "mac_role": "검증·방송·조회만"},
            "explorer": EXPLORER + unsigned["txID"],
        }
        try:
            rec = self.store.put_pending(order)
        except ValueError as e:
            return {"ok": False, "error": f"주문 충돌: {e}"[:200]}
        if rec.get("state") != "PENDING" or rec["order"].get("created_at") != now:
            return {"ok": False, "error": f"같은 거래(txID {unsigned['txID'][:8]}…)가 이미 {rec.get('state')} 상태로 있습니다. 잠시 뒤 다시 요청하세요(동일 거래 재생성 금지)."}
        self._intent(payment_id, "DRAFTED", user=sender, receiver=p["address"], value=spec.amount_sun, max_fee=spec.fee_cap_sun, tx_hash=unsigned["txID"])
        self._intent(payment_id, "AWAITING_HUMAN", user=sender)
        return {"ok": True, "order": order, "superseded": superseded}

    # ── 휴대폰 서명 제출 → 방송 ────────────────────────────────────────────
    def submit_signed(self, payment_id: str, snapshot_sha256: str, signed_tx: dict, *, pre_broadcast_guard=None) -> dict:
        now = int(self.now_fn())
        same = self._same_signature_already_stored(payment_id, snapshot_sha256, signed_tx)
        if same is not None:
            return same                                    # VP 9/29: 동일 주문·동일 txID·동일 서명본 재제출 → 기존 상태 반환, 방송 중복 없음
        ok, msg = self.store.store_signature(payment_id, snapshot_sha256, {"signed_tx": signed_tx}, now=now)
        if not ok:
            same = self._same_signature_already_stored(payment_id, snapshot_sha256, signed_tx)   # Grok-05 #3: 동시 최초 제출 경합 → 진 쪽도 idempotent 응답
            if same is not None:
                return same
            self._dump_refused(payment_id, msg, signed_tx)   # 진단용: 지갑이 돌려준 서명 거래 원문(공개 데이터: raw/서명/txID) 보존
            return self._not_submitted(payment_id, msg)
        signed, why = self.store.consume(payment_id, snapshot_sha256)
        if not signed:
            return self._not_submitted(payment_id, f"consume failed: {why}")
        rec = self.store.get(payment_id); order = rec["order"]; spec = spec_from_order(order); txid = order["unsigned_tx"]["txID"]

        def refuse(reason: str, state: str = "NOT_SUBMITTED"):
            self.store.quarantine(payment_id, snapshot_sha256, reason)
            # A final guard may refuse after broadcast reservation. Keep that reservation unresolved.
            self._intent(payment_id, "UNKNOWN" if self.intents.state(payment_id) == "SUBMITTED" else "CANCELLED", reason=reason[:200])
            self._result(payment_id, {"state": "SIGNATURE_HELD", "reason": reason[:200], "payment_id": payment_id, "tx_hash": txid, "held_signature": True,
                                      "note": "서명본은 만료 전까지 유효할 수 있어 같은 지갑 새 주문을 체인 근거로 종결될 때까지 막는다."})
            # 응답 = 저장 상태(SIGNATURE_HELD). 방송 안 함은 not_broadcast 로 표시. 화면은 이 값을 '미송금 종결'로 취급하지 않는다.
            return {"state": "SIGNATURE_HELD", "not_broadcast": True, "reason": reason[:200], "payment_id": payment_id, "tx_hash": txid, "signature_quarantined": True,
                    "order_state": "SIGNED_REFUSED_NOT_BROADCAST", "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

        try:
            body = TX.verify_signed_trx(order["unsigned_tx"], signed["signed_tx"], spec, now_ms=now * 1000, max_expiry_ms=self.sign_window_s * 1000 + 60_000)
        except NT.NileTxError as e:
            return refuse(f"signature refused at executor: {e}")
        try:
            q2 = TX.quote_trx(self.node, spec, raw_len_bytes=len(order["unsigned_tx"]["raw_data_hex"]) // 2)
        except NT.NileTxError as e:
            return refuse(f"pre-broadcast requote failed: {e}")
        if not q2["would_succeed"]:
            return refuse(f"pre-broadcast check failed: balance {q2['balance_sun']} sun, worst fee {q2['worst_case_fee_sun']} sun, cap {spec.fee_cap_sun}")
        self._intent(payment_id, "SIGNED", user=spec.sender)
        ok, why = self.intents.reserve_broadcast(payment_id, spec.sender, txid, scope=TX.SCOPE_NILE_TRX)
        if not ok:
            self.store.quarantine(payment_id, snapshot_sha256, why)
            st = self.intents.state(payment_id)
            self._result(payment_id, {"state": "UNKNOWN" if st in ("SUBMITTED", "UNKNOWN", "ACCEPTED") else "SIGNATURE_HELD", "held_signature": st not in ("SUBMITTED", "UNKNOWN", "ACCEPTED"),
                                      "reason": f"blocked before broadcast: {why}"[:220], "payment_id": payment_id, "tx_hash": txid})
            return {"state": "UNKNOWN" if st in ("SUBMITTED", "UNKNOWN", "ACCEPTED") else "SIGNATURE_HELD", "not_broadcast": True, "reason": f"blocked before broadcast: {why}"[:220],
                    "payment_id": payment_id, "tx_hash": txid, "signature_quarantined": True, "order_state": "SIGNED_REFUSED_NOT_BROADCAST"}
        bcast = {"raw_data": body["raw_data"], "raw_data_hex": body["raw_data_hex"], "signature": body["signature"], "txID": txid, "visible": False}
        if pre_broadcast_guard is not None:
            try:
                allowed, guard_reason = pre_broadcast_guard()
            except Exception as e:                          # noqa: BLE001 — failure to check cannot authorize broadcasting
                allowed, guard_reason = False, f"guard failed ({type(e).__name__})"
            if not allowed:
                held = refuse(f"final broadcast guard refused: {guard_reason}")
                return {**held, "state": "NOT_SUBMITTED", "broadcast_guard_refused": True}
        try:
            r = self.node.broadcast(bcast)
        except Exception as e:                              # noqa: BLE001
            self._intent(payment_id, "UNKNOWN", reason=f"broadcast transport: {e}"[:200])
            return self._result(payment_id, {"state": "UNKNOWN", "reason": f"broadcast transport error: {e}"[:200], "payment_id": payment_id, "tx_hash": txid})
        cls, detail = NT.classify_broadcast(r)
        if cls == "REJECTED":
            # VP 9/29: 확정 전 실패 응답만으로 최종 실패·잠금 해제를 선언하지 않는다 → UNKNOWN 으로 두고 만료 확정 절차로 종결
            self._intent(payment_id, "UNKNOWN", reason=f"node rejected at validation: {detail}"[:200])
            return self._result(payment_id, {"state": "REJECTED_BY_NODE_UNCONFIRMED", "reason": f"node rejected at validation: {detail}"[:200], "payment_id": payment_id, "tx_hash": txid,
                                             "note": "노드가 검증 단계에서 거절했지만 확정 근거(확정 블록 시각·확정 조회) 전에는 종결하지 않음. 같은 지갑 새 주문은 종결 뒤."})
        if cls == "UNKNOWN":
            self._intent(payment_id, "UNKNOWN", reason=detail[:200])
            return self._result(payment_id, {"state": "UNKNOWN", "reason": f"broadcast result unknown: {detail}; resolve by txID only", "payment_id": payment_id, "tx_hash": txid})
        self._intent(payment_id, "ACCEPTED", reason="broadcast accepted by node")
        last = None
        for i in range(self.receipt_polls):
            if i:
                self.sleep_fn(self.poll_s)
            try:
                last = TX.reconcile_receipt_trx(order["unsigned_tx"], spec, TX.fetch_receipt_trx(self.node, txid))
            except Exception as e:                          # noqa: BLE001
                last = {"verdict": "LOOKUP_ERROR", "error": str(e)[:120]}
            if last.get("verdict") in ("CONFIRMED", "FAILED"):
                break
        return self._finish(payment_id, txid, last or {"verdict": "NOT_FOUND"})

    def _same_signature_already_stored(self, payment_id: str, snapshot_sha256: str, signed_tx) -> dict | None:
        """이미 서명이 저장된 주문에 같은 서명본(같은 txID·같은 signature)이 다시 오면(응답 유실 뒤 재제출) 기존 상태를 그대로 돌려준다.
        방송·상태 전이 없음. 다른 서명본이면 None(정상 경로에서 거부됨)."""
        rec = self.store.get(payment_id)
        if not rec or rec.get("order", {}).get("snapshot_sha256") != snapshot_sha256 or not rec.get("signed"):
            return None
        st = rec["signed"].get("signed_tx") or {}
        if not isinstance(signed_tx, dict):
            return None
        if str(signed_tx.get("txID") or "").lower() != str(st.get("txID") or "").lower() or (signed_tx.get("signature") or None) != (st.get("signature") or None):
            return None
        res = self._load_result(payment_id) or {}
        state = res.get("state") or ("SIGNATURE_HELD" if rec["state"] in ("SIGNED_THEN_WITHDRAWN", "SIGNED_REFUSED_NOT_BROADCAST") else "PROCESSING")   # 결과 파일 전(다른 요청이 방송 처리 중) = PROCESSING
        if res.get("state") in ("ACCEPTED_UNCONFIRMED", "UNKNOWN") and res.get("tx_hash"):
            res = self.status(payment_id).get("result") or res; state = res.get("state") or state
        return {**res, "state": state, "payment_id": payment_id, "tx_hash": rec["order"]["tx_id"], "idempotent": True, "order_state": rec["state"],
                "note": "같은 서명본 재제출: 기존 상태를 돌려줌(방송 중복 없음).", "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    def list_orders(self, sender: str, limit: int = 10) -> list[dict]:
        """연결된 지갑(sender)의 주문 요약(읽기 전용): 결과 상태·txID·수량·수취인. 서명본은 넣지 않는다."""
        sender = self._norm_sender(sender)
        if not sender:
            return []
        out = []
        for p in sorted(self.store.root.glob("phone_trx_*.json"), reverse=True):
            rec = json.loads(p.read_text(encoding="utf-8")); od = rec.get("order") or {}
            if od.get("kind") != "nile_trx" or self._norm_sender(od.get("user_eoa")) != sender:
                continue
            res = self._load_result(od["payment_id"]) or {}
            rc = res.get("receipt") or {}
            out.append({"payment_id": od["payment_id"], "created_at": od.get("created_at"), "receiver_alias": od.get("receiver_alias"), "receiver": od.get("receiver"),
                        "amount_trx": od.get("amount_trx"), "tx_hash": od.get("tx_id"), "order_state": rec.get("state"), "result_state": res.get("state"),
                        "block_number": rc.get("block_number"), "fee_sun": rc.get("fee_sun"), "fee_known": rc.get("fee_known"), "fee_note": rc.get("fee_note"),
                        "explorer": od.get("explorer"), "expire_at_ms": od.get("expire_at_ms")})
            if len(out) >= limit:
                break
        return out

    def _dump_refused(self, payment_id: str, msg: str, signed_tx) -> None:
        try:
            d = self.results_dir.parent / "phone_refused"; d.mkdir(parents=True, exist_ok=True)
            rec = self.store.get(payment_id); un = ((rec or {}).get("order") or {}).get("unsigned_tx") or {}
            sraw = str((signed_tx or {}).get("raw_data_hex") or ""); uraw = str(un.get("raw_data_hex") or "")
            info = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "payment_id": payment_id, "reason": msg, "signed_keys": sorted((signed_tx or {}).keys()),
                    "signed_txID": (signed_tx or {}).get("txID"), "unsigned_txID": un.get("txID"), "signed_raw_len": len(sraw) // 2, "unsigned_raw_len": len(uraw) // 2,
                    "raw_equal": sraw.lower() == uraw.lower(), "signed_raw_data_hex": sraw, "unsigned_raw_data_hex": uraw,
                    "signed_raw_data_json": (signed_tx or {}).get("raw_data"), "signature": (signed_tx or {}).get("signature"), "visible": (signed_tx or {}).get("visible")}
            (d / f"{payment_id}_{int(time.time())}.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:                              # noqa: BLE001
            sys.stderr.write(f"[phone_flow] dump refused failed: {e}\n")

    def _not_submitted(self, payment_id: str, msg: str) -> dict:
        """서명 저장/소비 거부(중복 제출·지문 불일치·취소됨 등). 이번 제출은 방송되지 않았지만 **원래 주문의 상태·결과를 함께 돌려주어**
        화면이 NOT_SUBMITTED 만 보고 '미송금'으로 종결하지 않게 한다(VP 9/29)."""
        rec = self.store.get(payment_id)
        return {"state": "NOT_SUBMITTED", "reason": msg, "payment_id": payment_id, "this_submission_broadcast": False,
                "order_state": rec.get("state") if rec else None, "tx_hash": (rec or {}).get("order", {}).get("tx_id"),
                "prior_result": self._load_result(payment_id), "intent_state": self.intents.state(payment_id),
                "note": "이번 제출은 전송되지 않았지만, 원래 주문의 서명이 이미 저장·전송됐을 수 있다. 원래 주문 상태(order_state 와 prior_result)로 판단하고 새 주문·재서명하지 않는다.",
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    # ── 결과 조회(같은 txID 재조회만) ───────────────────────────────────────
    def status(self, payment_id: str) -> dict:
        rec = self.store.get(payment_id)
        if rec is None:
            return {"ok": False, "error": "order not found"}
        order = rec["order"]; now = int(self.now_fn())
        exp_ms = int(order["expire_at_ms"]); server_expired = now * 1000 >= exp_ms
        node_ms = self._solid_time_ms() if server_expired else 0                       # Grok-05 #5: 서버 시계만으로 만료를 선언하지 않음 — 확정 블록 시각도 지나야 함
        expired = bool(server_expired and node_ms and node_ms > exp_ms)
        expiry_reason = ("server_clock+solidified_block_time" if expired else ("server_clock_only(node_time_unavailable_or_not_yet)" if server_expired else None))
        out = {"ok": True, "payment_id": payment_id, "order_state": rec["state"], "tx_hash": order["tx_id"], "explorer": order.get("explorer"),
               "expired": expired, "expiry_reason": expiry_reason, "server_time_ms": now * 1000, "solid_time_ms": node_ms or None, "expire_at_ms": exp_ms,
               "intent_state": self.intents.state(payment_id),
               "signature_stored": rec["state"] in ("SIGNED_VERIFIED", "CONSUMED", "SIGNED_THEN_WITHDRAWN", "SIGNED_REFUSED_NOT_BROADCAST"),
               "client_hold": self._hold_path(payment_id).exists(),
               "auto_expiry_settle": self.auto_expiry_settle}
        res = self._load_result(payment_id)
        if res and res.get("state") in ("ACCEPTED_UNCONFIRMED", "UNKNOWN", "REJECTED_BY_NODE_UNCONFIRMED", "FAILED_UNCONFIRMED") and res.get("tx_hash"):
            try:
                rc = TX.reconcile_receipt_trx(order["unsigned_tx"], spec_from_order(order), TX.fetch_receipt_trx(self.node, res["tx_hash"]))
            except Exception as e:                          # noqa: BLE001
                rc = {"verdict": "LOOKUP_ERROR", "error": str(e)[:120]}
            if rc.get("verdict") in ("NOT_FOUND", "LOOKUP_ERROR"):
                cur = self.intents.state(payment_id)
                if cur == "ACCEPTED" and rc.get("verdict") == "NOT_FOUND":
                    self._intent(payment_id, "UNKNOWN", reason="previously in block, now absent from lookups")   # 원장도 UNKNOWN 으로 되돌려 안전 종결 절차 진입
                res = self._expiry_settle(payment_id, res["tx_hash"], order, res, rc)
            else:
                res = self._finish(payment_id, res["tx_hash"], rc)
        elif res and res.get("state") == "SIGNATURE_HELD":
            res = self._settle_held_signature(order, res) or res
        out["result"] = res
        return out

    @staticmethod
    def _norm_sender(s):
        return norm_sender(s)

    def _solid_time_ms(self) -> int:
        """확정(solidified) 블록 시각(walletsolidity/getnowblock). 못 읽으면 0 → 만료 판정 불가(해제하지 않음). 미확정 최신 블록 시각은 쓰지 않는다."""
        try:
            nb = self.node.now_block(solid=True)
            if not NT._ok(nb):
                return 0
            return int(((nb.get("body") or {}).get("block_header") or {}).get("raw_data", {}).get("timestamp") or 0)
        except Exception:                                   # noqa: BLE001
            return 0

    def reject(self, payment_id: str, snapshot_sha256: str) -> dict:
        ok, msg = self.store.cancel(payment_id, snapshot_sha256)
        if ok and self.intents.state(payment_id) in ("DRAFTED", "AWAITING_HUMAN"):
            self._intent(payment_id, "CANCELLED", reason="user rejected on phone")
        rec = self.store.get(payment_id)
        if ok and rec and rec.get("state") == "SIGNED_THEN_WITHDRAWN":
            self._result(payment_id, {"state": "SIGNATURE_HELD", "payment_id": payment_id, "tx_hash": rec["order"]["tx_id"], "held_signature": True,
                                      "reason": "withdrawn after signature stored", "note": "서명본은 만료 전까지 유효할 수 있어 같은 지갑 새 주문을 체인 근거로 종결될 때까지 막는다."})
        return {"ok": ok, "detail": msg if ok else None, "error": None if ok else msg}

    # ── 내부 ──────────────────────────────────────────────────────────────
    def _finish(self, payment_id: str, txid: str, rc: dict) -> dict:
        v = rc.get("verdict")
        cur = self.intents.state(payment_id)
        if v == "CONFIRMED":
            state = "FINAL_CONFIRMED_SOLIDITY"; self._intent_if(payment_id, "CONFIRMED", cur, ("ACCEPTED", "UNKNOWN"), reason="solidity confirmed, receipt matched")
        elif v == "ACCEPTED":
            state = "ACCEPTED_UNCONFIRMED"; self._intent_if(payment_id, "ACCEPTED", cur, ("ACCEPTED", "UNKNOWN"), reason="in block, awaiting solidity")
        elif v == "FAILED":
            state = "FAILED_ONCHAIN"; self._intent_if(payment_id, "FAILED", cur, ("ACCEPTED", "UNKNOWN"), reason="executed with failure (solidity)")
        elif v == "FAILED_UNCONFIRMED":
            state = "FAILED_UNCONFIRMED"                   # 확정 전 실패 응답 → 최종 실패 아님, 잠금 유지
        elif v == "MISMATCH":
            state = "UNKNOWN"; self._intent_if(payment_id, "UNKNOWN", cur, ("ACCEPTED",), reason="receipt mismatch; manual review")   # UNKNOWN→UNKNOWN 반복 기록은 하지 않음
        elif v == "PENDING":
            state = "ACCEPTED_UNCONFIRMED" if cur == "ACCEPTED" else "UNKNOWN"
        else:                                             # NOT_FOUND / LOOKUP_ERROR (방송 직후 폴링 중)
            state = "UNKNOWN"
        return self._result(payment_id, {"state": state, "payment_id": payment_id, "tx_hash": txid, "receipt": rc, "explorer": EXPLORER + txid,
                                         "note": "결과 불명/미확정은 같은 txID 재조회로만 종결. 새 거래 재전송 없음."})

    def _intent_if(self, pid, new, cur, allowed_from, **f):
        if cur in allowed_from:
            self._intent(pid, new, **f)

    def _intent(self, payment_id: str, state: str, **fields):
        try:
            if "scope" not in fields:
                fields["scope"] = TX.SCOPE_NILE_TRX
            self.intents.append(payment_id, state, **fields)
        except Exception as e:                              # noqa: BLE001
            sys.stderr.write(f"[phone_flow] intent append {state} failed for {payment_id}: {e}\n")

    def _result(self, payment_id: str, res: dict, save: bool = True) -> dict:
        res = {**res, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if save:
            p = self.results_dir / f"{payment_id}.json"; tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8"); os.replace(tmp, p)
        return res

    def _load_result(self, payment_id: str) -> dict | None:
        p = self.results_dir / f"{payment_id}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
