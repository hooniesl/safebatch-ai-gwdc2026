/* 실제 핸들러 모의(VP 9/29 §4): phone.html 의 인라인 스크립트를 가짜 DOM·fetch·지갑·타이머로 구동한다. 실제 서버·AI·서명·방송 0.
   jsc -e 'var ARG_CLIENT="…/phone_client.js"; var ARG_INLINE="…/_phone_inline.js";' phone_handlers_mock.js */
const out = []; let failures = 0;
function assert(c, m) { out.push((c ? "ok   " : "FAIL ") + m); if (!c) failures++; }
/* ── 가짜 DOM ── */
function mkEl(id) {
  const el = { id, tagName: "DIV", textContent: "", innerHTML: "", value: "", disabled: false, style: {}, attrs: {}, children: [], _cls: new Set(), onclick: null, listeners: {} };
  el.classList = { add: c => el._cls.add(c), remove: c => el._cls.delete(c), toggle: (c, f) => { if (f === undefined) f = !el._cls.has(c); f ? el._cls.add(c) : el._cls.delete(c); return f; }, contains: c => el._cls.has(c) };
  Object.defineProperty(el, "className", { get: () => [...el._cls].join(" "), set: v => { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); } });
  el.focus = () => { el.focused = true; }; el.setAttribute = (k, v) => { el.attrs[k] = v; }; el.getAttribute = k => el.attrs[k]; el.appendChild = c => { el.children.push(c); return c; };
  el.addEventListener = (n, f) => { (el.listeners[n] = el.listeners[n] || []).push(f); }; el.querySelectorAll = () => []; el.closest = () => null; el.parentElement = null;
  return el;
}
const els = {};
const document = { getElementById: id => (els[id] = els[id] || mkEl(id)), createElement: tag => { const e = mkEl(""); e.tagName = tag.toUpperCase(); return e; }, addEventListener: () => {}, documentElement: mkEl("html"), title: "" };
const timers = []; let tnow = 1_790_700_000_000;
const window = globalThis; globalThis.window = window; globalThis.document = document; globalThis.navigator = {}; globalThis.prompt = () => {};
globalThis.setTimeout = (f, ms) => { timers.push({ f, at: tnow + (ms || 0) }); return timers.length; };
function flushTimers(maxMs) { const until = tnow + (maxMs || 1e9); let n = 0; while (timers.length && n < 200) { timers.sort((a, b) => a.at - b.at); if (timers[0].at > until) break; const t = timers.shift(); tnow = t.at; t.f(); n++; drainMicrotasks(); } }
function mkStorage() { const m = {}; return { setItem: (k, v) => { m[k] = v; }, getItem: k => (k in m ? m[k] : null), removeItem: k => { delete m[k]; }, _m: m }; }
/* ── 가짜 서버·지갑 ── */
const SENDER = "TJ1aFHjZsTyDtpUkwHXkPEC8Ay4w6ixHFY", MAC = "TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz";
const counts = { chat: 0, prepare: 0, signed: 0, hold: 0, reject: 0, sign_wallet: 0, status: 0, orders: 0 };
let routes = {};                                                 // path prefix → handler(body) returning {status, body} or throwing
globalThis.fetch = async (url, opt) => {
  const path = String(url).replace(/^__SB_BASE__/, "");
  const body = opt && opt.body ? JSON.parse(opt.body) : null;
  if (path.startsWith("/api/chat")) counts.chat++; if (path.startsWith("/api/order/prepare")) counts.prepare++; if (path.startsWith("/api/order/signed")) counts.signed++;
  if (path.startsWith("/api/order/hold")) counts.hold++; if (path.startsWith("/api/order/reject")) counts.reject++; if (path.startsWith("/api/order/status")) counts.status++; if (path.startsWith("/api/orders?")) counts.orders++;
  const key = Object.keys(routes).find(k => path.startsWith(k));
  if (!key) return { status: 200, json: async () => ({ ok: true, ai_mode: "MOCK_RULES", ai_provider: "KILN_LIVE", contacts: [], orders: [] }) };
  const r = await routes[key](body, path);
  if (r instanceof Error) throw r;
  return { status: r.status || 200, json: async () => { if (r.badJson) throw new Error("bad json"); return r.body; } };
};
function setWallet(addr) { globalThis.tron = { tronWeb: { defaultAddress: { base58: addr }, fullNode: { host: "https://nile.trongrid.io" }, trx: { sign: async tx => { counts.sign_wallet++; return { txID: tx.txID, raw_data: tx.raw_data, raw_data_hex: tx.raw_data_hex, signature: ["sig"] }; } } }, request: async () => [], on: () => {} }; }
const TX = "a".repeat(64), TXB = "b".repeat(64);
const ORDER = { payment_id: "phone_trx_A", tx_id: TX, user_eoa: SENDER, receiver: MAC, receiver_alias: "맥북지갑", amount_trx: "2", snapshot_sha256: "s".repeat(64), expire_at_ms: tnow + 600000, chain_id: 1, fee_cap_trx: "2",
                quote: { worst_case_fee_sun: 401000, bandwidth_bytes: 401, bandwidth_price_sun: 1000, free_bandwidth_left: 600, receiver_exists: true, balance_sun: 5e6 }, checks: [1], signing: { who: "phone", mac_signs: false, mac_role: "b" }, ai: { mode: "KILN_LIVE", note: "n" }, unsigned_tx: { txID: TX, raw_data: {}, raw_data_hex: "aa" } };
