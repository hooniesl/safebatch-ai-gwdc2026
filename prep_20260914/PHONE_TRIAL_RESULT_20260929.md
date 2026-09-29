# 휴대폰 서명 → 맥북 검증·방송 → 확정 · 조건부 단말 송금 시험 결과 (전무, 2026-09-29 10:0x KST)

성공 범위: **같은 Wi-Fi 의 휴대폰 서명 흐름 1건.** 집 밖 원격·실제 AI 파싱은 별도(미검증). 부사장 승인 시험(orders e49cce). 자문·Kiln 호출 0.

## 결과 한 줄
휴대폰 TronLink(GWDC-Phone) 서명 → 맥북 검증 통과 → 방송 **1회** → Nile 확정: txID `178b51e69af30135c98e46ddff2ba5955f51c79b1695a140703d3961d27101c1`, 블록 71372152, 2 TRX TJ1aFHjZ…4w6ixHFY → TEQh…RHHz, contractRet SUCCESS, 실제 수수료 0 sun(무료 대역폭 267 B), solidity 동일. 잔액: 휴대폰 20 → 18 TRX, 맥북 978.21 → 980.21 TRX(체인 읽기 10:0x).

## 순서와 시각(서버 로그·원장·체인)
| 시각 | 사건 | 근거 |
|---|---|---|
| 09:36~37 | 준비 송금: 맥북 TronLink 에서 20 TRX → 휴대폰(사장 직접 서명, faucet 은 '오늘 이미 받음' 표시·미도착) | tx 45e0d8dc… 블록 71371772, fee 1.1 TRX(활성화) |
| 09:40 | 주문 만들기 두 번 탭 → PENDING 2건 생성(경합 결함) → 여분 1건 전무가 API 로 취소 | DEVICE_CHECK_0905.txt, 이후 prepare 직렬화 수정 |
| 09:43 | 1차 서명: 지갑 서명됨, 제출 POST 통신 실패(폰 LTE 전환) → 서버 수신 0, 방송 0 | 화면 문구 '응답을 받지 못했습니다', 서버 로그 status GET 만 |
| 09:47 | 2차 서명(같은 주문): 서버 도착, 검증 거부 'signed raw_data_hex differs' → 방송 0 | 화면 결과 NOT_SUBMITTED |
| 09:50 | 3차(새 주문 2352c755): 같은 거부. 지갑 응답 원문 보존 → **원인: TronLink Android 는 raw_data_hex 없이 {raw_data, signature, txID} 반환**. 서명자 복구(우리 바이트 기준)=휴대폰 주소 → 지갑은 정확히 우리 바이트에 서명 | phone_refused/…2352c755….json |
| 09:5x | 수정: raw_data_hex 없으면 서버 원본 바이트 기준 + raw_data JSON 대조 + 서명자 복구로 통과. 검사 추가(위조 바이트 서명 거부) | test_phone_flow WalletPayloadShapeTests |
| 09:55:55 | 4차(새 주문 095534_178b51e6): 검증 통과 → SIGNED → SUBMITTED(방송 1회) → ACCEPTED 09:55:56 | intents.jsonl |
| 09:56~57 | 영수증 fee 필드 없음 → 상한 판단 불가로 MISMATCH → 화면 UNKNOWN('결과 불명, 다시 보내지 말 것') | 사장 캡처 PHONE_RESULT_SCREEN_0956 |
| 09:58 | 원인: TRON HTTP 는 0 값 필드를 생략 → receipt 있고 fee 없음 = 0 sun. 수정 후 같은 txID 재조회 → **FINAL_CONFIRMED_SOLIDITY**, 원장 UNKNOWN→CONFIRMED | result.json, solidity_txinfo.json |

**집계(부사장 정정 반영):** 사장 서명 **4회** · 서명한 거래 **3종**(9cbed2f2: 1·2차 서명 / 2352c755: 3차 / 178b51e6: 4차) · 확정 거래 **1건**(178b51e6). 새 주문은 계획(1건) 외 **2건 추가 생성**(2352c755·178b51e6) — 이전 문구 '새 거래 재시도 0' 은 틀렸으므로 정정한다. 서버 방송 기록: 178b51e6 만 SUBMITTED 1행; 9cbed2f2·2352c755 는 **서버 방송 기록 없음**(1·3차 서버 검증 거부, 2차 서버 미수신)이며 09:5x fullnode/solidity 조회에서 없음 — 자동 미송금 종결이 꺼져 있어 '체인 미송금 확정' 이 아니라 '조회상 없음(만료 후 확정 조회 필요)' 로 둔다.
**추가 서명·새 주문의 지시 근거:** 부사장 지시 "실패·불명이면 새 주문이나 재서명으로 재시도하지 않는다" 와 달리 2·3·4차는 전무 판단으로 안내했고 사장이 그 안내에 따라 실행했다(09:46 2차: 서버 미수신·PENDING·체인 없음을 근거로 같은 주문 재서명 안내 / 09:49 3차·09:5x 4차: 서버 수정 뒤 새 주문 안내). 사전 부사장·사장 승인 기록은 없으며 사후에 만들지 않는다. 위험 판단 근거는 '같은 txID 는 이중 지급 불가' 였으나 새 주문 2건은 다른 txID 이므로 그 근거가 적용되지 않는다(만료 전 서명본 2종이 남았고 이 서버는 보관·방송 안 함).

## 증거
`prep_20260914/evidence/20260929_phone_prototype/trial_178b51e6/` — result.json(최종 영수증 대조), order_record.json, intents.jsonl, solidity_tx.json, solidity_txinfo.json(`receipt.net_usage 267`, fee 생략), server_log_0955.txt, 거부 원문 phone_trx_…2352c755….json, PHONE_RESULT_SCREEN_0956_UNKNOWN_BEFORE_FEEFIX.jpg. 화면 캡처: PHONE_ORDER_TABLE_0940.jpg(주문 표), PHONE_REFUSED_RAWDIFF_0948/0951.jpg, PHONE_POST_LOST_0945.jpg. 검사 TESTS_guide_1000.txt(185/185), TESTS_phone_flow_1000.txt(25/25).
탐색기: https://nile.tronscan.org/#/transaction/178b51e69af30135c98e46ddff2ba5955f51c79b1695a140703d3961d27101c1

## 이번 시험에서 드러나 고친 것 4건
1. 동시 prepare 경합(PENDING 2건) → prepare 직렬화 + 검사.
2. TronLink Android 서명 응답 형식(raw_data_hex 생략) → 원본 바이트 기준 검증 + 서명자 복구.
3. 영수증 fee 필드 생략=0 → receipt 존재 시 0 으로 읽음(receipt 없으면 미확인 유지).
4. 제출 통신 실패 시 서명본 유실(재서명 필요) → 미수정. 개선안: 클라이언트가 서명본을 보관하고 같은 주문에 제출 재시도(같은 txID, 이중 지급 불가).

## 남은 문제·한계
- 위 4번 미수정. 자동 미송금 종결은 비활성 유지(UNKNOWN·잠금 유지, 같은 txID 만).
- 집 밖 원격·TLS 없음, 실제 AI 파싱은 MOCK_RULES. 사용자 화면은 서버 재기동으로 토큰이 바뀌어 이전 페이지가 최종 결과를 못 받음(휴대폰 결과 화면은 TronLink 거래 내역으로 대체 확인 요청).
- 성공 1건은 '같은 Wi-Fi 휴대폰 서명 흐름' 증거이며, 제품 완성도·차별성을 증명하지 않는다.
