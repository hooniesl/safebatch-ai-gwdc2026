"""외부 채팅(Telegram 등) → 송금 intent (9/29 사장 선택: "텔레그램에서 지시 → TronLink 로 연결", VP_REMOTE_MINIMAL_APPROVAL_20260929 §2·§3,
VP_REVIEW_TG_IMPL_20260929 §2~§4 보완).

역할: 외부 채팅 한 문장을 **intent 1건**으로 고정하고(외부 메시지 식별자로 멱등), 승인 링크(/a/<token>)를 만든다.
링크로 연 페이지는 새 chat(AI) 호출 없이 같은 intent 를 불러와 **미서명 주문(prepare)** 을 최대 1건만 만들고, 서명은 페이지의 명시적 클릭으로만 한다.

불변식:
- 멱등은 **메시지 키(source:user:message_id)** 로만 한다. 같은 키 재전달 → 같은 intent(새 AI 호출·새 주문 없음). 같은 키에 다른 내용·다른 지갑 → 충돌(IntentConflict) 거부.
  '같은 문장' 만으로 병합하지 않는다(다른 발신 지갑·수량·수취인이 합쳐지는 결함 제거). 모든 키는 rec["keys"] 에 영구 기록되어 재기동 뒤에도 조회된다.
- **내구 예약**: AI 계층(chat) 호출 전에 CREATING 기록을 먼저 쓴다. chat 뒤 기록 전에 중단되면 같은 키의 재전달은 CREATING 을 돌려주고 **추가 AI 호출을 하지 않는다**(결과 불명 → 사용자가 새 문장으로 다시 요청).
- intent 는 발신 지갑(sender) 이 정해져야 주문을 만든다. 연결된 지갑이 intent.sender 와 다르면 주문을 만들지 않는다.
- intent 1건 = 주문 최대 1건. prepare 전에 PREPARING 표시를 쓰고, 이미 만든 주문(PENDING·미만료)이 있으면 돌려준다. 주문 생성 뒤 결합 전에 중단되면
  같은 제안(proposal_id)·같은 지갑의 PENDING 주문을 찾아 결합한다(추가 생성 없음). 종결됐으면 결과만 돌려준다.
- 시험 모드(test_mode): 해석은 명시적 모의 제공자(MockKiln, 운영 예산 미소모), 주문은 만들 수 있으나 **서명본 제출·보관 등록을 서버가 거부**한다(페이지도 승인 버튼을 숨김).
- 승인 토큰은 링크에만 있고 로그에 남기지 않는다. 토큰 소지·지갑 주소 문자열은 사용자 신원 증명이 아니다(서명 지갑 검증·tailnet 한정 HTTPS 와 함께 쓰는 접근 범위 제한일 뿐).
- 파일 잠금(fcntl)으로 프로세스·스레드 간 생성/결합을 직렬화한다. 기록은 0600 임시 파일 → fsync → 원자 교체 → 디렉터리 fsync.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import time
from decimal import Decimal

INTENT_TTL_S = 900          # 링크로 새 주문을 만들 수 있는 시간(제안 TTL 과 동일). 지난 뒤에는 기존 주문 조회만
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
TERMINAL = ("FINAL", "FAILED", "ORDER_CANCELLED")


class IntentConflict(Exception):
    """같은 메시지 키로 다른 내용/지갑이 왔다(재전달이 아니라 위조·오류) → 거부."""


def _ns(flow, s):
    """지갑 주소 비교용 정규화(base58/hex → base58). 없으면 None."""
    return flow._norm_sender(s) if s else None


def _norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())[:300]


class IntentStore:
    def __init__(self, root: pathlib.Path, now_fn=time.time):
        self.root = pathlib.Path(root); self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.now_fn = now_fn
        import threading
        self._tlock = threading.Lock()

    # ── 잠금·저장 ─────────────────────────────────────────────────────────
    class _Lock:
        def __init__(self, path: pathlib.Path):
            self.path = path; self.fd = None
        def __enter__(self):
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600); fcntl.flock(self.fd, fcntl.LOCK_EX); return self
        def __exit__(self, *a):
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)
            except OSError:
                pass

    def _locked(self):
        return self._Lock(self.root / ".lock")

    def _path(self, intent_id: str) -> pathlib.Path:
        return self.root / f"{intent_id}.json"

    def _write(self, rec: dict) -> dict:
        p = self._path(rec["intent_id"]); tmp = p.with_name(f".{rec['intent_id']}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp.exists():
                tmp.unlink()
        return rec

    def get(self, intent_id: str) -> dict | None:
        p = self._path(str(intent_id or ""))
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def all(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            if p.name.startswith("."):
                continue
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:                               # noqa: BLE001
                continue
        return out

    def by_token(self, token: str) -> dict | None:
        if not TOKEN_RE.match(str(token or "")):
            return None
        for r in self.all():
            if secrets.compare_digest(r.get("token", ""), token):
                return r
        return None

    def by_external_key(self, key: str) -> dict | None:
        for r in self.all():
            if key in (r.get("keys") or []) or r.get("external_key") == key:
                return r
        return None

    # ── 생성(메시지 키 멱등·내구 예약) ────────────────────────────────────
    def create(self, flow, *, text: str, sender: str | None, source: str, external_key: str, external_user: str,
               sender_label: str | None = None, test_mode: bool = False) -> tuple[dict, bool]:
        """(intent, created). 같은 키 → 기존 intent(AI 호출 0). 같은 키·다른 내용 → IntentConflict.
        CREATING 기록을 chat 전에 쓴다(중단 뒤 재전달이 추가 AI 호출을 만들지 않게)."""
        text_n = _norm_text(text)
        with self._tlock, self._locked():
            now = int(self.now_fn())
            ex = self.by_external_key(external_key)
            if ex:
                if ex.get("text") != text_n or _ns(flow, ex.get("sender")) != _ns(flow, sender) or ex.get("external_user") != external_user:
                    raise IntentConflict("same message key with different content/wallet/user")
                return ex, False
            rec = {"intent_id": secrets.token_hex(6), "token": secrets.token_urlsafe(24), "source": source, "external_key": external_key, "keys": [external_key],
                   "external_user": external_user, "text": text_n, "sender": sender, "sender_label": sender_label, "test_mode": bool(test_mode),
                   "created_at": now, "expires_at": now + INTENT_TTL_S, "state": "CREATING", "payment_id": None,
                   "notify": {}, "history": [{"t": now, "state": "CREATING"}]}
            self._write(rec)                                    # 내구 예약: 여기서 죽어도 같은 키는 CREATING 으로 조회된다
        # AI 계층 1회(잠금 밖: 느린 호출이 다른 사용자의 접수를 막지 않게). 시험 모드는 명시적 모의 제공자·운영 예산 미소모
        try:
            chat = flow.chat_test(text_n) if test_mode else flow.chat(text_n)
        except Exception as e:                                # noqa: BLE001
            with self._tlock, self._locked():
                cur = self.get(rec["intent_id"]) or rec
                cur["state"] = "CREATE_FAILED"; cur["error"] = str(e)[:160]; cur["history"].append({"t": int(self.now_fn()), "state": "CREATE_FAILED"}); self._write(cur)
            return cur, True
        with self._tlock, self._locked():
            cur = self.get(rec["intent_id"]) or rec
            cur.update({"kind": chat.get("kind"), "kind_detail": chat.get("kind_detail"), "proposal_id": chat.get("proposal_id"),
                        "proposal": {k: v for k, v in (chat.get("proposal") or {}).items() if k in ("alias", "address", "amount_trx", "amount_sun", "network")},
                        "proposal_full": (flow.proposals.get(chat.get("proposal_id")) if chat.get("proposal_id") else None),
                        "question": chat.get("question"), "explain": chat.get("explain"), "reason_code": chat.get("reason_code"), "confirm_note": chat.get("confirm_note"),
                        "understood": chat.get("understood"), "ai": chat.get("ai"), "fee_cap_trx": chat.get("fee_cap_trx"),
                        "state": {"proposal": "PROPOSED", "question": "QUESTION", "decline": "DECLINED"}.get(chat.get("kind"), "OTHER")})
            cur["history"].append({"t": int(self.now_fn()), "state": cur["state"]})
            self._write(cur)
            return cur, True

    # ── 주문 결합(재진입·중단 복구 멱등) ─────────────────────────────────
    def _find_pending_for(self, flow, rec: dict, sender: str) -> dict | None:
        """주문 생성 뒤 결합 전 중단 복구: 같은 제안·같은 지갑의 PENDING 주문(추가 생성 없이 결합)."""
        for od in flow.store.pending_orders():
            if od.get("kind") == "nile_trx" and od.get("proposal_id") == rec.get("proposal_id") and flow._norm_sender(od.get("user_eoa")) == flow._norm_sender(sender):
                return od
        return None

    def prepare(self, flow, *, token: str, sender: str, confirm_resend: bool = False) -> dict:
        with self._tlock, self._locked():
            rec = self.by_token(token)
            now = int(self.now_fn())
            if not rec:
                return {"ok": False, "error": "승인 링크를 찾을 수 없습니다(만료·삭제·잘못된 링크)."}
            if rec.get("state") == "CREATING":
                return {"ok": False, "error": "이 요청은 접수 처리가 끝나지 않았습니다(결과 불명). 채팅에서 새 문장으로 다시 요청하세요.", "intent": self.view(rec, flow)}
            if rec.get("kind") != "proposal":
                return {"ok": False, "error": "이 요청은 주문 대상이 아닙니다(질문 또는 거절).", "intent": self.view(rec, flow)}
            if not rec.get("sender"):
                return {"ok": False, "error": "발신 지갑이 지정되지 않은 요청입니다. 채팅에서 지갑(아이폰/안드로이드)을 지정해 다시 요청하세요.", "intent": self.view(rec, flow)}
            if flow._norm_sender(sender) != flow._norm_sender(rec["sender"]):
                return {"ok": False, "error": f"연결된 지갑({sender[:6]}…{sender[-4:]})이 요청의 발신 지갑({rec.get('sender_label') or ''} {rec['sender'][:6]}…{rec['sender'][-4:]})과 다릅니다. 요청한 지갑으로 연결하세요.",
                        "wallet_mismatch": True, "intent": self.view(rec, flow)}
            if rec.get("payment_id"):
                existing = flow.store.get(rec["payment_id"])
                if existing:
                    res = flow.status(rec["payment_id"])
                    if existing.get("state") == "PENDING" and not res.get("expired"):
                        return {"ok": True, "order": existing["order"], "reused": True, "intent": self.view(rec, flow)}
                    return {"ok": False, "error": "이 요청의 주문은 이미 처리됐거나 만료되었습니다. 결과를 확인하세요(새 주문 없음).", "done": True, "status": res, "intent": self.view(rec, flow)}
            recovered = self._find_pending_for(flow, rec, sender)
            if recovered:                                       # 생성 뒤 결합 전 중단 → 기존 주문 결합(추가 생성 0)
                rec["payment_id"] = recovered["payment_id"]; rec["state"] = "ORDER_PENDING"
                rec["history"].append({"t": now, "state": "ORDER_PENDING", "payment_id": rec["payment_id"], "recovered": True}); self._write(rec)
                return {"ok": True, "order": recovered, "reused": True, "recovered": True, "intent": self.view(rec, flow)}
            if now >= int(rec.get("expires_at", 0)):
                return {"ok": False, "error": "승인 링크가 만료되었습니다. 채팅에서 다시 요청하세요(새 주문 없음).", "expired": True, "intent": self.view(rec, flow)}
            pid = rec.get("proposal_id")
            if pid and pid not in flow.proposals and rec.get("proposal_full"):
                flow.proposals[pid] = dict(rec["proposal_full"])   # 재기동으로 메모리 제안이 사라졌으면 저장한 같은 제안 복원(새 AI 호출 없음)
            rec["history"].append({"t": now, "state": "PREPARING"}); self._write(rec)
            r = flow.prepare(pid, sender, confirm_resend=bool(confirm_resend))
            if r.get("ok"):
                rec["payment_id"] = r["order"]["payment_id"]; rec["state"] = "ORDER_PENDING"
                rec["history"].append({"t": now, "state": "ORDER_PENDING", "payment_id": rec["payment_id"]})
            else:
                rec["history"].append({"t": now, "state": "PREPARE_FAILED", "error": str(r.get("error"))[:160]})
            self._write(rec)
            r["intent"] = self.view(rec, flow)
            return r

    # ── Telegram 승인만으로 송금(VP_TELEGRAM_ONLY_APPROVAL §2~§5) ─────────────
    def prepare_bg(self, flow, *, intent_id: str) -> dict:
        """백그라운드 준비: 지갑 연결 없이 intent.sender 로 미서명 거래를 서버가 고정하고 승인 대기로 표시. 주문 1건·재진입 재사용."""
        rec = self.get(intent_id)
        if not rec:
            return {"ok": False, "error": "intent not found"}
        if not rec.get("sender"):
            return {"ok": False, "error": "발신 지갑 미지정 — 승인 대기로 표시하지 않음"}
        r = self.prepare(flow, token=rec["token"], sender=rec["sender"], confirm_resend=bool(rec.get("confirm_resend")))
        with self._tlock, self._locked():
            rec = self.get(intent_id) or rec
            if r.get("ok"):
                o = r["order"]
                rec["awaiting_approval"] = True; rec["fingerprint"] = o["snapshot_sha256"]; rec["approval_expires_at"] = int(o["expire_at_ms"]) // 1000
                rec["summary"] = {"amount_trx": o["amount_trx"], "receiver_alias": o["receiver_alias"], "receiver": o["receiver"], "sender": o["user_eoa"], "sender_label": rec.get("sender_label"),
                                  "worst_fee_sun": int((o.get("quote") or {}).get("worst_case_fee_sun") or 0), "fee_cap_sun": int(o["fee_cap_sun"]), "max_deduct_sun": int(o["amount_sun"]) + min(int((o.get("quote") or {}).get("worst_case_fee_sun") or 0), int(o["fee_cap_sun"])),
                                  "expire_at": int(o["expire_at_ms"]) // 1000, "payment_id": o["payment_id"], "tx_id": o["tx_id"]}
                rec["history"].append({"t": int(self.now_fn()), "state": "AWAITING_APPROVAL"})
            else:
                rec["awaiting_approval"] = False; rec["prepare_error"] = str(r.get("error"))[:200]
            self._write(rec)
            r["intent"] = self.view(rec, flow)
            return r

    def pending_for_user(self, flow, external_user: str) -> list[dict]:
        """그 사용자(개인 대화)의 유효한 승인 대기 요청(미만료·미소비·주문 PENDING)."""
        now = int(self.now_fn()); out = []
        for r in self.all():
            if r.get("external_user") != external_user or not r.get("awaiting_approval") or r.get("approval"):
                continue
            if now >= int(r.get("approval_expires_at") or 0) or now >= int(r.get("expires_at") or 0):
                continue
            rec = flow.store.get(r.get("payment_id") or "")
            if not rec or rec.get("state") != "PENDING":
                continue
            out.append(r)
        return out

    # ── 실행 소유권·단계(§2): 주문별 exec 기록 하나. approve 와 resume 는 같은 _run_execution 만 호출한다 ──
    #    stage: RESERVED → SIGNING → SIGNED(서명본 내구 보관) → SUBMITTING → SUBMITTED | NOT_SUBMITTED | POLICY_REFUSED | SIGN_FAILED | LIMIT_UNKNOWN | INVESTIGATE
    #    소유권: owner 토큰(이 프로세스의 _live_exec 에 있으면 살아 있음) + pid. 살아 있는 작업은 시간 경과로 빼앗지 않는다. 죽은 소유자의 SIGNING 은 재서명 없이 INVESTIGATE.
    _live_exec: set = set()
    TERMINAL_STAGES = ("SUBMITTED", "NOT_SUBMITTED", "POLICY_REFUSED", "SIGN_FAILED", "LIMIT_UNKNOWN", "INVESTIGATE")

    def approve(self, flow, signer, policy, *, intent_id: str, approval_key: str, fingerprint: str, external_user: str, summary_msg_id=None, ledger=None) -> dict:
        """승인 소비(원자적 1회) → 단일 실행 경로. 재전달·동시 승인·재시작 뒤 중복 서명/방송 없음."""
        with self._tlock, self._locked():
            rec = self.get(intent_id); now = int(self.now_fn())
            if not rec:
                return {"ok": False, "error": "intent not found"}
            if rec.get("external_user") != external_user:
                return {"ok": False, "error": "다른 사용자의 요청", "refused": True}
            ap = rec.get("approval")
            if ap:
                if ap.get("key") == approval_key:
                    ex = rec.get("exec") or {}
                    return {"ok": ex.get("stage") == "SUBMITTED", "idempotent": True, "state": ex.get("stage") or ap.get("state"), "result": ex.get("result"), "intent": self.view(rec, flow)}
                return {"ok": False, "error": "이미 다른 승인으로 처리된 요청(추가 실행 없음)", "already": True, "intent": self.view(rec, flow)}
            if not rec.get("awaiting_approval") or not rec.get("payment_id"):
                return {"ok": False, "error": "승인 대기 상태가 아닙니다", "refused": True}
            if rec.get("fingerprint") != fingerprint:
                return {"ok": False, "error": "요약과 거래 지문이 다릅니다(내용 변경) — 새 요약·새 승인 필요", "refused": True}
            if now >= int(rec.get("approval_expires_at") or 0):
                rec["awaiting_approval"] = False; rec["history"].append({"t": now, "state": "APPROVAL_EXPIRED"}); self._write(rec)
                return {"ok": False, "error": "승인 유효시간이 지났습니다(전송 없음). 새로 요청하세요", "expired": True}
            orec = flow.store.get(rec["payment_id"])
            if not orec or orec.get("state") != "PENDING" or orec["order"].get("snapshot_sha256") != fingerprint:
                return {"ok": False, "error": "주문이 더 이상 서명 가능 상태가 아닙니다", "refused": True, "order_state": (orec or {}).get("state")}
            rec["approval"] = {"key": approval_key, "t": now, "summary_msg_id": summary_msg_id, "state": "APPROVED", "fingerprint": fingerprint,
                               "payment_id": rec["payment_id"], "external_user": external_user}   # 내구 기록: 여기서 죽어도 같은 키만 재개
            rec["awaiting_approval"] = False; rec["history"].append({"t": now, "state": "APPROVED", "key": approval_key}); self._write(rec)
        return self._run_execution(flow, signer, policy, ledger, intent_id, reason="approve")

    def resume_approved(self, flow, signer, policy, *, intent_id: str, ledger=None) -> dict | None:
        """재시작 복구: 같은 단일 실행 경로. 승인 기록이 없으면 None."""
        rec = self.get(intent_id)
        if not rec or not rec.get("approval"):
            return None
        return self._run_execution(flow, signer, policy, ledger, intent_id, reason="resume")

    @staticmethod
    def _pid_alive(pid) -> bool:
        try:
            os.kill(int(pid), 0); return True
        except (OSError, TypeError, ValueError):
            return False

    def _set_stage(self, intent_id: str, stage: str, **extra) -> dict:
        with self._tlock, self._locked():
            rec = self.get(intent_id); ex = rec.get("exec") or {}
            ex["stage"] = stage; ex.update(extra); ex["t"] = int(self.now_fn()); rec["exec"] = ex
            if stage in self.TERMINAL_STAGES:
                ex["owner"] = None; rec["approval"]["state"] = stage
            rec["history"].append({"t": int(self.now_fn()), "state": "EXEC_" + stage}); self._write(rec)
            return ex

    def _execution_guard(self, flow, signer, policy, rec: dict, order: dict) -> tuple[bool, str]:
        """Validate the persisted approval, order bytes and current policy before each irreversible step."""
        from safebatch import trx_tx as TX
        try:
            if rec.get("test_mode"):
                return False, "test-mode intent cannot be signed or submitted"
            if getattr(signer, "kind", "none") == "local" and not policy.is_one_shot_trial():
                return False, "local signer requires the explicit one-shot trial policy"
            ok, why = policy.check_static(order)
            if not ok:
                return False, why
            now = int(self.now_fn()); ap = rec.get("approval") or {}
            if not ap or not ap.get("key") or not rec.get("fingerprint"):
                return False, "approval binding missing"
            if now >= min(int(rec["approval_expires_at"]), int(rec["expires_at"]), int(order["expire_at_ms"]) // 1000):
                return False, "approval or order expired before execution"
            if int(ap["t"]) > now or int(ap["t"]) >= int(rec["approval_expires_at"]):
                return False, "invalid approval time"
            if (rec["payment_id"] != order["payment_id"] or rec["proposal_id"] != order["proposal_id"]
                    or _ns(flow, rec.get("sender")) != _ns(flow, order["user_eoa"])):
                return False, "intent/order binding changed"
            for key in ("fingerprint", "payment_id", "external_user"):
                if key in ap and ap[key] != rec.get(key):
                    return False, "approval content changed"
            p = rec.get("proposal") or {}; summary = rec.get("summary") or {}
            if p.get("address") != order["receiver"] or int(p.get("amount_sun", -1)) != order["amount_sun"]:
                return False, "proposal/order binding changed"
            expected = {"payment_id": order["payment_id"], "tx_id": order["tx_id"], "sender": order["user_eoa"], "receiver": order["receiver"],
                        "amount_trx": order["amount_trx"], "fee_cap_sun": order["fee_cap_sun"], "expire_at": int(order["expire_at_ms"]) // 1000}
            if any(summary.get(k) != v for k, v in expected.items()):
                return False, "approved summary/order binding changed"
            u = order["unsigned_tx"]
            snapshot = {"raw_data_hex": u["raw_data_hex"], "txID": u["txID"], "sender": order["user_eoa"], "receiver": order["receiver"],
                        "amount_sun": order["amount_sun"], "fee_cap_sun": order["fee_cap_sun"], "expire_at_ms": order["expire_at_ms"]}
            digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if digest != rec["fingerprint"] or digest != order["snapshot_sha256"] or order["tx_id"] != u["txID"]:
                return False, "approved transaction fingerprint changed"
            spec = TX.TrxSpec(sender=order["user_eoa"], receiver=order["receiver"], amount_sun=order["amount_sun"],
                              expire_at_ms=order["expire_at_ms"], fee_cap_sun=order["fee_cap_sun"])
            TX.verify_unsigned_trx(u, spec, now_ms=now * 1000, max_expiry_ms=flow.sign_window_s * 1000 + 60_000)
            if getattr(signer, "kind", "none") != "none" and signer.address != order["user_eoa"]:
                return False, "signer does not match the approved sender"
        except (KeyError, ValueError, TypeError, IndexError, AttributeError, TX.TrxTxError):
            return False, "stored approval or unsigned transaction is invalid"
        return True, ""

    def _run_execution(self, flow, signer, policy, ledger, intent_id: str, reason: str) -> dict:
        import phone_signer as SG
        with self._tlock, self._locked():
            rec = self.get(intent_id); pid_ = rec.get("payment_id"); orec = flow.store.get(pid_ or "")
            if not orec:
                return {"ok": False, "error": "order not found", "state": "NO_ORDER"}
            order = orec["order"]; ex = rec.get("exec") or {}; stage = ex.get("stage")
            def out(ok, **k):
                return {"ok": ok, "state": ex.get("stage"), "result": ex.get("result"), "error": ex.get("error"), "resumed": reason == "resume", "intent": self.view(rec, flow), **k}
            if stage in self.TERMINAL_STAGES:
                if stage == "SUBMITTED" and reason == "resume":
                    return out(True, status_only=True)
                return out(stage == "SUBMITTED")
            if ex.get("owner") and (ex["owner"] in IntentStore._live_exec or (ex.get("pid") != os.getpid() and self._pid_alive(ex.get("pid")))):
                return out(False, busy=True, error="실행 중(다른 작업이 소유)")          # 살아 있는 작업은 빼앗지 않는다
            def stop_at(state, message):
                nonlocal ex
                ex = {**ex, "stage": state, "error": message, "owner": None}; rec["exec"] = ex
                rec["approval"]["state"] = state; rec["history"].append({"t": int(self.now_fn()), "state": "EXEC_" + state}); self._write(rec)
                return out(False)
            # Any existing signature/result is read-only, even when the stop file or expiry now blocks new activity.
            st = flow.status(order["payment_id"])
            if st.get("signature_stored") or st.get("result"):
                ex = {**ex, "stage": "SUBMITTED", "owner": None, "status_only": True,
                      "result": st.get("result") or {"state": "SUBMITTED_STATUS_ONLY", "payment_id": order["payment_id"], "tx_hash": order["tx_id"]}}
                rec["exec"] = ex; rec["approval"]["state"] = "SUBMITTED"; self._write(rec)
                return out(True, status_only=True)
            if stage in ("SIGNING", "SUBMITTING"):
                return stop_at("INVESTIGATE", "interrupted during signing/submission; no automatic re-sign or re-submit")
            if stage not in (None, "RESERVED", "SIGNED") or (stage in (None, "RESERVED") and ex.get("signed_tx")):
                return stop_at("INVESTIGATE", "unknown or inconsistent execution stage; reservation retained")
            if stage == "SIGNED" and not isinstance(ex.get("signed_tx"), dict):
                return stop_at("INVESTIGATE", "stored signature missing; no automatic re-sign")
            if orec.get("state") != "PENDING":
                return stop_at("POLICY_REFUSED", "order is no longer pending")
            ok, why = self._execution_guard(flow, signer, policy, rec, order)
            if not ok:
                return stop_at("POLICY_REFUSED", why)
            token = secrets.token_hex(8)
            if ledger is None:
                return stop_at("LIMIT_UNKNOWN", "limit ledger not configured")
            try:
                rr = ledger.reserve(order, policy, require_existing=bool(stage))
            except (SG.LedgerError, OSError, ValueError) as e:
                return stop_at("LIMIT_UNKNOWN", f"ledger: {e}"[:200])
            if not rr.get("ok"):
                return stop_at("POLICY_REFUSED", str(rr.get("error"))[:200])
            if rr["entry"]["state"] != "RESERVED":
                return stop_at("INVESTIGATE", "reservation was already consumed; result unknown, no re-sign")
            if not stage:
                ex = {"stage": "RESERVED", "reserved": rr.get("entry"), "attempts": 0}
            ex["owner"] = token; ex["pid"] = os.getpid(); ex["attempts"] = int(ex.get("attempts") or 0) + 1; rec["exec"] = ex
            rec["history"].append({"t": int(self.now_fn()), "state": "EXEC_START", "reason": reason, "stage": ex["stage"]}); self._write(rec)
            IntentStore._live_exec.add(token)

        approval_snapshot = dict(rec["approval"])

        def recheck(order_state="PENDING"):
            fresh = self.get(intent_id); current = flow.store.get(order["payment_id"])
            if not fresh or not current or current.get("state") != order_state or current.get("order") != order:
                return False, "order changed before execution", "POLICY_REFUSED"
            if fresh.get("approval") != approval_snapshot or (fresh.get("exec") or {}).get("owner") != token:
                return False, "approval or execution ownership changed", "POLICY_REFUSED"
            ok, why = self._execution_guard(flow, signer, policy, fresh, order)
            if not ok:
                return False, why, "POLICY_REFUSED"
            try:
                rr = ledger.reserve(order, policy, require_existing=True)
            except (SG.LedgerError, OSError, ValueError) as e:
                return False, f"ledger: {e}"[:200], "LIMIT_UNKNOWN"
            return (bool(rr.get("ok") and rr["entry"]["state"] == "RESERVED"), str(rr.get("error") or "reservation is not pending"), "POLICY_REFUSED")

        def final_broadcast_guard():
            ok, why, _ = recheck(order_state="CONSUMED")
            return ok, why

        try:
            if ex["stage"] == "RESERVED":
                if getattr(signer, "kind", "none") == "none":
                    self._set_stage(intent_id, "SIGN_FAILED", error="signer not configured; signing was not attempted")
                    try:
                        ledger.release(order["payment_id"], "no signer; signing was not attempted")
                    except (SG.LedgerError, OSError) as e:
                        self._set_stage(intent_id, "SIGN_FAILED", ledger_note=f"release failed, reservation retained: {e}"[:200])
                    return self._final(flow, intent_id, reason)
                self._set_stage(intent_id, "SIGNING")
                ok, why, failed_stage = recheck()
                if not ok:
                    self._set_stage(intent_id, failed_stage, error=why)
                    return self._final(flow, intent_id, reason)
                try:
                    signed = signer.sign(order["unsigned_tx"], order["user_eoa"])
                except Exception as e:                        # noqa: BLE001 — 호출자가 예외를 받았어도 서명 생성 여부는 불명
                    self._set_stage(intent_id, "INVESTIGATE", error=f"signing outcome unknown ({type(e).__name__}); reservation retained")
                    return self._final(flow, intent_id, reason)
                ex = self._set_stage(intent_id, "SIGNED", signed_tx=signed)            # 서명본 내구 보관(복구 시 재사용, 재서명 없음)
            if ex["stage"] == "SIGNED":
                ok, why, failed_stage = recheck()
                if not ok:
                    self._set_stage(intent_id, failed_stage, error=why)
                    return self._final(flow, intent_id, reason)
                ex = self._set_stage(intent_id, "SUBMITTING")
                res = flow.submit_signed(order["payment_id"], order["snapshot_sha256"], ex["signed_tx"],
                                         pre_broadcast_guard=final_broadcast_guard)    # 재견적·방송 예약 뒤에도 실제 방송 직전 정책을 재검사
                if res.get("state") == "NOT_SUBMITTED":
                    self._set_stage(intent_id, "NOT_SUBMITTED", result=res, error=res.get("reason"))   # 서명본은 존재 → 한도 예약 유지(임의 반환 없음)
                else:
                    self._set_stage(intent_id, "SUBMITTED", result=res)
                    self._ledger_consume(ledger, order["payment_id"], intent_id)
            return self._final(flow, intent_id, reason)
        finally:
            IntentStore._live_exec.discard(token)

    def _ledger_consume(self, ledger, payment_id: str, intent_id: str) -> None:
        """방송 시도 뒤 예약 → 소비. 실패하면 예약이 그대로 남아(합산 유지) 원장 증거가 사라지지 않는다."""
        import phone_signer as SG
        try:
            ledger.consume(payment_id)
        except (SG.LedgerError, OSError) as e:
            with self._tlock, self._locked():
                rec = self.get(intent_id); rec["exec"]["ledger_note"] = f"consume failed, reservation kept: {e}"[:200]; self._write(rec)

    def _final(self, flow, intent_id: str, reason: str) -> dict:
        rec = self.get(intent_id); ex = rec.get("exec") or {}
        return {"ok": ex.get("stage") == "SUBMITTED", "state": ex.get("stage"), "result": ex.get("result"), "error": ex.get("error"), "resumed": reason == "resume", "intent": self.view(rec, flow)}

    def order_allowed(self, rec: dict, payment_id: str) -> bool:
        """승인 링크 범위: 그 intent 에 결합된 주문만 조회·제출할 수 있다."""
        return bool(payment_id) and rec.get("payment_id") == payment_id

    # ── 알림 기록(회신 실패는 송금과 분리) ───────────────────────────────
    def note_notify(self, intent_id: str, state: str, ok: bool) -> None:
        with self._tlock, self._locked():
            rec = self.get(intent_id)
            if not rec:
                return
            n = rec.setdefault("notify", {}); cur = n.get(state) or {"attempts": 0}
            cur.update({"ok": bool(ok), "t": int(self.now_fn()), "attempts": int(cur.get("attempts", 0)) + 1}); n[state] = cur
            self._write(rec)

    # ── 조회(페이지·어댑터 공용) ──────────────────────────────────────────
    def view(self, rec: dict, flow=None) -> dict:
        """토큰·서명본 없는 공개 뷰. 주문이 있으면 현재 상태·결과를 같은 txID 조회로 갱신."""
        now = int(self.now_fn())
        v = {k: rec.get(k) for k in ("intent_id", "source", "text", "sender", "sender_label", "created_at", "expires_at", "kind", "kind_detail", "proposal", "question", "explain", "reason_code",
                                    "confirm_note", "ai", "fee_cap_trx", "state", "payment_id", "test_mode", "notify", "awaiting_approval", "fingerprint", "approval_expires_at", "summary", "prepare_error")}
        if rec.get("approval"):
            v["approval"] = {k: rec["approval"].get(k) for k in ("state", "t", "error")}
        if rec.get("exec"):
            v["exec"] = {k: rec["exec"].get(k) for k in ("stage", "attempts", "error", "ledger_note", "status_only")}
        v["link_expired"] = now >= int(rec.get("expires_at", 0))
        v["proposal_id"] = rec.get("proposal_id")
        if flow is not None and rec.get("payment_id"):
            st = flow.status(rec["payment_id"])
            v["order_state"] = st.get("order_state"); v["tx_hash"] = st.get("tx_hash"); v["expired"] = st.get("expired"); v["result"] = st.get("result")
            res = (st.get("result") or {}).get("state")
            v["state"] = ("FINAL" if res == "FINAL_CONFIRMED_SOLIDITY" else "UNKNOWN" if res in ("UNKNOWN", "ACCEPTED_UNCONFIRMED", "SIGNATURE_HELD", "REJECTED_BY_NODE_UNCONFIRMED", "FAILED_UNCONFIRMED")
                          else "FAILED" if res in ("FAILED_ONCHAIN", "EXPIRED_NOT_ON_CHAIN") else ("ORDER_SIGNED" if st.get("signature_stored") else ("ORDER_CANCELLED" if st.get("order_state") == "CANCELLED" else ("ORDER_EXPIRED" if st.get("expired") else "ORDER_PENDING"))))
        return v

    def summary_line(self, rec: dict) -> str:
        p = rec.get("proposal") or {}
        if rec.get("kind") == "proposal":
            return f"{p.get('amount_trx')} TRX → {p.get('alias')} ({p.get('address')}) · Nile · 보내는 지갑 {rec.get('sender_label') or '(미지정)'} {rec.get('sender') or ''}".strip()
        if rec.get("kind") == "question":
            return f"확인 필요: {rec.get('question')}"
        if rec.get("kind") == "decline":
            return f"거절: {rec.get('explain')} ({rec.get('reason_code')})"
        return f"상태 {rec.get('state')}"
