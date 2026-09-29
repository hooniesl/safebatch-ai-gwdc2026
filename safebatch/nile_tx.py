"""일반 Nile TRC20(USDT) 전송 어댑터 — GasFree 를 쓰지 않는 주경로(사장 선택 9/28, VP_FIRST_LIVE_FLOW_PLAN).

구성(모두 표준 라이브러리 + curl 전송; 서명은 지갑(TronLink)이 한다):
- NileNode: TronGrid Nile HTTP API(curl). getnowblock / triggerconstantcontract / triggersmartcontract / broadcasttransaction /
  gettransactionbyid / gettransactioninfobyid(full·solidity) / getaccount / getaccountresource / getchainparameters.
- TransferSpec: 서버가 승인 내용에서 만든 전송 사양(발신 EOA·수취인·토큰 계약·수량(최소단위)·TRX fee_limit(sun)·만료).
  **USDT 수량과 TRX 수수료 한도는 별개다.** TRX 부족은 USDT 감액으로 해결하지 않는다.
- build_unsigned(): tronpy 의 protobuf 인코더(설치 확인: 노드 산출 raw_data_hex/txID 와 동일 재현)로 raw_data → raw_data_hex/txID 를 만들고,
  이 모듈의 **독립 순수 파이썬 protobuf 디코더**(decode_raw) 로 바이트를 다시 풀어 사양과 항목별 대조한다.
- verify_signed(): 지갑이 돌려준 서명 거래의 raw_data_hex 가 미서명 원본과 바이트 동일 + txID 재계산 + 서명자 복구(secp256k1) == 발신 EOA.
- classify_broadcast(): 노드 응답을 ACCEPTED / REJECTED(체인 도달 없음, 코드 보존) / UNKNOWN(중복·연결·서버 상태) 로 구분.
- reconcile_receipt(): gettransactionbyid(raw_data_hex 바이트 동일·contractRet) + gettransactioninfobyid(receipt.result·Transfer 로그의
  from/to/amount/token·수수료 ≤ fee_limit) + solidity 조회(확정) 를 원본 사양과 항목별 대조. 미확정은 PENDING 으로 유지.
같은 txID 조회로만 응답 유실을 푼다. timeout 뒤 새 거래를 만들어 재송금하지 않는다(호출자 규칙: intent_log.reserve_broadcast).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import time

from . import tip712

NILE_FULLNODE = "https://nile.trongrid.io"
NILE_CHAIN_ID = 3448148188
NILE_USDT = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"          # TetherToken(Nile) — 9/28 21:3x wallet/getcontract 로 이름 확인
TRANSFER_SELECTOR = "a9059cbb"
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TYPE_URL = "type.googleapis.com/protocol.TriggerSmartContract"
TRIGGER_SMART_CONTRACT = 31
SCOPE_NILE_TRC20 = f"nile:{NILE_CHAIN_ID}:trc20:{NILE_USDT}"
RAW_LEN_TRANSFER = 211           # 9/28 실측: transfer(address,uint256) 미서명 raw 길이(바이트). 서명 1개 전송본 281B, 과금 345B
# 대역폭 과금 규칙(공식 java-tron develop, 9/28 22:4x 확인 — chainbase/.../db/BandwidthProcessor.java consume(), common/.../Constant.java):
#   supportVM 체인(Nile: getAllowCreationOfContracts=1)은 bytesSize = Transaction(ret 제외) 직렬화 크기 + MAX_RESULT_SIZE_IN_TX(64) × 계약 수.
#   Nile 4.8.2.2 실측(9/28 22:4x, TriggerSmartContract 서명 1개 3건): net_usage/net_fee ÷ 1000 = 직렬화 크기 + 64 정확히 일치.
MAX_RESULT_SIZE_IN_TX = 64
PER_SIGN_LENGTH = 65
BANDWIDTH_RULE = "java-tron BandwidthProcessor.consume: serialized Transaction(without ret) + MAX_RESULT_SIZE_IN_TX(64) per contract"

REJECT_CODES = {"SIGERROR", "CONTRACT_VALIDATE_ERROR", "CONTRACT_EXE_ERROR", "BANDWITH_ERROR", "TAPOS_ERROR",
                "TOO_BIG_TRANSACTION_ERROR", "TRANSACTION_EXPIRATION_ERROR"}
UNKNOWN_CODES = {"DUP_TRANSACTION_ERROR", "SERVER_BUSY", "NO_CONNECTION", "NOT_ENOUGH_EFFECTIVE_CONNECTION", "OTHER_ERROR"}


class NileTxError(Exception):
    pass


# ── 노드 클라이언트(curl) ─────────────────────────────────────────────────
class NileNode:
    """TronGrid Nile. 파이썬 ssl(LibreSSL) 문제로 curl 전송. 응답 {"http": int, "body": dict|str}."""
    def __init__(self, base: str = NILE_FULLNODE, timeout_s: int = 15, runner=None):
        self.base = base.rstrip("/")
        self.timeout_s = timeout_s
        self.runner = runner or self._curl

    def _curl(self, method: str, url: str, body: dict | None) -> tuple[int, str]:
        cmd = ["curl", "-s", "-m", str(self.timeout_s), "-X", method, url, "-H", "Content-Type: application/json",
               "-w", "\n__HTTP__%{http_code}"]
        if body is not None:
            cmd += ["-d", json.dumps(body)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise NileTxError(f"curl failed rc={p.returncode}: {p.stderr.strip()[:120]}")
        out, _, code = p.stdout.rpartition("\n__HTTP__")
        return int(code or 0), out

    def post(self, path: str, body: dict | None = None, method: str = "POST") -> dict:
        code, text = self.runner(method, f"{self.base}/{path.lstrip('/')}", body)
        try:
            parsed = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            parsed = text
        return {"http": code, "body": parsed}

    # 조회
    def now_block(self, solid: bool = False) -> dict:
        return self.post(("walletsolidity" if solid else "wallet") + "/getnowblock")

    def account(self, addr: str) -> dict:
        return self.post("wallet/getaccount", {"address": addr, "visible": True})

    def account_resource(self, addr: str) -> dict:
        return self.post("wallet/getaccountresource", {"address": addr, "visible": True})

    def chain_parameters(self) -> dict:
        return self.post("wallet/getchainparameters", None, method="GET")

    def constant_call(self, owner: str, contract: str, selector: str, parameter_hex: str) -> dict:
        return self.post("wallet/triggerconstantcontract", {"owner_address": owner, "contract_address": contract,
                                                            "function_selector": selector, "parameter": parameter_hex, "visible": True})

    def broadcast(self, signed_tx: dict) -> dict:
        return self.post("wallet/broadcasttransaction", signed_tx)

    def tx_by_id(self, txid: str, solid: bool = False) -> dict:
        return self.post(("walletsolidity" if solid else "wallet") + "/gettransactionbyid", {"value": txid, "visible": True})

    def tx_info(self, txid: str, solid: bool = False) -> dict:
        return self.post(("walletsolidity" if solid else "wallet") + "/gettransactioninfobyid", {"value": txid})


def _ok(r: dict) -> bool:
    return r.get("http") == 200 and isinstance(r.get("body"), dict)


# ── 주소·ABI 보조 ───────────────────────────────────────────────────────────
def addr20(t_addr: str) -> bytes:
    return tip712.tron_address_to_evm20(t_addr)


def addr21_hex(t_addr: str) -> str:
    return "41" + addr20(t_addr).hex()


def transfer_data(receiver: str, amount_units: int) -> str:
    if not isinstance(amount_units, int) or isinstance(amount_units, bool) or amount_units <= 0 or amount_units >= 1 << 256:
        raise NileTxError("amount must be a positive int below 2^256")
    return TRANSFER_SELECTOR + addr20(receiver).hex().rjust(64, "0") + format(amount_units, "x").rjust(64, "0")


# ── 잔액·자원·에너지 견적(읽기 전용) ──────────────────────────────────────────
def usdt_balance(node: NileNode, owner: str, token: str = NILE_USDT) -> int:
    r = node.constant_call(owner, token, "balanceOf(address)", addr20(owner).hex().rjust(64, "0"))
    if not _ok(r) or not (r["body"].get("result") or {}).get("result"):
        raise NileTxError(f"balanceOf failed: http={r.get('http')} body={str(r.get('body'))[:120]}")
    return int(r["body"]["constant_result"][0], 16)


def estimate_transfer_energy(node: NileNode, owner: str, receiver: str, amount_units: int, token: str = NILE_USDT) -> dict:
    """triggerconstantcontract 로 transfer 를 시뮬레이션: energy_used 와 성공 여부(잔액 부족이면 revert)."""
    r = node.constant_call(owner, token, "transfer(address,uint256)", transfer_data(receiver, amount_units)[8:])
    if not _ok(r) or not (r["body"].get("result") or {}).get("result"):
        raise NileTxError(f"estimate failed: http={r.get('http')} body={str(r.get('body'))[:160]}")
    b = r["body"]
    # 9/28 21:4x 실측: 성공 시 result={"result":true}, transaction.ret=[{}]; 잔액 초과 시 result.message="REVERT opcode executed",
    # transaction.ret=[{"ret":"FAILED"}]. (TetherToken 의 transfer 는 반환값이 0 으로 찍히므로 constant_result 로 판정하지 않는다)
    ret = ((b.get("transaction") or {}).get("ret") or [{}])[0]
    msg = (b.get("result") or {}).get("message")
    return {"energy_used": int(b.get("energy_used") or 0), "would_succeed": not msg and ret.get("ret") != "FAILED",
            "message": msg, "ret": ret.get("ret")}


def chain_prices(node: NileNode) -> dict:
    """대상 망(Nile)의 현재 단가: getEnergyFee(sun/energy), getTransactionFee(sun/bandwidth byte). 메인넷 수치를 쓰지 않는다."""
    r = node.chain_parameters()
    if not _ok(r):
        raise NileTxError("chain parameters unavailable")
    vals = {x.get("key"): x.get("value") for x in r["body"].get("chainParameter", [])}
    if vals.get("getEnergyFee") is None or vals.get("getTransactionFee") is None:
        raise NileTxError("getEnergyFee/getTransactionFee missing")
    return {"energy_sun": int(vals["getEnergyFee"]), "bandwidth_sun": int(vals["getTransactionFee"])}


def energy_price_sun(node: NileNode) -> int:
    return chain_prices(node)["energy_sun"]


def account_snapshot(node: NileNode, owner: str, token: str = NILE_USDT) -> dict:
    a = node.account(owner); res = node.account_resource(owner)
    if not _ok(a) or not _ok(res):
        raise NileTxError("account/resource query failed")
    return {"trx_sun": int(a["body"].get("balance") or 0), "usdt_units": usdt_balance(node, owner, token),
            "free_net_limit": int(res["body"].get("freeNetLimit") or 0), "free_net_used": int(res["body"].get("freeNetUsed") or 0),
            "energy_limit": int(res["body"].get("EnergyLimit") or 0), "energy_used": int(res["body"].get("EnergyUsed") or 0),
            "account_exists": bool(a["body"].get("address"))}


# ── 전송 사양 ───────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class TransferSpec:
    sender: str
    receiver: str
    token: str
    amount_units: int
    fee_limit_sun: int
    expire_at_ms: int            # 서명·방송 만료(ms). 사용자 기한을 넘지 않게 호출자가 정한다.
    token_symbol: str = "USDT"
    token_decimal: int = 6
    network: str = "nile"

    def __post_init__(self):
        for n in ("amount_units", "fee_limit_sun", "expire_at_ms"):
            v = getattr(self, n)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise NileTxError(f"{n} must be a positive int")
        if self.network != "nile":
            raise NileTxError("only nile is supported")
        for n in ("sender", "receiver", "token"):
            addr20(getattr(self, n))            # base58check + 0x41 검사


# ── 순수 파이썬 protobuf 디코더(검증용, tronpy 와 독립) ───────────────────────
def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = 0; out = 0
    while True:
        if i >= len(b):
            raise NileTxError("truncated varint")
        c = b[i]; i += 1
        out |= (c & 0x7F) << shift; shift += 7
        if not c & 0x80:
            return out, i
        if shift > 70:
            raise NileTxError("varint too long")


def _fields(b: bytes) -> list[tuple[int, int, object]]:
    i = 0; out = []
    while i < len(b):
        tag, i = _varint(b, i)
        fno, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            if i + ln > len(b):
                raise NileTxError("truncated bytes field")
            v = b[i:i + ln]; i += ln
        elif wt == 1:
            v = int.from_bytes(b[i:i + 8], "little"); i += 8
        elif wt == 5:
            v = int.from_bytes(b[i:i + 4], "little"); i += 4
        else:
            raise NileTxError(f"unsupported wire type {wt}")
        out.append((fno, wt, v))
    return out


def decode_raw(raw_hex: str) -> dict:
    """Transaction.raw 바이트 → dict. 예상 밖 필드는 unexpected 에 모아 검증에서 거부한다."""
    raw = bytes.fromhex(raw_hex)
    out = {"ref_block_bytes": None, "ref_block_hash": None, "expiration": None, "timestamp": None, "fee_limit": 0,
           "contracts": [], "memo": None, "unexpected": []}
    for fno, wt, v in _fields(raw):
        if fno == 1 and wt == 2: out["ref_block_bytes"] = v.hex()
        elif fno == 4 and wt == 2: out["ref_block_hash"] = v.hex()
        elif fno == 8 and wt == 0: out["expiration"] = v
        elif fno == 14 and wt == 0: out["timestamp"] = v
        elif fno == 18 and wt == 0: out["fee_limit"] = v
        elif fno == 10 and wt == 2: out["memo"] = v.hex()
        elif fno == 11 and wt == 2: out["contracts"].append(_decode_contract(v))
        else: out["unexpected"].append(("raw", fno, wt))
    return out


def _decode_contract(b: bytes) -> dict:
    c = {"type": None, "type_url": None, "permission_id": 0, "param": None, "unexpected": []}
    for fno, wt, v in _fields(b):
        if fno == 1 and wt == 0: c["type"] = v
        elif fno == 2 and wt == 2:
            for f2, w2, v2 in _fields(v):
                if f2 == 1 and w2 == 2: c["type_url"] = v2.decode("utf-8", "replace")
                elif f2 == 2 and w2 == 2: c["param"] = _decode_trigger(v2)
                else: c["unexpected"].append(("any", f2, w2))
        elif fno == 5 and wt == 0: c["permission_id"] = v
        else: c["unexpected"].append(("contract", fno, wt))
    return c


def _decode_trigger(b: bytes) -> dict:
    t = {"owner_address": None, "contract_address": None, "call_value": 0, "data": None, "call_token_value": 0, "token_id": 0, "unexpected": []}
    for fno, wt, v in _fields(b):
        if fno == 1 and wt == 2: t["owner_address"] = v.hex()
        elif fno == 2 and wt == 2: t["contract_address"] = v.hex()
        elif fno == 3 and wt == 0: t["call_value"] = v
        elif fno == 4 and wt == 2: t["data"] = v.hex()
        elif fno == 5 and wt == 0: t["call_token_value"] = v
        elif fno == 6 and wt == 0: t["token_id"] = v
        else: t["unexpected"].append(("trigger", fno, wt))
    return t


def txid_of(raw_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(raw_hex)).hexdigest()


# ── 미서명 거래 작성(tronpy 인코더) + 독립 대조 ─────────────────────────────────
def encode_raw_data(raw_data: dict) -> str:
    """raw_data(JSON) → raw_data_hex. tronpy protobuf 인코더(9/28 확인: 노드 산출 txID 와 동일)."""
    from tronpy.proto import transaction as ptx
    return ptx._raw_data_to_protobuf(raw_data).SerializeToString().hex()


def build_unsigned(node: NileNode, spec: TransferSpec, *, now_ms: int | None = None, ref_block: dict | None = None,
                   max_expiry_ms: int = 24 * 3600 * 1000) -> dict:
    """미서명 거래: {"txID","raw_data","raw_data_hex","visible":False,"checks":[...]}.
    ref block 은 노드 최신 블록(getnowblock) 을 쓴다(TAPOS). 만료는 spec.expire_at_ms(사용자 기한 이내)."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if ref_block is None:
        nb = node.now_block()
        if not _ok(nb) or not nb["body"].get("blockID"):
            raise NileTxError("getnowblock failed")
        ref_block = {"blockID": nb["body"]["blockID"], "number": nb["body"]["block_header"]["raw_data"]["number"]}
    block_id = ref_block["blockID"]
    raw_data = {
        "ref_block_bytes": block_id[12:16], "ref_block_hash": block_id[16:32],
        "expiration": int(spec.expire_at_ms), "timestamp": now_ms, "fee_limit": int(spec.fee_limit_sun),
        "contract": [{"parameter": {"value": {"owner_address": addr21_hex(spec.sender), "contract_address": addr21_hex(spec.token),
                                              "data": transfer_data(spec.receiver, spec.amount_units), "call_value": 0},
                                    "type_url": TYPE_URL}, "type": "TriggerSmartContract"}],
    }
    raw_hex = encode_raw_data(raw_data)
    tx = {"txID": txid_of(raw_hex), "raw_data": raw_data, "raw_data_hex": raw_hex, "visible": False}
    tx["checks"] = verify_unsigned(tx, spec, now_ms=now_ms, max_expiry_ms=max_expiry_ms)
    tx["ref_block_number"] = ref_block.get("number")
    return tx


