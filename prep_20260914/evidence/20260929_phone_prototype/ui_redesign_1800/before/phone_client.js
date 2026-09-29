/* SafeBatch 휴대폰 화면 — 서명·복구 핵심 로직(DOM 없이 검사 가능). phone.html 이 deps(api/storage/ui/now)를 주입해 사용한다.
   단계: PREPARED(복구 정보 저장) → SIGNING(지갑 호출 직전 저장) → SIGNED(서명본 보관) → SUBMITTING → SUBMITTED → 종결(체인 종결 시 삭제).
   VP 9/29 보완 3건:
   ① 재귀 재제출 제거: 복구 동작(recover) 1회당 재제출 최대 1회. 제출 실패 시 서명본·주문 기록을 보존하고 멈춘다(사용자가 다시 '이어서 확인' 을 눌러야 다음 복구).
   ② SIGNING 을 지갑 호출 전에 저장. 결과 불명(페이지 종료·저장 실패·일반 지갑 오류)은 미서명으로 종결하지 않는다. 사용자 거절(4001/reject/denied/cancel)만 미서명.
      SIGNING/SIGNED 기록은 사용자 확인 버튼으로 지우지 않는다(지갑 내역 부재는 미서명 증거가 아님). 체인 종결(status.result)로만 종결. 서버에도 보유 사실(hold)을 등록해 새 주문을 양쪽에서 막는다.
   ③ 만료된 보관 서명본 삭제 뒤 그 경로를 종료(재제출 없음). 만료는 체인 미송금 증거가 아니므로 주문 조회·잠금 원칙은 유지하고 '새 주문을 만드세요' 안내를 하지 않는다.
      만료 판단은 휴대폰 시계가 아니라 서버 status 의 expired 로만 한다.
   보관 위치: localStorage(sb_inflight_<토큰>). 서명본은 화면·로그·자문에 넣지 않는다. 삭제 조건: 체인 종결 / (PREPARED 이고 서버 서명 없음+PENDING·CANCELLED+결과 없음) / 사용자 미서명 확인. */
