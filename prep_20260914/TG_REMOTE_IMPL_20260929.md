# 텔레그램 한 문장 → TronLink 승인 · 승인 단계 축소 구현·모의 검증 보고 (전무 · 2026-09-29 21:1x KST · orders 7b48f2 후속)

요청서: `VP_REMOTE_MINIMAL_APPROVAL_20260929.md`(사장 선택 확정 "텔레그램 개인 채팅에서 지시 → TronLink 연결·승인"). 범위 = 구현 + 모의 검증. **추가 유료 호출 0 · 실송금 0 · Telegram 실발송 0 · 하니봇 재시작 0.** 이 기능은 실증 전 "구현 중".

## 1. 결론
- 정상 경로를 **명령 제출 → (자동) 해석·지갑/네트워크·잔액·수취인·수수료·중복 확인·미서명 거래 준비 → 최종 카드 1장 → [TronLink에서 승인·서명] 1클릭 → 지갑 서명 → 검증·방송·같은 txID 확정 조회 → 완료** 로 바꿨다. 준비 버튼(수수료 확인)은 제거했고 단계는 자동화했다(숨김 아님).
- 텔레그램 입구: 사장 개인 대화 문장 → 서버 intent(메시지 식별자로 멱등) → 요청 요약 + 승인 링크 회신 → 링크 페이지는 **새 AI 호출 없이 같은 intent** 를 불러와 미서명 주문 최대 1건 → 서명은 페이지 클릭으로만 → 상태를 같은 대화에 회신. 어댑터는 서명·방송을 하지 않는다.
- 모의 검사 전부 통과: guide **259/259**(기존 252 + 새 7), safebatch 105/105, 실제 핸들러 모의 1~5(기존)·6~10(신규) PASS, 화면 규칙 모의 Q PASS, 폭 320/360/390/430 넘침 0.
- 실기(iOS/Android 링크 진입·클릭 수)는 **미실측**. 텔레그램 입구는 하니봇 재시작 + 활성 파일 전까지 비활성(안내만).

## 2. 변경 파일
| 파일 | 변경 | 백업/증거 |
|---|---|---|
| `guide/phone_intent.py` (신규) | IntentStore: create(멱등: external_key·같은 사용자 같은 문장 120 s) · prepare(발신 지갑 일치·15분 만료·intent 당 주문 1건·재진입 재사용·서버 재기동 뒤 제안 복원) · view(상태 PROPOSED/ORDER_PENDING/ORDER_SIGNED/FINAL/UNKNOWN/FAILED/ORDER_CANCELLED) | — |
| `guide/phone_server.py` | `GET /i/<token>`(같은 페이지에 intent 토큰 meta 주입, 없으면 404+안내), `GET /api/intent?token=`, `POST /api/intent/create`(source telegram/test 만), `POST /api/intent/prepare`. access log 에서 `/i/<masked>`·`token=<masked>` | `evidence/20260929_tg_remote_impl/before/phone_server.py` |
| `guide/phone.html` | 제안 뒤 **자동 prepare**(제안·요청번호당 1회, 새로고침/재렌더 반복 없음) · 단일 최종 카드(수량·별칭·전체 주소·보내는 지갑·Nile·예상 수수료/상한·**최대 차감**·유효시간, 기술 정보는 접힘) · [TronLink에서 승인·서명] 명시 클릭만 지갑 호출 · intent 모드(입력·수정·새 송금 없음, 지갑 불일치/미연결/만료/질문/거절 안내) · **prepare 실패 이유를 카드에 표시**(20:47 결함 ①) · 조작/호출 집계 `ops` | `before/phone.html` |
| `guide/phone_client.js` | 화면 규칙: `blocked` 상태(이유), `intentGate`(링크 페이지 행동 결정: show-result/presign/connect/mismatch/auto-prepare/message), `maxDeduct`, AI 미호출 표기(20:47 결함 ②) | `before/phone_client.js` |
| `guide/phone_ai.py` | 관문 거부·미호출은 "AI 미호출(…)" 로 표기(기존 "모델 출력 검증 실패(not json)" 오표기 수정) | `before/phone_ai.py` |
| `guide/phone_wallets.json` (신규) | 발신 지갑 등록(아이폰 TQNXymgx… · 안드로이드 TJ1aFHjZ…, 단어 목록, default null) | — |
| `/Users/djl/Desktop/hani_bot/gwdc_tg_adapter.py` (신규) | 트리거(트론링크/트론+보내·송금) · 지갑 단어 해석·되묻기(10분) · 메시지 재전달 dedupe · intent 생성 → 요약+HTTPS 승인 링크+TronLink 딥링크 회신 · 상태 폴링 회신(회신 실패 무해) · ENABLED 파일 없으면 안내만 | — |
| `/Users/djl/Desktop/hani_bot/hani_final.py` | handle_message 앞부분에 관리자 개인 대화 + 트리거일 때만 어댑터 호출(try/except, 송금 없음). **실행 중 하니봇에는 미적용(재시작 전)** | 원본은 launchd 프로세스 메모리 · 파일 diff 로 확인 |
| `guide/tests/test_phone_intent.py`, `tests/_hm_intent_scenarios.js` → `phone_handlers_mock_intent.js`, `test_phone_flow.py`(모의 4 갱신·intent 모의 추가) | 검사 | `evidence/20260929_tg_remote_impl/tests/` |

