"""서명 파일 소비 경로(실행기 ↔ 서명 화면). executor.make_hooks(human_sign=FileSigner(store).sign) 로 연결한다.
- sign(order): 주문을 OrderStore 에 PENDING 으로 올리고, 사람이 /sign 에서 서명해 서버가 검증(SIGNED_VERIFIED)할 때까지 기다린 뒤
  consume(1회)으로 서명본을 받는다. 취소/기한 경과/timeout 이면 None(서명 없음 → 실행기는 NOT_SUBMITTED).
- 주문의 snapshot_sha256 로만 대상을 지정하므로 다른 주문의 서명/취소가 섞이지 않는다.
"""
from __future__ import annotations

import dataclasses
import time

from order_store import OrderStore


class FileSigner:
    def __init__(self, store: OrderStore, timeout_s: int = 600, poll_s: float = 1.0, sleep_fn=time.sleep, now_fn=time.time):
        self.store, self.timeout_s, self.poll_s, self.sleep_fn, self.now_fn = store, timeout_s, poll_s, sleep_fn, now_fn

    def sign(self, order) -> dict | None:
        od = dataclasses.asdict(order) if dataclasses.is_dataclass(order) else dict(order)
        try:
            rec = self.store.put_pending(od)          # 기존 기록이 있으면 상태 보존(재활성화 없음)
        except ValueError as e:
            return {"refused": f"order conflict: {e}"}
        if rec["state"] != "PENDING":
            return {"refused": self._reason(rec["state"])}
        deadline = int(od["expire_at_ms"]) // 1000 if od.get("kind") == "nile_trc20" else int(od["message"]["deadline"])
        t0 = self.now_fn()
        while True:
            rec = self.store.get(od["payment_id"])
            if rec is None:
                return {"refused": "order record missing"}
            if rec["state"] == "SIGNED_VERIFIED":
                signed, why = self.store.consume(od["payment_id"], od["snapshot_sha256"])
                return signed if signed else {"refused": f"consume failed: {why}"}
            if rec["state"] != "PENDING":
                return {"refused": self._reason(rec["state"])}
            now = self.now_fn()
            if now >= deadline or now - t0 > self.timeout_s:
                self.store.cancel(od["payment_id"], od["snapshot_sha256"])
                return {"refused": "deadline/timeout while waiting for signature (order cancelled server-side; wallet-side signature unknown)"}
            self.sleep_fn(self.poll_s)

    def refuse(self, order, reason: str) -> tuple[bool, str]:
        """실행기가 소비한 서명을 거절했을 때(비용/기한/사양 재검 실패) 격리. make_nile_hooks(on_refused=signer.refuse)."""
        od = dataclasses.asdict(order) if dataclasses.is_dataclass(order) else dict(order)
        return self.store.quarantine(od["payment_id"], od["snapshot_sha256"], reason)

    @staticmethod
    def _reason(state: str) -> str:
        return {"CANCELLED": "cancelled before a signature was stored (wallet-side signature unknown)",
                "SIGNED_THEN_WITHDRAWN": "signature exists but was withdrawn by the user — NOT submitted",
                "CONSUMED": "signature already consumed once — no second use",
                "SIGNED_REFUSED_NOT_BROADCAST": "signature was refused by the executor and quarantined — NOT broadcast"}.get(state, f"order state {state}")
