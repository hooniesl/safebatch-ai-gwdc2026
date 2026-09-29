"""Nile 네이티브 TRX 전송(TransferContract) 어댑터 — 휴대폰 서명 경로(9/29 첫 시제품).

nile_tx(TRC20 USDT) 와 같은 원칙을 TRX 전송에 적용한다. 서명은 지갑(휴대폰 TronLink)이, 검증·방송·조회는 맥북 서버가 한다.
- build_unsigned_trx(): tronpy protobuf 인코더로 raw_data → raw_data_hex/txID. 독립 순수 파이썬 디코더(nile_tx._fields)로 바이트를 다시 풀어 사양과 대조.
- verify_signed_trx(): 지갑이 돌려준 거래의 raw_data_hex 가 원본과 바이트 동일 + txID 재계산 + 서명자 복구(secp256k1) == 보내는 계정.
- quote_trx(): 잔액·대역폭·수취인 활성 여부로 예상 최대 비용(sun)을 계산. 수수료 상한은 사용자가 승인한 값이며 영수증에서 초과 시 성공으로 분류하지 않는다.
- reconcile_receipt_trx(): gettransactionbyid(raw 바이트 동일·contractRet) + gettransactioninfobyid(fee ≤ 상한·블록) + solidity 동일 → CONFIRMED / ACCEPTED / PENDING / FAILED / MISMATCH / NOT_FOUND.
같은 txID 조회로만 응답 유실을 푼다. 새 거래를 만들어 재송금하지 않는다.
비용 근거(9/29 Nile getchainparameters 읽기): getTransactionFee 1000 sun/byte, getCreateNewAccountFeeInSystemContract 1 TRX,
getCreateAccountFee 0.1 TRX. 대역폭 과금 바이트 = 직렬화(서명 포함) + 64 (nile_tx.BANDWIDTH_RULE, supportVM 체인 공통).
"""
from __future__ import annotations

import dataclasses
import time

from . import nile_tx as NT

TYPE_URL_TRANSFER = "type.googleapis.com/protocol.TransferContract"
TRANSFER_CONTRACT = 1
SCOPE_NILE_TRX = f"nile:{NT.NILE_CHAIN_ID}:trx"
SUN = 1_000_000
NEW_ACCOUNT_FEE_SUN_DEFAULT = 1_000_000          # getCreateNewAccountFeeInSystemContract (9/29 읽기)
CREATE_ACCOUNT_FEE_SUN_DEFAULT = 100_000         # getCreateAccountFee (9/29 읽기)
BANDWIDTH_PRICE_SUN_DEFAULT = 1_000              # getTransactionFee (9/29 읽기)


class TrxTxError(NT.NileTxError):
    pass


@dataclasses.dataclass(frozen=True)
class TrxSpec:
    sender: str
    receiver: str
    amount_sun: int
    expire_at_ms: int
    fee_cap_sun: int                 # 사용자가 승인한 총 수수료 상한(sun). 거래 자체 필드가 아니라 영수증 검증 기준.
    network: str = "nile"

    def __post_init__(self):
        for n in ("amount_sun", "expire_at_ms", "fee_cap_sun"):
            v = getattr(self, n)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise TrxTxError(f"{n} must be a positive int")
        if self.network != "nile":
            raise TrxTxError("only nile is supported")
        for n in ("sender", "receiver"):
            NT.addr20(getattr(self, n))
        if self.sender == self.receiver:
            raise TrxTxError("sender and receiver must differ (self-transfer not part of the phone flow)")


# ── 독립 디코더 ────────────────────────────────────────────────────────────────
def _decode_transfer(b: bytes) -> dict:
    t = {"owner_address": None, "to_address": None, "amount": 0, "unexpected": []}
    for fno, wt, v in NT._fields(b):
        if fno == 1 and wt == 2: t["owner_address"] = v.hex()
        elif fno == 2 and wt == 2: t["to_address"] = v.hex()
        elif fno == 3 and wt == 0: t["amount"] = v
        else: t["unexpected"].append(("transfer", fno, wt))
    return t