## 3. 변경 전후 사용자 조작·호출 (모의 실측, 실제 핸들러 모의 6·7 / 실기 미실측)
| 경로 | 사용자 조작(앱 화면) | 지갑 앱 조작 | AI 호출 | 주문 | 서명 | 제출/방송 |
|---|---|---|---|---|---|---|
| **변경 전**(페이지, 9/29 I-V/I-T 실측) | 문장 입력+[송금 내용 확인] 1 · [수수료 확인] 1 · [지갑에서 확인·서명] 1 (+최초 [지갑 연결] 1, 중복이면 [그래도 다시 보내기] 1) = **3~5** | TronLink 서명 팝업 승인 1 (+잠금 해제/생체 필요 시) | 1 | 1 | 1 | 1 |
| **변경 후 · 페이지**(모의 6) | 문장 입력+[송금 준비] 1 · [TronLink에서 승인·서명] 1 = **2** (+최초 [지갑 연결] 1) | 서명 팝업 1 | 1 | 1 | 1 | 1 |
| **변경 후 · 텔레그램 링크**(모의 7) | 채팅 문장 1 · 링크 탭 1 · [TronLink에서 승인·서명] 1 = **3** (+최초 [지갑 연결] 1 · 링크가 TronLink 안에서 안 열리면 복사→Discover 붙여넣기 +2) | 서명 팝업 1 | 0(채팅 접수 때 1회만) | 1(재진입 재사용) | 1 | 1 |
- 모의 집계 원문: `evidence/20260929_tg_remote_impl/tests/phone_handlers_mock_intent_last.txt` (6: clicks send 1·approve 1·connect 0, wallet_sign 1, chat 1·prepare 1·hold 1·signed 1 / 7: send 0·approve 1, chat 0·intent prepare 1·sign 1·signed 1).
- 클릭 수 표기: 텔레그램 제출 뒤 **링크 1 + 페이지 승인 1 + 지갑 승인 1 = 전체 탭 3(승인 2회)**. 실기 전에는 '두 번으로 끝' 이라고 표기하지 않는다.
- 광고 문구 금지: "두 번이면 완료" 는 앱 화면 기준 모의값이며 앱 열기·지갑 연결·잠금 해제·생체 인증·중복 확인·TronLink 앱 전환은 실기에서 별도 집계한다.

