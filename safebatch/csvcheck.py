"""SafeBatch AI · CSV 입력 검증 (로컬, 외부 접속 없음).

- 열: recipient, amount, memo (헤더 필수, 순서 무관, 대소문자 무시)
- 원본 행번호(row_no)는 파일의 물리 줄번호(헤더=1, 첫 데이터=2)로 보존한다.
- 금액은 부동소수점 없이 문자열→정수 최소단위(6자리 소수, USDT 기준)로 변환한다.
- 누락·잘못된 금액·잘못된 주소·중복 의심 행을 표시하고 상태별 건수·합계를 낸다.
- 이 모듈은 어떤 서비스에도 연결하지 않으며, 검증 결과는 지급 성공을 뜻하지 않는다.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field, asdict

DECIMALS = 6
UNIT = 10 ** DECIMALS
# 정수 범위 상한: 지급 1건에 허용하는 최소단위 최대값 (10억 USDT). 이보다 크면 입력 거부.
MAX_UNITS = 1_000_000_000 * UNIT
AMOUNT_RE = re.compile(r"^(0|[1-9][0-9]*)(\.([0-9]{1,%d}))?$" % DECIMALS)
REQUIRED = ("recipient", "amount", "memo")

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def parse_amount_units(text: str) -> int:
    """'12.5' -> 12500000. 음수·지수·NaN·6자리 초과·공백·빈값은 ValueError."""
    if text is None:
        raise ValueError("AMOUNT_MISSING")
    s = text.strip()
    if s == "":
        raise ValueError("AMOUNT_MISSING")
    m = AMOUNT_RE.match(s)  # 앞뒤 공백만 허용; 내부 공백·부호·지수·콤마는 불일치로 거부
    if not m:
        raise ValueError("AMOUNT_FORMAT")  # 음수, 지수(1e3), NaN, 7자리 이상 소수, 콤마 등
    whole = int(m.group(1))
    frac = (m.group(3) or "").ljust(DECIMALS, "0")
    units = whole * UNIT + int(frac)
    if units <= 0:
        raise ValueError("AMOUNT_ZERO")
    if units > MAX_UNITS:
        raise ValueError("AMOUNT_RANGE")
    return units


def format_units(units: int) -> str:
    whole, frac = divmod(units, UNIT)
    return f"{whole}.{frac:0{DECIMALS}d}"


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError("bad char")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def is_tron_address(text: str) -> bool:
    """Base58Check: 25바이트, 첫 바이트 0x41, 마지막 4바이트 = sha256d 체크섬."""
    if not text or len(text) != 34 or text[0] != "T":
        return False
    try:
        raw = _b58decode(text)
    except ValueError:
        return False
    if len(raw) != 25 or raw[0] != 0x41:
        return False
    chk = hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4]
    return chk == raw[21:]


@dataclass
class Row:
    row_no: int
    recipient: str
    amount_text: str
    memo: str
    amount_units: int | None = None
    status: str = "OK"  # OK | ERROR | DUPLICATE_SUSPECT
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckResult:
    rows: list[Row]
    header_errors: list[str]
    counts: dict
    total_payable_units: int
    content_sha256: str

    def to_dict(self) -> dict:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "header_errors": self.header_errors,
            "counts": self.counts,
            "total_payable_units": self.total_payable_units,
            "total_payable": format_units(self.total_payable_units),
            "content_sha256": self.content_sha256,
        }


def check_csv_text(text: str) -> CheckResult:
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # strict=True: 닫히지 않은 따옴표 등 구조 오류는 csv.Error 로 파일 전체를 거부한다.
    reader = csv.reader(io.StringIO(text), strict=True)
    rows: list[Row] = []
    header_errors: list[str] = []
    try:
        records = list(reader)  # 물리 줄번호를 위해 line_num 을 함께 쓰므로 아래에서 다시 읽는다
    except csv.Error as e:
        return CheckResult([], [f"CSV_PARSE_ERROR:{e}"], _counts([]), 0, sha)
    if not records:
        return CheckResult([], ["EMPTY_FILE"], _counts([]), 0, sha)
    header = records[0]
    names = [h.strip().lower() for h in header]
    dup = sorted({n for n in names if names.count(n) > 1 and n})
    if dup:
        header_errors.append("HEADER_DUPLICATE:" + ",".join(dup))  # 같은 열 이름이 둘이면 뒤 열을 채택하지 않고 거부
    cols = {n: i for i, n in enumerate(names)}
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        header_errors.append("HEADER_MISSING:" + ",".join(missing))
    if header_errors:
        return CheckResult([], header_errors, _counts([]), 0, sha)

    reader = csv.reader(io.StringIO(text), strict=True)
    next(reader)
    prev_end = reader.line_num  # 헤더가 끝난 물리 줄
    for rec in reader:
        start_line = prev_end + 1  # 이 레코드가 시작한 물리 줄번호(여러 줄 메모여도 시작 줄 기준)
        prev_end = reader.line_num
        if not any(c.strip() for c in rec):
            continue  # 완전 빈 줄은 건너뜀(번호는 유지)

        def col(name: str) -> str:
            i = cols[name]
            return rec[i].strip() if i < len(rec) else ""

        row = Row(row_no=start_line, recipient=col("recipient"), amount_text=col("amount"), memo=col("memo"))
        if len(rec) != len(header):
            row.reasons.append(f"COLUMN_COUNT_MISMATCH:expected={len(header)},got={len(rec)}")
        if not row.recipient:
            row.reasons.append("RECIPIENT_MISSING")
        elif not is_tron_address(row.recipient):
            row.reasons.append("RECIPIENT_FORMAT")
        try:
            row.amount_units = parse_amount_units(row.amount_text)
        except ValueError as e:
            row.reasons.append(str(e))
        if row.reasons:
            row.status = "ERROR"
        rows.append(row)

    # 중복 의심: 같은 수신자가 2행 이상 (오류 행 포함). 자동 병합하지 않는다.
    by_recipient: dict[str, list[Row]] = {}
    for r in rows:
        if r.recipient:
            by_recipient.setdefault(r.recipient, []).append(r)
    for recipient, group in by_recipient.items():
        if len(group) > 1:
            nos = ",".join(str(r.row_no) for r in group)
            for r in group:
                r.reasons.append(f"DUPLICATE_RECIPIENT:rows={nos}")
                if r.status == "OK":
                    r.status = "DUPLICATE_SUSPECT"

    total = sum(r.amount_units for r in rows if r.status == "OK")
    return CheckResult(rows, header_errors, _counts(rows), total, sha)


def _counts(rows: list[Row]) -> dict:
    c = {"total_rows": len(rows), "OK": 0, "ERROR": 0, "DUPLICATE_SUSPECT": 0}
    for r in rows:
        c[r.status] += 1
    return c


if __name__ == "__main__":  # python3 csvcheck.py file.csv
    import json
    import sys

    with open(sys.argv[1], encoding="utf-8") as f:
        print(json.dumps(check_csv_text(f.read()).to_dict(), ensure_ascii=False, indent=2))