def _decode_contract(b: bytes) -> dict:
    c = {"type": None, "type_url": None, "permission_id": 0, "param": None, "unexpected": []}
    for fno, wt, v in NT._fields(b):
        if fno == 1 and wt == 0: c["type"] = v
        elif fno == 2 and wt == 2:
            for f2, w2, v2 in NT._fields(v):
                if f2 == 1 and w2 == 2: c["type_url"] = v2.decode("utf-8", "replace")
                elif f2 == 2 and w2 == 2: c["param"] = _decode_transfer(v2)
                else: c["unexpected"].append(("any", f2, w2))
        elif fno == 5 and wt == 0: c["permission_id"] = v
        else: c["unexpected"].append(("contract", fno, wt))
    return c


def decode_raw_trx(raw_hex: str) -> dict:
    raw = bytes.fromhex(raw_hex)
    out = {"ref_block_bytes": None, "ref_block_hash": None, "expiration": None, "timestamp": None, "fee_limit": 0,
           "contracts": [], "memo": None, "unexpected": []}
    for fno, wt, v in NT._fields(raw):
        if fno == 1 and wt == 2: out["ref_block_bytes"] = v.hex()
        elif fno == 4 and wt == 2: out["ref_block_hash"] = v.hex()
        elif fno == 8 and wt == 0: out["expiration"] = v
        elif fno == 14 and wt == 0: out["timestamp"] = v
        elif fno == 18 and wt == 0: out["fee_limit"] = v
        elif fno == 10 and wt == 2: out["memo"] = v.hex()
        elif fno == 11 and wt == 2: out["contracts"].append(_decode_contract(v))
        else: out["unexpected"].append(("raw", fno, wt))
    return out


# ── 미서명 거래 ────────────────────────────────────────────────────────────────
def build_unsigned_trx(node: NT.NileNode, spec: TrxSpec, *, now_ms: int | None = None, ref_block: dict | None = None,
                       max_expiry_ms: int = 24 * 3600 * 1000) -> dict:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if ref_block is None:
        nb = node.now_block()
        if not NT._ok(nb) or not nb["body"].get("blockID"):
            raise TrxTxError("getnowblock failed")
        ref_block = {"blockID": nb["body"]["blockID"], "number": nb["body"]["block_header"]["raw_data"]["number"]}
    block_id = ref_block["blockID"]
    raw_data = {
        "ref_block_bytes": block_id[12:16], "ref_block_hash": block_id[16:32],
        "expiration": int(spec.expire_at_ms), "timestamp": now_ms,
        "contract": [{"parameter": {"value": {"owner_address": NT.addr21_hex(spec.sender), "to_address": NT.addr21_hex(spec.receiver),
                                              "amount": int(spec.amount_sun)},
                                    "type_url": TYPE_URL_TRANSFER}, "type": "TransferContract"}],
    }
    raw_hex = NT.encode_raw_data(raw_data)
    tx = {"txID": NT.txid_of(raw_hex), "raw_data": raw_data, "raw_data_hex": raw_hex, "visible": False}
    tx["checks"] = verify_unsigned_trx(tx, spec, now_ms=now_ms, max_expiry_ms=max_expiry_ms)
    tx["ref_block_number"] = ref_block.get("number")
    return tx