## 4. 모의 검사 항목(실제 서버·AI 실호출·서명·방송 0)
- 핸들러 모의 6~10: 명령→자동 prepare→최종 카드→승인 1클릭→지갑 요청 1→제출 1→확정 · 새로고침/재렌더로 prepare 반복 0 · 링크 진입 chat 0·intent prepare 1 · 링크 재진입(PENDING 재사용, FINAL 완료 화면·서명 불가) · 지갑 불일치/미연결/만료 → 주문 0·이유 표시 · 중복 → 명시 확인만 · 잔액 부족 → 이유 표시·재전송/서명 버튼 없음 · 질문 intent → 주문 0.
- 기존 모의 1~5(조회 실패 미둔갑·재진입 완료·늦은 응답 격리·편집/지갑 변경 무효·새로고침/새 송금 부작용 0) 유지, 4 는 자동 prepare 에 맞춰 갱신(지갑 변경 뒤 늦은 prepare 응답 무효 + 준비 중 지갑 서명 0).
- 서버 검사(`test_phone_intent.py`): 같은 메시지 재전달·2분 내 같은 문장 → 같은 intent·chat 1회 · 다른 지갑 prepare 거부 · 재진입 같은 주문 · 확정 뒤 새 주문·재방송 0 · 지갑 미지정/만료/질문 intent 주문 0 · 재기동 뒤 제안 복원(chat 0) · HTTP: source 제한 403·페이지 meta 주입·404 안내·access log 토큰 마스킹.
- 어댑터 검사: 트리거·지갑 단어·모호/미지정 되묻기·비활성 안내(intent 0)·message_id dedupe·회신 문구(요약·HTTPS 링크·딥링크, 토큰은 링크로만)·상태 회신 순서(준비→확정)·회신 예외 무해·질문/거절은 링크 없음.
- 화면 캡처(모의 API·지갑, Chrome headless): `evidence/20260929_tg_remote_impl/after_preview/` input/confirm/presign/dup/processing/done/unknown @320~430 → `OVERFLOW_REPORT.txt` 넘침 0. (blocked·intent 모드 캡처는 미생성 — 미리보기 생성기 상태 목록 밖.)

## 5. 서버 상태(직접 관측)
- 21:08 구서버 PID 48670 종료 → 재기동 명령의 상대 경로 실수로 **약 1분(21:08~21:09:26) 8791 다운(HTTPS 502)** → 21:09:26 PID 64288 정상(같은 토큰·URL, R3 파일 used 4/4 halted null 유지, SB_KILN_BUDGET_FILE=R3, 재사용 비활성). health 200 · HTTPS 200 · `/i/<잘못된 토큰>` 404 · 페이지 meta 주입 확인. 다운 중 사장 요청은 로그상 없음.
- 이 서버로 링크 페이지·intent API 가 살아 있다. 텔레그램 입구는 하니봇 쪽이 재시작되기 전까지 없다.

## 6. 외부 채팅 → TronLink 전달 조사(§4)
- 기존 연동: 하니봇 `hani_final.py`(launchd com.djl.hanibot, telebot 폴링, 관리자 ADMIN_CHAT_ID=<masked> = hani_config 정본) 와 컨트롤봇 `safekeep/control_bot.py`(별도 토큰). 같은 봇 토큰으로 두 번째 폴러를 띄우면 409 충돌(hani_bot.log 3/24 실측)이라 **하니봇 프로세스 안에 훅**으로 넣었다. 사용자 식별은 chat_id 만(대화 내용·지갑 주소 불신).
- TronLink 딥링크 공식 문서(docs.tronlink.org/mobile/deeplink, 9/29 조회): `tronlinkoutside://pull.activity?param=<urlencoded {"url":…,"action":"open","protocol":"TronLink","version":"1.0"}>` (v4.10.0+). iOS/Android 차이·Telegram 클라이언트에서 커스텀 스킴 링크가 탭 가능한지는 문서에 없음 → **실기 실측 전 '지원 추정'**. 회신에는 HTTPS 링크(Tailscale Serve, tailnet 한정)와 딥링크를 함께 준다. 안 열리면 HTTPS 링크 복사 → TronLink Discover 주소창 붙여넣기(조작 +2, 집계에 포함).
- 승인 링크: intent 토큰(24 B urlsafe)·발신 지갑·15분 만료에 묶임. 토큰 단독으로 서명 불가(서명은 지갑 앱, 서명 지갑 = intent 지갑). 로그 마스킹.

