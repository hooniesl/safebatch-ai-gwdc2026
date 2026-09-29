/* 휴대폰 화면 로직 모의 시험(jsc, DOM 없음 — 실제 브라우저·실서명·방송 없음). phone_client.js 를 가짜 api/storage/ui 로 구동한다.
   VP 9/29 시나리오: A 제출 전 통신 단절 → 복구 시 같은 서명본 재제출·방송 1회 · B 서버 수신 후 응답 유실 → 재제출은 idempotent(방송 1회)
   C 새로고침·서버 재기동(같은 저장소) → 같은 주문 조회·재서명 0 · D 두 번 탭 → 서명 1회 · E 격리/중복/저장 실패/서명 전 취소/만료 서명본 폐기 */
load(ARG_CLIENT);
const out = []; let failures = 0;
function assert(c, m) { out.push((c ? "ok   " : "FAIL ") + m); if (!c) failures++; }
function mkStorage(broken) { const m = {}; return { setItem: (k, v) => { if (broken) throw new Error("QuotaExceeded"); m[k] = v; }, getItem: k => (k in m ? m[k] : null), removeItem: k => { delete m[k]; } }; }
function mkUI() { const u = { msgs: [], results: [], locks: [] }; u.msg = s => u.msgs.push(s); u.result = r => u.results.push(r.state); u.lock = b => u.locks.push(b); u.log = s => u.msgs.push("log:" + s); return u; }
const TX = "t".repeat(64);
const ORDER = { payment_id: "phone_trx_T1", snapshot_sha256: "s".repeat(64), tx_id: TX, expire_at_ms: 2_000_000, unsigned_tx: { txID: TX } };
const NOW = { t: 1_000_000 };
const okSign = async tx => ({ txID: tx.txID, raw_data: { x: 1 }, signature: ["sig1"] });
function run(name, fn) { fn().then(() => {}).catch(e => assert(false, name + " threw " + e)); }

/* 가짜 서버: 주문 1건. 서명본을 받으면 저장·방송(1회)·FINAL. 같은 서명본 재제출은 idempotent. */
function mkServer(opts) {
  const S = { signature_stored: false, order_state: "PENDING", result: null, broadcasts: 0, signedPosts: 0, statusCalls: 0, failFirstPost: !!opts.failFirstPost, lostResponseFirst: !!opts.lostResponseFirst, sig: null };
  S.holds = 0;
  S.api = async (p, b) => {
    if (p.startsWith("/api/order/hold")) { S.holds++; return { ok: true, hold: true }; }
    if (p.startsWith("/api/order/signed")) {
      S.signedPosts++;
      if (S.failFirstPost && S.signedPosts === 1) throw new Error("network down before server");
      if (S.signature_stored) { if (JSON.stringify(b.signed_tx.signature) === JSON.stringify(S.sig)) return { ...S.result, idempotent: true, order_state: S.order_state }; return { state: "NOT_SUBMITTED", reason: "duplicate", order_state: S.order_state, prior_result: S.result }; }
      S.signature_stored = true; S.sig = b.signed_tx.signature; S.order_state = "CONSUMED"; S.broadcasts++; S.result = { state: "FINAL_CONFIRMED_SOLIDITY", tx_hash: b.signed_tx.txID, payment_id: b.payment_id };
      if (S.lostResponseFirst && S.signedPosts === 1) throw new Error("response lost after server accepted");
      return S.result;
    }
    S.statusCalls++;
    return { ok: true, order_state: S.order_state, signature_stored: S.signature_stored, result: S.result, tx_hash: TX, expired: NOW.t > ORDER.expire_at_ms, expiry_reason: NOW.t > ORDER.expire_at_ms ? "server_clock+solidified_block_time" : null };
  };
  return S;
}

// A 제출 전 통신 단절 → 첫 제출 실패로 정지(서명본 보존) → 사용자가 [이어서 확인](resume) 1회 → 같은 서명본 재제출 → 방송 1회, 재서명 0
run("A", async () => {
  const st = mkStorage(false), ui = mkUI(), S = mkServer({ failFirstPost: true }); let walletCalls = 0;
  const c = SBClient.create({ api: S.api, storage: st, ui, now: () => NOW.t });
  const d0 = await c.sign(ORDER, async tx => { walletCalls++; return okSign(tx); });
  assert(d0.done === false && d0.why === "post-failed:1" && c.loadInflight().signed_tx, "A 첫 제출 실패 → 정지·서명본 보존(" + d0.why + ")");
  const d = await c.resume();
  assert(walletCalls === 1 && S.signedPosts === 2 && S.broadcasts === 1, "A 이어서 확인 → 같은 서명본 재제출(POST " + S.signedPosts + "회)·방송 " + S.broadcasts + "회·지갑 서명 " + walletCalls + "회");
  assert(d.done === true && c.loadInflight() === null, "A 체인 종결 뒤 복구 정보 삭제(" + d.why + ")");
});

