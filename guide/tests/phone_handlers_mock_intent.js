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

/* ── 9/29 승인 단계 축소 + 외부 채팅 승인 링크 모의(VP_REMOTE_MINIMAL_APPROVAL §5). 앞부분(가짜 DOM·fetch·지갑)은 phone_handlers_mock.js 와 같다. ── */
const IPHONE = "TQNXymgxq4j5grpQFcTMSkhjXHNDsTW3mb";
const INTENT_TOKEN = "tokA_abcdefghijklmnopqrstuvwxyz";
function intentView(extra) { return { ok: true, intent: { intent_id: "i1", source: "telegram", text: "맥북지갑한테 트론 2개 보내 트론링크앱 사용해서", sender: IPHONE, sender_label: "아이폰", created_at: 1, expires_at: 9e12, kind: "proposal", kind_detail: "normal",
  proposal_id: "p1", proposal: { alias: "맥북지갑", address: MAC, amount_trx: "2", amount_sun: 2000000 }, ai: { mode: "MOCK_KILN", calls: 0 }, fee_cap_trx: 2, state: "PROPOSED", payment_id: null, link_expired: false, ...(extra || {}) } }; }
const ORDER_I = { ...ORDER, payment_id: "phone_trx_I", user_eoa: IPHONE };
function bootIntent(storage, token) { globalThis.SB_INTENT_TOKEN = token === undefined ? INTENT_TOKEN : token; const p = boot(storage); drainMicrotasks(); drainMicrotasks(); drainMicrotasks(); flushTimers(3000); drainMicrotasks(); drainMicrotasks(); return p; }
function opsOf() { return JSON.parse(JSON.stringify(page.ops)); }

