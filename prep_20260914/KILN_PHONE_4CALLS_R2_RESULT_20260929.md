# Kiln qwen3-32b 휴대폰 흐름 R2 결과 (전무, 2026-09-29 13:5x KST) — AI 성공 4/4, 원격 HTTPS 는 사장 손 필요

승인: 사장 "응 상한 승인할께"(부사장 전달, orders 7b48f2). 추가 4회·$0.01 상한·재시도 없음·CLI+서버 합산. 요청서 `KILN_REQUEST_PHONE_4CALLS_R2_20260929.md`.

## 1. 호출 결과 (직접 관측: `guide/logs/kiln_calls.jsonl`, `kiln_phone_runs.jsonl`, `phone_ai_decisions.jsonl`, `kiln_raw/<call_id>.json`)
| # | 시각 | 경로 | 문장 | call_id | finish | tool_calls | 판정 | usage.cost($) |
|---|---|---|---|---|---|---|---|---|
| 1 | 13:51:06 | CLI 형식 검증(KilnLive 직접) | 맥북지갑한테 트론 2개 | 8e5a712a | tool_calls | 1 · parse_transfer_request | proposal normal, 수량 2 | 0.00004492 |
| 2 | 13:51:18 | 서버 `/api/chat`(127.0.0.1, 휴대폰과 같은 경로) | …2개 보내줘 | 3a506bac | tool_calls | 1 | proposal normal, fee_cap 2 TRX | 0.00003008 |
| 3 | 13:51:19 | 서버 `/api/chat` | …2개, 예산 3 트론 5분 안에 | e628cf9d | tool_calls | 1 · change{budget 3, deadline 5} | proposal **adapt**: budget 3,000,000·deadline 300s·fee_cap 1,000,000, 수량 2 유지, 지어낸 조건 0 | 0.00003648 |
| 4 | 13:51:21 | 서버 `/api/chat` | …1000개 | e886d607 | tool_calls | 1 · amount "1000" | **decline** OVER_PER_REQUEST_CAP, 주문 0 (거절 판정은 코드) | 0.00003108 |

- **AI 성공 4/4**(모두 `source=tool_calls`, 함수명 일치, 스키마·의미 검증 통과, `raw_saved=true`, `cost_status=server_usage`). 규칙 폴백 0.
- 비용 합계 **$0.00014256**(상한 $0.01 안). 토큰 입력 386+… / reasoning_tokens 1(형식 1회 기준). request_id 는 여전히 서버 헤더 없음(null).
- Kiln 누적(전체 로그) **14회·$0.00060296**(9/28 3 + Mac 3흐름 3 + 11:28 4 + R2 4).
- 5번째 대화(상한 도달 뒤)는 관문에서 **실호출 없이** 거부됨: `ai.calls=0`, `error="AI 미호출(call cap reached (4/4))"`, 규칙 폴백 표시, 예산 파일 used 4 불변(비용 0).
- 1차(11:28) 실패에 대해: 요청 설정을 9/28 형태(tool_choice auto·`/no_think` system 맨 앞)로 바꾼 뒤 4/4 정상화를 **확인**했다. **정확한 실패 원인은 미확정** — 두 설정을 함께 바꿨고 1차 응답 본문이 보존되지 않아 단일 원인을 확정할 근거가 없다(부사장 검수 정정). 이번 원문: content `"\n\n"`, reasoning 없음, tool_calls 에 인자.

