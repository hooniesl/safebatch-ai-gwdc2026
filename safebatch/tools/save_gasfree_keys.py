#!/usr/bin/env python3
"""GasFree API Key / API Secret 을 gwdc_2026/.env 에 숨김 입력으로 저장한다. 값은 화면·로그·인수에 남지 않는다.

- 프롬프트는 getpass(숨김). 값은 명령 인수로 받지 않으므로 셸 기록에 남지 않는다.
- 기존 .env 내용은 보존(추가). 같은 키 줄이 이미 있으면 --replace 없이는 거부하고, --replace 면 옛 줄을 주석으로 남긴다.
- 파일 권한 600. 저장 후 출력은 키 이름·존재 여부·길이 구간·권한뿐이다.
- 실제 GasFree API 호출은 하지 않는다.
- --check : 저장 없이 존재 여부·권한만 확인.

맥 Terminal 에서:
  /opt/homebrew/bin/python3 /Users/djl/Desktop/hani_bot/AI_CONTEST/gwdc_2026/safebatch/tools/save_gasfree_keys.py
  /opt/homebrew/bin/python3 /Users/djl/Desktop/hani_bot/AI_CONTEST/gwdc_2026/safebatch/tools/save_gasfree_keys.py --check
"""
from __future__ import annotations

import argparse
import getpass
import os
import pathlib
import stat
import sys

CONTEST_DIR = pathlib.Path(__file__).resolve().parents[2]   # gwdc_2026/
ENV_PATH = CONTEST_DIR / ".env"
KEYS = ("GASFREE_API_KEY", "GASFREE_API_SECRET")


def _read_lines() -> list[str]:
    return ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.is_file() else []


def _present(lines: list[str]) -> dict:
    out = {}
    for k in KEYS:
        val = ""
        for l in lines:
            s = l.strip()
            if s.startswith(k + "="):
                val = s[len(k) + 1:].strip().strip('"').strip("'")
        out[k] = {"present": bool(val), "length_band": ("0" if not val else "<16" if len(val) < 16 else "16-63" if len(val) < 64 else ">=64")}
    return out


def check() -> int:
    if not ENV_PATH.is_file():
        print(f"{ENV_PATH}: 없음"); return 1
    mode = stat.S_IMODE(ENV_PATH.stat().st_mode)
    pres = _present(_read_lines())
    print(f"{ENV_PATH}: mode {oct(mode)} ({'OK' if mode == 0o600 else '600 아님'})")
    for k, v in pres.items():
        print(f"  {k}: {'있음' if v['present'] else '없음'} (길이 구간 {v['length_band']})")
    return 0 if all(v["present"] for v in pres.values()) and mode == 0o600 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replace", action="store_true", help="기존 키 줄을 주석 처리하고 새 값으로 교체")
    ap.add_argument("--check", action="store_true", help="저장 없이 존재 여부·권한만 확인")
    args = ap.parse_args()
    if args.check:
        return check()
    if not sys.stdin.isatty():
        print("대화형 터미널에서 실행하세요(키는 숨김 프롬프트로 입력).", file=sys.stderr); return 2

    lines = _read_lines()
    pres = _present(lines)
    if any(v["present"] for v in pres.values()) and not args.replace:
        print(f"{ENV_PATH} 에 이미 저장된 키가 있습니다: " + ", ".join(k for k, v in pres.items() if v["present"]) + ". 교체하려면 --replace."); return 3

    values = {}
    for k in KEYS:
        v1 = getpass.getpass(f"{k} (숨김 입력, 붙여넣고 Enter): ").strip()
        v2 = getpass.getpass("확인을 위해 한 번 더: ").strip()
        if not v1:
            print(f"{k}: 빈 값 — 아무것도 저장하지 않았습니다."); return 4
        if v1 != v2:
            print(f"{k}: 두 입력이 다릅니다 — 아무것도 저장하지 않았습니다."); return 5
        if any(c.isspace() for c in v1) or "=" in v1 or '"' in v1 or "'" in v1:
            print(f"{k}: 공백/따옴표/'=' 포함 — 잘못 붙여넣은 것 같습니다. 저장하지 않았습니다."); return 6
        values[k] = v1

    if args.replace:
        lines = [("# superseded " + l) if any(l.strip().startswith(k + "=") for k in KEYS) else l for l in lines]
    body = "\n".join(lines) + ("\n" if lines else "") + "".join(f"{k}={values[k]}\n" for k in KEYS)
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(ENV_PATH, 0o600)
    del values
    print(f"saved: {', '.join(KEYS)} → {ENV_PATH} (값 비표시), 기존 줄 보존 {len(lines)}")
    return check()


if __name__ == "__main__":
    sys.exit(main())