// B 서버 수신 후 응답 유실 → 정지 → resume: 서버에 서명 저장됨 → 재제출 없이 상태 조회로 종결. 같은 서명본 명시 재제출도 idempotent
run("B", async () => {
  const st = mkStorage(false), ui = mkUI(), S = mkServer({ lostResponseFirst: true }); let walletCalls = 0;
  const c = SBClient.create({ api: S.api, storage: st, ui, now: () => NOW.t });
  const d0 = await c.sign(ORDER, async tx => { walletCalls++; return okSign(tx); });
  assert(d0.why === "post-failed:1" && S.broadcasts === 1, "B 응답 유실 → 정지(방송은 이미 1회)");
  const d = await c.resume();
  assert(S.signedPosts === 1 && S.broadcasts === 1 && walletCalls === 1 && d.done === true && d.why === "chain-terminal:FINAL_CONFIRMED_SOLIDITY", "B 이어서 확인 → 재제출 없이 원래 주문 조회로 종결(POST " + S.signedPosts + "회)");
  const r = await S.api("/api/order/signed", { payment_id: ORDER.payment_id, snapshot_sha256: ORDER.snapshot_sha256, signed_tx: { txID: TX, signature: ["sig1"] } });
  assert(r.idempotent === true && S.broadcasts === 1, "B 같은 서명본 재제출 → idempotent·방송 여전히 1회");
});

// C 서명 뒤 서버 불통(제출·조회 모두 실패) → 새로고침(새 인스턴스, 같은 저장소) + 서버 복구(같은 토큰) → resume → 같은 서명본 제출 1회·재서명 0
run("C", async () => {
  const st = mkStorage(false), ui = mkUI(), S = mkServer({}); let walletCalls = 0;
  const c1 = SBClient.create({ api: async () => { throw new Error("down"); }, storage: st, ui, now: () => NOW.t });
  const d1 = await c1.sign(ORDER, async tx => { walletCalls++; return okSign(tx); });
  const rec = c1.loadInflight();
  assert(d1.why === "post-failed:1" && rec && rec.stage === "SUBMITTING" && rec.signed_tx && rec.signed_tx.txID === TX, "C 불통 중 서명본 보관(단계 " + (rec && rec.stage) + ")");
  const ui2 = mkUI(); const c2 = SBClient.create({ api: S.api, storage: st, ui: ui2, now: () => NOW.t });
  const d2 = await c2.resume();
  assert(walletCalls === 1 && S.signedPosts === 1 && S.broadcasts === 1 && d2.done === true, "C 새로고침 뒤 같은 서명본 제출 1회·방송 1회·재서명 0(" + d2.why + ")");
  assert(ui2.msgs.some(m => m.includes("같은 서명본")), "C 사용자에게 재제출 안내");
});

// D 두 번 탭: sign 을 연속 두 번 호출(첫 번째는 지갑 서명 대기 중) → 두 번째는 busy 거부, 지갑 서명 1회·POST 1회·방송 1회
run("D", async () => {
  const st = mkStorage(false), ui = mkUI(), S = mkServer({}); let walletCalls = 0, resolveSign = null;
  const c = SBClient.create({ api: S.api, storage: st, ui, now: () => NOW.t });
  const slowSign = tx => { walletCalls++; return new Promise(r => { resolveSign = () => r(okSign(tx)); }); };
  const p1 = c.sign(ORDER, slowSign); const p2 = c.sign(ORDER, slowSign);
  const b = await p2; assert(b.aborted === "busy", "D 두 번째 탭은 busy 로 거부");
  resolveSign(); const a = await p1;
  assert(walletCalls === 1 && S.signedPosts === 1 && S.broadcasts === 1 && a.done === true, "D 두 번 탭 → 지갑 서명 " + walletCalls + "회·POST " + S.signedPosts + "회·방송 " + S.broadcasts + "회");
});