def verify_unsigned(tx: dict, spec: TransferSpec, *, now_ms: int, max_expiry_ms: int = 24 * 3600 * 1000) -> list:
    """raw_data_hex 를 독립 디코더로 풀어 사양과 항목별 대조. 하나라도 실패하면 NileTxError."""
    checks: list = []

    def chk(name, cond, detail=""):
        checks.append({"check": name, "pass": bool(cond), "detail": str(detail)[:160]})
        if not cond:
            raise NileTxError(f"{name} 실패: {detail}")

    raw_hex = str(tx.get("raw_data_hex") or "")
    chk("raw_hex_present", bool(raw_hex) and all(c in "0123456789abcdefABCDEF" for c in raw_hex), "raw_data_hex")
    chk("txid_matches_sha256(raw)", tx.get("txID") == txid_of(raw_hex), f"{tx.get('txID')}")
    d = decode_raw(raw_hex)
    chk("no_unexpected_raw_fields", not d["unexpected"], d["unexpected"])
    chk("no_memo_field", d["memo"] is None, d["memo"])
    chk("ref_block_present", d["ref_block_bytes"] and len(d["ref_block_bytes"]) == 4 and d["ref_block_hash"] and len(d["ref_block_hash"]) == 16, "ref block")
    chk("one_contract", len(d["contracts"]) == 1, len(d["contracts"]))
    c = d["contracts"][0]
    chk("contract_type_trigger_smart_contract", c["type"] == TRIGGER_SMART_CONTRACT and c["type_url"] == TYPE_URL, f"{c['type']} {c['type_url']}")
    chk("no_unexpected_contract_fields", not c["unexpected"] and c["permission_id"] == 0, f"{c['unexpected']} perm={c['permission_id']}")
    p = c["param"] or {}
    chk("no_unexpected_trigger_fields", p and not p["unexpected"], p.get("unexpected"))
    chk("owner_is_sender", p.get("owner_address") == addr21_hex(spec.sender), p.get("owner_address"))
    chk("contract_is_token", p.get("contract_address") == addr21_hex(spec.token), p.get("contract_address"))
    chk("no_call_value", p.get("call_value") == 0 and p.get("call_token_value") == 0 and p.get("token_id") == 0, "call_value/token")
    chk("data_is_transfer(receiver,amount)", p.get("data") == transfer_data(spec.receiver, spec.amount_units), (p.get("data") or "")[:80])
    chk("fee_limit_equals_spec", d["fee_limit"] == spec.fee_limit_sun, d["fee_limit"])
    chk("expiration_equals_spec", d["expiration"] == spec.expire_at_ms, d["expiration"])
    chk("expiration_in_future", d["expiration"] > now_ms, f"exp={d['expiration']} now={now_ms}")
    chk("expiration_within_max", d["expiration"] - now_ms <= max_expiry_ms, d["expiration"] - now_ms)
    # Grok#4: 아티팩트(prepare 시각의 바이트)를 서명 대기 뒤 재검증해도 통과해야 한다 → '미래 아님' + '만료 창 안' 만 본다(120초 규칙 폐지)
    chk("timestamp_not_future", d["timestamp"] is not None and d["timestamp"] <= now_ms + 120_000, d["timestamp"])
    chk("timestamp_within_window", d["timestamp"] is not None and now_ms - d["timestamp"] <= max_expiry_ms, now_ms - d["timestamp"])
    rd = tx.get("raw_data")
    if isinstance(rd, dict):                                   # JSON 표현(있으면) 과 바이트 디코드 일치
        v = rd["contract"][0]["parameter"]["value"]
        same = (rd.get("fee_limit") == d["fee_limit"] and rd.get("expiration") == d["expiration"] and rd.get("timestamp") == d["timestamp"]
                and rd.get("ref_block_bytes") == d["ref_block_bytes"] and rd.get("ref_block_hash") == d["ref_block_hash"]
                and _norm_addr(v.get("owner_address")) == p["owner_address"] and _norm_addr(v.get("contract_address")) == p["contract_address"]
                and v.get("data") == p["data"])
        chk("raw_json_matches_bytes", same, "raw_data json vs decoded bytes")
    return checks


