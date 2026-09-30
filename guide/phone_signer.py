"""서명 인터페이스 분리 + 한도 원장 + 봇 지갑 키 보관 (VP_TELEGRAM_ONLY_APPROVAL §6, VP_REVIEW_TG_ONLY_SIGNER §1, 사장 A안 9/29 23:4x).

- signer: NoSigner(운영 기본, 서명 주체 미정 → 실행 0) · MockSigner(검사용 키, 모의 전용) · LocalKeySigner(A안: 별도 Nile 봇 전용 지갑, 암호화 키 파일).
- LimitLedger(§1): 서명 **전에** payment_id 에 묶인 한도 예약을 파일 잠금 안에서 원자적으로 기록(JSONL, 추가 전용). 예약→소비/해제 전이.
  비용 = 금액 + 수수료 상한(최악). 일자 기준 = 예약 시각의 KST 달력일. 미종결(RESERVED) 예약은 날짜가 바뀌어도 합산에 남는다.
  원장 미초기화·파손 행 → 불명으로 보고 **차단**(0 으로 취급하지 않음). 같은 주문의 복구는 새 예약을 만들지 않고 기존 예약을 돌려준다.
  해제는 signer 미설정처럼 서명 호출이 없었음이 확실할 때만. 서명 호출 뒤 예외·서명/방송 결과 불명은 예약을 유지한다.
  명시 1회 시험 정책은 발신·수취·정확한 금액과 lifetime 예약 1건(해제 이력 포함)을 묶고 날짜 변경으로 권한을 갱신하지 않는다.
- 앱 수준 한도(정책)는 이 서버 안에서만 강제된다. 체인이 강제하는 권한 범위(계정 권한/다중서명)는 별개이며, '암호화 키 보관' 만으로 실행 주체의 권한이 제한된다고 주장하지 않는다.
"""
from __future__ import annotations

import fcntl
import datetime
import hashlib
import json
import os
import pathlib
import secrets
import stat
import time

NILE_CHAIN_ID = 0xcd8690dc
KST = 9 * 3600


class LedgerError(Exception):
    """원장 미초기화·파손·잠금 실패 → 한도 불명 → 실행 차단."""


def _kst_day(t: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(t) + KST))


