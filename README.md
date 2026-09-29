# SafeBatch AI — Furiosa A (Agentic Payment) · GWDC 2026 Korea Hackathon · Solo Builder

**One-line declaration (Furiosa A):** A Korean-language assistant that turns a user's stated transfer goal into checked conditions and a *bounded* TRON Nile testnet USDT payment — the model (Kiln `qwen3-32b`) explains the candidate computed by code (it does not create or adjust amounts), a human approves a fingerprint of the exact conditions, and code executes only inside the user's budget, allow-list, deadline and TRX cap, **declining otherwise**.

Track choice: **Furiosa A** (execute-to-completion within rules; adapt on changed rules; decline when rules cannot be met). Model: Kiln API `qwen3-32b` only. Chain: TRON **Nile testnet** (test funds; not a real exchange deposit). Participant: Solo Builder.

> Honesty notes. (1) The exchange-guidance screen (Upbit → Binance own-account USDT conditions) and the Nile test payment are **separate**; we do not claim the test payment completes the exchange goal. (2) "AI decided the payment" is **not** claimed: candidates, amount adjustment, blocking, verification, signing and broadcast are code + human. (3) No usability-improvement or competitor-superiority claim is made (see §7).

## 1. What AI does vs what code does vs what the human does

| Actor | Does | Does NOT |
|---|---|---|
| **Kiln qwen3-32b (AI)** | Guide screen: structures the user's free-text goal (from/to/asset/network/amount/own-account), asks for the one missing input, proposes next action. Payment flows: reads user rules + the **code-computed** candidate list and returns `{"choice_index", "reason"}` (one Korean sentence). Decline flow: returns `{"decision":"decline","reason"}` **after** code has already blocked. | Create candidates, change amount/address/fee/deadline, approve, sign, broadcast, override a code block. Any output outside the allowed JSON keys is rejected and never used for execution. |
| **Code (deterministic)** | Rule cards with sources/dates; candidate plans from rules (`demo_flows.candidate_plans`); amount adjustment only in `max_within_budget`; TRX cap split (Energy fee_limit + charged bandwidth = 281 B + 64 B); unsigned tx build + independent protobuf decode (20 checks); confirm fingerprint (all conditions + raw bytes + txID); signer EOA recovery; pre-broadcast re-quote; single broadcast with intent ledger reservation; receipt reconciliation (4 cost bounds + Transfer log + solidity). | Sign (wallet only), talk to real exchanges. |
| **Human** | Reads the confirm fingerprint and approves only if identical; signs once in TronLink on `/sign`; can cancel. | — |

## 2. How to run (verified 2026-09-29 on macOS 15, Python 3.9.6, in a clean virtualenv using only this folder)

Dependencies: Python 3.9+, `curl` (node/Kiln HTTP), and one third-party package — `tronpy==0.6.2` (protobuf encoder for the unsigned Transaction; decoding/verification is pure Python). Video generation is optional and not needed to run the product.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # tronpy==0.6.2
cp .env.example .env && chmod 600 .env   # fill KILN_API_KEY only if you will call Kiln (--kiln); tests and the guide's rules-only mode need no key
```
Reproduction log (clean copy of this folder + fresh venv): see `prep_20260914/SUBMISSION_CHECK_20260929.md` §1.

```bash
cd gwdc_2026
# 1) tests (synthetic fake node; no network, no wallet)
(cd guide && python3 -m unittest discover -s tests)        # 156 tests (synthetic fake node)
(cd safebatch && python3 -m unittest discover -s tests)    # 105 tests
# 2) guide screen (Korean UI): http://127.0.0.1:8765/  and signing screen /sign
python3 guide/app.py --port 8765
# 3) Nile flows (read-only prepare → human-confirmed execute → resolve by txID)
python3 guide/run_nile_live.py prepare --receiver <T-addr> --amount-units 1000000 --budget-units 1000000 \
  --mode exact_amount --trx-cap-sun 5000000 --deadline-s 21600 --goal "<goal text>" --out PREPARE.json
python3 guide/run_nile_live.py execute --artifact PREPARE.json --confirm-digest <sha256 from PREPARE> \
  --confirmed-by <name> --batch-id <id> --flow-id <id> --kiln --out FLOW.json      # waits for TronLink signature on /sign
python3 guide/run_nile_live.py resolve --flow-json FLOW.json                      # same-txID lookup only; never rebroadcasts
python3 guide/run_nile_live.py decline --receiver <T-addr> --amount-units 1000000 --budget-units 600000 \
  --mode exact_amount --trx-cap-sun 5000000 --flow-id <id> --kiln --out DECLINE.json   # code blocks first; AI explains; 0 orders