const FINAL = { state: "FINAL_CONFIRMED_SOLIDITY", payment_id: "phone_trx_A", tx_hash: TX, receipt: { block_number: 7, fee_sun: 0, fee_known: true, fee_field_present: false, fee_within_cap: true }, explorer: "https://x/" + TX };
const LIST_A = [{ payment_id: "phone_trx_A", created_at: 1790671567, receiver_alias: "맥북지갑", receiver: MAC, amount_trx: "2", tx_hash: TX, order_state: "CONSUMED", result_state: "FINAL_CONFIRMED_SOLIDITY", block_number: 7, fee_sun: 0, fee_known: true }];
const PROP = { kind: "proposal", kind_detail: "normal", proposal_id: "p1", text: "맥북지갑한테 트론 2개", understood: {}, proposal: { alias: "맥북지갑", address: MAC, amount_trx: "2" }, fee_cap_trx: "2", ai: { mode: "KILN_LIVE", call_id: "c1", calls: 1 } };
/* ── 페이지 로드(인라인 스크립트 실행) ── */
load(ARG_CLIENT);
load(ARG_INLINE);
let page = null;
function boot(storage) { globalThis.localStorage = storage || mkStorage(); for (const k in els) delete els[k]; timers.length = 0; page = globalThis.__page(); drainMicrotasks(); flushTimers(3000); return { el: id => document.getElementById(id) }; }
function on(ev) { page.setUi(SBClient.screen.on(page.ui(), ev)); }
const U = () => page.ui();
function snapshot() { return JSON.stringify(counts); }

/* 1) 조회 실패는 성공 시각·기록 없음으로 둔갑하지 않음 */
(function () { try {
  routes = { "/api/orders?": async () => ({ body: { orders: LIST_A } }), "/api/order/status": async () => ({ status: 500, body: { error: "boom" } }) };
  setWallet(SENDER); const p = boot(); 
  const list0 = p.el("historyList").innerHTML; assert(list0.indexOf("phone_trx_A") >= 0, "1 초기 목록 로드");
  on({ type: "request", text: "x" }); on({ type: "proposal", proposal: PROP, walletAddr: SENDER }); on({ type: "order", order: ORDER }); on({ type: "signing" }); page.render();
  routes["/api/orders?"] = async () => ({ status: 503, body: { error: "busy" } });
  const before = snapshot(); let res; page.doRefresh().then(r => { res = r; }); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(res && res.failed === true && res.status === false && res.history === false, "1 status 500 + history 503 → failed 명시(" + JSON.stringify(res) + ")");
  assert(p.el("toast").textContent.indexOf("조회 실패") >= 0 && p.el("toast").textContent.indexOf("갱신") < 0, "1 실패 문구 · 성공 시각 없음");
  assert(p.el("historyList").innerHTML === list0, "1 이전 목록 유지(기록 없음으로 안 바뀜)");
  assert(U().state === "processing", "1 화면 상태 유지");
  routes["/api/orders?"] = async () => (new Error("network down")); routes["/api/order/status"] = async () => ({ body: { ok: true }, badJson: true });
  res = undefined; page.doRefresh().then(r => { res = r; }); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(res && res.failed === true && p.el("historyList").innerHTML === list0, "1 통신 끊김·비JSON → 실패·목록 유지");
  routes["/api/orders?"] = async () => ({ body: { orders: [] } }); routes["/api/order/status"] = async () => ({ body: { ok: true, order_state: "CONSUMED", signature_stored: true, result: { ...FINAL, state: "ACCEPTED_UNCONFIRMED" } } });
  res = undefined; page.doRefresh().then(r => { res = r; }); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(res && res.failed === false && p.el("historyList").innerHTML.indexOf("기록이 없습니다") >= 0, "1 진짜 빈 목록은 '기록 없음'");
  assert(JSON.parse(snapshot()).chat === 0 && JSON.parse(snapshot()).prepare === 0 && JSON.parse(snapshot()).signed === 0 && JSON.parse(snapshot()).hold === 0, "1 새로고침은 GET 만(chat/prepare/signed/hold 0)");
} catch (e) { assert(false, "scenario threw " + e + " " + (e && e.stack || "")); } })();

