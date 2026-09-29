# 제출 준비 점검 v4.3 · 2026-09-29 16:4x KST (전무) — 산출물 경로·재현 결과·제출을 막는 항목

## 0. v4 변경(a안: 휴대폰 흐름 포함, 확인 범위 명시) — v3 기준본은 `evidence/20260929_submission_v3_baseline/`(읽기 전용, SHA256SUMS)
| 제출물 | 경로 | v4 상태 |
|---|---|---|
| README | `README_SUBMISSION.md` = `public_release_20260929/README.md` = `public_release_20260929/README_SUBMISSION.md` | §4b 휴대폰 흐름: 같은 Wi-Fi 서명 송금 1건(규칙 파싱) · Kiln 4/4 = CLI 형식 1 + 서버 `/api/chat` 3(맥북 입력, 휴대폰 타이핑 아님) · 원격 = Tailscale Serve HTTPS 맥북에서 확인, 휴대폰 LTE **미검증** |
| 발표 PDF v4 | `prep_20260914/DECK_SafeBatchAI_FuriosaA_20260929.pdf` (10쪽, 원본 `DECK_20260929.html`, Chrome headless) | 1쪽·3쪽 휴대폰 시제품 1줄, 9쪽 = 안전 속성 축약 + 휴대폰 3사실 + 사장 캡처(PHONE_ORDER_TABLE_0940, 토큰 없음), 10쪽 한계·공개 범위. 미리보기 `evidence/20260929_phone_prototype/deck_v4_1548/` |
| 데모 영상 v4 | `prep_20260914/DEMO_VIDEO_SafeBatchAI_FuriosaA_20260929.mp4` (175.3 s ≤ 180, H.264 1280×720 + AAC) · 생성기 `make_video3.py` · 프레임 `evidence/20260929_video_frames/v3/` · 대본 `VIDEO_SCRIPT_20260929.md` 7b 행 | 장면 7b 휴대폰 시제품(SEPARATE PROTOTYPE 라벨, 있는 그대로), 다른 장면 나레이션 축약. 실시간 녹화 아님 |
| 공개 묶음 | `public_release_20260929/` (177 파일 ≈7.2 MB) | 최신 `guide/phone_ai.py`·`tests/test_phone_ai.py`, submission PDF·mp4·대본 갱신, 증거 `kiln_r2_1351/`·`trial_178b51e6/`(서명본 order_record 제외)·`remote_https_1513/`·`TESTS_*_r2c.txt`, 보고서 PHONE_TRIAL_RESULT·KILN_PHONE_4CALLS(_R2)_RESULT. 재현(묶음 내 `/usr/bin/python3 -m unittest discover tests`): guide **224/224**·safebatch **105/105** (`TESTS_public_*_1548_r2c.txt`). 비밀값: 실제 경로 토큰 0건(전수 대조), 키/시드/개인키 패턴 0건, `.env` 없음, 세션 URL 은 `<token>` 마스킹본만 |

**v4.1(16:0x, 부사장 R2c 검수 반영):** 대본 `VIDEO_SCRIPT_20260929.md` 를 실제 v4 클립 길이(11장면, 합계 175.30 s, 휴대폰 장면 2:13.74–2:33.51)·영어 음성(say Samantha)+AAC·증거 수(Mac 확정 2 + 휴대폰 확정 1, guide 224 + safebatch 105)에 맞춰 재작성, 공개 사본 `submission/`·`prep_20260914/` 동기화(SHA 동일). 비밀값 범위 정정: **주문 서명 보관본(order_record.json) 제외, 체인 조회 응답(v4_tx.json·v4_solid_now.json·solidity_tx.json)의 온체인 signature 필드는 포함**(탐색기 공개 데이터, 개인키 아님). 공개 사본 `.gitignore` 추가(178 파일 7.3 MB, 바이트코드 캐시·심볼릭 링크 0). 휴대폰 LTE 절차 `PHONE_LTE_CHECK_STEPS_20260929.md`, 공개 승인안 `GITHUB_PUBLISH_PROPOSAL_20260929.md`.