def _norm_addr(a) -> str | None:
    if not a:
        return None
    if a.startswith("T") and len(a) == 34:
        return addr21_hex(a)
    return a.lower()


# ── 서명 거래 검증 ──────────────────────────────────────────────────────────
def recover_signer(raw_hex: str, sig_hex: str) -> str | None:
    sig = bytes.fromhex(sig_hex.removeprefix("0x"))
    if len(sig) != 65:
        return None
    r = int.from_bytes(sig[:32], "big"); s = int.from_bytes(sig[32:64], "big"); v = sig[64]
    Q = tip712.recover_pubkey(hashlib.sha256(bytes.fromhex(raw_hex)).digest(), r, s, v)
    return tip712.pubkey_to_tron_address(Q) if Q else None


def verify_signed(unsigned: dict, signed: dict, spec: TransferSpec, *, now_ms: int, max_expiry_ms: int = 24 * 3600 * 1000) -> dict:
    """지갑이 돌려준 서명 거래를 미서명 원본과 대조. 통과 시 방송용 본문 {"raw_data","raw_data_hex","signature","txID","visible"} 반환."""
    if not isinstance(signed, dict):
        raise NileTxError("signed tx must be an object")
    raw_hex = str(signed.get("raw_data_hex") or "")
    if raw_hex.lower() != str(unsigned["raw_data_hex"]).lower():
        raise NileTxError("signed raw_data_hex differs from the unsigned original (refuse)")
    if str(signed.get("txID") or "").lower() != unsigned["txID"].lower() or txid_of(raw_hex) != unsigned["txID"]:
        raise NileTxError("txID mismatch (refuse)")
    verify_unsigned({"txID": unsigned["txID"], "raw_data_hex": raw_hex, "raw_data": unsigned.get("raw_data")}, spec, now_ms=now_ms, max_expiry_ms=max_expiry_ms)
    sigs = signed.get("signature")
    if not isinstance(sigs, list) or len(sigs) != 1 or not isinstance(sigs[0], str):
        raise NileTxError("exactly one signature required")
    sig = sigs[0].removeprefix("0x")
    if len(sig) != 130 or any(c not in "0123456789abcdefABCDEF" for c in sig):
        raise NileTxError("signature must be 65-byte hex")
    signer = recover_signer(raw_hex, sig)
    if signer != spec.sender:
        raise NileTxError("signer mismatch (recovered EOA != approved sender)")
    if int(spec.expire_at_ms) <= now_ms:
        raise NileTxError("transaction expired before broadcast")
    n_contracts = len(decode_raw(raw_hex)["contracts"])
    return {"raw_data": unsigned["raw_data"], "raw_data_hex": unsigned["raw_data_hex"], "signature": [sig], "txID": unsigned["txID"],
            "visible": False, "signer": signer,
            "signed_size_bytes": signed_tx_size_bytes(raw_hex, 1, len(sig) // 2),                     # 전송 바이트(실제 서명 길이)
            "bandwidth_bytes": charged_bandwidth_bytes(raw_hex, 1, n_contracts, len(sig) // 2)}       # 과금 바이트(+64×계약)


def _len_delimited(n: int) -> int:
    """protobuf length-delimited 필드 크기: tag(1) + varint(len) + n."""
    v = n; c = 1
    while v >= 0x80:
        v >>= 7; c += 1
    return 1 + c + n


def signed_tx_size_bytes(raw_hex: str, n_sigs: int = 1, sig_len: int = PER_SIGN_LENGTH) -> int:
    """**전송(직렬화) 바이트**: Transaction{raw_data(field 1) + signature(field 2)×n}, ret 없음. 임의 여유 없음(정확값).
    과금 대역폭이 아니다 — 과금은 charged_bandwidth_bytes()."""
    return _len_delimited(len(raw_hex) // 2) + n_sigs * _len_delimited(sig_len)


def charged_bandwidth_bytes(raw_hex: str, n_sigs: int = 1, n_contracts: int = 1, sig_len: int = PER_SIGN_LENGTH) -> int:
    """**과금 대역폭 바이트**(java-tron BandwidthProcessor, VM 지원 체인): 전송 바이트 + MAX_RESULT_SIZE_IN_TX(64) × 계약 수.
    무료/스테이킹 대역폭에서 차감되거나(net_usage) 부족하면 getTransactionFee(sun/B) 로 소각(net_fee)되는 기준이 이 값이다."""
    return signed_tx_size_bytes(raw_hex, n_sigs, sig_len) + MAX_RESULT_SIZE_IN_TX * int(n_contracts)


# ── 방송 분류 ───────────────────────────────────────────────────────────────
def classify_broadcast(r: dict) -> tuple[str, str]:
    """(ACCEPTED|REJECTED|UNKNOWN, detail). REJECTED = 노드가 검증 단계에서 거절(체인 도달 없음, 코드 보존).
    UNKNOWN = 접수 여부를 이 응답만으로 알 수 없음(중복·연결·서버) → 같은 txID 조회로만 해소."""
    if not _ok(r):
        return "UNKNOWN", f"http={r.get('http')} body={str(r.get('body'))[:120]}"
    b = r["body"]
    if b.get("result") is True:
        return "ACCEPTED", f"txid={b.get('txid')}"
    code = str(b.get("code") or "")
    msg = b.get("message") or ""
    if msg and len(msg) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in msg):
        msg = bytes.fromhex(msg).decode("utf-8", "replace")      # 노드는 message 를 hex 로 준다; 아니면 원문 유지
    if code in REJECT_CODES:
        return "REJECTED", f"{code}: {msg}"[:200]
    return "UNKNOWN", f"{code or 'no-code'}: {msg}"[:200]


# ── 영수증 대조 ─────────────────────────────────────────────────────────────
def fetch_receipt(node: NileNode, txid: str) -> dict:
    return {"tx": node.tx_by_id(txid), "info": node.tx_info(txid), "solid": node.tx_info(txid, solid=True)}


def reconcile_receipt(unsigned: dict, spec: TransferSpec, rec: dict, *, total_cap_sun: int | None = None, bandwidth_max_sun: int | None = None,
                      bandwidth_bytes: int | None = None) -> dict:
    """체인 조회 결과를 원본 사양·원본 바이트와 항목별 대조. 비용은 네 기준: 총 fee ≤ 총상한(total_cap_sun; 없으면 fee_limit),
    energy_fee ≤ fee_limit(spec), net_fee ≤ bandwidth_max_sun(있으면), net_usage(무료/스테이킹 차감 바이트) ≤ bandwidth_bytes(있으면; 과금 기준 = 전송+64).
    하나라도 넘으면 성공으로 분류하지 않는다(MISMATCH).
    verdict: CONFIRMED(solidity 확정+전항목 일치) / ACCEPTED(블록 포함·일치·미확정) / FAILED(실행 실패·revert) / MISMATCH / NOT_FOUND."""
    txid = unsigned["txID"]
    tx = rec.get("tx") or {}; info = rec.get("info") or {}; solid = rec.get("solid") or {}
    txb = tx.get("body") if isinstance(tx.get("body"), dict) else {}
    ib = info.get("body") if isinstance(info.get("body"), dict) else {}
    sb = solid.get("body") if isinstance(solid.get("body"), dict) else {}
    items = {"txid": txid, "found_tx": bool(txb.get("txID")), "found_info": bool(ib.get("id")), "solid_found": bool(sb.get("id"))}
    if not items["found_tx"] and not items["found_info"]:
        items["verdict"] = "NOT_FOUND"; return items
    items["txid_match"] = (txb.get("txID") in (None, txid)) and (ib.get("id") in (None, txid))
    items["raw_bytes_identical"] = (txb.get("raw_data_hex") or "").lower() == unsigned["raw_data_hex"].lower() if txb.get("raw_data_hex") else None
    ret = (txb.get("ret") or [{}])[0] if txb.get("ret") else {}
    items["contract_ret"] = ret.get("contractRet")
    receipt = ib.get("receipt") or {}
    items["receipt_result"] = receipt.get("result")
    items["block_number"] = ib.get("blockNumber")
    items["fee_sun"] = int(ib.get("fee") or 0)
    items["energy_usage_total"] = receipt.get("energy_usage_total"); items["net_usage"] = receipt.get("net_usage")
    items["energy_fee_sun"] = receipt.get("energy_fee"); items["net_fee_sun"] = receipt.get("net_fee")
    cap = int(total_cap_sun) if total_cap_sun is not None else spec.fee_limit_sun
    items["total_cap_sun"] = cap; items["energy_fee_limit_sun"] = spec.fee_limit_sun; items["bandwidth_max_sun"] = bandwidth_max_sun
    items["bandwidth_bytes"] = bandwidth_bytes
    items["net_usage_within_bound"] = bandwidth_bytes is None or int(receipt.get("net_usage") or 0) <= int(bandwidth_bytes)
    items["fee_within_limit"] = (items["fee_sun"] <= cap and int(receipt.get("energy_fee") or 0) <= spec.fee_limit_sun
                                 and (bandwidth_max_sun is None or int(receipt.get("net_fee") or 0) <= int(bandwidth_max_sun))
                                 and items["net_usage_within_bound"])
    # Transfer 로그 대조
    logs = ib.get("log") or []
    token20 = addr20(spec.token).hex(); s20 = addr20(spec.sender).hex(); r20 = addr20(spec.receiver).hex()
    match = []
    for lg in logs:
        tp = lg.get("topics") or []
        if (str(lg.get("address") or "").lower().removeprefix("41") == token20 and len(tp) == 3 and tp[0].lower() == TRANSFER_TOPIC
                and tp[1].lower()[-40:] == s20 and tp[2].lower()[-40:] == r20):
            match.append(int(lg.get("data") or "0", 16))
    items["transfer_logs_matching_parties"] = len(match)
    items["amount_expected"] = spec.amount_units
    items["amount_actual"] = match[0] if len(match) == 1 else None
    items["amount_match"] = len(match) == 1 and match[0] == spec.amount_units
    # Grok#3: solidity 본문을 id/block 만이 아니라 receipt.result·fee·로그·contractResult 까지 fullnode 와 동일해야 확정
    def _core(b):
        return {"id": b.get("id"), "blockNumber": b.get("blockNumber"), "fee": b.get("fee"), "result": (b.get("receipt") or {}).get("result"),
                "energy_fee": (b.get("receipt") or {}).get("energy_fee"), "net_fee": (b.get("receipt") or {}).get("net_fee"),
                "log": [(str(l.get("address")).lower(), [t.lower() for t in (l.get("topics") or [])], str(l.get("data")).lower()) for l in (b.get("log") or [])],
                "contractResult": b.get("contractResult")}
    items["solid_block_match"] = bool(sb.get("id") == txid and ib.get("blockNumber") is not None and _core(sb) == _core(ib))
    items["solid_result"] = (sb.get("receipt") or {}).get("result")
    executed_ok = items["contract_ret"] == "SUCCESS" and items["receipt_result"] == "SUCCESS"
    all_match = (items["txid_match"] and items["raw_bytes_identical"] is True and executed_ok and items["amount_match"]
                 and items["fee_within_limit"] and items["block_number"] is not None)
    if items["contract_ret"] not in (None, "SUCCESS") or items["receipt_result"] not in (None, "SUCCESS") or items.get("solid_result") not in (None, "SUCCESS"):
        items["verdict"] = "FAILED"
    elif all_match and items["solid_block_match"]:
        items["verdict"] = "CONFIRMED"
    elif all_match:
        items["verdict"] = "ACCEPTED"
    elif items["found_tx"] and not items["found_info"]:
        items["verdict"] = "PENDING"                      # 방송 접수됐으나 아직 블록 미포함
    else:
        items["verdict"] = "MISMATCH"
    return items