(function (root) {
  const CHAIN_TERMINAL = ["FINAL_CONFIRMED_SOLIDITY", "FAILED_ONCHAIN", "EXPIRED_NOT_ON_CHAIN"];
  const STAGES = ["PREPARED", "SIGNING", "SIGNED", "SUBMITTING", "SUBMITTED"];
  const USER_REJECT_MSG = /^(user\s+(rejected|denied|declined|cancell?ed)|사용자가?\s*(거절|취소))/i;   // Grok-05 #6: 부분일치 금지, 4001 또는 명시 문구만
  function isUserReject(e) { const code = e && (e.code || (e.error && e.error.code)); const m = String(e && (e.message || e) || ""); return code === 4001 || String(code) === "4001" || USER_REJECT_MSG.test(m.trim()); }

  function create(deps) {
    const { api, storage, ui, now, key } = deps;
    const KEY = key || "sb_inflight";
    let busy = false;

    /* Grok-05 #1: 세대(epoch) CAS. 쓰기는 저장소의 현재 기록이 우리가 읽은 기록(payment_id·epoch)과 같을 때만. 삭제도 같은 조건(스테일 콜백이 지우지 못함). */
    function current() { try { return JSON.parse(storage.getItem(KEY) || "null"); } catch (e) { return null; } }
    function sameGen(rec) { const c = current(); if (!rec) return c === null; return !!c && c.payment_id === rec.payment_id && (c.epoch || 0) === (rec.epoch || 0); }
    function write(rec, expect) {
      try {
        if (expect !== undefined && !sameGen(expect)) { ui.log("stale write refused for " + rec.payment_id); return false; }
        const r = { ...rec, epoch: (rec.epoch || 0) + 1 };
        storage.setItem(KEY, JSON.stringify(r));
        const back = JSON.parse(storage.getItem(KEY) || "null");
        if (!back || back.payment_id !== r.payment_id || back.stage !== r.stage || back.epoch !== r.epoch) throw new Error("readback mismatch");
        Object.assign(rec, r);
        return true;
      } catch (e) { ui.msg("복구 정보를 이 휴대폰에 저장하지 못했습니다(" + (e && e.message || e) + "). 브라우저 저장소를 허용한 뒤 다시 시도하세요."); return false; }
    }
    function loadInflight() { return current(); }
    function clearInflight(expect) { try { if (expect !== undefined && !sameGen(expect)) { ui.log("stale clear refused"); return false; } storage.removeItem(KEY); return true; } catch (e) { ui.log("clearInflight failed: " + (e && e.message || e)); return false; } }
    function setStage(rec, stage, extra) { const r = { ...rec, stage, ...(extra || {}), updated_at: now() }; return write(r, rec) ? r : null; }

    function decide(s, rec) {
      const res = s && s.result;
      if (res && CHAIN_TERMINAL.includes(res.state)) return { done: true, why: "chain-terminal:" + res.state };
      const stage = (rec && rec.stage) || "PREPARED";
      if (stage === "PREPARED" && s && s.signature_stored === false && ["PENDING", "CANCELLED"].includes(s.order_state) && !res) return { done: true, why: "never-signed:" + s.order_state };
      return { done: false, why: "keep:" + stage + ":" + (res ? res.state : s && s.order_state) };
    }

    async function fetchStatus(pid) { try { return await api("/api/order/status?payment_id=" + encodeURIComponent(pid)); } catch (e) { ui.log("status error " + (e && e.message || e)); return null; } }

    function finish(d, rec) { if (d.done) { if (clearInflight(rec)) ui.lock(false); else { d.done = false; d.why = "stale:" + d.why; ui.lock(true); } } else { ui.lock(true); } return d; }

    async function checkOrder(pid, opts) {
      if (busy && !(opts && opts.internal)) { ui.lock(true); return { done: false, why: "busy" }; }   // 제출·서명 진행 중에는 외부 폴링이 판단·삭제하지 않음
      const rec = loadInflight();
      const s = await fetchStatus(pid);
      if (!s) { ui.lock(true); return { done: false, why: "status-error", s: null }; }
      if (s.result) ui.result(s.result);
      return settle(s, rec);
    }

    /* 공통 판단: decide 만. (VP 9/29: 'CANCELLED·미보관 2회 → 잠금 해제' 철회 — 서명된 거래의 미송금 근거가 아니다. 서명 보유 기록은 체인 종결 전까지 유지·잠금.) */
    function settle(s, rec) {
      const d = decide(s, rec); d.s = s;
      if (!d.done && rec && rec.signed_tx && !s.signature_stored && !s.client_hold) registerHold(rec);   // 서버에도 보유 사실 등록(서명 바이트 없음)
      return finish(d, rec);
    }
    async function registerHold(rec) { try { await api("/api/order/hold", { payment_id: rec.payment_id, snapshot_sha256: rec.snapshot_sha256, tx_hash: rec.tx_hash }); } catch (e) { ui.log("hold register failed: " + (e && e.message || e)); } }

    /* 재제출 가능: 서명본 존재·SIGNED/SUBMITTING·서버 주문 PENDING(서명 미저장)·서버 기준 만료 아님·txID 동일 */
    function canResubmit(rec, s) {
      if (!rec || !rec.signed_tx || !["SIGNED", "SUBMITTING"].includes(rec.stage)) return false;
      if (!s || s.order_state !== "PENDING" || s.signature_stored) return false;
      if (s.expired) return false;
      if (rec.signed_tx.txID !== rec.tx_hash || (s.tx_hash && s.tx_hash !== rec.tx_hash)) return false;
      return true;
    }

    /* 제출 1회. 실패하면 기록 보존·정지(재귀 없음). 성공하면 원래 주문 조회로 판단. */
    async function submitOnce(rec) {
      const r0 = setStage(rec, "SUBMITTING", { submit_count: (rec.submit_count || 0) + 1 }); if (!r0) return { done: false, why: "storage" };
      let r;
      try { r = await api("/api/order/signed", { payment_id: rec.payment_id, snapshot_sha256: rec.snapshot_sha256, signed_tx: rec.signed_tx }); }
      catch (e) {
        ui.log("signed POST error " + (e && e.message || e));
        ui.msg("서명본을 보냈지만 응답을 받지 못했습니다. 보내졌을 수 있으므로 다시 서명하지 않습니다. 서명본과 주문(" + rec.payment_id + ")은 보관되어 있으며, [이어서 확인] 을 누르면 같은 주문을 조회하고 필요할 때 같은 서명본을 1회 다시 제출합니다.");
        ui.lock(true); return { done: false, why: "post-failed:" + r0.submit_count, s: null };
      }
      const refused = r.state === "NOT_SUBMITTED" || !(r.state);
      setStage(r0, refused ? "SUBMIT_REFUSED" : "SUBMITTED", { server_state: r.state || null });   // Grok-05 #2/#7: 거부·빈 응답은 SUBMITTED 로 두지 않고 재제출 불가 단계로 고정
      ui.result(r);
      if (r.state === "NOT_SUBMITTED") ui.msg("이번 제출은 전송되지 않았습니다(" + (r.reason || "") + "). 원래 주문 상태로 판단합니다.");
      if (r.idempotent) ui.msg("같은 서명본을 이미 받았습니다. 기존 결과를 표시합니다(중복 전송 없음).");
      return await checkOrder(rec.payment_id, { internal: true });
    }

    /* 서명 버튼: PREPARED 저장 → SIGNING 저장 → 지갑 서명 → SIGNED(서명본 보관) → 제출 1회 */
    async function sign(o, walletSign) {
      if (busy) { ui.msg("처리 중입니다. 잠시 기다리세요."); return { aborted: "busy" }; }
      busy = true;
      try {
        const rec0 = { payment_id: o.payment_id, snapshot_sha256: o.snapshot_sha256, tx_hash: o.tx_id, expire_at_ms: o.expire_at_ms, stage: "PREPARED", signed_tx: null, submit_count: 0, started_at: now() };
        if (!write(rec0)) return { aborted: "storage" };
        ui.lock(true);
        const rec1 = setStage(rec0, "SIGNING", { signing_at: now() });
        if (!rec1) { clearInflight(rec0); ui.lock(false); return { aborted: "storage" }; }
        let signed;
        try { signed = await walletSign(o.unsigned_tx); }
        catch (e) {
          const m = String(e && (e.message || e.code) || e);
          if (isUserReject(e)) { clearInflight(rec1); ui.lock(false); ui.msg("지갑에서 서명을 거절했습니다(전송 없음): " + m); return { aborted: "wallet-rejected" }; }
          ui.msg("지갑 응답을 확인할 수 없습니다(" + m + "). 서명이 됐을 수도 있어 이 주문을 미서명으로 처리하지 않습니다. 이 주문이 종결될 때까지 새 주문을 만들 수 없으며, [이어서 확인] 으로 상태만 조회합니다.");
          ui.lock(true); await registerHold(rec1); return { aborted: "wallet-unknown", stage: "SIGNING" };
        }
        if (!signed || signed.txID !== o.tx_id) { ui.msg("지갑이 돌려준 거래 ID가 주문과 다릅니다. 제출하지 않고 이 주문을 조회합니다."); return await checkOrder(o.payment_id, { internal: true }); }
        const rec2 = setStage(rec1, "SIGNED", { signed_tx: { txID: signed.txID, raw_data: signed.raw_data, raw_data_hex: signed.raw_data_hex, signature: signed.signature, visible: signed.visible }, signed_at: now() });
        if (!rec2) { ui.msg("서명본을 보관하지 못해 제출하지 않습니다. 이 주문은 서명되었을 수 있으니 새로 만들지 말고 [이어서 확인] 을 누르세요."); ui.lock(true); await registerHold(rec1); return { aborted: "storage-signed", stage: "SIGNING" }; }
        await registerHold(rec2);
        return await submitOnce(rec2);
      } finally { busy = false; }
    }

    /* 복구 1회: 저장된 주문만 조회. 서명본이 있고 서버 주문이 PENDING·서버 기준 만료 전이면 같은 서명본 재제출 **최대 1회**. 실패해도 재귀하지 않는다. */
    async function recover() {
      const rec = loadInflight();
      if (!rec) return null;
      ui.lock(true);
      const s = await fetchStatus(rec.payment_id);
      if (!s) { ui.msg("서버에 연결되지 않습니다. 이 주문(" + rec.payment_id + ")은 보관 중이며 새로 만들지 않습니다."); return { done: false, why: "status-error" }; }
      if (s.result) ui.result(s.result);
      if (rec.signed_tx && s.expired && !s.signature_stored) {
        const n = (rec.expired_seen || 0) + 1;                       // Grok-05 #4/#5: 만료 1회 관측으로 폐기하지 않음. 서버가 근거(expiry_reason)와 함께 2회 연속 만료라 할 때만
        if (n < 2 || !s.expiry_reason) { write({ ...rec, expired_seen: n }, rec); ui.msg("서버가 만료라고 답했습니다(" + (s.expiry_reason || "근거 없음") + "). 한 번 더 확인한 뒤 서명본을 폐기합니다. 재제출은 하지 않습니다."); const d0 = decide(s, rec); d0.s = s; return finish(d0, rec); }
        const r = { ...rec, signed_tx: null, signed_dropped_at: now(), stage: "SIGNED_EXPIRED", expired_seen: n };
        if (!write(r, rec)) return { done: false, why: "stale" };
        ui.msg("보관한 서명본은 서버·노드 기준 만료가 2회 확인되어 폐기했습니다. 이 주문의 최종 결과는 계속 같은 txID 로 조회합니다.");
        const d = decide(s, r); d.s = s; return finish(d, r);        // ③ 삭제 뒤 경로 종료(재제출 없음), 잠금·조회 원칙 유지
      }
      if (rec.expired_seen && !s.expired) write({ ...rec, expired_seen: 0 }, rec);
      if (canResubmit(rec, s)) { ui.msg("서버에 서명이 없어 보관한 같은 서명본(같은 txID)을 1회 다시 제출합니다(재서명 없음)."); return await submitOnce(rec); }
      return settle(s, rec);
    }

    async function resume() {
      const f = loadInflight();
      if (!f) return null;
      if (busy) return { done: false, why: "busy" };
      busy = true;
      try { ui.msg("이전 주문 " + f.payment_id + "(단계 " + f.stage + ")을 이어서 처리합니다(새로 보내지 마세요)."); return await recover(); }
      finally { busy = false; }
    }

    return { loadInflight, clearInflight, decide, canResubmit, checkOrder, sign, resume, recover, isUserReject, CHAIN_TERMINAL, STAGES };
  }
  /* 제안 화면 안내(VP 9/29 C): 관용 매칭 사실·입력 문장·이해한 수취인을 정상/폴백/적응 모두에서 표시. HTML 로 해석하지 않도록 이스케이프한 행과, textContent 용 메시지를 돌려준다. */
  function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])); }
  function proposalNotice(r) {
    const u = (r && r.understood) || {}; const p = (r && r.proposal) || {};
    const rows = [];
    if (u.alias_match) rows.push(["입력 문장 → 이해한 수취인", escapeHtml(r.text) + " → " + escapeHtml(p.alias) + " (띄어쓰기/조사 차이를 무시해 등록된 이름과 맞춤 — 아니면 진행하지 마세요)"]);
    const messages = [];
    if (r && r.confirm_note) messages.push(String(r.confirm_note));
    if (r && r.kind_detail === "adapt" && r.next) messages.push(String(r.next));
    return { rows, messages };
  }
  root.SBClient = { create, CHAIN_TERMINAL, STAGES, proposalNotice, escapeHtml };
})(typeof window !== "undefined" ? window : globalThis);