제출 행정 현재(16:0x, 기존 기록 대조 · 재질문 없음): 제출 폼 URL 확인됨(9/28 메일) · 현장 체크인 완료(사장 보고) · **팀 ID 미확인**(메일·Luma·Q&A 에 없음, team34 는 Kiln org) · **Telegram 체크인 미확인**(완료 기록 없음) · Kiln 비밀번호 변경 가능 여부/변경 완료 미확인(9/28 19:50 콘솔 "coming soon", 비밀번호 자체는 필요 없음) · **공개 GitHub 미생성**(승인안 정정: gh 설치·계정 hooniesl, 저장소 404, 16:2x 토큰 401 → 사장 `gh auth login` 필요) · 최종 제출 미실행. 팀 ID·비번은 문의 초안 `ORGANIZER_INQUIRY_DRAFT_20260929.md`(미발송, 발송은 사장 승인).

**v4.2(16:3x):** 휴대폰 LTE 기존 주문 조회 확인(`evidence/20260929_phone_prototype/remote_lte_1629/`) → README §4b 3본, PDF v4.1(9·10·1쪽 문장, 10쪽 유지), 영상 v4.1(177.8 s ≤ 180, 장면 10·11 문장), 대본 v4.1 시간표 갱신, 공개 사본 submission/ SHA 동일. 범위 표기: "existing-order lookup from the phone on LTE verified; remote chat/signing unverified". gh 인증 OK(hooniesl, scopes repo) — 게시는 사장 공개 승인 대기.

**v4.3(16:4x) 공개 GitHub 게시 완료(사장 승인):** https://github.com/hooniesl/safebatch-ai-gwdc2026 — Public·main·커밋 8d3c2e45(178)+6b2c9f95(LTE 증거 4)=182 파일, force-push 없음, 라이선스 미지정. 로그아웃 접근: 저장소·raw README·submission PDF/mp4·kiln_calls.jsonl 모두 200. README 참조 중 공개 사본에 없는 것: `prep_20260914/SESSION.md`(내부 일지, 의도적 제외). 제출 폼에 넣을 값: 저장소 URL 위, 영상 `submission/DEMO_VIDEO_SafeBatchAI_FuriosaA_20260929.mp4`(177.8 s), PDF `submission/DECK_SafeBatchAI_FuriosaA_20260929.pdf`(10쪽).

제출을 막는 항목(v4.3): 팀 ID·Telegram 체크인 미확인, 최종 제출 미실행(사장 승인 필요). GitHub 항목은 해소. 팀 ID·Telegram 체크인 미확인, 공개 GitHub 미생성(승인 대기), 최종 제출 미실행. 휴대폰 LTE 조회는 제출 조건이 아니며 미검증으로 표기한 채 제출 가능(사장이 캡처하면 README·PDF 문장만 '확인'으로 바꾼다).

## (이력) v3 · 02:5x

## 1. 로컬 완성본(부사장 검수 대상) — 원본 `gwdc_2026/`, 공개 사본 `gwdc_2026/public_release_20260929/`
| 제출물 | 경로 | 상태·검증 |
|---|---|---|
| README | `README_SUBMISSION.md` (사본 `public_release_20260929/README.md`) | 실행법(venv·`requirements.txt`·`.env.example`)·A 선언("explains", proposes 삭제)·사전/행사 구분 근거(mtime, 원 이력 불변)·tx 2건·Kiln 로그·거절 0건·AI/코드/사람 구분·파일 링크·실거래/합성 구분 |
| 데모 영상 v3 | `prep_20260914/DEMO_VIDEO_SafeBatchAI_FuriosaA_20260929.mp4` (사본 `submission/`) · 대본 `VIDEO_SCRIPT_20260929.md` · 생성기 `make_video2.py` · 프레임 `evidence/20260929_video_frames/v2/` | ≈178 s(≤180), H.264 1280×720 + AAC, ffmpeg 8.1.1 · 영어 나레이션(`say` Samantha)+영어 자막 · **보완된 실제 안내 화면**(목표 입력→조건 확인→다음 행동, 규칙만·AI 미호출) · 정상/적응은 기록 주문의 `/sign` 재현(RECORDED RUN) · 거절 기록 · AI/코드/사람 배지 · Gemini 채택 문구(separate prototype·no alternative amount·carried…since-fixed bug·not re-tested) |
| 발표 PDF v3 | `prep_20260914/DECK_SafeBatchAI_FuriosaA_20260929.pdf` (10쪽, 원본 `DECK_20260929.html`) | byte-identical 삭제(주문 수·intent 줄 수 전후 동일) · 실제 화면 3장(보완 후) · Out 항목에 최초 지갑 연결 미검증·에너지 원인 미상 · 캡션 "AI explained / code computed & checked / human approved & signed" · Live/Synthetic 구분 · 우위 주장 없음 |
| 공개 묶음 | `public_release_20260929/` (132 파일 ≈4.6 MB) | 최신 코드(자문 반영)·검사 157/157 통과(묶음 내 실행)·`requirements.txt`(tronpy==0.6.2, protobuf==6.33.6)·`.env.example`·증빙·submission/(PDF·mp4·대본·A 요약)·`ADVISOR_REVIEWS`·`USER_TEST_PLAN`. 제외: `.env`, `guide/pending/*`, `logs_server.txt`, plan_results, PNG, 사장 비교 원본, 문의 초안. 스캔: 키·비밀번호·이메일·시드 0건("password: coming"은 문구) |