// E1 격리(SIGNATURE_HELD) → 복구 정보 유지·잠금 유지·재제출 없음(signature_stored=true)
run("E1", async () => {
  const st = mkStorage(false), ui = mkUI();
  const api = async (p, b) => p.startsWith("/api/order/signed") ? { state: "SIGNATURE_HELD", not_broadcast: true, order_state: "SIGNED_REFUSED_NOT_BROADCAST" }
    : { ok: true, order_state: "SIGNED_REFUSED_NOT_BROADCAST", signature_stored: true, result: { state: "SIGNATURE_HELD" }, tx_hash: TX, expired: false };
  const c = SBClient.create({ api, storage: st, ui, now: () => NOW.t });
  const d = await c.sign(ORDER, okSign);
  assert(d.done === false && c.loadInflight() !== null && ui.locks[ui.locks.length - 1] === true, "E1 격리 → 복구 유지·잠금(" + d.why + ")");
  const d2 = await c.resume(); assert(d2.done === false && c.canResubmit(c.loadInflight(), d2.s) === false, "E1 격리 주문은 재제출 안 함");
});

// E2 중복 제출 응답(NOT_SUBMITTED) + 원래 주문 UNKNOWN → 유지 / FINAL → 종결
run("E2", async () => {
  for (const [rs, expectDone] of [["UNKNOWN", false], ["FINAL_CONFIRMED_SOLIDITY", true]]) {
    const st = mkStorage(false), ui = mkUI();
    const api = async (p, b) => p.startsWith("/api/order/signed") ? { state: "NOT_SUBMITTED", reason: "duplicate", order_state: "CONSUMED" }
      : { ok: true, order_state: "CONSUMED", signature_stored: true, result: { state: rs }, tx_hash: TX, expired: false };
    const c = SBClient.create({ api, storage: st, ui, now: () => NOW.t });
    const d = await c.sign(ORDER, okSign);
    assert(d.done === expectDone, "E2 NOT_SUBMITTED + 원래 주문 " + rs + " → " + (expectDone ? "종결" : "유지") + "(" + d.why + ")");
  }
});

// E3 저장 실패 → 서명 0 · E4 서명 전 취소(PREPARED, 서버 서명 없음) → 삭제 · E5 서명 완료 뒤 서버 미수신은 미서명으로 보지 않음
run("E3-5", async () => {
  const st = mkStorage(true), ui = mkUI(); let walletCalls = 0;
  const c = SBClient.create({ api: async () => { throw new Error("must not"); }, storage: st, ui, now: () => NOW.t });
  const d = await c.sign(ORDER, async tx => { walletCalls++; return okSign(tx); });
  assert(d.aborted === "storage" && walletCalls === 0, "E3 저장 실패 → 지갑 서명 0");
  const c4 = SBClient.create({ api: async () => ({}), storage: mkStorage(false), ui: mkUI(), now: () => NOW.t });
  assert(c4.decide({ order_state: "CANCELLED", signature_stored: false, result: null }, { stage: "PREPARED" }).done === true, "E4 PREPARED + 취소 → 삭제");
  assert(c4.decide({ order_state: "PENDING", signature_stored: false, result: null }, { stage: "SIGNED", signed_tx: { txID: TX } }).done === false, "E5 SIGNED 단계의 서버 미수신 → 유지(미서명 아님)");
  assert(c4.decide({ order_state: "CANCELLED", signature_stored: false, result: null }, { stage: "SIGNING", signed_tx: null }).done === false, "E5 SIGNING 단계는 취소 응답에도 미서명 종결 안 함");
  assert(c4.canResubmit({ stage: "SIGNED", signed_tx: { txID: TX }, tx_hash: TX }, { order_state: "PENDING", signature_stored: false, expired: true }) === false, "E5 만료 주문은 재제출 안 함");
  assert(c4.canResubmit({ stage: "SIGNED", signed_tx: { txID: TX }, tx_hash: TX }, { order_state: "CANCELLED", signature_stored: false, expired: false }) === false, "E5 취소 주문은 재제출 안 함");
});