def verify_unsigned_trx(tx: dict, spec: TrxSpec, *, now_ms: int, max_expiry_ms: int = 24 * 3600 * 1000) -> list:
    checks: list = []

    def chk(name, cond, detail=""):
        checks.append({"check": name, "pass": bool(cond), "detail": str(detail)[:160]})
        if not cond:
            raise TrxTxError(f"{name} 실패: {detail}")

    raw_hex = str(tx.get("raw_data_hex") or "")
    chk("raw_hex_present", bool(raw_hex) and all(c in "0123456789abcdefABCDEF" for c in raw_hex), "raw_data_hex")
    chk("txid_matches_sha256(raw)", tx.get("txID") == NT.txid_of(raw_hex), f"{tx.get('txID')}")
    d = decode_raw_trx(raw_hex)
    chk("no_unexpected_raw_fields", not d["unexpected"], d["unexpected"])
    chk("no_memo_field", d["memo"] is None, d["memo"])
    chk("no_fee_limit(native transfer)", d["fee_limit"] == 0, d["fee_limit"])
    chk("ref_block_present", d["ref_block_bytes"] and len(d["ref_block_bytes"]) == 4 and d["ref_block_hash"] and len(d["ref_block_hash"]) == 16, "ref block")
    chk("one_contract", len(d["contracts"]) == 1, len(d["contracts"]))
    c = d["contracts"][0]
    chk("contract_type_transfer", c["type"] == TRANSFER_CONTRACT and c["type_url"] == TYPE_URL_TRANSFER, f"{c['type']} {c['type_url']}")
    chk("no_unexpected_contract_fields", not c["unexpected"] and c["permission_id"] == 0, f"{c['unexpected']} perm={c['permission_id']}")
    p = c["param"] or {}
    chk("no_unexpected_transfer_fields", p and not p["unexpected"], p.get("unexpected"))
    chk("owner_is_sender", p.get("owner_address") == NT.addr21_hex(spec.sender), p.get("owner_address"))
    chk("to_is_receiver", p.get("to_address") == NT.addr21_hex(spec.receiver), p.get("to_address"))
    chk("amount_equals_spec", p.get("amount") == spec.amount_sun, p.get("amount"))
    chk("expiration_equals_spec", d["expiration"] == spec.expire_at_ms, d["expiration"])
    chk("expiration_in_future", d["expiration"] > now_ms, f"exp={d['expiration']} now={now_ms}")
    chk("expiration_within_max", d["expiration"] - now_ms <= max_expiry_ms, d["expiration"] - now_ms)
    chk("timestamp_not_future", d["timestamp"] is not None and d["timestamp"] <= now_ms + 120_000, d["timestamp"])
    chk("timestamp_within_window", d["timestamp"] is not None and now_ms - d["timestamp"] <= max_expiry_ms, now_ms - d["timestamp"])
    rd = tx.get("raw_data")
    if isinstance(rd, dict):
        v = rd["contract"][0]["parameter"]["value"]
        same = (rd.get("expiration") == d["expiration"] and rd.get("timestamp") == d["timestamp"]
                and rd.get("ref_block_bytes") == d["ref_block_bytes"] and rd.get("ref_block_hash") == d["ref_block_hash"]
                and NT._norm_addr(v.get("owner_address")) == p["owner_address"] and NT._norm_addr(v.get("to_address")) == p["to_address"]
                and int(v.get("amount")) == p["amount"])
        chk("raw_json_matches_bytes", same, "raw_data json vs decoded bytes")
    return checks