def _fsync_dir(path: pathlib.Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private_new(path: pathlib.Path, data: bytes) -> None:
    """Create without overwriting, with private permissions before any data is written."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_dir(path.parent)


def _read_private(path: pathlib.Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("local signer: key/passphrase must be owned regular files with mode 0600")
        return fh.read()


class _FLock:
    def __init__(self, path: pathlib.Path):
        self.path = path; self.fd = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600); fcntl.flock(self.fd, fcntl.LOCK_EX); return self
    def __exit__(self, *a):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)
        except OSError:
            pass


class LimitLedger:
    """한도 예약 원장(추가 전용 JSONL). 항목: {payment_id, txid, amount_sun, fee_cap_sun, cost_sun, day, state, t, reason}. 주문별 마지막 항목이 현재 상태."""
    STATES = ("RESERVED", "CONSUMED", "RELEASED")

    def __init__(self, path: pathlib.Path, now_fn=time.time):
        self.path = pathlib.Path(path); self.now_fn = now_fn
        self.init_marker = self.path.with_suffix(self.path.suffix + ".init")

    def init(self, note: str = "") -> None:
        """운영 원장 명시 초기화(전무가 승인 뒤 1회). 마커가 있어야 '비어 있음' 을 0 으로 인정한다."""
        with self._lock():
            if self.init_marker.exists():
                self._read()
                return
            if self.path.exists() and self.path.stat().st_size:
                raise LedgerError("refusing to initialize an existing nonempty ledger")
            if not self.path.exists():
                _write_private_new(self.path, b"")
            _write_private_new(self.init_marker, json.dumps({"initialized_at": int(self.now_fn()), "note": note}, ensure_ascii=False).encode())

    def _lock(self):
        return _FLock(self.path.with_suffix(self.path.suffix + ".lock"))

    def _read(self) -> dict[str, dict]:
        """엄격 파싱: 마커 없음 → 미초기화, 파손 행 → LedgerError. 반환 = 주문별 현재 상태."""
        if not self.init_marker.exists():
            raise LedgerError("ledger not initialized (marker missing)")
        if not self.path.exists():
            raise LedgerError("ledger file missing while marker exists")
        cur: dict[str, dict] = {}
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if not isinstance(r, dict) or r.get("state") not in self.STATES or not isinstance(r.get("payment_id"), str) or not r["payment_id"]:
                    raise ValueError("bad fields")
                for key in ("amount_sun", "fee_cap_sun", "cost_sun", "t"):
                    if type(r.get(key)) is not int or r[key] < 0:
                        raise ValueError("invalid integer field")
                if r["amount_sun"] <= 0 or r["cost_sun"] != r["amount_sun"] + r["fee_cap_sun"]:
                    raise ValueError("invalid cost")
                if not isinstance(r.get("day"), str) or datetime.date.fromisoformat(r["day"]).isoformat() != r["day"]:
                    raise ValueError("invalid reservation day")
                for key in ("sender", "receiver"):
                    if not isinstance(r.get(key), str) or not r[key]:
                        raise ValueError("missing order binding")
                for key in ("txid", "snapshot_sha256"):
                    if not isinstance(r.get(key), str) or len(r[key]) != 64 or any(c not in "0123456789abcdef" for c in r[key]):
                        raise ValueError("invalid transaction binding")
                prev = cur.get(r["payment_id"])
                if prev:
                    if any(prev[k] != r[k] for k in ("txid", "snapshot_sha256", "sender", "receiver", "amount_sun", "fee_cap_sun", "cost_sun", "day")):
                        raise ValueError("reservation content changed")
                    if prev["state"] != "RESERVED" and r["state"] != prev["state"]:
                        raise ValueError("invalid reservation transition")
                elif r["state"] != "RESERVED" or r["day"] != _kst_day(r["t"]):
                    raise ValueError("reservation history missing or day mismatch")
            except (ValueError, TypeError, KeyError) as e:
                raise LedgerError(f"ledger line {i} corrupt: {e}") from e
            cur[r["payment_id"]] = r
        return cur

    def _append(self, rec: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n"); fh.flush(); os.fsync(fh.fileno())

    def used_sun(self, cur: dict[str, dict], day: str) -> int:
        """오늘(KST) 소비/예약 + 다른 날의 미종결 예약(날짜 변경으로 사라지지 않음)."""
        total = 0
        for r in cur.values():
            if r["state"] == "CONSUMED" and r.get("day") == day:
                total += int(r["cost_sun"])
            elif r["state"] == "RESERVED":
                total += int(r["cost_sun"])
        return total

    def reserve(self, order: dict, policy: "SignerPolicy", *, require_existing: bool = False) -> dict:
        """서명 전 원자 예약. 같은 주문이 이미 RESERVED/CONSUMED 면 그 항목을 돌려준다(중복 차감 없음)."""
        with self._lock():
            cur = self._read()
            pid = order["payment_id"]
            ok, why = policy.check_static(order)
            if not ok:
                return {"ok": False, "error": why}
            now = int(self.now_fn()); day = _kst_day(now)
            cost = int(order["amount_sun"]) + int(order["fee_cap_sun"])
            binding = {"txid": order.get("tx_id"), "snapshot_sha256": order.get("snapshot_sha256"), "sender": order.get("user_eoa"),
                       "receiver": order.get("receiver"), "amount_sun": order["amount_sun"], "fee_cap_sun": order["fee_cap_sun"], "cost_sun": cost}
            existing = cur.get(pid)
            if require_existing and not existing:
                raise LedgerError("existing execution has no durable reservation")
            if existing and (existing["state"] == "RELEASED" or any(existing.get(k) != v for k, v in binding.items())):
                return {"ok": False, "error": "reservation released or order binding changed"}
            # Lifetime counts every distinct reservation, including released/uncertain ones, across calendar days.
            if policy.lifetime_max_transactions is not None and len(cur) + (0 if existing else 1) > policy.lifetime_max_transactions:
                return {"ok": False, "error": "lifetime transaction cap exceeded"}
            if self.used_sun(cur, day) + (0 if existing else cost) > policy.daily_total_max_sun:
                return {"ok": False, "error": f"daily total cap exceeded (used {self.used_sun(cur, day)} + {cost} > {policy.daily_total_max_sun})"}
            if existing:
                return {"ok": True, "existing": True, "entry": existing}
            rec = {"payment_id": pid, **binding,
                   "day": day, "state": "RESERVED", "t": now, "reason": "reserved before signing"}
            self._append(rec)
            return {"ok": True, "existing": False, "entry": rec}

    def _transition(self, payment_id: str, new_state: str, reason: str, allow_from: tuple[str, ...]) -> dict:
        with self._lock():
            cur = self._read()
            r = cur.get(payment_id)
            if not r:
                raise LedgerError(f"no reservation for {payment_id}")
            if r["state"] == new_state:
                return r
            if r["state"] not in allow_from:
                raise LedgerError(f"cannot move {payment_id} from {r['state']} to {new_state}")
            rec = {**r, "state": new_state, "t": int(self.now_fn()), "reason": reason}
            self._append(rec); return rec

    def consume(self, payment_id: str, reason: str = "broadcast attempted") -> dict:
        return self._transition(payment_id, "CONSUMED", reason, ("RESERVED",))

    def release(self, payment_id: str, reason: str) -> dict:
        """서명 호출이 없었음이 확실할 때만. 예외·중단 등 결과가 불명이면 호출하지 않는다."""
        return self._transition(payment_id, "RELEASED", reason, ("RESERVED",))

    def state_of(self, payment_id: str) -> dict | None:
        with self._lock():
            return self._read().get(payment_id)


class SignerPolicy:
    """정적 검사(체인·수취인·건별 금액/수수료·중지 파일). 누적 한도는 LimitLedger.reserve 가 원자적으로 판정한다."""
    def __init__(self, *, allowed_receivers: set[str], per_tx_max_sun: int = 2_000_000, fee_cap_max_sun: int = 2_000_000,
                 daily_total_max_sun: int = 10_000_000, stop_file: pathlib.Path | None = None, allowed_senders: set[str] | None = None,
                 exact_amount_sun: int | None = None, lifetime_max_transactions: int | None = None):
        self.allowed_receivers = set(allowed_receivers); self.per_tx_max_sun = int(per_tx_max_sun); self.fee_cap_max_sun = int(fee_cap_max_sun)
        self.daily_total_max_sun = int(daily_total_max_sun); self.stop_file = stop_file
        self.allowed_senders = None if allowed_senders is None else set(allowed_senders)
        self.exact_amount_sun = exact_amount_sun; self.lifetime_max_transactions = lifetime_max_transactions

    def is_one_shot_trial(self) -> bool:
        return (self.allowed_senders is not None and len(self.allowed_senders) == 1 and len(self.allowed_receivers) == 1
                and not self.allowed_senders.intersection(self.allowed_receivers) and self.exact_amount_sun == 2_000_000
                and self.per_tx_max_sun == 2_000_000 and self.fee_cap_max_sun == 2_000_000
                and self.lifetime_max_transactions == 1 and self.daily_total_max_sun == 4_000_000 and self.stop_file is not None)

    def check_static(self, order: dict) -> tuple[bool, str]:
        if self.stop_file and self.stop_file.exists():
            return False, "signer stopped (stop file present)"
        if order.get("network") != "nile" or order.get("chain_id") != NILE_CHAIN_ID:
            return False, "network not nile"
        if self.allowed_senders is not None and order.get("user_eoa") not in self.allowed_senders:
            return False, "sender not in allowed list"
        if order.get("receiver") not in self.allowed_receivers:
            return False, "receiver not in allowed list"
        if type(order.get("amount_sun")) is not int or order["amount_sun"] <= 0 or type(order.get("fee_cap_sun")) is not int or order["fee_cap_sun"] < 0:
            return False, "invalid amount or fee cap"
        if self.exact_amount_sun is not None and order["amount_sun"] != self.exact_amount_sun:
            return False, "amount does not match the one-shot trial"
        if order["amount_sun"] > self.per_tx_max_sun:
            return False, f"amount {order.get('amount_sun')} > per-tx max {self.per_tx_max_sun}"
        if int(order.get("fee_cap_sun") or 0) > self.fee_cap_max_sun:
            return False, f"fee cap {order.get('fee_cap_sun')} > max {self.fee_cap_max_sun}"
        return True, ""

    # 하위 호환(구 검사): 정적 검사만
    def check(self, order: dict) -> tuple[bool, str]:
        return self.check_static(order)


def load_trial_policy(path: str | pathlib.Path | None, *, stop_file: pathlib.Path) -> SignerPolicy:
    """Explicit local-signer trial only; loading never creates a key, ledger or approval."""
    if not path:
        raise ValueError("local signer requires --signer-trial-policy or SB_SIGNER_TRIAL_POLICY_FILE")
    d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    expected = {"version": 1, "network": "nile", "chain_id": NILE_CHAIN_ID, "exact_amount_sun": 2_000_000,
                "fee_cap_max_sun": 2_000_000, "lifetime_max_transactions": 1}
    if not isinstance(d, dict) or set(d) != set(expected) | {"sender", "receiver"} or any(type(d[k]) is not type(v) or d[k] != v for k, v in expected.items()):
        raise ValueError("invalid one-shot Nile trial policy")
    from tronpy.keys import is_base58check_address
    if any(not isinstance(d[k], str) or not is_base58check_address(d[k]) for k in ("sender", "receiver")) or d["sender"] == d["receiver"]:
        raise ValueError("trial sender/receiver must be distinct valid addresses")
    return SignerPolicy(allowed_senders={d["sender"]}, allowed_receivers={d["receiver"]}, exact_amount_sun=2_000_000,
                        lifetime_max_transactions=1, daily_total_max_sun=4_000_000, stop_file=stop_file)


def _sign_raw(private_key, unsigned_tx: dict) -> dict:
    h = hashlib.sha256(bytes.fromhex(unsigned_tx["raw_data_hex"])).digest()
    raw = private_key.sign_msg_hash(h); sig = bytes.fromhex(raw.hex() if hasattr(raw, "hex") else bytes(raw).hex())
    return {"txID": unsigned_tx["txID"], "raw_data": unsigned_tx["raw_data"], "raw_data_hex": unsigned_tx["raw_data_hex"], "signature": [sig.hex()], "visible": False}


class NoSigner:
    """운영 기본: 서명 주체 미정 → 승인 처리 거부(서명·방송 0)."""
    kind = "none"
    address = None

    def sign(self, unsigned_tx: dict, sender: str) -> dict:
        raise RuntimeError("signer not configured (서명 주체 미정 — 사장 지갑 선택 뒤 설계)")


class MockSigner:
    """검사용 키(tronpy PrivateKey)로 서명. 실지갑 아님. sender 가 키 주소와 같을 때만 서명한다. sign_count 로 서명 횟수를 입증."""
    kind = "mock"

    def __init__(self, private_key, delay_fn=None):
        self.key = private_key; self.address = private_key.public_key.to_base58check_address(); self.sign_count = 0; self.delay_fn = delay_fn

    def sign(self, unsigned_tx: dict, sender: str) -> dict:
        if sender != self.address:
            raise RuntimeError("mock signer address mismatch")
        self.sign_count += 1
        if self.delay_fn:
            self.delay_fn()
        return _sign_raw(self.key, unsigned_tx)


class LocalKeySigner:
    """A안: 별도 Nile 봇 전용 지갑. 키는 scrypt(암호문 파일 밖의 별도 0600 암호 파일)로 유도한 키로 AES-256-GCM 암호화해 보관.
    무인 재시작 조건 = 암호 파일이 그 경로에 있을 때만 복호화 가능. 키 원문은 메모리에만. 주소는 복호화 결과에서 계산해 등록 주소와 대조한다."""
    kind = "local"
    KDF = {"n": 2 ** 15, "r": 8, "p": 1, "length": 32}

    def __init__(self, key_file: pathlib.Path, pass_file: pathlib.Path):
        self.key_file = pathlib.Path(key_file); self.pass_file = pathlib.Path(pass_file); self._pk = None; self.address = None; self.sign_count = 0
        self._load()

    @classmethod
    def _derive(cls, passphrase: bytes, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        return Scrypt(salt=salt, length=cls.KDF["length"], n=cls.KDF["n"], r=cls.KDF["r"], p=cls.KDF["p"]).derive(passphrase)

    @classmethod
    def create(cls, key_file: pathlib.Path, pass_file: pathlib.Path, *, passphrase: bytes | None = None, private_key_bytes: bytes | None = None) -> str:
        """새 지갑 생성(승인 뒤 1회). 암호 파일이 없으면 무작위 암호(32B urlsafe) 생성·0600 저장. 반환 = 주소(공개값). 비밀값은 출력하지 않는다."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from tronpy.keys import PrivateKey
        key_file = pathlib.Path(key_file); pass_file = pathlib.Path(pass_file)
        if key_file.exists() or key_file.is_symlink():
            raise FileExistsError(f"key file exists: {key_file} (덮어쓰지 않음)")
        pass_file.parent.mkdir(parents=True, exist_ok=True); key_file.parent.mkdir(parents=True, exist_ok=True)
        if pass_file.exists() or pass_file.is_symlink():
            existing_pass = _read_private(pass_file).strip()
            if passphrase is not None and passphrase != existing_pass:
                raise ValueError("provided passphrase does not match existing passphrase file")
            passphrase = existing_pass
        else:
            passphrase = passphrase or secrets.token_urlsafe(32).encode()
            _write_private_new(pass_file, passphrase)
        if not passphrase:
            raise ValueError("empty passphrase")
        pk_bytes = private_key_bytes or secrets.token_bytes(32)
        pk = PrivateKey(pk_bytes); address = pk.public_key.to_base58check_address()
        salt = secrets.token_bytes(16); nonce = secrets.token_bytes(12)
        ct = AESGCM(cls._derive(passphrase, salt)).encrypt(nonce, pk_bytes, address.encode())
        _write_private_new(key_file, json.dumps({"v": 1, "kdf": "scrypt", **{k: v for k, v in cls.KDF.items() if k != "length"}, "salt": salt.hex(), "nonce": nonce.hex(), "ct": ct.hex(),
                                               "address": address, "network": "nile", "created_at": int(time.time())}, indent=1).encode())
        return address

    def _load(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from tronpy.keys import PrivateKey
        if not self.key_file.exists() or not self.pass_file.exists():
            raise RuntimeError("local signer: key file or passphrase file missing")
        d = json.loads(_read_private(self.key_file)); passphrase = _read_private(self.pass_file).strip()
        if d.get("v") != 1 or d.get("network") != "nile" or d.get("kdf") != "scrypt" or not passphrase:
            raise RuntimeError("local signer: invalid encrypted-key metadata")
        try:
            pk_bytes = AESGCM(self._derive(passphrase, bytes.fromhex(d["salt"]))).decrypt(bytes.fromhex(d["nonce"]), bytes.fromhex(d["ct"]), d["address"].encode())
        except Exception as e:                                # noqa: BLE001 — 잘못된 암호/파손: 복호화 실패(키 원문 없음)
            raise RuntimeError(f"local signer: decrypt failed ({type(e).__name__})") from e
        pk = PrivateKey(pk_bytes); addr = pk.public_key.to_base58check_address()
        if addr != d.get("address"):
            raise RuntimeError("local signer: address mismatch after decrypt")
        self._pk = pk; self.address = addr

    def sign(self, unsigned_tx: dict, sender: str) -> dict:
        if sender != self.address:
            raise RuntimeError("local signer: sender is not the bot wallet")
        self.sign_count += 1
        return _sign_raw(self._pk, unsigned_tx)


def build_signer(kind: str, key_hex: str | None = None, key_file: str | None = None, pass_file: str | None = None):
    if kind == "mock":
        from tronpy.keys import PrivateKey
        return MockSigner(PrivateKey(bytes.fromhex(key_hex or "33" * 32)))
    if kind == "local":
        return LocalKeySigner(pathlib.Path(key_file), pathlib.Path(pass_file))
    return NoSigner()


if __name__ == "__main__":                                    # 승인 뒤 1회용 CLI: create --key <파일> --pass <파일> / check --key --pass (주소만 출력)
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["create", "check"]); ap.add_argument("--key", required=True); ap.add_argument("--pass", dest="pw", required=True)
    a = ap.parse_args()
    if a.cmd == "create":
        print("address", LocalKeySigner.create(pathlib.Path(a.key), pathlib.Path(a.pw)))
    else:
        print("address", LocalKeySigner(pathlib.Path(a.key), pathlib.Path(a.pw)).address)
