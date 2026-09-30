#!/usr/bin/env python3
"""새 휴대폰 화면 미리보기(모의 API·모의 지갑, 실제 서버·AI·서명·방송 없음). 상태별 HTML 을 만들고 Chrome headless 로 폭별 캡처 + 가로 넘침 측정.
사용: /usr/bin/python3 tests/phone_ui_preview.py [출력폴더]"""
import json, pathlib, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent.parent
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else HERE.parent / "prep_20260914" / "evidence" / "20260929_phone_prototype" / "ui_redesign_1800" / "after")
OUT.mkdir(parents=True, exist_ok=True)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAC = "TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz"; SENDER = "TJ1aFHjZsTyDtpUkwHXkPEC8Ay4w6ixHFY"
TX = "ced1b7badadb7737c851897d563a7a4be672689702ca1f753b9f1b1d000c434e"; TX2 = "178b51e69af30135c98e46ddff2ba5955f51c79b1695a140703d3961d27101c1"
orders = [{"payment_id": "phone_trx_20260929_174607_ced1b7ba", "created_at": 1790671567, "receiver_alias": "맥북지갑", "receiver": MAC, "amount_trx": "2", "tx_hash": TX, "order_state": "CONSUMED", "result_state": "FINAL_CONFIRMED_SOLIDITY", "block_number": 71381548, "fee_sun": 0, "fee_known": True, "fee_note": None, "explorer": "https://nile.tronscan.org/#/transaction/" + TX},
          {"payment_id": "phone_trx_20260929_095534_178b51e6", "created_at": 1790643334, "receiver_alias": "맥북지갑", "receiver": MAC, "amount_trx": "2", "tx_hash": TX2, "order_state": "CONSUMED", "result_state": "FINAL_CONFIRMED_SOLIDITY", "block_number": 71372152, "fee_sun": 0, "fee_known": True, "fee_note": "fee 필드 생략 = 0 sun", "explorer": "https://nile.tronscan.org/#/transaction/" + TX2},
          {"payment_id": "phone_trx_20260929_094013_9cbed2f2", "created_at": 1790642413, "receiver_alias": "맥북지갑", "receiver": MAC, "amount_trx": "2", "tx_hash": "9cbed2f2b2b9bac21eb0ac202ef3aaa906f53e01ccdd9a9f81b374f333e20f40", "order_state": "CANCELLED", "result_state": "UNKNOWN", "explorer": None}]
proposal = {"kind": "proposal", "kind_detail": "normal", "proposal_id": "p1", "text": "맥북 지갑의 트론 2개", "understood": {"alias_match": "space_or_particle_tolerant"}, "proposal": {"alias": "맥북지갑", "address": MAC, "amount_trx": "2", "address_confirmed_at": "2026-09-21"}, "fee_cap_trx": "2", "ai": {"mode": "KILN_LIVE", "note": "Kiln qwen3-32b 실호출", "call_id": "ff8fac1c", "calls": 1},
            "confirm_note": "받는 사람을 등록된 '맥북지갑' 으로 이해했습니다(입력 문장: '맥북 지갑의 트론 2개'). 아니면 진행하지 마세요."}
order = {"payment_id": "phone_trx_20260929_181500_deadbeef", "tx_id": "d" * 64, "user_eoa": SENDER, "receiver": MAC, "receiver_alias": "맥북지갑", "amount_trx": "2", "snapshot_sha256": "5" * 64, "expire_at_ms": 1790675000000, "chain_id": 3448148188, "fee_cap_trx": "2",
         "quote": {"worst_case_fee_sun": 401000, "bandwidth_bytes": 401, "bandwidth_price_sun": 1000, "free_bandwidth_left": 600, "receiver_exists": True, "balance_sun": 5000000}, "checks": list(range(19)), "signing": {"who": "이 휴대폰의 지갑 앱", "mac_signs": False, "mac_role": "검증·방송"}, "ai": {"mode": "KILN_LIVE", "note": "실호출"}}