## 7. 새 외부 발송·가입·설치·유료 호출이 필요한 지점(보고, 실행 0)
1. **하니봇 재시작**(launchd kickstart com.djl.hanibot) — 훅 적용에 필요. 서비스 재시작이라 사장 승인 필요. 재시작 전까지 텔레그램 문장은 기존 흐름대로 처리된다.
2. **활성 파일** `guide/logs/tg_adapter_enabled` 생성(전무, 승인 뒤). 없으면 훅은 "접수하지 않습니다" 안내만.
3. **Telegram 회신** = 사장 본인 대화에 한정(요청 접수·링크·상태). 새 계정/봇/가입 없음.
4. **Kiln**: R3 4/4 소진. 텔레그램 접수의 해석은 예산 관문이 거부해 규칙 해석(AI 미호출)로 진행된다. 실제 AI 성공 증거를 원하면 새 승인(R4, 1회 $0.01 상한 등) 필요.
5. **실송금**: 서명·방송은 새 승인 없이는 0.

## 8. 다음 최소 실기 검증안(승인 대상, 실행 0)
| 단계 | 채널·기기 | 요청 수 | Kiln | 지급/수수료 | 필요한 준비 |
|---|---|---|---|---|---|
| A. 링크 진입만 | 전무가 `source=test` intent **기기별 1건(총 2건)** 생성 → 링크를 사장에게 전달 → iOS·Android 각각 열기(서명하지 않음) | 2(기기별 1) | 0(규칙 해석) | 0(미서명 주문만; 안드로이드는 주문 생성, 아이폰은 잔액 부족 화면) | 하니봇 재시작 불필요. 링크 진입 경로·클릭 수 실측 |
| B. 텔레그램 1건 실송금 | 사장 개인 대화 → 안드로이드(7.633 TRX) 발신 2 TRX → 맥북지갑 | 1 | 0 또는 R4 1회($0.01 상한) | 2 TRX + 실수수료(예상 0) · 서명 1 · 방송 1 | 하니봇 재시작 승인 + 활성 파일 |
| C. 아이폰 | 같은 흐름 아이폰 발신 | 1 | 동상 | 2 TRX + 수수료 · 서명 1 · 방송 1 | **아이폰 잔액 1.000 TRX(21:0x 체인 읽기) → 최소 2.5 TRX 필요**: 안드로이드→아이폰 2 TRX 공급(서명 1) 또는 faucet 재시도 |
- 실패·불명 시 자동 새 주문/재서명/새 유료 호출 없음. 완료 판정은 같은 txID 최종 확정.

## 9. 남은 문제·제약
- 실기 미실측: iOS/Android 링크 진입 경로, 실제 클릭 수, 딥링크 동작, Telegram 링크 렌더링.
- blocked·intent 모드 화면 캡처 미생성(미리보기 생성기 확장 필요).
- 공개 저장소(hooniesl/safebatch-ai-gwdc2026) push 보류(실증 전 '구현 중').
- 기존 인수시험 4건 증거(`trial4_1840/`)와 이 버전 증거(`20260929_tg_remote_impl/`)는 분리.

## 10. 실기 링크 진입 검증 결과 (사장 손 · 21:39~22:00 · 서명 0 · 송금 0 · 유료 호출 0)
방법: 전무가 `source=test` intent 를 만들고 승인 링크를 사장 본인 텔레그램(하니봇 대화)으로 발송(`guide/tools_send_test_link.py`, 발송 3건: 아이폰 21:39 결함판·아이폰 21:47·안드로이드 21:55). 하니봇 훅·어댑터는 여전히 비활성.

