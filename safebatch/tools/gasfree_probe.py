#!/usr/bin/env python3
"""GasFree Nile 읽기 전용 인증 조회 — token/all, provider/all, address/{owner}.
결과를 evidence JSON 으로 남긴다(비밀값·Authorization 헤더 없음). 서명·송금 없음.

사용: python3 safebatch/tools/gasfree_probe.py --owner TEQh... --out evidence.json
"""
import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from safebatch.gasfree_client import GasFreeClient, GasFreeError, ok  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True, help="EOA(사장 Nile 테스트 지갑) 주소")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    started = datetime.datetime.now().astimezone().isoformat()
    result = {"started_at": started, "network": "nile", "base": None, "owner_eoa": a.owner,
              "tokens": None, "providers": None, "address": None, "calls": [], "verdict": {}}
    try:
        c = GasFreeClient()
    except GasFreeError as e:
        result["verdict"] = {"auth": "KEYS_MISSING", "detail": str(e)}
        pathlib.Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result["verdict"], ensure_ascii=False)); return 2
    result["base"] = c.base + c.prefix

    for name, fn in (("tokens", c.tokens), ("providers", c.providers), ("address", lambda: c.address(a.owner))):
        try:
            r = fn()
            result[name] = {"http": r["http"], "body": r["body"], "ok": ok(r)}
        except GasFreeError as e:
            result[name] = {"http": None, "body": None, "ok": False, "error": str(e)}
    result["calls"] = c.calls

    t = result["tokens"]; p = result["providers"]; ad = result["address"]
    verdict = {"auth": "AUTHENTICATED" if (t and t["ok"]) else "NOT_AUTHENTICATED"}
    if t and t["ok"]:
        toks = (t["body"].get("data") or {}).get("tokens") or []
        verdict["supported_tokens"] = [
            {"symbol": x.get("symbol"), "tokenAddress": x.get("tokenAddress"), "decimal": x.get("decimal"),
             "activateFee": x.get("activateFee"), "transferFee": x.get("transferFee"), "supported": x.get("supported")}
            for x in toks]
    if p and p["ok"]:
        provs = (p["body"].get("data") or {}).get("providers") or []
        verdict["providers"] = [{"address": x.get("address"), "name": x.get("name"),
                                 "config": x.get("config")} for x in provs]
    if ad is not None:
        verdict["address_http"] = ad["http"]
        verdict["address_code"] = (ad["body"] or {}).get("code") if isinstance(ad.get("body"), dict) else None
        if ad["ok"]:
            d = ad["body"].get("data") or {}
            verdict["gasfree_account"] = {k: d.get(k) for k in (
                "gasFreeAddress", "active", "nonce", "allowSubmit", "frozen", "assets") if k in d}
    result["verdict"] = verdict
    result["finished_at"] = datetime.datetime.now().astimezone().isoformat()
    pathlib.Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["auth"] == "AUTHENTICATED" else 1


if __name__ == "__main__":
    sys.exit(main())