/* 2) SIGNED 기록 남은 채 재진입 + 서버 FINAL → 완료 화면·새 송금 도달, 추가 서명/제출/방송 0 */
(function () { try {
  const st = mkStorage(); st.setItem("sb_inflight___SB_BASE__", JSON.stringify({ payment_id: "phone_trx_A", snapshot_sha256: ORDER.snapshot_sha256, tx_hash: TX, stage: "SIGNED", signed_tx: { txID: TX, signature: ["sig"] }, epoch: 2, submit_count: 1 }));
  routes = { "/api/orders?": async () => ({ body: { orders: LIST_A } }), "/api/order/status": async () => ({ body: { ok: true, order_state: "CONSUMED", signature_stored: true, result: FINAL, tx_hash: TX, expired: false } }) };
  setWallet(SENDER); const c0 = snapshot(); const p = boot(st); drainMicrotasks(); flushTimers(3000); drainMicrotasks();
  assert(U().state === "done" && p.el("dAmount").textContent === "2 TRX" && p.el("dTo").textContent.indexOf("맥북지갑") >= 0, "2 재진입 → 완료 화면·금액·수취인 복원(" + U().state + ")");
  assert(p.el("dFee").textContent.indexOf("0 TRX") >= 0, "2 fee_known=true·fee 필드 생략 → 0 TRX");
  assert(page.client().loadInflight() === null, "2 보관 기록은 같은 주문 확정으로 정리");
  const dc = JSON.parse(snapshot()), d0 = JSON.parse(c0);
  assert(dc.sign_wallet === d0.sign_wallet && dc.signed === d0.signed && dc.hold === d0.hold, "2 추가 서명·서명본 제출·hold 0");
  p.el("newSend").onclick(); assert(U().state === "input" && U().order === null && U().proposal === null, "2 새 송금 도달");
} catch (e) { assert(false, "scenario threw " + e + " " + (e && e.stack || "")); } })();

/* 3) A 의 늦은 FINAL 응답 도중 B 의 새 복구 기록: B 삭제·잠금 해제 없음 */
(function () { try {
  const st = mkStorage(); routes = { "/api/orders?": async () => ({ body: { orders: [] } }) };
  setWallet(SENDER); const p = boot(st);
  let gate; routes["/api/order/status"] = async (b, path) => { if (path.indexOf("phone_trx_A") >= 0) { await new Promise(r => { gate = r; }); return { body: { ok: true, order_state: "CONSUMED", signature_stored: true, result: FINAL } }; } return { body: { ok: true, order_state: "PENDING", signature_stored: false, result: null } }; };
   on({ type: "request", text: "x" }); on({ type: "proposal", proposal: PROP, walletAddr: SENDER }); on({ type: "order", order: { ...ORDER, payment_id: "phone_trx_B", tx_id: TXB } }); page.render();
  const pending = page.client().checkOrder("phone_trx_A"); drainMicrotasks();
  st.setItem("sb_inflight___SB_BASE__", JSON.stringify({ payment_id: "phone_trx_B", snapshot_sha256: "b".repeat(64), tx_hash: TXB, stage: "SIGNED", signed_tx: { txID: TXB, signature: ["s"] }, epoch: 1 }));
  gate(); let d; pending.then(x => { d = x; }); drainMicrotasks(); drainMicrotasks();
  const rec = page.client().loadInflight();
  assert(d && d.done === false && rec && rec.payment_id === "phone_trx_B" && rec.stage === "SIGNED", "3 A 늦은 FINAL → B 기록 보존(" + (d && d.why) + ")");
  assert(U().state !== "done" && U().order.payment_id === "phone_trx_B", "3 화면도 B 유지(A 완료로 덮지 않음)");
} catch (e) { assert(false, "scenario threw " + e + " " + (e && e.stack || "")); } })();