// E6 만료된 보관 서명본 폐기: 서버 expired+근거를 2회 연속 확인해야 폐기 → 경로 종료·재제출 없음·'새 주문' 안내 없음·잠금 유지
run("E6", async () => {
  const st = mkStorage(false), ui = mkUI(); let posts = 0;
  st.setItem("sb_inflight", JSON.stringify({ payment_id: ORDER.payment_id, snapshot_sha256: ORDER.snapshot_sha256, tx_hash: TX, expire_at_ms: 100, stage: "SIGNED", signed_tx: { txID: TX, signature: ["s"] } }));
  const api = async (p) => p.startsWith("/api/order/signed") ? (posts++, { state: "x" }) : { ok: true, order_state: "PENDING", signature_stored: false, result: null, tx_hash: TX, expired: true, expiry_reason: "server_clock+solidified_block_time" };
  const c = SBClient.create({ api, storage: st, ui, now: () => NOW.t });
  const d1 = await c.resume(); assert(c.loadInflight().signed_tx !== null && c.loadInflight().expired_seen === 1 && posts === 0, "E6 만료 1회 관측 → 아직 폐기 안 함·재제출 0");
  const d = await c.resume();
  const rec = c.loadInflight();
  assert(posts === 0 && rec && rec.signed_tx === null && rec.stage === "SIGNED_EXPIRED", "E6 만료 2회 확인 뒤 서명본 폐기·재제출 0(" + d.why + ")");
  assert(!ui.msgs.some(m => m.includes("새 주문")) && d.done === false && ui.locks[ui.locks.length - 1] === true, "E6 '새 주문' 안내 없음·주문 기록·잠금 유지");
  // 근거 없는 expired(boolean 만) 는 폐기하지 않음
  const st2 = mkStorage(false); st2.setItem("sb_inflight", JSON.stringify({ payment_id: ORDER.payment_id, snapshot_sha256: ORDER.snapshot_sha256, tx_hash: TX, expire_at_ms: 100, stage: "SIGNED", signed_tx: { txID: TX, signature: ["s"] } }));
  const api2 = async (p) => ({ ok: true, order_state: "PENDING", signature_stored: false, result: null, tx_hash: TX, expired: true, expiry_reason: null });
  const c2 = SBClient.create({ api: api2, storage: st2, ui: mkUI(), now: () => NOW.t });
  await c2.resume(); await c2.resume(); await c2.resume();
  assert(c2.loadInflight().signed_tx !== null, "E6 근거 없는 expired 는 폐기하지 않음");
});

// F 지속적인 POST 실패(조회는 성공): 제출은 복구 동작당 최대 1회, 재귀 없음, 서명본·주문 보존
run("F", async () => {
  const st = mkStorage(false), ui = mkUI(); let posts = 0, statusCalls = 0;
  const api = async (p) => { if (p.startsWith("/api/order/signed")) { posts++; throw new Error("network"); } statusCalls++; return { ok: true, order_state: "PENDING", signature_stored: false, result: null, tx_hash: TX, expired: false }; };
  const c = SBClient.create({ api, storage: st, ui, now: () => NOW.t });
  const d = await c.sign(ORDER, okSign);
  assert(posts === 1 && d.done === false && d.why.startsWith("post-failed"), "F 서명 뒤 첫 제출 실패 → 1회로 정지(" + d.why + ")");
  const d2 = await c.resume(); const d3 = await c.resume();
  const rec = c.loadInflight();
  assert(posts === 3 && rec && rec.signed_tx && rec.signed_tx.txID === TX && rec.submit_count === 3, "F 복구 동작당 재제출 1회씩(총 POST " + posts + ")·서명본 보존(submit_count " + (rec && rec.submit_count) + ")");
  assert(ui.locks[ui.locks.length - 1] === true, "F 잠금 유지");
});

// G 지갑 서명 직후 SIGNED 저장 실패 → SIGNING 기록 유지 → 새로고침 뒤에도 미서명 종결 없음·잠금 유지·서버에 보유 등록. 사용자 확인 버튼 없음
run("G", async () => {
  const m = {}; let fails = false; const st = { setItem: (k, v) => { if (fails && v.includes('"SIGNED"')) throw new Error("quota"); m[k] = v; }, getItem: k => (k in m ? m[k] : null), removeItem: k => { delete m[k]; } };
  const ui = mkUI(); let posts = 0, holds = 0;
  const api = async (p) => { if (p.startsWith("/api/order/hold")) { holds++; return { ok: true }; } if (p.startsWith("/api/order/signed")) { posts++; return { state: "x" }; } return { ok: true, order_state: "PENDING", signature_stored: false, result: null, tx_hash: TX, expired: false, client_hold: holds > 0 }; };
  const c = SBClient.create({ api, storage: st, ui, now: () => NOW.t });
  const d = await c.sign(ORDER, async tx => { fails = true; return okSign(tx); });
  const rec = c.loadInflight();
  assert(d.aborted === "storage-signed" && rec && rec.stage === "SIGNING" && posts === 0 && holds === 1, "G SIGNED 저장 실패 → SIGNING 유지·제출 0·서버 보유 등록 " + holds);
  fails = false; const ui2 = mkUI(); const c2 = SBClient.create({ api, storage: st, ui: ui2, now: () => NOW.t });
  const d2 = await c2.resume(); const d3 = await c2.resume();
  assert(d3.done === false && c2.loadInflight() !== null && ui2.locks[ui2.locks.length - 1] === true && typeof c2.confirmNotSigned === "undefined", "G 새로고침·반복 조회 뒤에도 잠금 유지·삭제 경로 없음(" + d3.why + ")");
});