/* 6) 정상(페이지 직접 입력): 명령 → 자동 prepare → 최종 카드 1장 → [승인] 1클릭 → 지갑 서명 1 → 제출 1 → 확정. 사용자 클릭 = 보내기 1 + 승인 1 */
(function () { try {
  globalThis.SB_INTENT_TOKEN = "";
  routes = { "/api/orders?": async () => ({ body: { orders: [] } }), "/api/chat": async () => ({ body: PROP }), "/api/order/prepare": async () => ({ body: { ok: true, order: ORDER } }),
             "/api/order/hold": async () => ({ body: { ok: true } }), "/api/order/signed": async () => { signedDone = true; return { body: { ok: true, state: "ACCEPTED_UNCONFIRMED", payment_id: "phone_trx_A", tx_hash: TX } }; },
             "/api/order/status": async () => ({ body: signedDone ? { ok: true, order_state: "CONSUMED", signature_stored: true, result: FINAL } : { ok: true, order_state: "PENDING", signature_stored: false, result: null } }) };
  let signedDone = false;
  setWallet(SENDER); const p = boot(); const c0 = JSON.parse(snapshot());
  p.el("text").value = "맥북지갑한테 트론 2개"; p.el("send").onclick(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(U().state === "presign" && !p.el("stageConfirm").classList.contains("hidden") && p.el("confirmTitle").textContent.indexOf("최종 확인") >= 0, "6 명령 → 자동 준비 → 최종 카드(presign) " + U().state);
  assert(p.el("cFrom").textContent === SENDER && p.el("cAddr").textContent === MAC && p.el("cAmount").textContent === "2 TRX" && p.el("cMax").textContent.indexOf("2.401") >= 0 && p.el("cFee").textContent.indexOf("상한 2 TRX") >= 0, "6 카드: 보내는 지갑·전체 주소·2 TRX·최대 차감 2.401·상한");
  assert(!p.el("sign").classList.contains("hidden") && p.el("sign").disabled === false && p.el("cConnect").classList.contains("hidden") && p.el("resend").classList.contains("hidden"), "6 버튼: 승인 1개만 활성(연결/재전송 숨김)");
  const c1 = JSON.parse(snapshot()); assert(c1.chat === c0.chat + 1 && c1.prepare === c0.prepare + 1 && c1.sign_wallet === c0.sign_wallet && c1.signed === c0.signed, "6 준비까지: chat 1·prepare 1·지갑 서명 0·제출 0");
  page.render(); p.el("refreshBtn").onclick(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(JSON.parse(snapshot()).prepare === c1.prepare && JSON.parse(snapshot()).chat === c1.chat, "6 새로고침·재렌더로 prepare/chat 반복 없음");
  p.el("sign").onclick(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks(); flushTimers(6000); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  const c2 = JSON.parse(snapshot());
  assert(c2.sign_wallet === c1.sign_wallet + 1 && c2.signed === c1.signed + 1 && c2.hold === c1.hold + 1 && c2.prepare === c1.prepare, "6 승인 1클릭 → 지갑 서명 1·제출 1·hold 1·추가 prepare 0");
  assert(U().state === "done" && p.el("dAmount").textContent === "2 TRX", "6 확정 → 완료 화면(" + U().state + ")");
  const o = opsOf(); assert(o.clicks.send === 1 && o.clicks.approve === 1 && o.clicks.connect === 0 && o.wallet_sign === 1, "6 조작 집계: 보내기 1·승인 1·연결 0·지갑 서명 1 " + JSON.stringify(o.clicks));
} catch (e) { assert(false, "scenario 6 threw " + e + " " + (e && e.stack || "")); } })();

/* 7) 외부 채팅 승인 링크(intent): 페이지 로드 → chat 호출 0 → 자동 prepare(intent 경로) 1 → 카드 → 승인 → 확정. 재진입은 같은 주문 재사용(새 prepare 는 서버가 같은 주문을 돌려줌) */
(function () { try {
  let prepN = 0;
  routes = { "/api/orders?": async () => ({ body: { orders: [] } }), "/api/intent?": async () => ({ body: intentView() }), "/api/chat": async () => ({ body: PROP }),
             "/api/intent/prepare": async (b) => { prepN++; if (b.token !== INTENT_TOKEN) return { status: 409, body: { ok: false, error: "bad token" } }; if (b.sender !== IPHONE) return { status: 409, body: { ok: false, error: "wallet mismatch", wallet_mismatch: true } }; return { body: { ok: true, order: ORDER_I, reused: prepN > 1 } }; },
             "/api/order/hold": async () => ({ body: { ok: true } }), "/api/order/signed": async () => { signedDone = true; return { body: { ok: true, state: "ACCEPTED_UNCONFIRMED", payment_id: "phone_trx_I", tx_hash: TX } }; },
             "/api/order/status": async () => ({ body: signedDone ? { ok: true, order_state: "CONSUMED", signature_stored: true, result: { ...FINAL, payment_id: "phone_trx_I" } } : { ok: true, order_state: "PENDING", signature_stored: false, result: null } }) };
  let signedDone = false;
  setWallet(IPHONE); const c0 = JSON.parse(snapshot()); const p = bootIntent();
  assert(JSON.parse(snapshot()).chat === c0.chat, "7 링크 진입: chat(AI) 호출 0");
  assert(U().intent && U().state === "presign" && p.el("intentLine").textContent.indexOf("채팅 요청") >= 0 && p.el("cFrom").textContent.indexOf("아이폰") >= 0, "7 intent 로드 → 자동 준비 → 최종 카드(" + U().state + ")");
  assert(prepN === 1 && p.el("stageInput").classList.contains("hidden") && p.el("edit").classList.contains("hidden"), "7 intent prepare 1회 · 입력/수정 없음(새 요청은 채팅에서)");
  p.el("refreshBtn").onclick(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks(); page.render();
  assert(prepN === 1, "7 새로고침으로 prepare 반복 없음");
  p.el("sign").onclick(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks(); flushTimers(6000); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  const c2 = JSON.parse(snapshot());
  assert(c2.sign_wallet === c0.sign_wallet + 1 && c2.signed === c0.signed + 1 && U().state === "done", "7 승인 1클릭 → 서명 1·제출 1 → 완료(" + U().state + ")");
  assert(p.el("newSend").classList.contains("hidden"), "7 링크 페이지에는 새 송금 버튼 없음");
  const o = opsOf(); assert(o.clicks.send === 0 && o.clicks.approve === 1 && o.clicks.connect === 0, "7 조작 집계: 보내기 0·승인 1·연결 0 " + JSON.stringify(o.clicks));
} catch (e) { assert(false, "scenario 7 threw " + e + " " + (e && e.stack || "")); } })();

/* 8) 링크 재진입: 서버가 같은 주문(PENDING)을 돌려줌 → 새 주문 없음 · 확정된 intent 재진입 → 완료 화면·서명 불가 */
(function () { try {
  let prepN = 0;
  routes = { "/api/orders?": async () => ({ body: { orders: [] } }), "/api/intent?": async () => ({ body: intentView({ payment_id: "phone_trx_I", state: "ORDER_PENDING", order_state: "PENDING", tx_hash: TX }) }),
             "/api/intent/prepare": async () => { prepN++; return { body: { ok: true, order: ORDER_I, reused: true } }; } };
  setWallet(IPHONE); const p = bootIntent();
  assert(U().state === "presign" && prepN === 1 && U().order.payment_id === "phone_trx_I", "8 PENDING 재진입 → 같은 주문으로 카드(서버 reused)");
  routes["/api/intent?"] = async () => ({ body: intentView({ payment_id: "phone_trx_I", state: "FINAL", order_state: "CONSUMED", tx_hash: TX, result: { ...FINAL, payment_id: "phone_trx_I" } }) });
  const c0 = JSON.parse(snapshot()); const p2 = bootIntent();
  assert(U().state === "done" && JSON.parse(snapshot()).sign_wallet === c0.sign_wallet, "8 확정 intent 재진입 → 완료 화면·서명 호출 0(" + U().state + ")");
  p2.el("sign").onclick(); drainMicrotasks(); assert(JSON.parse(snapshot()).sign_wallet === c0.sign_wallet, "8 완료 뒤 승인 핸들러 실행 안 됨");
} catch (e) { assert(false, "scenario 8 threw " + e + " " + (e && e.stack || "")); } })();

/* 9) 발신 지갑 불일치·미연결·만료: 주문을 만들지 않고 이유 표시. 지갑을 맞추면 자동 준비 */
(function () { try {
  let prepN = 0;
  routes = { "/api/orders?": async () => ({ body: { orders: [] } }), "/api/intent?": async () => ({ body: intentView() }), "/api/intent/prepare": async (b) => { prepN++; return b.sender === IPHONE ? { body: { ok: true, order: ORDER_I } } : { status: 409, body: { ok: false, error: "mismatch" } }; } };
  setWallet(SENDER); const p = bootIntent();                                  // 안드로이드 지갑으로 접속했는데 요청은 아이폰
  assert(prepN === 0 && p.el("blockBox").textContent.indexOf("발신 지갑") >= 0 && p.el("sign").classList.contains("hidden"), "9 지갑 불일치 → prepare 0·이유 표시·서명 없음");
  setWallet(IPHONE); page.refreshWallet(); page.intentStep(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(prepN === 1 && U().state === "presign", "9 지갑을 맞추면 자동 준비 1회(" + U().state + ")");
  globalThis.tron = { tronWeb: null, request: async () => [], on: () => {} }; globalThis.tronLink = undefined; globalThis.tronWeb = undefined;   // TronLink 안이지만 아직 미연결
  const p2 = bootIntent();
  assert(!p2.el("cConnect").classList.contains("hidden") && p2.el("openApp").classList.contains("hidden") && p2.el("prepStatus").textContent.indexOf("연결") >= 0 && prepN === 1, "9 TronLink 안·지갑 미연결 → [지갑 연결] 안내·앱 열기 버튼 없음·prepare 없음");
  routes["/api/intent?"] = async () => ({ body: intentView({ link_expired: true }) }); setWallet(IPHONE);
  const p3 = bootIntent();
  assert(prepN === 1 && p3.el("prepStatus").textContent.indexOf("만료") >= 0 && U().state !== "presign", "9 링크 만료 → 새 주문 없음·안내");
} catch (e) { assert(false, "scenario 9 threw " + e + " " + (e && e.stack || "")); } })();

/* 10) 중복 명령(15분 안 같은 송금) → 명시 확인만 · 잔액 부족 → 이유가 카드에 보이고 재전송 버튼 없음 · 질문/거절 intent → 주문 없음 */
(function () { try {
  routes = { "/api/orders?": async () => ({ body: { orders: [] } }), "/api/intent?": async () => ({ body: intentView() }),
             "/api/intent/prepare": async (b) => b.confirm_resend ? { body: { ok: true, order: ORDER_I } } : { status: 409, body: { ok: false, error: "dup", duplicate_of: { state: "FINAL_CONFIRMED_SOLIDITY", tx_hash: TX } } } };
  setWallet(IPHONE); const p = bootIntent(); const c0 = JSON.parse(snapshot());
  assert(U().state === "dup" && !p.el("resend").classList.contains("hidden") && p.el("sign").classList.contains("hidden"), "10 중복 → 명시 확인 버튼만(서명 없음)");
  p.el("resend").onclick(); drainMicrotasks(); drainMicrotasks(); drainMicrotasks();
  assert(U().state === "presign" && JSON.parse(snapshot()).sign_wallet === c0.sign_wallet, "10 [그래도 다시 보내기] → 카드(서명은 아직 0)");
  routes["/api/intent/prepare"] = async () => ({ status: 409, body: { ok: false, error: "잔액 부족: 잔액 1.000000 TRX < 보낼 2 + 최악 수수료 0.401 TRX" } });
  const p2 = bootIntent();
  assert(U().state === "blocked" && p2.el("blockBox").textContent.indexOf("잔액 부족") >= 0 && p2.el("resend").classList.contains("hidden") && p2.el("sign").classList.contains("hidden") && p2.el("confirmTitle").textContent.indexOf("보낼 수 없습니다") >= 0, "10 잔액 부족 → 이유 표시·재전송/서명 버튼 없음(9/29 20:47 결함 수정)");
  routes["/api/intent?"] = async () => ({ body: intentView({ kind: "question", question: "받는 사람(별칭)을 알려 주세요.", proposal: null, proposal_id: null }) });
  let prepN = 0; routes["/api/intent/prepare"] = async () => { prepN++; return { body: { ok: true, order: ORDER_I } }; };
  const p3 = bootIntent();
  assert(prepN === 0 && p3.el("prepStatus").textContent.indexOf("확인이 필요") >= 0, "10 질문 intent → prepare 0·안내");
} catch (e) { assert(false, "scenario 10 threw " + e + " " + (e && e.stack || "")); } })();


/* 11) [TronLink 앱에서 열기] 버튼: TronLink 밖 + 지갑 연결 안내일 때만 표시 · 클릭 1회 = 딥링크 이동 1회(AI/주문/서명/방송 0) · 두 번째 클릭은 이동 없이 복사 안내 · TronLink 안이면 숨김 */
(function () { try {
  let prepN = 0;
  routes = { "/api/orders?": async () => ({ body: { orders: [] } }), "/api/intent?": async () => ({ body: intentView() }), "/api/intent/prepare": async () => { prepN++; return { body: { ok: true, order: ORDER_I } }; } };
  globalThis.tron = undefined; globalThis.tronLink = undefined; globalThis.tronWeb = undefined;
  globalThis.location = { href: "https://mac.example.ts.net/a/" + INTENT_TOKEN }; globalThis.document.hidden = false;
  const c0 = JSON.parse(snapshot()); const p = bootIntent();
  assert(!p.el("openApp").classList.contains("hidden") && p.el("cConnect").classList.contains("hidden") && p.el("prepStatus").textContent.indexOf("TronLink 앱에서 열기") >= 0, "11 TronLink 밖 → [앱에서 열기] 표시·[지갑 연결] 숨김");
  assert(prepN === 0 && JSON.parse(snapshot()).chat === c0.chat, "11 버튼 표시만으로 prepare/chat 0");
  p.el("openApp").onclick(); drainMicrotasks();
  assert(String(globalThis.location.href).startsWith("tronlinkoutside://pull.activity?param=") && decodeURIComponent(globalThis.location.href).indexOf("/a/" + INTENT_TOKEN) >= 0, "11 클릭 → 같은 intent URL 로 딥링크 이동 1회 " + String(globalThis.location.href).slice(0, 40));
  const o1 = opsOf(); assert(o1.clicks.openApp === 1 && JSON.parse(snapshot()).chat === c0.chat && prepN === 0 && JSON.parse(snapshot()).sign_wallet === c0.sign_wallet, "11 버튼 자체의 AI/주문/서명/방송 0");
  flushTimers(3000); drainMicrotasks();
  assert(!p.el("copyHint").classList.contains("hidden") && p.el("copyLink").getAttribute("data-copy").indexOf("/a/" + INTENT_TOKEN) >= 0, "11 앱 전환 안 되면 복사 안내(링크 복사 버튼)");
  globalThis.location.href = "https://mac.example.ts.net/a/" + INTENT_TOKEN;
  p.el("openApp").onclick(); drainMicrotasks();
  assert(globalThis.location.href.indexOf("tronlinkoutside") < 0 && opsOf().clicks.openApp === 1, "11 두 번째 클릭은 자동 반복 없음(이동 0)");
  setWallet(IPHONE); const p2 = bootIntent();
  assert(p2.el("openApp").classList.contains("hidden") && U().state === "presign", "11 TronLink 안(지갑 있음) → 버튼 숨김·자동 준비");
} catch (e) { assert(false, "scenario 11 threw " + e + " " + (e && e.stack || "")); } })();

/* 12) 시험 모드 intent: 승인 버튼 숨김·배너 표시·서명 핸들러가 지갑을 부르지 않음 */
(function () { try {
  routes = { "/api/orders?": async () => ({ body: { orders: [] } }), "/api/intent?": async () => ({ body: intentView({ test_mode: true }) }), "/api/intent/prepare": async () => ({ body: { ok: true, order: ORDER_I } }) };
  setWallet(IPHONE); const c0 = JSON.parse(snapshot()); const p = bootIntent();
  assert(U().state === "presign" && p.el("sign").classList.contains("hidden") && !p.el("testBanner").classList.contains("hidden"), "12 시험 모드 → 카드는 보이되 승인 버튼 없음·배너");
  p.el("sign").onclick(); drainMicrotasks(); drainMicrotasks();
  assert(JSON.parse(snapshot()).sign_wallet === c0.sign_wallet && JSON.parse(snapshot()).signed === c0.signed && opsOf().clicks.approve === 0, "12 서명 핸들러 강제 호출도 지갑 서명 0·제출 0");
} catch (e) { assert(false, "scenario 12 threw " + e + " " + (e && e.stack || "")); } })();

drainMicrotasks();
print(out.join("\n")); print("RESULT " + (failures === 0 ? "PASS" : "FAIL " + failures));
