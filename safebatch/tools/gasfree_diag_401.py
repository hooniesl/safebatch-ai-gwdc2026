#!/usr/bin/env python3
"""GasFree 401 원인 대조(읽기 전용 config GET 만, 송금·서명 없음, 비밀값 미출력).
가설 A: 키가 Nile 이 아닌 다른 환경(mainnet /tron) 용  → 같은 서명으로 open.gasfree.io/tron/... 을 GET 해 본다(설정 조회, 자금 무관).
가설 B: 헤더/서명 구성 오류 → 문서 예시와 동일한 message 구성인지 self-test 로 재확인.
가설 C: 키 미승인/미등록 → 두 환경 모두 'Apikey not found.' 이면 남는 가설.
"""
import base64, hashlib, hmac, json, os, subprocess, sys, tempfile, time, pathlib, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from safebatch.gasfree_client import load_keys, sign  # noqa: E402

key, secret = load_keys()
out = {"checked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "key_len": len(key),
       "secret_len": len(secret), "results": []}

# 가설 B self-test: 문서 예시 방식(f"{method}{path}{ts}", HMAC-SHA256, base64)과 sign() 동일성
ts = 1731912286
ref = base64.b64encode(hmac.new(b"S", f"GET/nile/api/v1/config/token/all{ts}".encode(), hashlib.sha256).digest()).decode()
out["sign_selftest_matches_doc_formula"] = (ref == sign("S", "GET", "/nile/api/v1/config/token/all", ts))


def get(base, path):
    t = int(time.time())
    fd, hp = tempfile.mkstemp(prefix=".gfd_", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write(f"Timestamp: {t}\nAuthorization: ApiKey {key}:{sign(secret, 'GET', path, t)}\n")
    os.chmod(hp, 0o600)
    try:
        p = subprocess.run(["curl", "-sS", "--max-time", "20", "-H", f"@{hp}", "-w", "\n__HTTP__%{http_code}", base + path],
                           capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(hp)
    body, _, code = p.stdout.rpartition("\n__HTTP__")
    return {"base": base, "path": path, "http": int(code.strip() or 0), "body_head": body[:160], "curl_rc": p.returncode}


for base, path in (("https://open-test.gasfree.io", "/nile/api/v1/config/token/all"),
                   ("https://open.gasfree.io", "/tron/api/v1/config/token/all"),
                   ("https://open-test.gasfree.io", "/nile/api/v1/config/provider/all")):
    out["results"].append(get(base, path))
# 헤더 이름 변형(소문자 timestamp) — 서버가 대소문자 구분하는지
out["results"].append({"note": "header names are case-insensitive in HTTP; no variant tested"})
print(json.dumps(out, ensure_ascii=False, indent=2))
pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/dev/null").write_text(json.dumps(out, ensure_ascii=False, indent=2))