### 재현 결과 (공개 폴더만 → `/tmp/sb_clean_20260929`, 새 venv, Python 3.9.6)
- 1차: tronpy 만 설치 → guide 41 오류(`No module named 'google'`) → `protobuf==6.33.6` 명시. 2차: guide 156/156(당시)·safebatch 105/105 · 앱 기동 `/`·`/sign`·`/api/health` OK · CLI OK. 자문 반영 후 묶음 내 guide 157/157 재확인.

## 2. 제출 행정
| 항목 | 근거 | 상태 | 다음 행동 |
|---|---|---|---|
| **제출 링크** | 9/28 18:08 Bricksum 메일: Google Form `https://docs.google.com/forms/d/e/1FAIpQLSf8DosHqhZOk6Zg4SXBfUuBYR9B0yJ8ZXepIMPCvGT74jUs_Q/viewform`, 9/29 21:00~9/30 12:00 KST, 필수 4종(공개 GitHub·영상≤3분·PDF≤10쪽·README 내 tx+Kiln 로그), 사전 제작 표기 필수 | 확인됨 | 사장 제출(별도) |
| 팀 ID | 메일 본문·첨부·Luma·Q&A 에 없음(로그인 ID team34@… 만) | **미확인** | 문의 초안 §3 |
| 현장 체크인 | 9/28 사장 보고 "arrived/checked in onsite" | 완료(사장 보고) | — |
| Telegram 체크인(team-XXX_Name) | 완료 기록 없음 | **미확인** | 팀 ID 확인 후 사장 손 |
| Solo Builder | Kiln 계정 메일 제목 "– Solo Builder" 발급(정황) | 정황 | 팀 변경 없으면 추가 통보 불필요로 보임 |
| Kiln 비밀번호 변경 | 9/28 19:50 콘솔 "coming soon"(당시 불가) | 미확인 | 콘솔 재확인(사장/부사장) |
| 공개 GitHub | 로컬만 | **미생성(승인 대기)** | §4 |

## 3. 문의 초안(미발송): `ORGANIZER_INQUIRY_DRAFT_20260929.md`

## 4. 공개 GitHub 제안(승인 대상)
사장 개인 계정 · `safebatch-ai-gwdc2026`(public) · 범위 `public_release_20260929/` 전체 · 첫 커밋부터 공개(기존 이력 없음, force-push 해당 없음) · `.gitignore`: `.env`, `guide/pending/*.json`, `guide/logs_server.txt`, `__pycache__/`.

## 5. 변경 없음·주의
- Kiln 제품 호출 0(누적 6), 송금·서명 0, 9/29 자문 Kimi1·Grok1·Gemini1(재시도 0). Gmail 읽기 2회(비밀번호 미기록). 안내 서버 재기동(PID 380). 임시 재생 서버 종료.
- 사용자 테스트(5~10분) 미실시 — `USER_TEST_PLAN_20260929.md`. 소수 테스트로 우위 주장 없음.
