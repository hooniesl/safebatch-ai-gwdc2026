"""TIP-712(EIP-712 호환) 타이핑 데이터 해시 + secp256k1 서명 복구 — 표준 라이브러리만(keccak-256·secp256k1 순수 파이썬).

목적: GasFree PermitTransfer 서명(TronLink `_signTypedData`)의 **서명자 EOA 를 서버가 독립 복구**해 승인된 EOA 와 대조한다(9/28 부사장 검수 1).
검증 상태(2026-09-28 21:15, 정직): ① keccak-256·secp256k1 복구·TRON 주소는 설치된 pycryptodome/coincurve/tronpy 와 교차 일치. ② 구조체/도메인 해시는
독립 ABI 인코더(eth_abi, 20바이트 주소)와 완전 일치 → 표준 EIP-712 규칙 구현임은 확인. ③ **TRON 지갑(TronLink/tronweb signTypedData)이 같은 규칙을 쓰는지는
미검증** — docs.gasfree.io 의 submit 예시 서명은 Nile/mainnet 도메인·20/21바이트 주소 변형 모두에서 예시 user 로 복구되지 않아(예시용 값으로 추정) 공식 벡터가 되지 못했다.
④ TIP-712 원문(tronprotocol/tips tip-712.md, 9/28 21:10 확인): "address: need to remove TRON unique prefix(0x41) and encoded as uint160", "chainId … = block.chainid & 0xffffffff"(Nile 0xcd8690dc=3448148188) — 이 구현의 규칙과 일치.
따라서 이 검증은 "형식 검사가 아닌 암호학적 복구" 이되, 실지갑 서명 1회 대조 전에는 정상 서명을 잘못 거부(오탐)할 수 있다. 미탐이 없다고 단정하지 않는다.
"""
from __future__ import annotations

import hashlib

# ── keccak-256 (FIPS-202 Keccak-f[1600], padding 0x01) ─────────────────────
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
       0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
       0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_MASK = (1 << 64) - 1


def _rol(x, n):
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _MASK if n else x


def _keccak_f(A):
    for rc in _RC:
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
        A = [[A[x][y] ^ D[x] for y in range(5)] for x in range(5)]
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rol(A[x][y], _ROT[x][y])
        A = [[B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        A[0][0] ^= rc
    return A


def keccak256(data: bytes) -> bytes:
    rate = 136
    msg = bytearray(data) + b"\x01"
    msg += b"\x00" * ((-len(msg)) % rate)
    msg[-1] |= 0x80
    A = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            x, y = i % 5, i // 5
            A[x][y] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        A = _keccak_f(A)
    out = b""
    for i in range(4):
        x, y = i % 5, i // 5
        out += A[x][y].to_bytes(8, "little")
    return out


# ── secp256k1 ──────────────────────────────────────────────────────────────
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _inv(a, m):
    return pow(a, -1, m)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1; x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1) * _inv(2 * y1, P) % P
    else:
        lam = (y2 - y1) * _inv(x2 - x1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def _mul(k, pt):
    r = None
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        k >>= 1
    return r


def recover_pubkey(msg_hash: bytes, r: int, s: int, v: int):
    """v: 0/1 (또는 27/28). 반환 (x, y) 또는 None."""
    if v >= 27:
        v -= 27
    if not (1 <= r < N and 1 <= s < N and v in (0, 1)):
        return None
    x = r
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        return None
    if (y & 1) != v:
        y = P - y
    R = (x, y)
    e = int.from_bytes(msg_hash, "big") % N
    rinv = _inv(r, N)
    Q = _add(_mul((s * rinv) % N, R), _mul((-e * rinv) % N, G))
    return Q


def pubkey_to_tron_address(Q) -> str:
    x, y = Q
    h = keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:]
    return b58check_encode(b"\x41" + h)


# ── base58check ────────────────────────────────────────────────────────────
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58check_encode(payload: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(payload + chk, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    pad = len(payload + chk) - len((payload + chk).lstrip(b"\x00"))
    return "1" * pad + out


def b58check_decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes(25, "big")
    payload, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != chk:
        raise ValueError("bad base58 checksum")
    return payload


def tron_address_to_evm20(addr: str) -> bytes:
    """T… base58 → 21바이트(0x41+20) → 20바이트(EIP-712 address 인코딩용)."""
    raw = b58check_decode(addr)
    if raw[0] != 0x41 or len(raw) != 21:
        raise ValueError("not a TRON address")
    return raw[1:]


# ── EIP-712 / TIP-712 인코딩 ───────────────────────────────────────────────
def _enc_type(name: str, types: dict) -> str:
    return name + "(" + ",".join(f"{f['type']} {f['name']}" for f in types[name]) + ")"


def _type_hash(name: str, types: dict) -> bytes:
    return keccak256(_enc_type(name, types).encode())


def _enc_value(t: str, v):
    if t == "address":
        return b"\x00" * 12 + tron_address_to_evm20(v)
    if t.startswith("uint"):
        return int(v).to_bytes(32, "big")
    if t == "string":
        return keccak256(str(v).encode())
    if t == "bytes32":
        return bytes.fromhex(v.removeprefix("0x"))
    raise ValueError(f"unsupported type {t}")


def hash_struct(name: str, data: dict, types: dict) -> bytes:
    enc = _type_hash(name, types)
    for f in types[name]:
        enc += _enc_value(f["type"], data[f["name"]])
    return keccak256(enc)


DOMAIN_TYPES = {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                 {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}]}


def typed_data_hash(domain: dict, types: dict, primary: str, message: dict) -> bytes:
    ds = hash_struct("EIP712Domain", domain, DOMAIN_TYPES)
    ms = hash_struct(primary, message, types)
    return keccak256(b"\x19\x01" + ds + ms)


def recover_signer_tron(domain: dict, types: dict, primary: str, message: dict, sig_hex: str) -> str | None:
    sig = bytes.fromhex(sig_hex.removeprefix("0x"))
    if len(sig) != 65:
        return None
    r = int.from_bytes(sig[:32], "big"); s = int.from_bytes(sig[32:64], "big"); v = sig[64]
    Q = recover_pubkey(typed_data_hash(domain, types, primary, message), r, s, v)
    return pubkey_to_tron_address(Q) if Q else None


# ── 검사용 서명(결정적 k, RFC6979 아님·테스트 전용) ─────────────────────────
def _sign_for_tests(msg_hash: bytes, priv: int) -> str:
    e = int.from_bytes(msg_hash, "big") % N
    k = int.from_bytes(hashlib.sha256(priv.to_bytes(32, "big") + msg_hash).digest(), "big") % N or 1
    R = _mul(k, G)
    r = R[0] % N
    s = (_inv(k, N) * (e + r * priv)) % N
    v = R[1] & 1
    if s > N // 2:
        s = N - s; v ^= 1
    return (r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v + 27])).hex()


def priv_to_tron_address(priv: int) -> str:
    return pubkey_to_tron_address(_mul(priv, G))