| 기기 | 경로(실측) | 사용자 조작(승인 전) | 서버 기록 | 결과 화면 |
|---|---|---|---|---|
| 아이폰 15 Pro Max · LTE | 텔레그램 🔗 탭 → **텔레그램 안 브라우저**(TronLink 없음) → "요청한 지갑을 TronLink에서 연결하세요" → 링크 복사 → TronLink Discover 붙여넣기 → [지갑 연결] | 링크 탭 1 · 복사 1~2 · 붙여넣기·이동 1~2 · 지갑 연결 1 = **4~6** | 21:52:55 /i/ 200 → 21:53:50 `?utm_source=tronlink` → orders → 21:53:55 intent/prepare **409 잔액 부족**(1.0 < 2+0.267) | "보낼 수 없습니다 · 잔액 부족" 카드, 주문 0 (`real_iphone/IPHONE_2_*`, `IPHONE_3_*`) |
| 안드로이드 Note20 · LTE | 1차: **Tailscale 꺼짐 → "사이트에 연결할 수 없음"**(맥북 status offline 1h) → Tailscale 켬 → 🔗 탭 → 텔레그램 안 브라우저 → 연결 안내 → 복사 → TronLink Discover → [지갑 연결] → 자동 준비 | Tailscale 켜기 1 · 링크 탭 1 · 복사/붙여넣기 2~3 · 지갑 연결 1 = **5~6** | 21:59:18 /i/ 200 → 22:00:34 `?utm_source=tronlink` → 22:00:38 intent/prepare **200** → 주문 phone_trx_20260929_220034_e0157240 PENDING(미서명, 22:10:34 만료) | "최종 확인 · 승인하면 송금됩니다" 카드: 2 TRX·전체 주소·안드로이드 지갑·예상 최대 0.267/상한 2·최대 차감 2.267·유효 10:10:34 (`real_android_3_*`). 승인 버튼 미클릭 |
- 딥링크(`tronlinkoutside://…`): 텔레그램(iOS)에서 **파란 링크로 탭 가능** 확인(사장 진술). 탭 시 TronLink 가 바로 열려 페이지까지 갔는지는 **미확인**(서버 기록만으로 구분 불가). 안드로이드 미시험.
- 1차 아이폰 링크(21:39)는 페이지 결함으로 입력 화면이 떴다: 서버가 `__SB_INTENT__` 자리표시자를 통째로 치환하는데 JS 비교 문자열에도 같은 글자가 있어 토큰이 비었다 → 수정(`"__SB_" + "INTENT__"`)·회귀 검사(`test_phone_intent`: 토큰은 meta 1곳만)·서버 재기동 PID 66971(21:46) → 실제 WebKit(iPhone UA) 프로브로 intent 모드 확인 후 재발송.
- 결론: **링크 진입은 iOS/Android 모두 성공**, 자동 준비·지갑 결합·잔액 관문·최종 카드 실기 확인. 다만 텔레그램 안 브라우저에는 TronLink 가 없어 **복사→Discover 붙여넣기 우회가 매번 필요**(승인 전 조작 4~6, 목표 2~3 미달). 개선 후보(전무 제안, 미구현): 링크 페이지가 TronLink 밖에서 열리면 [TronLink 앱에서 열기] 버튼(페이지 안 딥링크) 1탭으로 대체 → 예상 조작 링크 1·앱 열기 1·지갑 연결 1·승인 1.
- 잔여: 안드로이드 미서명 주문 e0157240 은 22:10 만료(잠금 없음, 다음 prepare 가 대체). 링크 3건 만료 뒤 새 주문 불가. 하니봇 훅·활성 파일·실송금은 여전히 미실행.

