# 제출 준비 점검 v5 · 2026-09-30 (공개판 · 기술 항목만)

이전 점검표 v4.4는 `SUBMISSION_CHECK_20260929.md`(이력). 이 문서의 최종 갱신본은 `SUBMISSION_READY_REVIEW_20260930.md`(v5.1)이며, 수치가 다르면 그쪽이 우선한다. 이번 작업의 유료 호출·서명·방송·서비스 재시작·Telegram 발송은 **모두 0**.

## 0. 결론
- **04:00 자동재시작은 실제로 일어났고 수정본이 저장된 뒤 기동했다.** 실제 실행 관측은 없다(접수 비활성) → `RESTART_CHECK_0400_20260930.md`.
- **제출물 v5(README·PDF·영상·공개 사본)를 증거 범위대로 갱신했다.** 기존 Kiln/Furiosa A 3흐름(§4) · 휴대폰 1건+4건(§4b·§4c) · Telegram 봇지갑 1건(§4d)을 구분해 적었고, 미검증·수동 복구 사실을 그대로 남겼다.

## 1. 제출물 v5
| 제출물 | 경로 | v5 변경 | 검증 |
|---|---|---|---|
| README | `README.md` = `README_SUBMISSION.md` | §2 검사 324·phone 서버 실행줄·베어 클론 안내 · §3 "During event, continued" 행 + 도구 공개 · §4b 마지막 문장 정정 · **§4c 휴대폰 4건 표**(A-V/A-T/I-V/I-T, txID·블록·수수료·Kiln call·한계) · **§4d Telegram 봇지갑 1건 표**(자금·음성·승인·실행·규칙 해석·알림 수동 복구·이후 상태) · §5·§7 항목 추가 · §8 파일 목록 · Live/Synthetic 갱신(확정 8건, 인용 Kiln 12/19) | 3본 SHA 동일 |
| 발표 PDF | `submission/DECK_SafeBatchAI_FuriosaA_20260929.pdf` | 1쪽 4번째 불릿 · 3쪽 In 불릿 · **9쪽 = 휴대폰·텔레그램 4사실 + 아이폰 I-T 완료 화면(토큰 없음)** · 10쪽 한계·공개 범위(09-30 00:36까지, 도구 공개) | 10쪽. 9쪽 넘침은 축약 후 재렌더로 확인 |
| 데모 영상 | `submission/DEMO_VIDEO_SafeBatchAI_FuriosaA_20260929.mp4` · 대본 `VIDEO_SCRIPT_20260929.md` | 장면 10(휴대폰·텔레그램 시제품: ①1건 ②4건 ③봇지갑 1건) · 장면 11(증거·한계: 확정 2+5+1, 검사 324+105, 텔레그램 무인 미입증) · 장면 1~9 동일 | ffprobe ≤ 180 s(최종 v5.1: 178.79 s), H.264 1280×720 + AAC |
| 공개 사본 | 이 저장소 | 신규: `guide/phone_intent.py`·`phone_signer.py`, 테스트 6, `hani_bot_hook/`(어댑터·훅 발췌·음성핸들러 테스트), `kiln_calls.jsonl` 14→19줄, `kiln_raw` +5, trial4 AV/AT/IV/IT 증거, `20260930_bot_wallet_trial/`(키·설정·승인 원장 파일 제외), 보고 문서, 미사용 `guide/tests/phone_chat.py` 삭제 | 루트 배치 guide **324/324**·safebatch **105/105**(`run_public_tests.sh`) · 베어 실행 283 OK + import 오류 4(README §2 명시) · 비밀값 패턴 0, `.env`·phone_token·phone_session·bot_key·phone_wallets·*.pass 없음 |

## 2. 미확인·한계 (정직 표기)
- 새 알림 코드의 실제 실행(무인 완료 회신)은 미관측 — 접수 비활성이라 검증하려면 별도 승인·새 거래가 필요하다.
- A-V의 LTE, I-V의 음성 입력은 소유자 진술·캡처 기준. I-T 캡처는 Wi-Fi.
- 영상은 상한(180 s)에 약 1 s 여유. 재편집 시 나레이션 추가 금지.
- `hani_bot_hook/gwdc_tg_adapter.py`는 사설 봇 루트 기준 import를 쓰므로 베어 실행에선 4개 테스트 모듈이 import 오류(README §2 안내). 어댑터 파일은 운영본과 바이트 동일(해시 6e2788d6…).