result_final = {"state": "FINAL_CONFIRMED_SOLIDITY", "payment_id": order["payment_id"], "tx_hash": order["tx_id"], "receipt": {"block_number": 71381900, "fee_sun": 267000, "fee_known": True, "fee_field_present": True, "fee_within_cap": True}, "explorer": "https://nile.tronscan.org/#/transaction/" + order["tx_id"]}
STATES = {
  "input": {}, "confirm": {"proposal": proposal}, "presign": {"proposal": proposal, "order": order}, "dup": {"proposal": proposal, "dup": {"state": "FINAL_CONFIRMED_SOLIDITY", "tx_hash": TX, "explorer": orders[0]["explorer"]}},
  "processing": {"proposal": proposal, "order": order, "result": {**result_final, "state": "ACCEPTED_UNCONFIRMED"}}, "done": {"proposal": proposal, "order": order, "result": result_final},
  "unknown": {"proposal": proposal, "order": order, "result": {"state": "UNKNOWN", "payment_id": order["payment_id"], "tx_hash": order["tx_id"]}},
  "blocked": {"proposal": proposal, "blocked": "잔액 부족: 잔액 1.000000 TRX < 보낼 2.000000 + 최악 수수료 0.267000 TRX"},
  "intent_connect": {"proposal": proposal, "no_wallet": True, "intent": {"intent_id": "i1", "source": "telegram", "text": "맥북지갑한테 트론 2개 보내 트론링크앱 사용해서", "sender": SENDER, "sender_label": "안드로이드", "kind": "proposal", "proposal_id": "p1", "state": "PROPOSED", "link_expired": False, "payment_id": None, "test_mode": False}},
  "intent_test_presign": {"proposal": proposal, "order": order, "intent": {"intent_id": "i2", "source": "test", "text": "맥북지갑한테 트론 2개 보내 트론링크앱 사용해서", "sender": SENDER, "sender_label": "안드로이드", "kind": "proposal", "proposal_id": "p1", "state": "ORDER_PENDING", "link_expired": False, "payment_id": order["payment_id"], "test_mode": True}},
}
base = (HERE / "phone.html").read_text(encoding="utf-8").replace('src="__SB_BASE__/phone_client.js"', 'src="../phone_client.js"').replace('"__SB_BASE__"', '""')
def stub(state):
    fx = STATES[state]
    return f"""<script>
window.__FX = {json.dumps(fx, ensure_ascii=False)}; window.__ORDERS = {json.dumps(orders, ensure_ascii=False)};
if (!window.__FX.no_wallet) window.tron = {{ tronWeb: {{ defaultAddress: {{ base58: "{SENDER}" }}, fullNode: {{ host: "https://nile.trongrid.io" }}, trx: {{ sign: async t => t }} }}, request: async () => [], on: () => {{}} }};
window.fetch = async (url, opt) => {{
  const j = x => ({{ ok: true, status: 200, json: async () => x }});
  if (url.endsWith("/api/health")) return j({{ ok: true, ai_mode: "MOCK_RULES", ai_provider: "KILN_LIVE", network: "nile" }});
  if (url.endsWith("/api/contacts")) return j({{ contacts: [{{ alias: "맥북지갑", confirmed: true }}, {{ alias: "민수", confirmed: false }}, {{ alias: "지연", confirmed: false }}] }});
  if (url.indexOf("/api/orders?") >= 0) return j({{ orders: window.__ORDERS }});
  if (url.indexOf("/api/order/status") >= 0) return j({{ ok: true, order_state: "CONSUMED", signature_stored: true, result: window.__FX.result || null, expired: false }});
  return j({{ ok: false, error: "stub" }});
}};
window.addEventListener("load", () => setTimeout(() => {{
  const S = SBClient.screen; const fx = window.__FX; const W = "{SENDER}";
  ui = S.on(ui, {{ type: "request", text: fx.proposal ? fx.proposal.text : "" }}); ui.walletAddr = W;
  if (fx.proposal) ui = S.on(ui, {{ type: "proposal", proposal: fx.proposal, walletAddr: W }});
  if (fx.dup) {{ document.getElementById("dupBox").innerHTML = "<b>같은 지갑·수취인·수량의 이전 송금이 최근 15분 안에 있습니다.</b><div class='status ok'>완료(확정)</div><div class='long'>txID " + fx.dup.tx_hash + "</div><div class='sub'>한 번 더 보내려면 아래 버튼으로 확인합니다.</div>"; ui = S.on(ui, {{ type: "duplicate" }}); }}
  if (fx.order) ui = S.on(ui, {{ type: "order", order: fx.order }});
  if (fx.result) {{ ui = S.on(ui, {{ type: "signing" }}); ui = S.on(ui, {{ type: "result", result: fx.result }}); }}
  if ("{state}" === "unknown") ui = S.on(ui, {{ type: "unknown" }});
  if (fx.blocked) ui = S.on(ui, {{ type: "prepare-failed", reason: fx.blocked }});
  if (fx.intent) {{ ui = S.on(ui, {{ type: "intent", intent: fx.intent }}); }}
  render(); if (fx.proposal && !fx.intent) addMsg("me", fx.proposal.text);
  if (fx.intent && !fx.order) intentStep();
  document.querySelectorAll("#historyList li").forEach((li, i) => {{ if (i === 0) li.classList.add("open"); }});
  setTimeout(() => {{ const sw = document.documentElement.scrollWidth, iw = window.innerWidth; let over = [];
    document.querySelectorAll("body *").forEach(el => {{ const r = el.getBoundingClientRect(); if (r.right > iw + 1 && r.width > 0) over.push(el.tagName + (el.id ? "#" + el.id : "") + (el.className && typeof el.className === "string" ? "." + el.className.split(" ")[0] : "")); }});
    document.title = "sw=" + sw + " iw=" + iw + " over=" + over.length + (over.length ? " [" + over.slice(0, 5).join(",") + "]" : ""); }}, 400);
}}, 700));
</script>"""
if __name__ == "__main__":
    rep = []
    for st in STATES:
        html = base.replace("<script src=", stub(st) + "\n<script src=", 1)
        f = HERE / "tests" / f"_preview_{st}.html"; f.write_text(html, encoding="utf-8")
        for w in ([320, 360, 390, 430] if st in ("done", "presign", "input", "blocked", "intent_connect", "intent_test_presign") else [360]):
            # Chrome headless 는 창 폭 최소 500 → 좁은 뷰포트는 iframe 으로 만든다(같은 file 출처 접근 허용 플래그로 안쪽 측정값을 바깥 title 로 복사)
            fr = HERE / "tests" / f"_frame_{st}_{w}.html"
            fr.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><style>body{{margin:0;background:#888}}iframe{{border:0;display:block;width:{w}px;height:1080px;background:#fff}}</style></head><body>
<iframe id="f" src="_preview_{st}.html"></iframe><script>setTimeout(()=>{{try{{document.title=document.getElementById('f').contentDocument.title||'no-title'}}catch(e){{document.title='err '+e}}}},2500)</script></body></html>""", encoding="utf-8")
            png = OUT / f"{st}_{w}.png"
            subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files", f"--window-size={max(500, w + 20)},1100", "--virtual-time-budget=5000", f"--screenshot={png}", f"file://{fr}"], capture_output=True)
            dom = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--allow-file-access-from-files", f"--window-size={max(500, w + 20)},1100", "--virtual-time-budget=5000", "--dump-dom", f"file://{fr}"], capture_output=True, text=True).stdout
            import re; m = re.search(r"<title>([^<]*)</title>", dom); rep.append(f"{st}@{w}: {m.group(1) if m else 'no-measure'}")
    (OUT / "OVERFLOW_REPORT.txt").write_text("\n".join(rep) + "\n", encoding="utf-8"); print("\n".join(rep)); print("out", OUT)