## 11. 부사장 검수(VP_REVIEW_TG_IMPL_20260929) §1~§4 보완 결과 (22:1x~22:4x · 유료 호출·서명·방송·하니봇 재시작 0)
| 항 | 결함 | 수정 | 검사 |
|---|---|---|---|
| §1 되묻기 응답 미라우팅 | 훅이 `matches()`만 봐서 "아이폰/안드로이드" 단독 답이 탈락 | 어댑터 `should_route(text, chat_id)`(트리거 또는 **그 대화의 되묻기 대기(10분) + 지갑 단독 답**) → `route()`. hani_final 훅을 `should_route→route`로 교체, `send_fn=bot.send_message`(안정) | `test_routing_ask_wallet_…`: 원문→되묻기→"아이폰"→같은 원문·선택 지갑 intent 1건, 대기 없는 대화는 False · hani_final.py 훅 문자열 검사(`should_route`/`route`/관리자 개인 대화 조건) |
| §2 문장 병합·중복 키 | 120초 같은 문장 병합에 sender 없음, duplicate_keys 미조회 | 문장 병합 **제거**. 멱등은 메시지 키(`keys[]` 영구)만. 같은 키·다른 내용/지갑/사용자 → `IntentConflict`(409). 회신 지갑 = 서버가 돌려준 intent 의 지갑 | 두 지갑 같은 문장 → 2건 · 같은 키 내용 변경 → 409 · 10,000초 뒤·새 프로세스 재전달 → 같은 intent·chat 0 · 회신에 반환 지갑만 |
| §3 생성 권한·링크 범위 | source 문자열만 검사, 링크에 공통 세션 토큰 | `POST /api/intent/create` 는 **봇 키(X-SB-Bot-Key, guide/logs/bot_key.txt 0600)** 필수 + **서버 쪽 발신 지갑 허용 목록**(phone_wallets.json) · 승인 링크는 `/a/<intent_token>` 단독 범위(그 intent 조회·prepare·**그 주문만** status/hold/signed/reject, chat·create·일반 prepare 404, 다른 지갑 기록 빈 목록) · 옛 `/p/<세션>/i/` 제거 · source=test 는 시험 모드 강제 | 키 없음/오키/source 위조/미등록 지갑 → 403 · 링크 페이지에 세션 토큰 없음 · 다른 주문 403 · chat 404 · 로그에 승인/세션/봇 키 없음(경로 토큰 모양 세그먼트 전부 마스킹) |
| §4 동시·중단·회신 | 새 lambda 마다 인스턴스 재생성, 상태 파일 잠금 없음, chat 뒤 기록 전 중단 시 재전달 판별 불가, 회신 실패 무시 | 모듈 단일 인스턴스(`_adapter`, 잠금) · 상태 파일 fcntl 잠금+원자 교체 · **CREATING 내구 예약**(chat 전 기록; 중단 재전달 → 추가 AI 호출 0, 사용자에게 '결과 불명·새 문장' 안내) · 주문 생성 뒤 결합 전 중단 → 같은 제안·지갑 PENDING 결합(추가 생성 0) · 회신 실패는 기록(attempts≤3)·같은 결과만 재전송(`resend_failed_notifications`, 재송금 없음) · 처리 중 표시 120초 지나면 재시도 허용(서버 멱등) · 오류 문구에 URL 없음 | 동시 같은 키 4스레드 → intent 1·chat 1 · chat 예외/CREATING 재전달 · 결합 전 중단 복구 · 확정 알림 실패 3회 기록 → 재전송 1회 · 동시 handle(같은 3+다른 2) → intent 2 |
- 코드 검토(sonnet, 읽기 전용): Python 리뷰 4건(로그 마스킹 범위 → 경로 토큰 세그먼트 전부 마스킹 / 어댑터 in_progress 정체 → 120초 뒤 재시도 / sender 비교 정규화 / 잠금 범위(보류)) · 보안 리뷰 4건(서버 발신 지갑 허용 목록 → 적용 / 어댑터 예외 logging → 적용 / Host·Origin 은 인증 경계 아님(문서화) / 무잠금 읽기(원자 교체로 안전, 보류)). 핵심 불변식(시험 모드 서명 차단·링크 범위·지갑 불일치·멱등·비밀값 위생) 통과.
- 검사 결과: guide **263/263**(intent 10 + 핸들러 모의 6~12) · safebatch 105/105 · 넘침 0(기존 7상태 + blocked·intent_connect·intent_test_presign @320~430, `after_preview_tg2/`). 증거 `evidence/20260929_tg_remote_impl/after_tg2/`(수정본 원본), `tests_tg2/`.
- 서버 재기동 3회(21:46 PID 66971 → 22:24 PID 70979 → 22:4x PID 71210, 다운 없음, 같은 토큰·URL, R3 4/4 유지). 페이지/서버 버전 **2026-09-29.tg2**(meta `sb-page-version`, session.json version).