# ── 서명 거래 ──────────────────────────────────────────────────────────────────
def verify_signed_trx(unsigned: dict, signed: dict, spec: TrxSpec, *, now_ms: int, max_expiry_ms: int = 24 * 3600 * 1000) -> dict:
    if not isinstance(signed, dict):
        raise TrxTxError("signed tx must be an object")
    raw_hex = str(signed.get("raw_data_hex") or "")
    if not raw_hex:
        # 9/29 실측: TronLink Android(DApp 브라우저) 는 {raw_data, signature, txID} 만 돌려주고 raw_data_hex 를 생략한다.
        # 이 경우 서버 원본 바이트를 기준으로 삼고, 서명자 복구(아래)가 그 바이트에 대해 성립할 때만 통과시킨다(다른 바이트에 서명했으면 복구 실패).
        raw_hex = str(unsigned["raw_data_hex"])
        rd = signed.get("raw_data")
        if isinstance(rd, dict):
            try:
                v = rd["contract"][0]["parameter"]["value"]
                same = (int(v.get("amount")) == spec.amount_sun and NT._norm_addr(v.get("owner_address")) == NT.addr21_hex(spec.sender)
                        and NT._norm_addr(v.get("to_address")) == NT.addr21_hex(spec.receiver) and int(rd.get("expiration")) == spec.expire_at_ms
                        and rd.get("ref_block_bytes") == unsigned["raw_data"]["ref_block_bytes"] and rd.get("ref_block_hash") == unsigned["raw_data"]["ref_block_hash"])
            except Exception as e:                        # noqa: BLE001
                raise TrxTxError(f"wallet raw_data unreadable: {e}"[:120])
            if not same:
                raise TrxTxError("wallet raw_data JSON differs from the order (refuse)")
    if raw_hex.lower() != str(unsigned["raw_data_hex"]).lower():
        raise TrxTxError("signed raw_data_hex differs from the unsigned original (refuse)")
    if str(signed.get("txID") or "").lower() != unsigned["txID"].lower() or NT.txid_of(raw_hex) != unsigned["txID"]:
        raise TrxTxError("txID mismatch (refuse)")
    verify_unsigned_trx({"txID": unsigned["txID"], "raw_data_hex": raw_hex, "raw_data": unsigned.get("raw_data")}, spec, now_ms=now_ms, max_expiry_ms=max_expiry_ms)
    sigs = signed.get("signature")
    if not isinstance(sigs, list) or len(sigs) != 1 or not isinstance(sigs[0], str):
        raise TrxTxError("exactly one signature required (single-signer phone flow)")
    sig = sigs[0].removeprefix("0x")
    if len(sig) != 130 or any(c not in "0123456789abcdefABCDEF" for c in sig):
        raise TrxTxError("signature must be 65-byte hex")
    signer = NT.recover_signer(raw_hex, sig)
    if signer != spec.sender:
        raise TrxTxError("signer mismatch (recovered EOA != approved sender)")
    if int(spec.expire_at_ms) <= now_ms:
        raise TrxTxError("transaction expired before broadcast")
    return {"raw_data": unsigned["raw_data"], "raw_data_hex": unsigned["raw_data_hex"], "signature": [sig], "txID": unsigned["txID"],
            "visible": False, "signer": signer,
            "signed_size_bytes": NT.signed_tx_size_bytes(raw_hex, 1, len(sig) // 2),
            "bandwidth_bytes": NT.charged_bandwidth_bytes(raw_hex, 1, 1, len(sig) // 2)}


# ── 견적 ──────────────────────────────────────────────────────────────────────
def quote_trx(node: NT.NileNode, spec: TrxSpec, *, raw_len_bytes: int | None = None) -> dict:
    """보내는 계정 잔액·대역폭·수취인 활성 여부 → 예상 최대 비용. 읽기 전용 노드 호출만."""
    acc = node.account(spec.sender)
    if not NT._ok(acc):
        raise TrxTxError(f"getaccount(sender) failed http={acc.get('http')}")
    balance = int((acc["body"] or {}).get("balance") or 0)
    sender_exists = bool((acc["body"] or {}).get("address"))
    rcv = node.account(spec.receiver)
    if not NT._ok(rcv):
        raise TrxTxError(f"getaccount(receiver) failed http={rcv.get('http')}")
    receiver_exists = bool((rcv["body"] or {}).get("address"))
    res = node.account_resource(spec.sender)
    rb = res["body"] if NT._ok(res) else {}
    free_left = max(0, int(rb.get("freeNetLimit") or 0) - int(rb.get("freeNetUsed") or 0))
    staked_left = max(0, int(rb.get("NetLimit") or 0) - int(rb.get("NetUsed") or 0))
    params = {p.get("key"): int(p.get("value") or 0) for p in ((node.chain_parameters().get("body") or {}).get("chainParameter") or [])}
    bw_price = params.get("getTransactionFee") or BANDWIDTH_PRICE_SUN_DEFAULT
    new_acc_fee = params.get("getCreateNewAccountFeeInSystemContract") or NEW_ACCOUNT_FEE_SUN_DEFAULT
    create_fee = params.get("getCreateAccountFee") or CREATE_ACCOUNT_FEE_SUN_DEFAULT
    raw_len = int(raw_len_bytes or 0) or 0
    bw_bytes = NT.charged_bandwidth_bytes("00" * raw_len, 1, 1) if raw_len else 0
    bandwidth_fee_max = bw_bytes * bw_price                  # 무료/스테이킹 대역폭이 모자라면 전액 소각(최악)
    bandwidth_covered = (free_left + staked_left) >= bw_bytes
    activation_fee = 0 if receiver_exists else (new_acc_fee + create_fee)
    expected_max = (0 if bandwidth_covered else bandwidth_fee_max) + activation_fee
    worst_case = bandwidth_fee_max + activation_fee
    return {"sender_exists": sender_exists, "balance_sun": balance, "receiver_exists": receiver_exists,
            "free_bandwidth_left": free_left, "staked_bandwidth_left": staked_left, "bandwidth_bytes": bw_bytes,
            "bandwidth_price_sun": bw_price, "bandwidth_fee_max_sun": bandwidth_fee_max, "bandwidth_covered": bandwidth_covered,
            "activation_fee_sun": activation_fee, "expected_max_fee_sun": expected_max, "worst_case_fee_sun": worst_case,
            "fee_cap_sun": spec.fee_cap_sun, "within_cap": worst_case <= spec.fee_cap_sun,
            "would_succeed": sender_exists and balance >= spec.amount_sun + worst_case and worst_case <= spec.fee_cap_sun,
            "quoted_at": int(time.time())}


# ── 영수증 ────────────────────────────────────────────────────────────────────
def lookup_status(r: dict | None) -> str:
    """노드 조회 응답 분류(9/29 VP 검수): FOUND / EMPTY / ERROR.
    Nile 실측(9/29 09:0x): 없는 txID → HTTP 200 + {} ; 잘못된 입력 → HTTP 200 + {"Error": "..."} ; 오류·429/503·비JSON 은 ERROR.
    EMPTY 는 'HTTP 200 이고 본문이 정확히 빈 객체' 일 때만이다. 그 밖은 모두 조회 실패(ERROR)로 두고 NOT_FOUND 로 판단하지 않는다."""
    if not isinstance(r, dict) or r.get("http") != 200 or not isinstance(r.get("body"), dict):
        return "ERROR"
    b = r["body"]
    if "Error" in b or "error" in b:
        return "ERROR"
    if b == {}:
        return "EMPTY"
    if b.get("txID") or b.get("id"):
        return "FOUND"
    return "ERROR"


def fetch_receipt_trx(node: NT.NileNode, txid: str) -> dict:
    """fullnode + solidity 4종 조회. solidity 조회는 walletsolidity/gettransactionbyid·gettransactioninfobyid(확정 본문)."""
    return {"tx": node.tx_by_id(txid), "info": node.tx_info(txid), "solid_tx": node.tx_by_id(txid, solid=True), "solid": node.tx_info(txid, solid=True)}


def reconcile_receipt_trx(unsigned: dict, spec: TrxSpec, rec: dict) -> dict:
    """verdict: CONFIRMED(solidity 확정+전항목 일치) / ACCEPTED(블록 포함·미확정) / PENDING / FAILED(solidity 에서 실패 확정) /
    FAILED_UNCONFIRMED(fullnode 만 실패) / MISMATCH / NOT_FOUND(4종 조회가 모두 정상 응답으로 비어 있음) / LOOKUP_ERROR(하나라도 조회 실패·오류 본문)."""
    txid = unsigned["txID"]
    st = {k: lookup_status(rec.get(k)) for k in ("tx", "info", "solid_tx", "solid")}
    items = {"txid": txid, "lookups": st}
    if st["solid_tx"] == "FOUND" and st["tx"] != "FOUND":          # solidity 에 있으면 fullnode 빈 응답이어도 '있음'
        rec = {**rec, "tx": rec.get("solid_tx")}; st["tx"] = "FOUND"
    if st["solid"] == "FOUND" and st["info"] != "FOUND":
        rec = {**rec, "info": rec.get("solid")}; st["info"] = "FOUND"
    any_found = any(v == "FOUND" for v in st.values())
    any_error = any(v == "ERROR" for v in st.values())
    items["solid_lookups_ok"] = st["solid_tx"] in ("FOUND", "EMPTY") and st["solid"] in ("FOUND", "EMPTY")
    items.update({"found_tx": st["tx"] == "FOUND", "found_info": st["info"] == "FOUND", "solid_found": st["solid"] == "FOUND"})
    if not any_found:
        if any_error:
            items["verdict"] = "LOOKUP_ERROR"
            items["error"] = {k: (str((rec.get(k) or {}).get("http")) + ":" + str((rec.get(k) or {}).get("body"))[:80]) for k, v in st.items() if v == "ERROR"}
            return items
        items["verdict"] = "NOT_FOUND"; return items
    tx = rec.get("tx") or {}; info = rec.get("info") or {}; solid = rec.get("solid") or {}
    txb = tx.get("body") if isinstance(tx.get("body"), dict) else {}
    ib = info.get("body") if isinstance(info.get("body"), dict) else {}
    sb = solid.get("body") if isinstance(solid.get("body"), dict) else {}
    items["txid_match"] = (txb.get("txID") in (None, txid)) and (ib.get("id") in (None, txid))
    items["raw_bytes_identical"] = (txb.get("raw_data_hex") or "").lower() == unsigned["raw_data_hex"].lower() if txb.get("raw_data_hex") else None
    ret = (txb.get("ret") or [{}])[0] if txb.get("ret") else {}
    items["contract_ret"] = ret.get("contractRet")
    receipt = ib.get("receipt") or {}
    items["receipt_result"] = receipt.get("result")          # TransferContract 영수증에는 보통 없음(None 허용)
    items["block_number"] = ib.get("blockNumber")
    items["fee_sun"] = int(ib.get("fee") or 0)
    items["fee_field_present"] = ("fee" in ib) if items["found_info"] else None
    # 9/29 09:5x 실측(txID 178b51e6…): 무료 대역폭으로 처리된 TRX 전송의 gettransactioninfobyid 는 fee 필드 자체가 없다(TRON HTTP 는 protobuf 기본값 0 을 생략)
    # 와 receipt.net_usage 만 있다. 따라서 '영수증(receipt)이 있고 fee 가 없음' = 0 sun 으로 확정 읽기. receipt 자체가 없으면 미확인.
    items["fee_known"] = bool(items["found_info"] and (("fee" in ib) or isinstance(ib.get("receipt"), dict)))
    items["fee_note"] = ("fee 필드 생략 = 0 sun(TRON 은 0 값을 생략; receipt 존재)" if (items["fee_known"] and not items["fee_field_present"]) else None)
    items["net_usage"] = receipt.get("net_usage"); items["net_fee_sun"] = receipt.get("net_fee")
    items["fee_cap_sun"] = spec.fee_cap_sun
    # Grok-04 #7: 총액 fee(활성화 비용 포함)만 기준. fee 를 알 수 없으면(receipt 도 없음) 상한 안이라고 보지 않는다(MISMATCH → UNKNOWN 표시)
    items["fee_within_cap"] = items["fee_known"] and items["fee_sun"] <= spec.fee_cap_sun
    items["amount_expected_sun"] = spec.amount_sun
    if txb.get("raw_data_hex"):
        try:
            dec = decode_raw_trx(txb["raw_data_hex"])
            p = (dec["contracts"][0]["param"] if dec["contracts"] else {}) or {}
            items["chain_amount_sun"] = p.get("amount")
            items["chain_parties_match"] = (p.get("owner_address") == NT.addr21_hex(spec.sender) and p.get("to_address") == NT.addr21_hex(spec.receiver))
        except Exception as e:                                # noqa: BLE001
            items["chain_decode_error"] = str(e)[:120]
    items["amount_match"] = items.get("chain_amount_sun") == spec.amount_sun and bool(items.get("chain_parties_match"))

    def _core(b):
        return {"id": b.get("id"), "blockNumber": b.get("blockNumber"), "fee": b.get("fee"), "result": (b.get("receipt") or {}).get("result"),
                "net_fee": (b.get("receipt") or {}).get("net_fee"), "net_usage": (b.get("receipt") or {}).get("net_usage"),
                "contractResult": b.get("contractResult")}
    items["solid_block_match"] = bool(sb.get("id") == txid and ib.get("blockNumber") is not None and _core(sb) == _core(ib))
    items["solid_result"] = (sb.get("receipt") or {}).get("result")
    failed_full = items["contract_ret"] not in (None, "SUCCESS") or items["receipt_result"] not in (None, "SUCCESS")
    failed_solid = items["solid_found"] and (items.get("solid_result") not in (None, "SUCCESS") or (failed_full and items["solid_block_match"]))
    executed_ok = items["contract_ret"] == "SUCCESS" and items["receipt_result"] in (None, "SUCCESS")
    all_match = (items["txid_match"] and items["raw_bytes_identical"] is True and executed_ok and items["amount_match"]
                 and items["fee_within_cap"] and items["block_number"] is not None)
    if failed_solid:
        items["verdict"] = "FAILED"                        # solidity 에서 실패 확정일 때만 최종 실패
    elif failed_full:
        items["verdict"] = "FAILED_UNCONFIRMED"            # fullnode 만 실패 → 확정 전에는 최종 실패로 선언하지 않음
    elif all_match and items["solid_block_match"]:
        items["verdict"] = "CONFIRMED"
    elif all_match:
        items["verdict"] = "ACCEPTED"
    elif items["found_tx"] and not items["found_info"]:
        items["verdict"] = "PENDING"
    else:
        items["verdict"] = "MISMATCH"
    return items