// H 지갑 응답 대기 중 페이지 종료(SIGNING 저장 뒤 종료) → 재진입: 유지·재제출 없음·잠금. 일반 지갑 오류는 유지+보유 등록, 확실한 사용자 거절만 삭제
run("H", async () => {
  const st = mkStorage(false), ui = mkUI(); let posts = 0, holds = 0;
  st.setItem("sb_inflight", JSON.stringify({ payment_id: ORDER.payment_id, snapshot_sha256: ORDER.snapshot_sha256, tx_hash: TX, expire_at_ms: 2_000_000, stage: "SIGNING", signed_tx: null }));
  const api = async (p) => { if (p.startsWith("/api/order/hold")) { holds++; return { ok: true }; } if (p.startsWith("/api/order/signed")) { posts++; return {}; } return { ok: true, order_state: "PENDING", signature_stored: false, result: null, tx_hash: TX, expired: false }; };
  const c = SBClient.create({ api, storage: st, ui, now: () => NOW.t });
  const d = await c.resume();
  assert(d.done === false && posts === 0 && c.loadInflight().stage === "SIGNING" && ui.locks[ui.locks.length - 1] === true, "H 페이지 종료 뒤 재진입: 유지·재제출 0·잠금(" + d.why + ")");
  const st3 = mkStorage(false), ui3 = mkUI(); const c3 = SBClient.create({ api, storage: st3, ui: ui3, now: () => NOW.t });
  const e1 = await c3.sign(ORDER, async () => { throw new Error("Unknown method called"); });
  assert(e1.aborted === "wallet-unknown" && c3.loadInflight() && c3.loadInflight().stage === "SIGNING" && holds >= 1, "H 일반 지갑 오류 → SIGNING 유지·보유 등록");
  c3.clearInflight();
  const e2 = await c3.sign(ORDER, async () => { const err = new Error("User rejected the request"); err.code = 4001; throw err; });
  assert(e2.aborted === "wallet-rejected" && c3.loadInflight() === null, "H 확실한 사용자 거절(4001) → 미서명 삭제");
});

// I 휴대폰 시계가 서버보다 빠름: 로컬 now 가 만료를 넘어도 서버 expired=false 면 폐기하지 않고 재제출 허용
run("I", async () => {
  const st = mkStorage(false), ui = mkUI(); const S = mkServer({});
  st.setItem("sb_inflight", JSON.stringify({ payment_id: ORDER.payment_id, snapshot_sha256: ORDER.snapshot_sha256, tx_hash: TX, expire_at_ms: 100, stage: "SIGNED", signed_tx: { txID: TX, raw_data: {}, signature: ["sig1"] }, submit_count: 0 }));
  const c = SBClient.create({ api: S.api, storage: st, ui, now: () => 9_999_999_999 });
  const d = await c.resume();
  assert(S.signedPosts === 1 && S.broadcasts === 1 && d.done === true, "I 로컬 시계 초과·서버 만료 아님 → 서명본 유지·재제출 1회·확정(" + d.why + ")");
});

// J 세대(epoch): 폴링이 스테일 판단으로 삭제하려 할 때 그 사이 SIGNING 이 저장됐으면 삭제 거부
run("J", async () => {
  const st = mkStorage(false), ui = mkUI();
  let gate; const api = async (p) => { if (p.startsWith("/api/order/status")) { await new Promise(r => { gate = r; }); return { ok: true, order_state: "PENDING", signature_stored: false, result: null, tx_hash: TX, expired: false }; } return {}; };
  const c = SBClient.create({ api, storage: st, ui, now: () => NOW.t });
  st.setItem("sb_inflight", JSON.stringify({ payment_id: ORDER.payment_id, snapshot_sha256: ORDER.snapshot_sha256, tx_hash: TX, stage: "PREPARED", signed_tx: null, epoch: 1 }));
  const pending = c.checkOrder(ORDER.payment_id);                     // 스테일 폴링(PREPARED 를 읽음)
  await Promise.resolve(); await Promise.resolve();
  st.setItem("sb_inflight", JSON.stringify({ payment_id: ORDER.payment_id, snapshot_sha256: ORDER.snapshot_sha256, tx_hash: TX, stage: "SIGNING", signed_tx: null, epoch: 2 }));   // 그 사이 서명 시작
  gate(); const d = await pending;
  assert(d.done === false && d.why.startsWith("stale") && c.loadInflight() && c.loadInflight().stage === "SIGNING", "J 스테일 폴링의 삭제 거부(" + d.why + ")");
});

