globalThis.__page = function () {

const BASE = "__SB_BASE__";
const $ = id => document.getElementById(id);
const S = SBClient.screen;                  // 순수 화면 규칙(jsc 검사 대상)
let ui = S.initial();                        // {state, reqId, proposal, order, result, walletAddr}
let wallet = { address: null, node: null, via: null };
let health = null;
function log(s) { const d = $("logBody"); d.textContent = new Date().toLocaleTimeString() + " " + s + "\n" + d.textContent; }
function toast(t) { $("toast").textContent = t || ""; }
async function api(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(BASE + path, opt); let j;
  try { j = await r.json(); } catch (e) { j = { ok: false, error: "bad json" }; }
  if (typeof j !== "object" || j === null) j = { ok: false, error: "bad body" };
  j._http = r.status; if (r.status >= 400) { j.ok = false; j.error = j.error || ("http " + r.status); }
  return j;
}
function esc(s) { return SBClient.escapeHtml(s); }
function kv(rows) { return rows.map(([k, v, cls]) => `<div class="k">${esc(k)}</div><div class="${cls || ""}">${v}</div>`).join(""); }
function copyBtn(v, label) { return `<button type="button" class="link copy" data-copy="${esc(v)}" aria-label="${esc(label || "복사")}">복사</button>`; }
document.addEventListener("click", e => { const b = e.target.closest && e.target.closest("button.copy"); if (!b) return; const v = b.getAttribute("data-copy") || "";
  (navigator.clipboard && navigator.clipboard.writeText ? navigator.clipboard.writeText(v) : Promise.reject()).then(() => toast("복사했습니다"), () => { window.prompt("길게 눌러 복사하세요", v); }); });

/* ── 지갑 ───────────────────────────────────────────────── */
function tronWebNow() { return (window.tron && window.tron.tronWeb) || (window.tronLink && window.tronLink.tronWeb) || window.tronWeb || null; }
function tronInstalled() { return !!(window.tron || window.tronLink || window.tronWeb); }
function walletLine() {
  const tw = tronWebNow();
  if (!tronInstalled()) return { ok: false, text: "TronLink 을 찾지 못했습니다. 이 페이지를 TronLink 앱의 DApp 브라우저(Discover)에서 여세요." };
  if (!tw || !(tw.defaultAddress && tw.defaultAddress.base58)) return { ok: false, text: "TronLink 는 있으나 아직 연결되지 않았습니다. [지갑 연결]을 누르세요." };
  wallet.address = tw.defaultAddress.base58; wallet.node = (tw.fullNode && tw.fullNode.host) || "?";
  const nile = /nile/i.test(wallet.node);
  return { ok: true, nile, text: `계정 ${wallet.address} · 노드 ${wallet.node} · ${nile ? "Nile" : "노드 주소에 nile 이 없습니다. 지갑 앱 네트워크를 Nile 로 바꾸세요"}` + (wallet.via ? ` · ${wallet.via}` : "") };
}
function refreshWallet() {
  const w = walletLine(); $("walletState").textContent = w.text;
  if (w.ok) { $("walletShort").textContent = (w.nile ? "" : "⚠ Nile 아님 · ") + "지갑 " + S.shortAddr(wallet.address); $("connect").classList.add("hidden"); $("walletDetails").classList.remove("hidden"); }
  else { $("walletShort").textContent = "지갑 연결 안 됨"; $("connect").classList.remove("hidden"); $("walletDetails").classList.remove("hidden"); }
  const changed = S.walletChanged(ui, wallet.address);
  if (changed) { addMsg("bot", "지갑 계정이 바뀌었습니다. 이전 제안·주문으로는 서명할 수 없습니다. [새 송금]으로 다시 시작하세요."); ui = S.on(ui, { type: "wallet-changed", address: wallet.address }); render(); }
  return w;
}
$("connect").onclick = async () => {
  try {
    if (window.tron && typeof window.tron.request === "function") {
      try { await window.tron.request({ method: "eth_requestAccounts" }); wallet.via = "tron.eth_requestAccounts"; }
      catch (e) { if (window.tronLink && window.tronLink.request) { await window.tronLink.request({ method: "tron_requestAccounts" }); wallet.via = "tronLink.tron_requestAccounts"; } else throw e; }
    } else if (window.tronLink && typeof window.tronLink.request === "function") {
      await window.tronLink.request({ method: "tron_requestAccounts" }); wallet.via = "tronLink.tron_requestAccounts";
    } else if (window.tronWeb && window.tronWeb.defaultAddress && window.tronWeb.defaultAddress.base58) { wallet.via = "window.tronWeb"; }
    else { throw new Error("TronLink 확장/앱을 찾지 못했습니다."); }
    await new Promise(r => setTimeout(r, 400));
    refreshWallet(); ui.walletAddr = wallet.address; log("wallet connect: " + JSON.stringify(walletLine())); loadHistory();
  } catch (e) { $("walletShort").textContent = "연결 실패: " + (e && e.message || e); log("connect error " + (e && e.message || e)); }
};

/* ── 화면 렌더 ──────────────────────────────────────────── */
function addMsg(cls, text) { const d = document.createElement("div"); d.className = "msg " + cls; d.textContent = text; $("chat").appendChild(d); }
function show(id) { for (const s of ["stageInput", "stageConfirm", "stageProcessing", "stageDone"]) $(s).classList.toggle("hidden", s !== id); }
function render() {
  const st = ui.state;
  if (st === "input") { show("stageInput"); }
  if (st === "confirm" || st === "presign" || st === "dup") {
    show("stageConfirm"); const p = ui.proposal.proposal; const o = ui.order;
    $("confirmTitle").textContent = st === "presign" ? "서명 전 확인" : "해석 확인";
    $("cAmount").textContent = (o ? o.amount_trx : p.amount_trx) + " TRX";
    $("cTo").textContent = (o ? o.receiver_alias : p.alias) + " 에게";
    $("cAddr").textContent = o ? o.receiver : p.address;                       // 서명 전 전체 주소는 축약하지 않는다
    $("cFrom").textContent = wallet.address || "(지갑 연결 필요)";
    $("cAi").textContent = S.aiLine(ui.proposal.ai);
    $("cNotes").innerHTML = SBClient.proposalNotice(ui.proposal).messages.map(m => `<div class="note">${esc(m)}</div>`).join("");   // 관용 매칭 재확인은 한 번만(표 행 중복 제거, VP 9/29)
    $("feeBlock").classList.toggle("hidden", !o);
    if (o) {
      const q = o.quote; const f = S.feeLines(o, q);
      $("cFee").textContent = f.fee; $("cCap").textContent = f.cap; $("cExpire").textContent = new Date(o.expire_at_ms).toLocaleTimeString() + " 까지";
      $("cDetail").innerHTML = kv([["수수료 계산", esc(f.detail)], ["주문 ID", esc(o.payment_id) + " " + copyBtn(o.payment_id), "long"], ["txID", esc(o.tx_id) + " " + copyBtn(o.tx_id), "long"], ["지문", esc(o.snapshot_sha256), "long"],
                                   ["서버 검사", esc(o.checks.length + "/" + o.checks.length + " 통과")], ["chainId", esc(String(o.chain_id))], ["잔액", esc((q.balance_sun / 1e6).toFixed(6) + " TRX")], ["서명 주체", esc(o.signing.who)]]);
    }
    $("dupBox").classList.toggle("hidden", st !== "dup");
    $("prepare").classList.toggle("hidden", st !== "confirm"); $("resend").classList.toggle("hidden", st !== "dup");
    $("sign").classList.toggle("hidden", st !== "presign"); $("reject").classList.toggle("hidden", st !== "presign"); $("edit").classList.toggle("hidden", st === "presign");
    $("prepare").disabled = busyPrepare; $("resend").disabled = busyPrepare; $("sign").disabled = !S.canSign(ui, wallet.address);
  }
  if (st === "processing" || st === "unknown") {
    show("stageProcessing"); const o = ui.order || {}; const r = ui.result || {};
    $("pAmount").textContent = (o.amount_trx || "?") + " TRX"; $("pTo").textContent = (o.receiver_alias || "") + " 에게";
    $("pStatus").className = "status " + (st === "unknown" ? "bad" : "warn"); $("pStatus").textContent = st === "unknown" ? "결과 확인 필요" : "확정 확인 중";
    $("pText").textContent = st === "unknown" ? "보내졌을 수 있어 다시 보내지 마세요. 같은 거래(txID)만 계속 확인합니다." : (S.resultLabel(r.state) || "블록 포함 여부와 확정을 확인하고 있습니다.");
    $("pDetail").innerHTML = kv([["txID", esc(o.tx_id || r.tx_hash || "-") + copyBtn(o.tx_id || r.tx_hash || ""), "long"], ["주문 ID", esc(o.payment_id || r.payment_id || "-"), "long"]]);
    refreshRecoveryButtons();
  }
  if (st === "done") {
    show("stageDone"); const o = ui.order || {}; const r = ui.result || {}; const rc = r.receipt || {};
    $("dAmount").textContent = (o.amount_trx || "") + " TRX"; $("dTo").textContent = (o.receiver_alias || "") + " 에게";
    $("dFee").textContent = "실제 수수료 " + S.feeText(rc) + (rc.fee_within_cap === false ? " · 확인 필요(상한 초과)" : "");
    $("dDetail").innerHTML = kv([["받는 주소", esc(o.receiver || "-") + copyBtn(o.receiver || ""), "long"], ["txID", esc(r.tx_hash || "-") + copyBtn(r.tx_hash || ""), "long"], ["블록", esc(String(rc.block_number || "-"))],
                                 ["수수료 근거", esc(S.feeBasis(rc))], ["탐색기", r.explorer ? `<a href="${esc(r.explorer)}" target="_blank" rel="noopener">같은 txID 보기</a>` : "-"], ["주문 ID", esc(o.payment_id || r.payment_id || "-"), "long"]]);
  }
}
let busyPrepare = false;

/* ── 입력 → 해석(AI 예산 소비 가능: 연타 금지) ───────────── */
let sending = false;
$("send").onclick = async () => {
  const t = $("text").value.trim(); if (!t || sending) return;
  sending = true; $("send").disabled = true; $("send").textContent = "확인 중…";
  const my = (ui = S.on(ui, { type: "request", text: t })).reqId; addMsg("me", t); $("text").value = "";
  try {
    const r = await api("/api/chat", { text: t }); log("chat → " + r.kind + (r.ai && r.ai.call_id ? " call " + r.ai.call_id : "") + (r.ai && r.ai.carried_from ? " carried " + r.ai.carried_from : ""));
    if (!S.isCurrent(ui, my)) { log("stale chat response ignored"); return; }
    $("aiLine").textContent = "이 요청: " + S.aiLine(r.ai);
    if (r.kind === "question") { addMsg("bot", r.question); ui = S.on(ui, { type: "question" }); render(); return; }
    if (r.kind === "decline") { addMsg("bot", "거절: " + r.explain + " (주문 없음 · " + r.reason_code + ")"); ui = S.on(ui, { type: "decline" }); render(); return; }
    ui = S.on(ui, { type: "proposal", proposal: r, walletAddr: wallet.address }); render();
  } catch (e) { addMsg("bot", "서버에 연결되지 않습니다. 다시 시도하세요."); log("chat error " + (e && e.message || e)); }
  finally { sending = false; $("send").disabled = false; $("send").textContent = "송금 내용 확인"; }
};
$("edit").onclick = () => { ui = S.on(ui, { type: "edit" }); render(); $("text").focus(); };

/* ── 수수료 확인(prepare) ───────────────────────────────── */
async function doPrepare(confirmResend) {
  const w = refreshWallet(); if (!w.ok) { toast("먼저 지갑을 연결하세요."); return; }
  if (!ui.proposal || busyPrepare || !["confirm", "dup"].includes(ui.state)) return;
  busyPrepare = true; render();
  const my = ui.reqId;
  try {
    const r = await api("/api/order/prepare", { proposal_id: ui.proposal.proposal_id, sender: wallet.address, confirm_resend: !!confirmResend }); log("prepare → " + (r.ok ? r.order.payment_id : r.error));
    if (!S.isCurrent(ui, my)) { log("stale prepare response ignored"); return; }
    if (!r.ok) {
      if (r.duplicate_of) {
        const d = r.duplicate_of;
        $("dupBox").innerHTML = `<b>같은 지갑·수취인·수량의 이전 송금이 최근 15분 안에 있습니다.</b><div class="status ${S.stateClass(d.state)}">${esc(S.resultLabel(d.state))}</div><div class="long">txID ${esc(d.tx_hash || "-")}</div>` + (d.explorer ? `<div><a href="${esc(d.explorer)}" target="_blank" rel="noopener">탐색기에서 보기</a></div>` : "") + `<div class="sub">한 번 더 보내려면 아래 버튼으로 확인합니다.</div>`;
        ui = S.on(ui, { type: "duplicate" });
      } else { addMsg("bot", "주문을 만들지 못했습니다: " + r.error); ui = S.on(ui, { type: "prepare-failed" }); }
      render(); return;
    }
    ui = S.on(ui, { type: "order", order: r.order }); render();
  } catch (e) { addMsg("bot", "서버에 연결되지 않습니다. 다시 시도하세요."); log("prepare error " + (e && e.message || e)); }
  finally { busyPrepare = false; render(); }
}
$("prepare").onclick = () => doPrepare(false);
$("resend").onclick = () => doPrepare(true);

/* ── 서명·전송(SBClient) ────────────────────────────────── */
function lockNewOrders(on, why) {
  $("send").disabled = on || sending;                                   // 서명 버튼은 render 가 현재 주문·상태로만 결정(이전 주문 재활성 없음)
  if (on && why) addMsg("bot", why);
  if (!on && ui.state === "presign") render();
}
function refreshRecoveryButtons() { const f = client.loadInflight(); $("resumeBtn").classList.toggle("hidden", !f); }
const client = SBClient.create({ api, storage: window.localStorage, now: () => Date.now(), key: "sb_inflight_" + BASE,
                                 ui: { msg: t => addMsg("bot", t), result: r => showResult(r), lock: on => lockNewOrders(on), log: s => log(s) } });
$("sign").onclick = async () => {
  const tw = tronWebNow(); const o = ui.order;
  if (!tw || !o || !S.canSign(ui, wallet.address)) { toast("지금은 서명할 수 없습니다."); return; }   // 완료/이전 주문/지갑 변경 시 핸들러도 실행하지 않는다
  if (tw.defaultAddress.base58 !== o.user_eoa) { toast("지갑 계정이 주문의 보내는 지갑과 다릅니다."); return; }
  ui = S.on(ui, { type: "signing" }); render();
  const d = await client.sign(o, tx => tw.trx.sign(JSON.parse(JSON.stringify(tx))));
  refreshRecoveryButtons();
  if (d && d.aborted === "wallet-rejected") { ui = S.on(ui, { type: "sign-rejected" }); render(); return; }
  if (d && d.aborted) { ui = S.on(ui, { type: "unknown" }); render(); return; }
  loadHistory();
  if (d && d.done === false && !String(d.why || "").startsWith("post-failed")) pollStatus(o.payment_id, 60);
  if (d && d.done === false && String(d.why || "").startsWith("post-failed")) { ui = S.on(ui, { type: "unknown" }); render(); }
};
$("reject").onclick = async () => {
  const o = ui.order; if (!o || ui.state !== "presign") return;
  const r = await api("/api/order/reject", { payment_id: o.payment_id, snapshot_sha256: o.snapshot_sha256 });
  addMsg("bot", r.ok ? ((r.detail || "").startsWith("cancelled") ? "취소했습니다. 아무것도 전송되지 않았습니다." : "서명은 이미 만들어졌지만 이 화면은 전송하지 않습니다. 만료까지 이 지갑으로 새 주문은 만들 수 없습니다.") : "취소 실패: " + r.error);
  ui = S.on(ui, { type: "rejected" }); render();
};
$("resumeBtn").onclick = async () => { const d = await client.resume(); refreshRecoveryButtons(); loadHistory(); if (d && d.done === false && client.loadInflight()) pollStatus(client.loadInflight().payment_id, 24); };
$("newSend").onclick = () => { if (!S.canStartNew(ui, client.loadInflight())) { toast("아직 종결되지 않은 거래가 있어 새 송금을 시작할 수 없습니다."); return; }
  ui = S.on(ui, { type: "new" }); $("chat").innerHTML = ""; $("dupBox").innerHTML = ""; render(); $("text").focus(); };   // 호출·주문·서명·방송 없음

function showResult(r) {
  log("result " + r.state + " " + (r.tx_hash || "").slice(0, 12));
  const mine = S.resultBelongs(ui, r);
  if (!mine) { renderForeignResult(r); return; }                       // 다른 주문(복구 중 저장 주문 등)의 결과는 현재 입력을 덮지 않는다
  ui = S.on(ui, { type: "result", result: r }); render();
}
async function renderForeignResult(r) {
  const f = client.loadInflight(); const pid = r.payment_id;
  if (!pid) return;
  if (ui.order && ui.order.payment_id !== pid) { log("result for other order ignored (current " + ui.order.payment_id + "): " + pid); return; }   // 현재 주문이 있으면 다른 주문 결과는 화면을 바꾸지 않음
  if (f && f.payment_id !== pid) { log("result for non-inflight order ignored: " + pid); return; }              // 보관 기록과 다른 주문의 결과도 무시
  if (r.state === "FINAL_CONFIRMED_SOLIDITY" || (f && f.payment_id === pid)) {
    let o = null;
    if (!wallet.address) refreshWallet();
    try { if (wallet.address) { const h = await api("/api/orders?sender=" + encodeURIComponent(wallet.address)); if (h && h.ok !== false && Array.isArray(h.orders)) o = h.orders.find(x => x.payment_id === pid) || null; } } catch (e) { o = null; }
    const order = o ? { payment_id: o.payment_id, tx_id: o.tx_hash, amount_trx: o.amount_trx, receiver_alias: o.receiver_alias, receiver: o.receiver } : { payment_id: pid, tx_id: r.tx_hash };
    ui = S.on(ui, { type: "restored", order, result: r }); render();                 // 확정이면 done(새 송금 가능), 아니면 unknown(기록·잠금 유지)
  }
}

async function pollStatus(pid, n) {
  for (let i = 0; i < n; i++) {
    await new Promise(r => setTimeout(r, 5000));
    const d = await client.checkOrder(pid);
    refreshRecoveryButtons();
    if (!d || d.done) { loadHistory(); return; }
  }
  addMsg("bot", "아직 종결되지 않았습니다. 다시 열어도 같은 주문(" + pid + ")을 계속 조회합니다. 새로 보내지 마세요.");
}

/* ── 새로고침: 조회만(AI 호출·주문·서명·제출·방송 없음) ─── */
let refreshing = false;
async function doRefresh() {
  if (refreshing) return { skipped: true }; refreshing = true; $("refreshBtn").disabled = true; toast("조회 중…");
  const out = { wallet: false, status: null, history: null };                      // 각 조회 결과를 명시(성공/실패/해당 없음)
  try {
    out.wallet = !!refreshWallet().ok;
    const pid = (ui.order && ui.order.payment_id) || (client.loadInflight() || {}).payment_id;
    if (pid) {
      try { const s = await api("/api/order/status?payment_id=" + encodeURIComponent(pid)); out.status = !!(s && s.ok !== false && s._http < 400);
            if (out.status && s.result) showResult({ ...s.result, payment_id: s.result.payment_id || pid }); }
      catch (e) { out.status = false; log("refresh status error " + (e && e.message || e)); }
    }
    out.history = await loadHistory();                                             // true=성공(빈 목록 포함) · false=실패(이전 목록 유지) · null=지갑 없음
    const failed = (out.status === false) || (out.history === false);
    toast(failed ? "조회 실패 — 마지막 확인 상태를 유지합니다(" + (out.status === false ? "주문 상태" : "") + (out.status === false && out.history === false ? "·" : "") + (out.history === false ? "최근 송금" : "") + ")" : "갱신 " + new Date().toLocaleTimeString());
    out.failed = failed; return out;
  } catch (e) { toast("조회 실패 — 마지막 확인 상태를 유지합니다"); out.failed = true; return out; }
  finally { refreshing = false; $("refreshBtn").disabled = false; }
}
$("refreshBtn").onclick = doRefresh;

/* ── 최근 송금(단일 흰 목록, 항목 탭 → 상세) ─────────────── */
let lastHistory = null;
async function loadHistory() {
  if (!wallet.address) return null;
  let h; try { h = await api("/api/orders?sender=" + encodeURIComponent(wallet.address)); } catch (e) { h = null; }
  if (!h || h.ok === false || !Array.isArray(h.orders)) { log("history fetch failed; keeping previous list"); if (lastHistory === null) $("historyList").innerHTML = `<li class="sub">최근 송금을 조회하지 못했습니다(연결 확인 후 새로고침)</li>`; return false; }
  const rows = h.orders.filter(o => o.result_state); lastHistory = rows;
  if (!rows.length) { $("historyList").innerHTML = `<li class="sub">이 지갑의 송금 기록이 없습니다</li>`; return true; }
  $("historyList").innerHTML = rows.map(o => { const v = S.historyRow(o); return `<li data-pid="${esc(o.payment_id)}"><div class="head" role="button" tabindex="0" aria-expanded="false"><span class="badge ${v.cls}">${esc(v.label)}</span><span><span class="amt">${esc(v.amount)}</span> <span class="who">${esc(v.who)}</span></span><span class="when">${esc(v.when)}</span></div>` +
    `<div class="detail kv">${kv([["받는 주소", esc(o.receiver) + copyBtn(o.receiver), "long"], ["txID", esc(o.tx_hash) + copyBtn(o.tx_hash), "long"], ["블록", esc(String(o.block_number || "-"))], ["실제 수수료", esc(S.feeText(o))], ["탐색기", o.explorer ? `<a href="${esc(o.explorer)}" target="_blank" rel="noopener">같은 txID 보기</a>` : "-"], ["주문 ID", esc(o.payment_id), "long"], ["상태", esc(S.resultLabel(o.result_state))]])}</div></li>`; }).join("");
  return true;
}
$("historyList").addEventListener("click", e => { const h = e.target.closest && e.target.closest(".head"); if (!h) return; const li = h.parentElement; li.classList.toggle("open"); h.setAttribute("aria-expanded", li.classList.contains("open") ? "true" : "false"); });
$("historyList").addEventListener("keydown", e => { if ((e.key === "Enter" || e.key === " ") && e.target.classList.contains("head")) { e.preventDefault(); e.target.click(); } });

/* ── 시작 ───────────────────────────────────────────────── */
(async () => {
  try { health = await api("/api/health"); $("aiLine").textContent = "AI 연결: " + S.healthLine(health); } catch (e) { $("aiLine").textContent = "서버 연결 실패"; }
  try { const c = await api("/api/contacts"); $("contacts").textContent = "등록 수취인: " + c.contacts.map(x => x.alias + (x.confirmed ? "(확인됨)" : "(확인 전·불가)")).join(", "); } catch (e) {}
  refreshWallet();                                          // 복구 결과 표시에 지갑 주소가 필요하므로 먼저 읽는다(연결 요청 없음)
  const d = await client.resume();                          // 저장된 주문만 조회(재서명·새 주문 없음)
  refreshRecoveryButtons();
  if (d && d.done === false && client.loadInflight() && !String(d.why || "").startsWith("post-failed")) pollStatus(client.loadInflight().payment_id, 60);
  setTimeout(loadHistory, 1500);
  refreshWallet(); if (window.tron && window.tron.on) { try { window.tron.on("accountsChanged", () => refreshWallet()); window.tron.on("chainChanged", () => refreshWallet()); } catch (e) {} }
  setTimeout(refreshWallet, 1500);
  render();
})();

return { ui: () => ui, setUi: v => { ui = v; }, render, doRefresh, client: () => client, refreshWallet, loadHistory };
};
