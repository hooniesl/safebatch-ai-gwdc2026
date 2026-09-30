# Telegram '승인' 답장만으로 송금 · 공통 구현·모의 검증 보고 (전무 · 2026-09-29 23:2x KST · orders 7b48f2 후속)

요청서: `VP_TELEGRAM_ONLY_APPROVAL_20260929.md`(사장 목표 정정: 명령 → 백그라운드 준비 → 짧은 요약 → '승인' 답장 → 자동 서명·방송·확정 → 완료 회신. TronLink 앱 이동은 필수 아님).
범위: **결정 전 공통 구현 + 모의 검증**. 실제 서명 방식은 사장의 지갑 선택 뒤 확정. **이번 작업의 유료 호출·서명·방송·자금 공급·온체인 권한 변경 0.** R4(1회)는 22:43 접수에서 소진되어 추가 호출 없음. 이 기능은 실증 전 '구현 중'.

## 1. 현재 거래 상태(중복 지급 방지 확인, 22:49·23:1x 읽기)
- Telegram intent **0bb87914150c**(22:43, 안드로이드, "맥북지갑한테 트론 2개 보내 트론링크앱 사용해서"): state PROPOSED · payment_id 없음 · **주문·서명·방송 0**. Kiln R4 call a522e9d1 · $0.00003368 · used 1/1. 안드로이드 잔액 7.633 TRX(22:0x 이후 변동 없음).
- 이 intent 는 새 서명 지갑으로 바꿔 실행하지 않는다(요청서). 시험 주문 e0157240(만료·미서명)·a03a695b(시험 모드·미서명) 보존.
- 신규 접수는 22:49 비활성(`guide/logs/tg_adapter_enabled` → `.off_2249` 이름 변경, 삭제 아님). 하니봇(PID 73117, 22:40 재시작)은 이전 훅 코드로 실행 중이며 비활성 안내만 한다. 아래 새 훅 코드는 **다음 재시작 전 미적용**.

## 2. 구현(공통, 결정과 무관)
| 항 | 구현 | 위치 |
|---|---|---|
| §1 짧은 요약 | `🧾 승인 대기` 6줄: 보내는 지갑(라벨·끝 6자) / 받는 별칭·끝자리 + 전체 주소 / 금액·Nile / 예상 최대 수수료·상한·**최대 차감** / 승인 만료 / '취소' 안내. URL·딥링크·AI 로그 없음. 발신 지갑 미지정·준비 실패면 "준비 실패 — 승인 대기 아님" | `gwdc_tg_adapter.fmt_summary`, `_create` |
| §2 백그라운드 고정 | 접수 직후 `POST /api/intent/prepare_bg`(봇 키): 지갑 연결 없이 intent.sender 로 미서명 거래 작성·잔액/수수료/중복 검사·주문 PENDING → `awaiting_approval`, **지문=snapshot_sha256**, 승인 만료=거래 만료(10분) | `IntentStore.prepare_bg` |
| §3 승인 판정(LLM 없음) | `APPROVE_RE`(정확한 '승인/approve' 만, '승인하지 마' 불일치) · 인증된 관리자 개인 대화(chat_id) · **요약 메시지 reply** 우선, 일반 '승인'은 그 대화 대기 **정확히 1건**일 때만 · 여러 건/만료/전달(forward)/음성(비텍스트)/다른 사용자/취소 → 실행 없음 | `Adapter.should_route`·`_handle_decision`, hani 훅(reply_to·forward·content_type 전달, 회신 메시지 ID `note_sent`) |
| §4 원자적 1회 소비 | `IntentStore.approve`: 파일 잠금 안에서 사용자·지문·만료·주문 PENDING 검사 → `approval{key,t}` **내구 기록** 후 실행. 같은 승인 키 재전달 → 기존 결과(idempotent), 다른 키 → "이미 처리"(already), 지문 불일치 → 거부(새 요약·새 승인 필요) | `phone_intent.approve` |
| §5 실행 | 정책 검사 → signer.sign(고정 거래) → 기존 `submit_signed`(검증·방송 1회·같은 txID 조회) → SUBMITTED/NOT_SUBMITTED 기록. UNKNOWN 은 새 거래 없이 조회만. 완료 회신 실패는 같은 결과 재전송(≤3). 중단 복구 `resume_approved`: 서명 저장돼 있으면 조회만, 없고 PENDING·미만료면 같은 거래 1회 | `_execute_approved`, `resume_approved`, 어댑터 `_watch`/`resend_failed_notifications` |
| §6 signer 분리 | `phone_signer.py`: `NoSigner`(운영 기본, 승인 시 SIGN_FAILED·실행 0) · `MockSigner`(검사용 키, 모의 전용) · `SignerPolicy`(Nile chainId 고정·허용 수취인=확인된 등록 수취인·건별 금액/수수료 상한·일일 누적·중지 파일 `logs/signer_stop`·원장 `logs/signer_ledger.jsonl`). **앱 수준 한도**이며 체인 권한 범위와 구분 | `phone_signer.py`, 서버 `--signer none|mock` |
| 라우트(봇 키) | `POST /api/intent/prepare_bg`·`/approve`·`/pending`·`/view` (키 없음 403) | `phone_server.py` |