// K NOT_SUBMITTED(200) 뒤 재진입 없음: stage SUBMIT_REFUSED, recover 해도 재제출 0
run("K", async () => {
  const st = mkStorage(false), ui = mkUI(); let posts = 0;
  const api = async (p) => p.startsWith("/api/order/signed") ? (posts++, { state: "NOT_SUBMITTED", reason: "order is CANCELLED", order_state: "CANCELLED" }) : { ok: true, order_state: "PENDING", signature_stored: false, result: null, tx_hash: TX, expired: false };
  const c = SBClient.create({ api, storage: st, ui, now: () => NOW.t });
  const d = await c.sign(ORDER, okSign);
  assert(posts === 1 && c.loadInflight() && c.loadInflight().stage === "SUBMIT_REFUSED", "K NOT_SUBMITTED → SUBMIT_REFUSED 고정");
  await c.resume(); await c.resume();
  assert(posts === 1, "K 복구 반복해도 재제출 0(POST " + posts + ")");
});

// M 거절 판정: 4001 또는 명시 문구만. 'connection cancelled' 는 SIGNING 유지
run("M", async () => {
  const c = SBClient.create({ api: async () => ({ ok: true, order_state: "PENDING", signature_stored: false, result: null, tx_hash: TX, expired: false }), storage: mkStorage(false), ui: mkUI(), now: () => NOW.t });
  assert(c.isUserReject({ code: 4001, message: "x" }) === true && c.isUserReject(new Error("User rejected the request")) === true, "M 4001·명시 문구 → 거절");
  assert(c.isUserReject(new Error("connection cancelled")) === false && c.isUserReject(new Error("request was cancelled by navigation")) === false, "M 부분일치는 거절 아님");
  const e = await c.sign(ORDER, async () => { throw new Error("connection cancelled"); });
  assert(e.aborted === "wallet-unknown" && c.loadInflight().stage === "SIGNING", "M 'connection cancelled' → SIGNING 유지");
});

// N 서명 보유 + 서버 CANCELLED + 미보관 + 미만료: 몇 번 확인해도 폐기·잠금 해제 없음(미송금 근거 아님), 재제출 0, 서버에 보유 등록
run("N", async () => {
  const st = mkStorage(false), ui = mkUI(); let posts = 0, holds = 0;
  st.setItem("sb_inflight", JSON.stringify({ payment_id: ORDER.payment_id, snapshot_sha256: ORDER.snapshot_sha256, tx_hash: TX, expire_at_ms: 2_000_000, stage: "SIGNED", signed_tx: { txID: TX, signature: ["s"] } }));
  const api = async (p) => { if (p.startsWith("/api/order/hold")) { holds++; return { ok: true }; } if (p.startsWith("/api/order/signed")) { posts++; return {}; } return { ok: true, order_state: "CANCELLED", signature_stored: false, result: null, tx_hash: TX, expired: false, client_hold: holds > 0 }; };
  const c = SBClient.create({ api, storage: st, ui, now: () => NOW.t });
  let d; for (let i = 0; i < 3; i++) d = await c.resume();
  const rec = c.loadInflight();
  assert(d.done === false && rec && rec.signed_tx !== null && posts === 0 && holds >= 1 && ui.locks[ui.locks.length - 1] === true, "N 3회 확인 후에도 서명본 유지·잠금·재제출 0·보유 등록(" + d.why + ")");
});

drainMicrotasks();
for (const tag of ["A ", "B ", "C ", "D ", "E1", "E2", "E3", "E4", "E5", "E6", "F ", "G ", "H ", "I ", "J ", "K ", "M ", "N "]) { if (!out.some(l => l.slice(5).startsWith(tag))) { out.push("FAIL 시나리오 " + tag.trim() + " 결과 없음(미실행)"); failures++; } }
print(out.join("\n")); print("RESULT " + (failures === 0 ? "PASS" : "FAIL " + failures));
