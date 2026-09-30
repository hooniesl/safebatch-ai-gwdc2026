# 제출 준비 완료 검토 · v5.1 · 2026-09-30 (공개판)

이 문서는 공개 저장소용 기술 요약이다. 내부 원본은 제출 행정·검수 절차 등 운영 항목을 더 담고 있으며 저장소에 싣지 않는다. 이번 v5.1 작업에서 새 송금·서명·방송·서비스 재시작·접수 활성화는 **0**이다.

## 1. 공개 사본 재현 (보완 A)
- 방법: 공개 파일만으로 임시 배치를 만드는 재현 스크립트 `run_public_tests.sh`(저장소 루트, 실행 권한). 어댑터 `hani_bot_hook/gwdc_tg_adapter.py`는 운영본과 **바이트 동일**(SHA256 `6e2788d6…`), 코드 변경 없음. 스크립트가 `<tmp>/gwdc_tg_adapter.py` 사본과 `<tmp>/AI_CONTEST/gwdc_2026/`(저장소 **실제 복사**; 심볼릭 링크는 테스트의 `resolve()` 때문에 실패해 폐기)를 만들고 `SB_GUIDE_DIR`를 복사본 `guide/`로 고정한다. `.env`·`.git`·venv는 복사하지 않는다. 테스트 삭제·skip 0.
- README §2: 저장소 루트 기준 경로, `git clone … && cd safebatch-ai-gwdc2026`, venv, `pip install -r requirements.txt`, `./run_public_tests.sh`. 베어 실행(`guide/`에서 직접 `python3 -m unittest`) 시 283 OK + import 오류 4의 원인, `prep_20260914/evidence/`가 저장소에 포함돼야 하는 이유(`test_bandwidth_rule.py` 등이 기록 증거를 읽음)를 적었다.
- `requirements.txt`: `tronpy==0.6.2`, `protobuf==6.33.6`, `cryptography==46.0.7`(`phone_signer.py` LocalKeySigner 지연 import, `test_tg_signer_exec.py`가 사용).
- 검증(새 임시 디렉터리에 공개 사본 복사, 새 venv `/usr/bin/python3` 3.9.6, pip로 3개 설치, `PYTHON=<venv> ./run_public_tests.sh`): **guide 324/324 OK · safebatch 105/105 OK**, exit 0. 로그: `evidence/20260930_submission_prep/REPRO_fresh_venv_run_public_tests.log`(임시 경로만 `<TMPDIR>`/`<clone>`으로 치환). 로드 경로(로그 6~11행): `gwdc_tg_adapter` → `<TMPDIR>/…/gwdc_tg_adapter.py`, `phone_chat/phone_intent/phone_signer/phone_flow` → `<TMPDIR>/…/AI_CONTEST/gwdc_2026/guide/*.py`, 어댑터 GUIDE → 같은 `guide`. 전역 PYTHONPATH·운영 로그·토큰·원장 접근 없음.
- 발견·수정: 공개 사본에 있던 `guide/tests/phone_chat.py`(09-29 옛 파서 복사본, 소스 트리에는 없음)가 `HERE` 우선 sys.path 때문에 실제 `guide/phone_chat.py`를 가리고 있었다 → 삭제. 삭제 후 로드 경로가 `guide/phone_chat.py`로 바뀐 것을 로그로 확인.
- 사설 봇 음성핸들러 16/16은 내부 증거로 구분(README §4d, 봇 저장소 밖에서는 실행 불가 명시).

- 추가(09:05): 공개 사본 테스트 2개(`test_tg_approval.py`, `test_tg_signer_exec.py`)의 상수 `USER`가 소유자의 실제 Telegram 사용자 ID였다 → 자리표시 값으로 교체(테스트 전용 문자열, 어댑터 코드 변경 없음). 같은 venv로 `./run_public_tests.sh` 재실행 **guide 324/324 OK · safebatch 105/105 OK**, 로그 `evidence/20260930_submission_prep/REPRO_after_testid_mask_run_public_tests.log`.

## 2. 영상 자막 (보완 B)
- 장면 10 자막을 3줄로 축약(사실 유지: 휴대폰 4건 실제 Kiln, Telegram 봇 1건 규칙 해석, 완료 알림 수동 복구, 사진 = 아이폰 I-T). 장면 11 자막·나레이션에 "notice recovered manually" 추가. 나레이션 한 구절 삭제로 길이 확보.
- 최종 mp4: ffprobe **178.79 s**(≤180), H.264 1280×720 + AAC, 3,306,314 B. 프레임 확인: 장면 10 시작 2:14.5·중간 2:25·끝 2:35, 장면 11 시작 2:36·끝 2:57.5 — 자막 3줄 완전 표시. 장면 1~9는 변경 없음.
- 대본(`VIDEO_SCRIPT_20260929.md`): 장면 10 캡처 소스를 `trial4_1840/IT/IT_1_done_screen_2044.png`로 정정, 클립 표 갱신(s10 23.40 s, 합계 178.79 s), 렌더 이력 기록.

