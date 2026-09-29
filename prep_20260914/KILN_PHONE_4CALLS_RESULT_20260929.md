# Kiln qwen3-32b 휴대폰 흐름 4회 결과 · 실패 원인 · 수정 (전무, 2026-09-29 13:0x KST)

승인 범위: 사장 9/29 '승인'(orders 7b48f2, 요청서 `KILN_REQUEST_PHONE_4CALLS_20260929.md`, 최대 4회·$0.01 상한·재시도 없음). **4회 전부 소진, 재호출 없음.** 이 문서는 저장된 로그만으로 분석했고, 이번 세션의 Kiln 호출은 0회다.

## 1. 실제 호출 결과 (직접 관측: `guide/logs/kiln_calls.jsonl`, `guide/logs/kiln_phone_runs.jsonl`)
| # | 시각(로그) | flow_id | call_id | http | finish | prompt/completion | reasoning_tokens | tool_calls | 판정 | usage.cost($) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 11:28:07 | phone_format_check | b31c9e05 | 200 | stop | 380 / 92 | **92** | 없음 | 폴백(not json) | 0.00005584 |
| 2 | 11:28:10 | phone_normal | 7701b5a4 | 200 | stop | 382 / 30 | **30** | 없음 | 폴백(not json) | 0.00002412 |
| 3 | 11:28:12 | phone_adapt | de94a14f | 200 | stop | 392 / 19 | **19** | 없음 | 폴백(not json) | 0.00002184 |
| 4 | 11:28:13 | phone_decline | c76746b9 | 200 | stop | 383 / 29 | **29** | 없음 | 폴백(not json) | 0.00002400 |

- **AI 성공 0/4.** 4건 모두 규칙 파서 폴백(`ai_success=false`, `fallback="모델 출력 검증 실패(not json) → 규칙 파서"`). 화면·주문 결과(정상 2·적응 1·거절 1)는 규칙 파서가 낸 것이며 실제 AI 성공으로 집계하지 않는다.
- 비용: 4회 합계 **$0.0001258**(서버 usage.cost 합, 상한 $0.01 안). 토큰 입력 1,537 / 출력 170. Kiln 누적 **10회·$0.0004604**(9/28 3회 + 9/29 새벽 Mac 3흐름 3회 + 이번 4회).
- request_id 는 서버가 헤더로 주지 않아 4건 모두 null(9/28 부터 동일).
- 시각 주의: 이전 세션 문서의 라벨 "12:3x/12:4x" 는 파일 mtime(11:10) 보다 앞서 있다. 이 문서는 로그의 실제 시각(11:28)을 쓴다.

## 2. 실패 원인 분석 (저장 로그 근거, 재호출 없음)
**증상 공통점:** 4건 모두 `completion_tokens == reasoning_tokens`, `finish_reason=stop`, `tool_calls` 없음. 즉 서버가 낸 출력 토큰 전부가 *reasoning* 으로 분류됐고 도구 인자는 message 에 실리지 않았다. 출력 토큰 수(19~30개)는 `{"alias":"맥북지갑","amount":"2","asset":"TRX",…}` 크기와 맞고, 92개(1번)는 첫 호출의 프롬프트 캐시 미적중과 함께 더 긴 출력이 있었음을 뜻한다.

**9/28 성공 호출과의 차이(`guide/logs/flow_tools_tools-3d9803e9.json`, 19:43, 도구 호출 성공):**
| 항목 | 9/28 성공 | 9/29 11:28 실패 4건 |
|---|---|---|
| tool_choice | `"auto"` | 함수명 강제 `{"type":"function","function":{"name":"parse_transfer_request"}}` |
| `/no_think` 위치 | system 프롬프트 **맨 앞** | user 문장 **끝**에 덧붙임 |
| reasoning_tokens | 1 | 92 / 30 / 19 / 29 (= completion 전부) |
| finish_reason | `tool_calls` | `stop` |
| message.tool_calls | 정상 JSON arguments | 없음 |