## 2. 호출 전 보완(부사장 검수 4항목 — 검사로 입증, 실호출 0)
| 항목 | 구현 | 검사 |
|---|---|---|
| 성공 = tool_calls 1건·함수명 일치·인자 검증 | `phone_ai.extract_tool_call()` 엄격 판정. content/reasoning_content JSON 은 `ai.diagnostic` 에 기록만(성공 아님, 규칙 폴백) | test_reasoning_or_content_json_is_diagnostic_only_not_success · test_strict_tool_call_shape(함수명 불일치·2건·빈 인자 → none) |
| 예산·기한은 문맥으로만 인정 | `_condition_stated()`: `예산 N` / `N분` 문맥의 숫자와 같을 때만. 수량과 같은 숫자는 인정 안 함 | test_condition_must_be_in_budget_or_deadline_context |
| 보존 실패·비용 미확인 숨기지 않음 | `kiln_client.save_raw_message()` → (ok, err) 반환·`raw_saved/raw_error/raw_file/cost_usd/cost_status` 를 응답·JSONL 에 기록. cost 는 서버 usage.cost 숫자일 때만 확인, 아니면 None(0 아님) | test_save_raw_message_reports_failure · test_raw_save_failure_and_unknown_cost_are_not_success · est_cost None 검사 |
| 실패 시 후속 호출 중단·횟수 포함 | `KilnBudget`(logs/kiln_budget_r2_20260929.json, CLI·서버 공유) + `BudgetedKiln`: 실패·타임아웃도 used+1, `halted` 기록 → 이후 관문 거부(calls=0) | test_failure_counts_and_halts_following_calls · test_transport_error_counts_and_halts · test_cap_reached_blocks_without_calling |
러너 `phone_kiln_run.py` 는 `--stage format`(1회) → 통과해야 `--stage server --url`(3회, curl POST /api/chat). 어느 단계든 형식·의미·기록·통신 실패면 중단·기록.
검사: phone_ai 23/23, guide 전체 **215/215**, safebatch 105/105 (`evidence/20260929_phone_prototype/TESTS_*_1355_r2.txt`, `TESTS_safebatch_1350_r2.txt`). jsc 화면 모의는 guide 묶음 안(test_phone_client_logic_jsc_mock) 통과.

## 3. 원격(사설 HTTPS) 상태 — **미검증, 사장 손 1개 필요**
- 서버: PID 14089, **127.0.0.1:8791 바인딩**, 허용 Host = `djl-macbookpro.tail5c054d.ts.net`(+:443)·127.0.0.1·localhost. 옛 LAN 주소(192.168.0.251) 는 허용 목록에서 제거(요청 시 403 확인). 토큰 유지(phone_token.txt 재사용, URL 동일). 세션 파일 url = `https://djl-macbookpro.tail5c054d.ts.net/p/<token>/`.
- 맥북 확인: `/api/health` 200(Host ts.net 도 200, `ai_provider KILN_LIVE`), `/api/orders?sender=<휴대폰 지갑>` 200 → 178b51e6 CONFIRMED·71372152·fee 0 + 취소 3건 조회됨.
- **Tailscale Serve HTTPS 실패:** `tailscale cert` 13:50:28 → `500 … your Tailscale account does not support getting TLS certs`. `tailscale status` CertDomains=None. 즉 테일넷에 **HTTPS 인증서가 꺼져 있음**(admin console DNS 설정). `tailscale serve --bg --https=443 http://127.0.0.1:8791` 는 출력 없이 대기 상태(PID 13101, 13:41~; 종료 권한 거부로 그대로 둠). serve status "No serve config".
- 휴대폰 LTE 조회: 미실시. 서버 로그에 100.79.x 요청 0건. 원격 상태 = **미검증**.
- 예산 소진 뒤 서버 대화는 규칙 파서만 응답(`AI 미호출(call cap reached)` 표시). 휴대폰에서 실제 AI 대화를 보이려면 새 승인 필요.

## 4. 사장 다음 행동
1. Tailscale 관리 콘솔 `https://login.tailscale.com/admin/dns` → **HTTPS Certificates → Enable HTTPS**(무료, 계정 클릭 1회). 완료 알려주면 전무가 `tailscale serve` 재실행·health 확인 뒤 URL 안내.
2. 그 다음 휴대폰: Wi-Fi 끄고 LTE → TronLink Discover 에 `guide/logs/phone_session.json` 의 https URL 입력 → 지갑 연결·주문 조회 화면 + 상태바(LTE) 캡처. 서명 없음.