## 3. 사실 표현 통일 (보완 C)
| 항목 | 수정 |
|---|---|
| PDF 3쪽 | 한 문장을 둘로 분리: Phone prototype(Kiln tool call, 4 live runs) / Telegram variant(rule-based parsing, no Kiln call, 봇 지갑 서명, 1 run, notice recovered manually) |
| PDF 9쪽 ③ | "bot wallet signed and broadcast once" → "one signature with the bot wallet's local key, one broadcast by the MacBook server"(3쪽/9쪽 불일치 해소; 방송 주체는 맥북 서버) |
| README §4d 표 | "bot process picked up the fix at 04:00 restart" → 수정 파일 00:36 저장·04:00 예정 재시작으로 새 PID 관측·지연 import라 **로드·실행 미관측**, 사람 없이 완료 알림 전달도 미입증 |
| README §2 Secrets | TronLink 경로(§4~§4c)로 한정, §4d는 별도 봇 지갑 암호화 로컬 키 서명 예외 명시 |
| README §1 표 | 적용 범위 주석 추가(§4d는 텍스트 '승인' + 봇 로컬키, AI 열 미적용) |
| README §4 Kiln 로그 | "6 lines total" → 19줄 구성(3 안내화면 + 3 A흐름 + 4 실패 + 4 §4b + 4 §4c + 1 §4d) |
| README §3 도구 공개 | 개발 도구(Claude Code, Codex)·외부 검토 모델(GPT/Gemini/Grok/Kimi/Claude, 실제 기록 있는 것만)과 제품 추론(Kiln qwen3-32b)을 구분. §4d 사람 서명 = 자금 공급 tx뿐, 실행 tx는 봇 로컬키 서명 + 맥북 방송, '승인'은 채팅 메시지 |
| README §4c 서두 | 확정 4건 각 서명 1·방송 1, 새 Kiln 호출은 A-V·I-V·I-T, A-T 확정 실행은 재사용(호출 0), A-T·I-T는 깨끗한 첫 패스 아님 |
| README Live vs synthetic | 확정 8건 = 2+1+4+1 명시, 자금 공급 tx 0373528e 제외, "8 TRX"는 금액 합 |
| README 제목 §4d | "bot wallet signs and broadcasts" → "signs, MacBook server broadcasts" |

## 4. 외부 검토 지적 반영 (README §3에 공개한 검토 모델)
| 출처 | 지적 | 판정 | 조치 |
|---|---|---|---|
| Kimi | 복사 루프가 `.env`를 임시 배치로 복사할 수 있음 | 채택 | `.env`·`.env.*`·`.DS_Store` 제외 추가, 재실행 324/105 |
| Kimi | 어댑터 기본 GUIDE가 개인 절대경로 | 부분 채택 | 운영본 바이트 동일 유지(코드 미변경). 스크립트가 항상 `SB_GUIDE_DIR` 설정, README에 "기본값에 의존하지 말 것" 명시 |
| Kimi | 테스트가 `prep_20260914/` 증거를 읽는데 포함 여부 불명 | 채택 | README §2에 포함 사실·이유 명시 |
| Grok | Live/Synthetic 8건 집계가 6건으로 읽힘, 자금 공급 tx 혼동, "8 TRX" | 채택 | 2+1+4+1=8 명시, 자금 tx 제외, 금액 합 표기 |
| Grok | §3 "§4d 1건 봇 서명" — 사람 서명(자금)과 봇 서명(실행) 구분, '승인'은 채팅 | 채택 | 문장 교체 |
| Grok | §4c "각 건마다 Kiln 호출" — A-T 재사용, A-T·I-T 비청정 | 채택 | 서두 문장 교체 |
| Gemini | 3쪽 Telegram "MacBook broadcasts once"가 9쪽 "bot wallet broadcast"와 불일치 | 불일치 채택 / 방향 기각 | 실제 방송 주체는 맥북 서버(봇 지갑은 서명만) → 9쪽·README를 그쪽으로 통일 |
| Gemini | 장면 10 자막이 길어 잘릴 것(추측) | 기각(관측) | 최종 프레임 3장에서 3줄 완전 표시 확인 |
| Gemini | 장면 11 자막·나레이션에 '알림 수동 복구' 누락 | 채택 | 문구 추가, 길이 재조정 178.79 s |

## 5. 검사·해시
- 테스트: §1(새 임시·새 venv 324/105). 소스 트리 guide 324 / safebatch 105도 동일.
- PDF 10쪽. 9쪽은 넘침 3회 수정 뒤 확인.
- 최종 해시: `evidence/20260930_submission_prep/FINAL_SHA256.txt`(README 3본 동일·PDF·mp4·대본·어댑터·스크립트·requirements·재현 로그).

## 6. 남은 미확인 (정직 표기)
- 수정 어댑터 코드의 실제 로드·실행(접수 비활성이라 관측 없음) · 사람 없이 완료 알림 전달 미입증.
- A-V LTE·I-V 음성은 소유자 진술/캡처 기준. I-T 캡처는 Wi-Fi.
- 영상 178.79 s(여유 1.2 s).