**(9/29 14:5x 부사장 검수 뒤 표기: 아래는 추정이며, 설정 변경 후 R2 4/4 정상화는 확인됐으나 단일 원인은 미확정)** **가장 유력한 원인(추정):** tool_choice 를 함수명으로 **강제**하면 서버(vLLM 계열)가 출력 형식을 도구 인자 JSON 으로 제한해 생성한다. 이때 `<think>…</think>` 태그가 나오지 않아 서버의 reasoning 파서가 **출력 전체를 reasoning_content 로 분류**하고, message.content/tool_calls 가 비게 된다. 우리 클라이언트는 `tool_calls[0].function.arguments` 만 읽었으므로 빈 문자열 → `json.loads` 실패 → "not json" 폴백. 9/28 의 `"auto"` 경로는 모델이 `<think>\n\n</think>` 뒤 `<tool_call>` 을 직접 써서 서버 도구 파서가 정상 처리했다(reasoning 1 토큰).
**보조 원인:** `/no_think` 를 user 끝에 붙인 형태는 9/28 실증이 없다(9/28 은 system 맨 앞).
**한계:** 9/29 시점 `kiln_client` 는 usage 만 기록하고 응답 본문(content/reasoning_content)을 남기지 않아 reasoning_content 안에 실제로 JSON 이 있었는지는 **미확인**이다. 위 원인은 토큰 분포·finish·형식 차이로 추정한 것이며, 최종 확정은 실호출 1회가 필요하다(새 승인 필요, 이번 실행 없음).

## 3. 수정 (코드, 호출 0)
| 파일 | 변경 |
|---|---|
| `guide/kiln_client.py` | 응답 message 원문(choices/usage/model/id 만, 키·헤더 제외)을 `guide/logs/kiln_raw/<call_id>.json` 으로 보존(`save_raw_message`). 반환값에 `reasoning_content`, `call_id` 추가. `kiln_calls.jsonl` 레코드에 `raw_file` 경로 추가 |
| `guide/phone_ai.py` | (a) 요청 형태를 9/28 성공 경로로: `/no_think` system 맨 앞 + "도구를 정확히 1회 호출" 지시, `tool_choice="auto"`, user 문장은 그대로. (b) `extract_tool_arguments()`: tool_calls.arguments(문자열/객체) → content 의 `<tool_call>{…}</tool_call>`·JSON(`<think>` 블록 제거) → reasoning_content 의 JSON 순으로 인자 복구. 검증은 기존 `validate_model_output` 그대로(허용 키·주소 금지·양수). (c) 인자가 없으면 오류 문구에 finish/completion/reasoning/raw_file 을 남겨 다음 사후 분석이 가능하게 함. `ai.source`(어느 필드에서 얻었나)·`finish_reason`·`call_id` 기록 |
| `guide/phone_kiln_run.py` | 비용은 서버 `usage.cost` 우선(없으면 단가 추정). 레코드에 source/finish_reason/call_id 추가 |
변경하지 않은 것: 주소·수량·상한·만료를 코드가 결정하는 경계, 폴백=성공 아님 집계, 재시도 없음, MockKiln 기본 경로, 서버/화면 코드, Mac 3흐름·README/영상/PDF·public_release.

## 4. 모의 검사 (실호출 0)
`guide/tests/test_phone_ai.py` 5 → **14건**(신규 9):
- 요청 형태가 9/28 성공 경로와 같음(tool_choice auto, /no_think system 맨 앞, user 에 없음, max_tokens 300)
- **9/29 실패 형태 재현**(tool_calls 없음·본문 없음·reasoning=completion) → 여전히 폴백으로 분류되며 원인 문구(`no tool arguments … reasoning=92`)를 남김(성공으로 둔갑하지 않음)
- reasoning_content 안 JSON → 정상 제안 복구(source=reasoning_content) / content 의 `<think>`+`<tool_call>` → 적응 제안 복구(예산 3·기한 5분 → fee_cap 1 TRX·만료 300 s, 수량 2 유지) / arguments 가 객체 → 거절(OVER_PER_REQUEST_CAP) / arguments "" + reasoning JSON → 복구
- 잡문·HTTP 오류 → 폴백 유지(calls=1), 모델이 1 로 낮추거나 지어낸 예산 → 코드가 2 유지·조건 무시
- kiln_client 원문 보존은 choices 만 저장(인증 필드 미저장), 러너 비용은 usage.cost 우선

| 검사 | 결과 | 증거(`prep_20260914/evidence/20260929_phone_prototype/`) |
|---|---|---|
| guide 전체 (`/usr/bin/python3 -m unittest discover -s tests`) | **206/206 OK** (197+9) | `TESTS_guide_1305_kilnfix.txt` |
| phone_ai | 14/14 OK | `TESTS_phone_ai_1305_kilnfix.txt` |
| safebatch | 105/105 OK | `TESTS_safebatch_1305.txt` |
| 화면 jsc 모의 | 41 ok · 0 FAIL · RESULT PASS | `PHONE_CLIENT_JSC_MOCK_1305.txt` |
주의: 같은 폴더의 `*_1257_*` 파일은 tronpy 없는 Homebrew python3 로 잘못 돌린 결과(9건 import 오류)이며 `TESTS_1257_NOTE.txt` 에 표기했다. 유효 결과는 1305.