```
Secrets: `KILN_API_KEY` in `gwdc_2026/.env` (not included). Wallet private keys are never read; signing is done by the TronLink extension.

## 3. Pre-event vs during-event work (disclosure)

| Period | Work | Evidence |
|---|---|---|
| **Pre-event (2026-09-15)** | `safebatch/csvcheck.py`, first versions of `policy.py`, `flow.py`, `tests/test_local.py`, `tests/test_flow.py` (CSV validation, budget/allow-list policy, approval-wait flow; 39 tests). No AI, no chain, no wallet. | file mtimes 2026-09-15 13:32/16:23; `prep_20260914/SESSION.md` |
| **During event (2026-09-28 16:30 → 2026-09-29)** | Everything else: `guide/` (Korean guide UI, Kiln client, plan/AI-work card, `/sign`, Nile executor, CLI), `safebatch/nile_tx.py`, `intent_log.py`, `tip712.py`, `export.py`, `gasfree_*` (deferred path), all Nile flows and the three A demos. `policy.py`/`flow.py` were substantially modified during the event (mtime 2026-09-28 21:56). | file mtimes; `START_HERE.md` timeline; test baselines `prep_20260914/evidence/20260928_restart_audit/BASELINE_*.txt` |

## 4. Furiosa A evidence — three flows (2026-09-29 KST)

All on TRON Nile (chainId 3448148188), token TetherToken `TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf`, wallet `TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz` (self-transfer test wallet). Kiln call log: `guide/logs/kiln_calls.jsonl` (no credentials; 6 lines total).

| Flow | User rules | AI (Kiln qwen3-32b) | Code | Human | On-chain result |
|---|---|---|---|---|---|
| **① Normal** | exactly 1 USDT, budget 1.0, TRX cap 5, deadline 06:55 | 00:56:00 call `e851f510`, in 444/out 67 tok — "…TRX 수수료 한도 내에서 네트워크가 Nile 테스트넷인 유일한 옵션입니다." (explains the single candidate) | candidate 1 USDT; Energy fee_limit 4,655,000 + bandwidth 345,000 = 5,000,000 sun cap | fingerprint `ce741dfb…3cd3` approved; TronLink signature 01:01:53 | tx **`a7c6b7340fde03af6b9ac00158d916b1ec90595ed00c0e0ac548eea7e770ca24`**, block 71361508, SUCCESS, solidity-confirmed; Transfer 1 USDT; fee 345,000 sun (= charged bandwidth 345 B) |
| **② Adapt** | budget 1.0 → **0.6**, min 0.5, `max_within_budget` | 01:18 call `a5f4bba8`, in 813/out 61 — "예산이 1.0 USDT에서 0.6 USDT로 줄어들었기 때문에, 후보는 최대 0.6 USDT를 보내는 것을 반영합니다." That run ended **before signing** (executor revision-digest bug, fixed + regression test); the identical, validated judgment was **carried** into the re-run (`AI_CARRIED`, 0 extra calls; reuse allowed only if prior flow ended pre-signature, the call exists in the log with ok/200/qwen3-32b, the preserved raw response re-parses to the same choice, and rules/plan/fingerprint are identical) | candidate **0.6 USDT** computed by code from the reduced budget; before/after diff shown on `/sign` | fingerprint `dce84413…14da` approved; signature 01:3x | tx **`71a951bc63b73b3146f74d6b66b9b46b994be2f20a715ae57104acc0c83b4f88`**, block 71362119, SUCCESS, solidity-confirmed; Transfer 0.6 USDT; fee 345,000 sun |
| **③ Decline** | exactly 1 USDT, budget **0.6**, `exact_amount` (no reduction allowed) | 01:36 call `df785a78`, in 319/out 53 — "예산 0.6 USDT는 정확한 지급 금액 1 USDT보다 적어 결제가 불가능합니다." (explanation only; a pre-check run with **no** AI call already blocked) | `candidate_plans` → **0 candidates** → blocked: "exact amount 1000000 + fee 0 exceeds budget 600000"; no unsigned tx, no order, no intent, no approval | none (nothing to sign) | **0 orders · 0 signatures · 0 broadcasts · 0 tx**; ledgers unchanged before/after (`DECLINE_ai_v7.json`) |

Excluded from the A count: `nile_selftest_v4` (2026-09-28 23:28, 1 USDT, `MANUAL_TECH_CHECK`, no AI) — a manual technical check.

Per-flow token usage (Kiln `usage`): ① 444/67, ② 813/61, ③ 319/53 (prompt/completion). Guide-screen calls on 09-28: 186/300 (length-truncated → rules fallback), 278/26 (tool call), 233/64 (VALIDATED). Energy: not measured; no energy claim is made.

Evidence files: `prep_20260914/evidence/20260928_nile_live/` — `PREPARE_ai_normal_v5.json`, `FLOW_ai_normal_v5.json`, `RESOLVE_ai_normal_v5.txt`, `PREPARE_ai_adapt_v6.json`, `FLOW_ai_adapt_v6.json` (pre-signature failure), `FLOW_ai_adapt_v6b_reuse.json`, `RESOLVE_ai_adapt_v6b_reuse.txt`, `DECLINE_v7_precheck_no_call.json`, `DECLINE_ai_v7.json`; ledgers `guide/logs/nile_intents.jsonl`, `guide/logs/nile_approvals.jsonl`, `guide/logs/kiln_calls.jsonl`. Summary table: `prep_20260914/A_FLOWS_SUMMARY_20260929.md`.

## 4b. Phone flow (separate prototype, 2026-09-29 KST) — stated as-is

One Nile TRX transfer (**2 TRX**, tx `178b51e69af30135c98e46ddff2ba5955f51c79b1695a140703d3961d27101c1`, block 71372152, fee 0 sun, FINAL_CONFIRMED_SOLIDITY) was signed on an Android TronLink wallet and verified/broadcast **once** by the MacBook server (`guide/phone_server.py`, `guide/phone_flow.py`). The conversational parsing at that moment was **rule-based (no model call)**. During that trial the owner signed 4 times across 3 distinct orders; only the last was broadcast, the earlier two were refused by server verification or never reached the server (details: `prep_20260914/PHONE_TRIAL_RESULT_20260929.md`). It ran on the same Wi-Fi, not remotely.

Kiln `qwen3-32b` tool-call parsing was verified afterwards with 4/4 calls: **1 CLI format check** (`guide/phone_kiln_run.py --stage format`, direct client call) **plus 3 requests sent from the MacBook to the phone server's `/api/chat` route** (normal, adapt, decline; call ids `8e5a712a`, `3a506bac`, `e628cf9d`, `e886d607` in `guide/logs/kiln_calls.jsonl`, raw responses in `guide/logs/kiln_raw/`). The model only extracts alias / amount / asset / user-stated budget or deadline; code decides addresses, caps, expiry and declines, and discards any condition the user did not state. Those four calls **did not create orders or signatures**, and the inputs were sent from the MacBook, **not typed on the phone**. An earlier batch of 4 calls the same morning failed (all fell back to rules) and is reported in `prep_20260914/KILN_PHONE_4CALLS_RESULT_20260929.md`. Remote (off-LAN) access: the phone server binds to `127.0.0.1` and is reachable only inside the owner's private Tailscale network through **Tailscale Serve** with a Let's Encrypt certificate (tailnet-only; no public Funnel). On 2026-09-29 15:13 KST the MacBook itself fetched `/api/health` and the confirmed order `178b51e6…` over that HTTPS URL (HTTP 200, certificate verified; evidence `prep_20260914/evidence/20260929_phone_prototype/remote_https_1513/`). On 2026-09-29 16:29 KST the owner's Android phone on **LTE (Wi-Fi off)**, inside the TronLink DApp browser, fetched the same confirmed order over that HTTPS URL (owner screenshot with LTE status bar + matching server log line `16:29:47 GET /api/orders … 200`; evidence `prep_20260914/evidence/20260929_phone_prototype/remote_lte_1629/`). Verified scope: **existing-order lookup from the phone on LTE**. Remote chat parsing, signing and broadcasting from the phone remain **unverified**.

## 5. Safety properties (tested, synthetic)
- Approval is bound to a fingerprint of *everything the human saw* (network, parties, token, exact amount, budget/min/mode, TRX cap, Energy limit, bandwidth max + rule, absolute deadline, revision, raw bytes, txID). Any change → no execution.
- Signature: wallet-returned raw bytes must equal the unsigned original; txID recomputed; signer EOA recovered and compared; exactly one signature.
- Broadcast once, after an account-level ledger reservation; timeouts/unknown responses are resolved by the **same txID** only (never rebuilt/rebroadcast).
- Bandwidth charging follows java-tron `BandwidthProcessor`: serialized tx + `MAX_RESULT_SIZE_IN_TX` (64 B) per contract — verified against 3 real Nile receipts; both confirmed flows charged exactly 345,000 sun.
- Model output is validated for shape only (`choice_index` in range, allowed keys); the model cannot alter amounts or unblock a decline.

## 6. User research done (Korean first user) — and what it did *not* show
One user (the builder) compared a literal-translation screen vs the guide screen on a synthetic deposit screen (2026-09-28). The **original** guide screen failed to convey purpose/next action; the screen was then restructured (purpose → whose address → one next action → must-check conditions; details folded). **No claim** that the revised screen is better than translation or general-purpose AI: not re-tested, single user, order effects. Record: `prep_20260914/evidence/20260928_compare/COMPARE_RESULT_20260928.md`.

## 7. Limitations
- Self-transfer test wallet; exchange APIs are not touched. The exchange guide is informational and its product link to the payment flows is a separate open question.
- Adapt flow's AI judgment was carried from a pre-signature failed run (documented). First-time wallet-connect UX on `/sign` verified only with an already-connected wallet.
- Energy usage 14,650 per transfer was not burned from the wallet (cause not verified). No energy/latency measurement claims.
- GasFree / TRON C path exists in code but was deferred (Nile API key not issued); not part of this submission's claims.

## 8. Repository layout and file links
- `guide/` — [`app.py`](guide/app.py) server · [`index.html`](guide/index.html) Korean guide · [`plan.py`](guide/plan.py) intent/rules · [`rules_upbit_binance_usdt.json`](guide/rules_upbit_binance_usdt.json) sourced conditions · [`kiln_client.py`](guide/kiln_client.py) / [`kiln_choose.py`](guide/kiln_choose.py) model calls · [`demo_flows.py`](guide/demo_flows.py) candidates/re-check · [`nile_executor.py`](guide/nile_executor.py) approve/execute/receipt · [`run_nile_live.py`](guide/run_nile_live.py) CLI (prepare/execute/resolve/decline) · [`sign.html`](guide/sign.html) signing screen · [`tests/`](guide/tests) 156 tests
- `safebatch/` — [`nile_tx.py`](safebatch/nile_tx.py) unsigned tx, decoder, signature/receipt checks · [`intent_log.py`](safebatch/intent_log.py) ledger · [`policy.py`](safebatch/policy.py) / [`flow.py`](safebatch/flow.py) / [`csvcheck.py`](safebatch/csvcheck.py) (pre-event core) · [`tests/`](safebatch/tests) 105 tests
- Evidence — [`prep_20260914/evidence/20260928_nile_live/`](prep_20260914/evidence/20260928_nile_live) flows, receipts, resolve logs · [`guide/logs/kiln_calls.jsonl`](guide/logs/kiln_calls.jsonl) · [`guide/logs/nile_intents.jsonl`](guide/logs/nile_intents.jsonl) · [`guide/logs/nile_approvals.jsonl`](guide/logs/nile_approvals.jsonl) · [`prep_20260914/A_FLOWS_SUMMARY_20260929.md`](prep_20260914/A_FLOWS_SUMMARY_20260929.md) · user test [`prep_20260914/evidence/20260928_compare/COMPARE_RESULT_20260928.md`](prep_20260914/evidence/20260928_compare/COMPARE_RESULT_20260928.md) · test baselines [`prep_20260914/evidence/20260928_restart_audit/`](prep_20260914/evidence/20260928_restart_audit)
- Submission — [`submission/DECK_SafeBatchAI_FuriosaA_20260929.pdf`](submission/DECK_SafeBatchAI_FuriosaA_20260929.pdf) · [`submission/DEMO_VIDEO_SafeBatchAI_FuriosaA_20260929.mp4`](submission/DEMO_VIDEO_SafeBatchAI_FuriosaA_20260929.mp4) · [`submission/VIDEO_SCRIPT_20260929.md`](submission/VIDEO_SCRIPT_20260929.md)

### Pre-event vs during-event — basis
Classification uses file modification times and the dated session log (`prep_20260914/SESSION.md`, `START_HERE.md`); original timestamps were not altered. Pre-event files: `safebatch/csvcheck.py` (2026-09-15 13:32), `safebatch/tests/test_local.py` (09-15 13:32), `safebatch/tests/test_flow.py` (09-15 16:23); `policy.py`/`flow.py` began 09-15 and were rewritten during the event (mtime 09-28 21:56). Every other source file has mtime 2026-09-28 17:xx or later (event start 09-28 16:30 KST).

### Live vs synthetic — what each number means
- **Live (real Nile testnet)**: the two confirmed transactions, their receipts/solidity checks, the three Kiln calls of the A flows, and the decline run's ledger snapshot (order/intent/approval counts before and after; no whole-directory hash was taken).
- **Synthetic (fake node, no network)**: the 156 + 105 unit tests, including the decline-path and reuse-path regressions.