## 5. 원장
- STATUS `phone_prototype_20260929.owner_approval_1335_kiln_r2`(used 4, $0.00014256), `kiln_r2_result_1351`, `remote_state_1351`. orders 7b48f2 진행중(② Kiln R2 완료 · ③ 원격 HTTPS 사장 손 대기·LTE 미검증 · ⑤ 제출 초안 `SUBMISSION_DRAFT_PHONE_20260929.md`).
- 증거: `evidence/20260929_phone_prototype/kiln_r2_1351/`(예산 파일, 원문 4, runs/calls/decisions 발췌, 서버 로그, 세션 마스킹, cert 오류).

## 6. 부사장 검수 반영 R2b (14:2x, 유료 호출 0)
| 지시 | 구현 | 검사 |
|---|---|---|
| 호출 전 횟수 영구 예약·프로세스 간 잠금 | `KilnBudget.reserve()`(used+1·pending 항목을 파일에 먼저 기록) → 실호출 → `settle()`. 잠금 = `fcntl.flock(<집계>.lock)` + 스레드 Lock. CLI·서버 공통 | test_reservation_persists_before_call_and_exception_consumes_it · test_concurrent_threads_never_exceed_cap(8 스레드→4회) · test_cross_process_lock_never_exceeds_cap(3 프로세스×3 시도→정확히 5회) |
| 예외·중단도 예약 소비 | `BudgetedKiln.structure()` 가 예외를 잡아 실패로 정산(unknown cost·halted) | 위 첫 검사(RuntimeError → used 1·halted) |
| 집계 파일 누락 ≠ 새 승인 | 파일 없으면 `missing` → 호출 거부. 생성은 `phone_kiln_run.py --stage init --approval "<원문>"` 만 | test_missing_budget_file_is_not_a_fresh_approval |
| "{}"·필수 키 누락 → 성공 아님 | `REQUIRED_KEYS=(alias, amount, asset)` 키 없으면 검증 실패("missing required keys") | test_missing_info_is_question_not_fallback_and_not_halt |
| 정상적인 정보 부족 → 질문 | 키는 있고 값이 null → `kind=question, reason=MISSING_INFO, missing=[…]`(AI 성공, 폴백·차단 아님, 주문 0) | 동일 |
| 스키마·의미 실패 시 서버 직접 경로도 후속 차단 | `decide()` → `provider.on_outcome(False, why)` → `BudgetedKiln` 이 `halt()`. 스키마 실패·지어낸 조건·수량 불일치 모두 | test_schema_or_semantic_failure_halts_server_path |
검사: phone_ai 29/29, guide 221/221 (`evidence/20260929_phone_prototype/TESTS_*_1420_r2b.txt`). 기존 집계 파일(used 4) 관문 = "call cap reached (4/4)" 확인.
**주의:** 실행 중 서버(PID 14089)는 R2 코드로 떠 있어 새 예약·잠금·on_outcome 이 적용되려면 재기동이 필요하다(전무는 임의 재시작 안 함). 현재는 상한 도달 상태라 옛 코드로도 유료 호출은 불가(관문 거부 확인).
제출 초안 a안 반영: `README_SUBMISSION.md` §4b + `public_release_20260929/README.md` §4b(휴대폰 흐름·정직 표기). 휴대폰 코드·검사 파일을 공개 사본에 복사(§7 참조). PDF v3·영상 v3 는 그대로(재렌더 별도).

## 7. 공개 사본(public_release_20260929/) 갱신 목록 (14:2x)
guide/: phone_ai.py, phone_chat.py, phone_flow.py, phone_server.py, phone_kiln_run.py, phone.html, phone_client.js, phone_contacts.json(테스트넷 주소만), kiln_client.py, order_store.py · tests/: test_phone_ai.py, test_phone_flow.py, phone_client_mock.js · safebatch/: trx_tx.py, intent_log.py, nile_tx.py · logs/: kiln_calls.jsonl(14행), kiln_raw/ R2 원문 4건(choices/usage 만, 키 없음). 비밀값 스캔 0건(.env·pending·토큰 파일 미포함). 사본 내 검사 결과는 아래 보고 참조.