## 5. 원격 접속·휴대폰 캡처 상태 (13:0x 직접 관측)
- **Tailscale:** 맥북 설치·로그인 완료(`/usr/local/bin/tailscale`, Tailscale.app). 기기: 맥북 `djl-macbookpro.tail5c054d.ts.net` 100.80.123.111 온라인 · 사장 Android Note20 `younghoonlim-note20…` 100.79.15.20 **온라인** · iPhone 온라인 · Windows 오프라인. **`tailscale serve` 미설정**("No serve config") → 사설 HTTPS 종단 아직 없음.
- **서버:** 11:17 Tailscale IP(100.80.123.111)로 접근 시 403(Host 허용 목록 밖) → 11:26 PID 42701 재기동(advertise 추가, 같은 URL 유지). **현재 8791 리스너 없음**(PID 42701 종료, 이전 세션 종료와 함께 내려간 것으로 보임). 이번 세션에서 재기동하지 않았다(재기동은 부사장 지시 뒤).
- **휴대폰 LTE 조회 검증: 미실시.** 서버 로그에 100.79.x(휴대폰 Tailscale) 요청 0건. 유효한 원격 증거 없음.
- **휴대폰 캡처:** JPG 14장(08:5x 단말 확인 3 · 09:24 제안 · 09:48/09:51 raw 불일치 거절 · 09:45 POST 유실 · 09:56 결과 UNKNOWN · 10:01 홈 화면 TRX 18 등, `trial_178b51e6/` 포함). **10:01 이후 새 캡처 없음, LTE 상태바 캡처 없음.** 확정 1건: txID `178b51e6…` 블록 71372152(변경 없음).
- 원격 재개 시 순서(부사장 지시 뒤, 새 설치·공개 없음): ① `python3 guide/phone_server.py --host 0.0.0.0 --port 8791 --advertise 192.168.0.251,djl-macbookpro.tail5c054d.ts.net` ② `tailscale serve --bg --https=443 http://127.0.0.1:8791` ③ 맥북에서 `https://djl-macbookpro.tail5c054d.ts.net/p/<token>/api/health` 200 확인 ④ 사장 휴대폰 Wi-Fi 끄고 LTE → TronLink Discover 에서 같은 https URL → 지갑 연결·기존 주문 조회 화면 + 상태바 캡처(서명 없음).

## 6. 제출 준비 (독립 가능 항목 점검)
- 산출물 README/영상/PDF/`public_release_20260929/`(02:37)는 **Mac 3흐름 기준**이며 오늘의 휴대폰 흐름·복구 수정·Kiln 파싱 수정을 포함하지 않는다. 휴대폰 흐름을 제출물에 넣으려면 (a) 실제 AI 성공 증거(현재 0) 또는 "규칙 파서·모의 AI" 로 정직 표기, (b) 원격 검증 결과가 먼저 필요하다 → 내용 확정 전 산출물 재작업은 하지 않았다.
- 행정(팀ID·TG 체크인·공개 GitHub·제출폼)은 `SUBMISSION_CHECK_20260929.md` §2·§4 그대로(사장 손·승인 대상). 접수 9/29 21:00~9/30 12:00 KST.

## 7. 부사장 결정 요청
1. **Kiln 재승인 요청 여부:** 4회가 모두 파싱 문제로 소진되어 실제 AI 성공 증거가 없다. 수정판 검증에는 최소 **1회**(형식 확인), 시연 3흐름까지는 **4회**가 필요하다(예상 비용 회당 ≈$0.00003, 4회 < $0.0002). 추천: 사장에게 "Kiln 추가 4회(수정판 검증 1 + 휴대폰 3흐름), $0.01 상한, 재시도 없음" 재승인 요청. 승인 전 호출 0.
2. **원격 검증 착수:** 위 5절 ①~③ 은 승인 범위(Tailscale 사설 HTTPS) 안이며 전무가 바로 실행 가능. ④ 는 사장 휴대폰(LTE) 필요.
3. 실제 AI 성공이 끝내 없으면 제출물에 "휴대폰 대화 파싱은 규칙 기반(모의 AI)" 로 표기할지, 휴대폰 흐름을 제출물에서 빼고 Mac 3흐름만 낼지.

## 8. 원장
- STATUS `phone_prototype_20260929.kiln_phone_calls_1128`(4회·성공 0·$0.0001258), `owner_approval_1230.kiln_used=4`, `kiln_parse_fix_1305`, `remote_state_1305`, `phone_capture_state_1305`.
- orders 7b48f2 진행중(①hold ②MockKiln 완료 · ③원격 미검증 · ④Kiln 4/4 소진·AI 성공 0 · ⑤제출 준비 대기). 자문 누적 9/29 Kimi 2 · Grok 3 · Gemini 2(잔여 3·2·3), 이번 세션 자문 호출 0.
