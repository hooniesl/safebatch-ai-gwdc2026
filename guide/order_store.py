"""서명 주문 저장소(주문별 상태·원자적 쓰기·flock). 9/28 부사장 검수 2·3 반영.

상태: PENDING → SIGNED_VERIFIED | CANCELLED | SIGNED_THEN_WITHDRAWN(서명 저장 뒤 사용자가 취소: 서명 자체는 무효화 못 하며 '제출 안 함' 만 보장) | CONSUMED(실행기가 소비).
- 모든 변경은 payment_id + snapshot_sha256 로 대상을 지정한다(오래된 A 화면이 현재 B 를 바꿀 수 없음).
- 서명 저장은 PENDING 에서만. 취소 뒤 늦은 서명은 거부. 서명 검증: message/domain/types 가 서버 원본과 동일 + 서명자 EOA 복구 == 주문 user + 기한 미경과.
- 파일 쓰기는 tmp→fsync→rename(원자), 디렉터리 flock 으로 동시 요청 직렬화.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import time

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from safebatch import tip712  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from safebatch import trx_tx as TX  # noqa: E402

STATES = ("PENDING", "SIGNED_VERIFIED", "CANCELLED", "SIGNED_THEN_WITHDRAWN", "CONSUMED", "SIGNED_REFUSED_NOT_BROADCAST")


class OrderStore:
    def __init__(self, root: pathlib.Path):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = self.root / ".lock"

    def _path(self, payment_id: str) -> pathlib.Path:
        safe = "".join(c if c.isalnum() or c in "-_:." else "_" for c in payment_id)
        return self.root / f"{safe}.json"

    def _write(self, path: pathlib.Path, obj: dict):
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _locked(self):
        return _Lock(self._lock)

    def put_pending(self, order: dict) -> dict:
        """신규 주문만 PENDING 으로 생성. 같은 payment_id 가 이미 있으면 상태를 덮어쓰지 않는다(9/28 GPT-02 #2):
        같은 지문 → 기존 기록 그대로 반환 / 다른 지문 → 충돌 거부(ValueError)."""
        with self._locked():
            existing = self.get(order["payment_id"])
            if existing is not None:
                if existing["order"]["snapshot_sha256"] != order["snapshot_sha256"]:
                    raise ValueError(f"order {order['payment_id']} exists with a different snapshot; refusing to overwrite (state {existing['state']})")
                return existing
            rec = {"state": "PENDING", "order": order, "history": [{"ts": int(time.time()), "state": "PENDING"}]}
            self._write(self._path(order["payment_id"]), rec)
            return rec

    def get(self, payment_id: str) -> dict | None:
        p = self._path(payment_id)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def pending_orders(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            rec = json.loads(p.read_text(encoding="utf-8"))
            if rec.get("state") == "PENDING":
                out.append(rec["order"])
        return out

    def _transition(self, rec: dict, new_state: str, **extra):
        rec["state"] = new_state
        rec["history"].append({"ts": int(time.time()), "state": new_state, **{k: v for k, v in extra.items() if k != "signed"}})
        for k, v in extra.items():
            rec[k] = v

    def cancel(self, payment_id: str, snapshot_sha256: str) -> tuple[bool, str]:
        with self._locked():
            rec = self.get(payment_id)
            if rec is None or rec["order"]["snapshot_sha256"] != snapshot_sha256:
                return False, "order not found or snapshot mismatch"
            if rec["state"] == "PENDING":
                self._transition(rec, "CANCELLED"); self._write(self._path(payment_id), rec)
                return True, "cancelled: server has no stored signature; whether the wallet already produced a signature is unknown; nothing submitted"
            if rec["state"] == "SIGNED_VERIFIED":
                self._transition(rec, "SIGNED_THEN_WITHDRAWN"); self._write(self._path(payment_id), rec)
                return True, "signature already exists; it cannot be revoked — order marked withdrawn, will NOT be submitted"
            return False, f"cannot cancel in state {rec['state']}"

    def store_signature(self, payment_id: str, snapshot_sha256: str, signed: dict, now: int | None = None) -> tuple[bool, str]:
        now = int(now if now is not None else time.time())
        with self._locked():
            rec = self.get(payment_id)
            if rec is None or rec["order"]["snapshot_sha256"] != snapshot_sha256:
                return False, "order not found or snapshot mismatch"
            if rec["state"] != "PENDING":
                return False, f"order is {rec['state']}; late/duplicate signature rejected"
            order = rec["order"]
            if order.get("kind") in ("nile_trc20", "nile_trx"):   # 일반 Nile / 휴대폰 TRX: 서명 거래(raw 바이트 동일·txID·서명자) 검증
                ok, why = (verify_signed_tx_against_order if order.get("kind") == "nile_trc20" else verify_signed_trx_against_order)(order, signed, now)
                if not ok:
                    return False, why
                self._transition(rec, "SIGNED_VERIFIED", signed={"signed_tx": signed["signed_tx"], "signer": why, "verified_at": now})
                self._write(self._path(payment_id), rec)
                return True, why
            ok, why = verify_signature_against_order(order, signed, now)
            if not ok:
                return False, why
            self._transition(rec, "SIGNED_VERIFIED", signed={"sig": signed["sig"].removeprefix("0x"), "signer": why, "verified_at": now})
            self._write(self._path(payment_id), rec)
            return True, why          # why == 복구된 서명자 EOA

    def consume(self, payment_id: str, snapshot_sha256: str) -> tuple[dict | None, str]:
        """실행기 소비: SIGNED_VERIFIED 에서만 서명본을 내주고 CONSUMED 로 전이(1회)."""
        with self._locked():
            rec = self.get(payment_id)
            if rec is None or rec["order"]["snapshot_sha256"] != snapshot_sha256:
                return None, "order not found or snapshot mismatch"
            if rec["state"] != "SIGNED_VERIFIED":
                return None, f"not consumable in state {rec['state']}"
            self._transition(rec, "CONSUMED"); self._write(self._path(payment_id), rec)
            if rec["order"].get("kind") in ("nile_trc20", "nile_trx"):
                return {"signed_tx": rec["signed"]["signed_tx"]}, "ok"
            return {"message": rec["order"]["message"], "sig": rec["signed"]["sig"]}, "ok"


    def quarantine(self, payment_id: str, snapshot_sha256: str, reason: str) -> tuple[bool, str]:
        """소비된 서명이 실행기 검증(비용/기한/사양)에서 거절됨 → 방송 금지 상태로 격리(서명 자체는 지갑이 만든 것이라 무효화 못 함, Grok-02#4)."""
        with self._locked():
            rec = self.get(payment_id)
            if rec is None or rec["order"]["snapshot_sha256"] != snapshot_sha256:
                return False, "order not found or snapshot mismatch"
            if rec["state"] not in ("CONSUMED", "SIGNED_VERIFIED"):
                return False, f"cannot quarantine in state {rec['state']}"
            self._transition(rec, "SIGNED_REFUSED_NOT_BROADCAST", refused_reason=str(reason)[:200]); self._write(self._path(payment_id), rec)
            return True, "signature quarantined: will NOT be broadcast by this system (wallet signature remains valid on-chain until its expiration)"


def verify_signature_against_order(order: dict, signed: dict, now: int) -> tuple[bool, str]:
    """서버 원본 주문 기준: domain/types/message 동일 + 기한 + 서명자 복구 == 승인 EOA. 통과 시 (True, signer)."""
    if json.dumps(signed.get("message"), sort_keys=True) != json.dumps(order["message"], sort_keys=True):
        return False, "signed message differs from server order"
    if json.dumps(signed.get("domain"), sort_keys=True) != json.dumps(order["domain"], sort_keys=True):
        return False, "signed domain differs from server order"
    if json.dumps(signed.get("types"), sort_keys=True) != json.dumps(order["types"], sort_keys=True):
        return False, "signed types differ from server order"
    if now >= int(order["message"]["deadline"]):
        return False, "order deadline passed"
    sig = str(signed.get("sig") or "").removeprefix("0x")
    if len(sig) != 130 or any(c not in "0123456789abcdefABCDEF" for c in sig):
        return False, "signature format"
    try:
        signer = tip712.recover_signer_tron(order["domain"], order["types"], "PermitTransfer", order["message"], sig)
    except Exception as e:
        return False, f"signature recovery failed: {e}"
    if not signer:
        return False, "signature recovery failed"
    if signer != order["message"]["user"]:
        return False, f"signer mismatch (recovered {signer[:6]}…, expected order user)"
    return True, signer


def verify_signed_trx_against_order(order: dict, signed: dict, now: int) -> tuple[bool, str]:
    """휴대폰 TRX 주문(kind=nile_trx): signed["signed_tx"] 의 raw_data_hex 가 서버 원본과 바이트 동일 + txID + 서명자 복구 == 주문 발신 EOA + 만료 전."""
    st = signed.get("signed_tx")
    if not isinstance(st, dict):
        return False, "signed_tx object required"
    try:
        spec = TX.TrxSpec(sender=order["user_eoa"], receiver=order["receiver"], amount_sun=int(order["amount_sun"]),
                          expire_at_ms=int(order["expire_at_ms"]), fee_cap_sun=int(order["fee_cap_sun"]))
        body = TX.verify_signed_trx(order["unsigned_tx"], st, spec, now_ms=int(now) * 1000)
    except NT.NileTxError as e:
        return False, str(e)
    except Exception as e:
        return False, f"signed tx verification failed: {e}"[:160]
    return True, body["signer"]


def verify_signed_tx_against_order(order: dict, signed: dict, now: int) -> tuple[bool, str]:
    """일반 Nile 주문: signed["signed_tx"] 의 raw_data_hex 가 서버 원본 미서명 거래와 바이트 동일 + txID + 서명자 복구 == 주문 발신 EOA + 만료 전."""
    st = signed.get("signed_tx")
    if not isinstance(st, dict):
        return False, "signed_tx object required"
    try:
        spec = NT.TransferSpec(sender=order["user_eoa"], receiver=order["receiver"], token=order["token"], amount_units=int(order["amount_units"]),
                               fee_limit_sun=int(order["trx_fee_limit_sun"]), expire_at_ms=int(order["expire_at_ms"]))
        body = NT.verify_signed(order["unsigned_tx"], st, spec, now_ms=int(now) * 1000)
    except NT.NileTxError as e:
        return False, str(e)
    except Exception as e:
        return False, f"signed tx verification failed: {e}"[:160]
    return True, body["signer"]


class _Lock:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "a+")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN); self.fh.close()