## 12. [TronLink 앱에서 열기] 버튼 · 시험 모드 · 직접 진입 검증 준비
- 버튼: intent 링크 페이지가 **TronLink 밖**(window.tron 없음)에서 열렸을 때만 표시. 클릭 1회 → 현재 페이지 URL을 공식 딥링크(`tronlinkoutside://pull.activity?param={url,action:open,…}`)로 이동. AI 호출·intent·주문·서명·방송 0(모의 11: chat/prepare/sign 증가 0). 2.5초 뒤 앱으로 안 넘어갔으면 복사 안내([링크 복사])만, 두 번째 클릭은 이동 없음(자동 반복 금지). TronLink 안이면 숨김·자동 준비. 제3자 단축/중계 서비스 없음.
- 시험 모드(source=test / test_mode): 해석은 `flow.chat_test`(MockKiln 고정, 운영 예산 미소모, 재사용 없음). 미서명 주문은 만들 수 있으나 서버가 `/api/order/signed`·`hold` 를 409 로 거부, 페이지는 승인 버튼 숨김·배너 표시·서명 핸들러 차단(모의 12: 지갑 서명 0). 사용자가 실수로 눌러도 실서명/방송 불가.
- 직접 진입 검증(사장 손, 22:4x 발송): 시험 모드 링크 **2건**(아이폰 intent 1d7b09f852b6 · 안드로이드 cba6b981212a, 각 기기 1건·미서명 주문 최대 1건씩·유료 호출 0). 실제 WebKit(iPhone UA) 프로브: `/a/` 링크 → 페이지 버전 tg2 · "TronLink 앱 밖에서 열렸습니다 … [TronLink 앱에서 열기]" · 앱 열기 버튼 표시·지갑 연결 버튼 숨김·승인 버튼 숨김·시험 배너 · prepare 0. 기록할 것: 파란 링크 표시 vs 앱 직접 열림 성공 구분, 전체 탭 수(링크 1·버튼 1·지갑 연결 1 + 앱 전환·잠금 해제).
- e0157240(22:00 안드로이드 미서명 주문) 22:21 읽기 조회: order_state PENDING · signature_stored False · client_hold False · **expired True(서버 시계+확정 블록 시각)** · 결과 없음 · hold 파일 없음. 삭제·재활용 없음(잠금도 없음: 미서명은 sender_lock 대상 아님).