## 3. 모의 인수시험(§7) — `tests/test_tg_approval.py` 6건 + HTTP 1건, 전부 통과(guide 271/271 · safebatch 105/105)
| 시나리오 | 결과 |
|---|---|
| 한 문장 → 준비 → 요약(URL 없음) → 요약에 '승인' 답장 → MockSigner 서명 1 → 방송 1 → FINAL → 완료 회신 | ✅ 방송 1·원장 1건·완료 회신 "송금 완료(확정)" |
| 같은 승인 메시지 재전달 / 두 번째 '승인' | ✅ 같은 회신, 추가 실행 0 / "대기 없음", 방송 1 유지 |
| 동시 승인 4건(다른 메시지) | ✅ 1건만 실행, 나머지 already, 방송 1 |
| 다른 사용자 / 전달 메시지 / 음성 / 다른 메시지에 답장 / 여러 대기 + 일반 '승인' / 취소 뒤 승인 | ✅ 전부 실행 0 |
| 지문 불일치(내용 변경) / 만료 뒤 승인 | ✅ 거부, 실행 0 |
| 응답 유실(조회 NOT_FOUND) | ✅ UNKNOWN 유지·새 거래 0·"다시 보내지 마세요" 회신 |
| APPROVED 기록 뒤 실행 전 중단 → 재시작 복구 | ✅ 같은 거래 1회 실행, 재복구는 조회만, 같은 키 재승인 idempotent |
| 정책(건별 한도 초과) / 중지 파일 / 서명 주체 미정(NoSigner) | ✅ POLICY_REFUSED / 실행 0 / SIGN_FAILED, 방송 0 |
| 준비 실패(잔액 부족) | ✅ "준비 실패 — 승인 대기 아님", 승인 라우팅 안 됨 |
| HTTP: 4 라우트 봇 키 403 · 다른 사용자 409 · 승인 3회 요청 → 방송 1 · view 상태 | ✅ |
- 서버 재기동 PID 74559(`--signer none`, R4 파일 그대로 used 1/1, 토큰·URL 유지). 승인 요청이 와도 서명 주체 미정으로 실행되지 않는다(실기 전 안전).
- 기존 인수 증거(정상 3·복구 1, `trial4_1840/`)와 앱 이동 실기(`20260929_tg_remote_impl/`)는 별도 보존. 이번 증거 `evidence/20260929_tg_only_impl/{src,tests}`.

## 4. 서명 주체 후보(사장 결정 대상, 실행 0)
| 후보 | 방식 | 확인된 사실(문서) | 미확인·위험 | 필요한 승인 |
|---|---|---|---|---|
| A. (추천) 별도 Nile 봇 전용 지갑 | 맥북 서버가 `MockSigner` 와 같은 인터페이스로 로컬 키 서명. 키는 암호화 파일(0600)+정책 한도+중지 파일. 자금은 안드로이드→새 지갑 소액(예: 2.5 TRX) | 기존 코드로 즉시 붙는다(서명 인터페이스 동일). 사장 기존 지갑 시드/비번 불필요 | 키 보관 위치·백업·복구 절차, 맥북 침해 시 잔액 한도까지 노출(앱 한도만) | 새 지갑 생성·키 파일 보관·안드로이드→새 지갑 자금 1회(서명 1)·실송금 1건 |
| B. TronLink MCP 서버 agent-wallet (`@tronlink/mcp-server-tronlink` v0.1.1 + `@bankofai/agent-wallet`) | Direct API: 암호화 로컬 agent-wallet 이 서명·방송(HITL 없음). Nile 지원(`TL_TRONGRID_URL=https://nile.trongrid.io`) | 문서 9/29 조회: 키는 암호화 로컬 저장, 비밀번호가 유일한 관문, **건별 한도 없음**(잔액 검사만). 문서는 "운영 자금 이동은 mcp-tronlink-signer 권장" | 새 npm 설치·Node 런타임·55개 도구 노출(`tl_evaluate` 위험) — 우리 정책 한도는 서버 밖. 설치 승인 없음 | 설치 승인·비밀번호 보관·통합 검사 |
| C. `mcp-tronlink-signer` | 브라우저 TronLink 확장 승인 UI 를 열어 사람이 클릭 | 프로그램적 우회 없음(문서) | **사람이 맥북 브라우저에서 승인** → '텔레그램 승인만' 목표와 불일치 | — (목표 불일치) |
| D. 기존 안드로이드 지갑 권한 위임 | 체인 계정 권한(active permission)에 봇 키 추가 | TRON 다중서명/권한 문서 존재 | 온체인 권한 변경 tx(수수료), 안드로이드 지갑 owner 키로 서명 필요, 잘못 설정 시 자금 위험 | 권한 변경 승인·실측 |
- 어느 경우든 '암호화 키 보관' 만으로 권한이 제한되지는 않으며, 앱 한도(SignerPolicy)와 체인 권한을 구분해 표기한다.

## 5. 최소 실기 계획(결정 뒤, 승인 대상)
1. 사장 지갑 선택(A 추천). A 이면: 서버가 새 Nile 지갑 생성(키 파일 0600·백업 사본 위치 사장 지정) → 안드로이드→새 지갑 **2.5 TRX**(서명 1) → phone_wallets.json 에 '봇지갑' 등록.
2. 하니봇 재시작(새 훅: reply_to·note_sent) + 활성 파일 + 서버 `--signer <실제>`.
3. 텔레그램 1건: "봇지갑으로 맥북지갑한테 트론 2개 보내" → 요약 → 답장 '승인' → 서명 1·방송 1 → 확정 회신. Kiln: R4 소진 → 규칙 해석(0) 또는 새 승인 R5 1회($0.01).
4. 검증: 같은 txID 확정·완료 회신·조작 수(문장 1 + 답장 1 = 2, 앱 이동 0)·호출/서명/방송 횟수·비용·수수료.

## 6. 남은 문제
- 하니봇 훅 새 코드 미적용(재시작 필요) · 신규 접수 비활성 상태 · 서명 주체 미정.
- 22:43 intent 0bb87914150c 는 승인 대기가 아니다(옛 흐름·주문 없음). 새 흐름으로 재사용하지 않는다.
- 공개 저장소 push 보류.