/* 4) 편집/지갑 변경 뒤 이전 chat·prepare 응답 도착 → 이전 제안 비활성 (9/29 승인 단계 축소: 제안 뒤 prepare 는 자동, 버튼 없음) */
(function () { try {
  routes = { "/api/orders?": async () => ({ body: { orders: [] } }) };
  setWallet(SENDER); const p = boot();
  let gate; routes["/api/chat"] = async () => { await new Promise(r => { gate = r; }); return { body: PROP }; };
  p.el("text").value = "맥북지갑한테 트론 2개"; p.el("send").onclick(); drainMicrotasks();
  p.el("edit").onclick();                                       // 응답 전에 수정
  gate(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(U().state === "input" && U().proposal === null && p.el("stageConfirm").classList.contains("hidden"), "4 편집 뒤 늦은 chat 응답 → 제안 활성화 안 됨");
  routes["/api/chat"] = async () => ({ body: PROP });
  let gate2; routes["/api/order/prepare"] = async () => { await new Promise(r => { gate2 = r; }); return { body: { ok: true, order: ORDER } }; };
  const c0 = JSON.parse(snapshot());
  p.el("text").value = "맥북지갑한테 트론 2개"; p.el("send").onclick(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(U().state === "confirm" && JSON.parse(snapshot()).prepare === c0.prepare + 1, "4 정상 제안 → 자동 prepare 1회(버튼 없음, 서명 요청 아님)");
  setWallet("TOtherWallet11111111111111111111111"); page.refreshWallet();   // 응답 전에 지갑 변경
  gate2(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(U().state === "input" && U().order === null && !SBClient.screen.canSign(U(), "TOtherWallet11111111111111111111111"), "4 지갑 변경 뒤 늦은 prepare 응답 → 주문·서명 비활성");
  assert(JSON.parse(snapshot()).sign_wallet === c0.sign_wallet, "4 자동 준비 중 지갑 서명 호출 0");
} catch (e) { assert(false, "scenario threw " + e + " " + (e && e.stack || "")); } })();

/* 5) [새로고침]·[새 송금]의 부작용 0, 미종결 시 새 송금 차단, 연타 1회 */
(function () { try {
  routes = { "/api/orders?": async () => ({ body: { orders: LIST_A } }), "/api/order/status": async () => ({ body: { ok: true, order_state: "PENDING", signature_stored: false, result: { state: "UNKNOWN", payment_id: "phone_trx_A", tx_hash: TX } } }) };
  setWallet(SENDER); const p = boot(); 
  on({ type: "request", text: "x" }); on({ type: "proposal", proposal: PROP, walletAddr: SENDER }); on({ type: "order", order: ORDER }); on({ type: "signing" }); on({ type: "unknown" }); page.render();
  const c0 = JSON.parse(snapshot());
  let r1, r2; page.doRefresh().then(x => { r1 = x; }); page.doRefresh().then(x => { r2 = x; }); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(r2 && r2.skipped === true && r1 && r1.failed === false, "5 연타 → 두 번째는 건너뜀");
  p.el("newSend").onclick(); assert(U().state === "unknown", "5 결과 불명이면 새 송금 차단");
  const c1 = JSON.parse(snapshot());
  assert(c1.chat === c0.chat && c1.prepare === c0.prepare && c1.signed === c0.signed && c1.hold === c0.hold && c1.sign_wallet === c0.sign_wallet, "5 새로고침·새 송금: chat/prepare/signed/hold/지갑서명 증가 0");
  on({ type: "result", result: FINAL }); page.render(); p.el("newSend").onclick(); assert(U().state === "input", "5 종결 뒤 새 송금 가능");
  // 완료 뒤 이전 서명 버튼 실행 불가
  on({ type: "request", text: "x" }); on({ type: "proposal", proposal: PROP, walletAddr: SENDER }); on({ type: "order", order: ORDER }); on({ type: "result", result: FINAL }); page.render();
  const c2 = JSON.parse(snapshot()); p.el("sign").onclick(); drainMicrotasks(); drainMicrotasks();
  assert(JSON.parse(snapshot()).sign_wallet === c2.sign_wallet && U().state === "done", "5 완료 뒤 서명 핸들러 실행 안 됨");
} catch (e) { assert(false, "scenario threw " + e + " " + (e && e.stack || "")); } })();

drainMicrotasks();
print(out.join("\n")); print("RESULT " + (failures === 0 ? "PASS" : "FAIL " + failures));