## 13. 최소 승인안(한 번에) — 실행 0, 사장 결정
| 항목 | 내용 | 비용·위험 |
|---|---|---|
| A. 직접 진입 검증(지금 가능, 승인 불필요) | 22:4x 발송한 시험 모드 링크 2건으로 iOS/Android 각각: 링크 탭 → [TronLink 앱에서 열기] → 앱 전환 여부·같은 요청 복원·탭 수 기록. 서명·방송 불가 | 0 |
| B. 하니봇 재시작 + 훅 활성 | `launchctl kickstart -k gui/<uid>/com.djl.hanibot`(기존 launchd, 별도 폴러 없음) → 재시작 확인(pid·하트비트·`/status`) → `guide/logs/tg_adapter_enabled` 생성. 복구: 활성 파일 삭제로 즉시 접수 중단, 훅은 try/except·관리자 개인 대화 한정 | 하니봇 1~2분 중단. 다른 기능 영향 없음(훅은 송금/트론링크 문장만) |
| C. 안드로이드 텔레그램 실송금 1건 | 사장 개인 대화 "안드로이드로 맥북지갑한테 트론 2개 보내 트론링크앱 사용해서" → 링크 → 앱 열기 → 지갑 연결 → 승인 1 → 서명 1 → 방송 1 → 확정. 해석은 규칙(R3 소진, AI 미호출) 또는 **R4 1회($0.01 상한)** 선택 | 2 TRX + 실수수료(예상 0~0.267) · 서명 1 · 방송 1 · Kiln 0 또는 1회 |
| D. 아이폰 | Android 경로 검증 뒤 필요 시(잔액 1.0 → 2.5 TRX 공급 필요) | 보류(부사장 지시) |
- 이 기능 표기: 실증 전 **'구현 중'**. 기존 4건 증거·제출 준비는 별도 진행.

## 14. [TronLink 앱에서 열기] 직접 진입 실기 결과 (사장 손 · 22:31~22:34 · 시험 모드 · 서명·방송·유료 호출 0)
| 기기 | 경로(실측) | 조작 수(승인 전) | 서버 기록 | 결과 |
|---|---|---|---|---|
| 아이폰 · LTE | 텔레그램 🔗 탭 → 텔레그램 안 브라우저(시험 배너·[TronLink 앱에서 열기]) → 버튼 탭 → **TronLink 앱이 열리며 같은 요청 복원** → [지갑 연결] | 링크 1 · 버튼 1 · 지갑 연결 1 = **3**(복사·붙여넣기 0) | 22:31:38 /a/ 200 → 22:32:01 `?utm_source=tronlink` → prepare 409 잔액 부족 | '보낼 수 없습니다·잔액 부족' + 시험 모드 배너, 주문 0 (`real_iphone/IPHONE_4_*`) |
| 안드로이드 · LTE(Tailscale ON) | 🔗 탭 → 텔레그램 안 브라우저 → 버튼이 **화면 아래에 가려져** 처음엔 못 봄 → 스크롤 → 버튼 탭 → **TronLink 열림·같은 요청 복원** → [지갑 연결] → 자동 준비 | 링크 1 · 스크롤 1 · 버튼 1 · 지갑 연결 1 = **3~4** | 22:33:55 `?utm_source=tronlink` → 22:33:59 prepare 200 → 주문 a03a695b(시험 모드·미서명, 22:43:55 만료) | '최종 확인' 카드(2 TRX·전체 주소·안드로이드 지갑·예상 최대 0.267/상한 2·최대 차감 2.267) + 시험 배너, 승인 버튼 없음 (`real_android_4_*`, `real_android_5_*`) |
- 판정: **두 기기 모두 버튼으로 TronLink 직접 열림 성공**(파란 링크 표시와 구분: 버튼 탭 → 앱 전환 → 같은 intent 복원까지 서버·캡처로 확인). 복사·붙여넣기 우회 불필요. 승인 전 조작 3(+안드로이드 스크롤 1). 승인 2회(페이지 승인 1 + 지갑 서명 1)는 시험 모드라 실행하지 않았다(불가능하게 막음).
- 즉시 수정(22:3x): 버튼을 카드 맨 위(채팅 요청 문장 바로 아래)로 이동 → 스크롤 없이 보이게. 검사 통과·서버 재기동(같은 토큰, v tg2). 실기 재확인은 다음 시험에서.
- 시험 주문 a03a695b: 22:34 조회 PENDING·미서명·hold 없음·만료 전(22:43:55). 시험 모드라 서명본 제출은 서버가 거부. 삭제·재활용 없음.
- 두 기기 링크/준비 건수: intent 2건(기기별 1) · 미서명 주문 1건(안드로이드) · 아이폰 주문 0(잔액 부족) · AI 실호출 0(모의 제공자).
