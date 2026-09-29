# Furiosa A 3흐름 요약 · 정상 / 적응 / 거절 · 2026-09-29 01:40 KST (전무, 부사장 검수용)

목표(공통): Nile 테스트 USDT를 **본인 지갑 TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz** 로 보내는 테스트넷 연습. 실제 거래소 입금이 아니며 거래소 안내 목표와 별개(제품상 연결은 별도 검수). 서명은 Codex(9/21 허용 범위), 실행기·검증은 코드. 수동 기술 검사 v4(9/28 23:28, 1 USDT, MANUAL_TECH_CHECK)는 AI 시연에서 제외.

| 항목 | ① 정상 (v5) | ② 적응 (v6 → v6b) | ③ 거절 (v7) |
|---|---|---|---|
| 사용자 요청(규칙) | 1 USDT 정확히, 예산 1.0, exact_amount, TRX 상한 5, 기한 06:55:33 | 예산 1.0→**0.6**, 최소 0.5, **max_within_budget**(감액 허용), 기한 07:17:50 | 1 USDT 정확히, 예산 **0.6**, exact_amount(**감액 불허**) |
| 코드가 한 일(모델 무관) | 후보 1개 계산(1 USDT, Energy 4,655,000+BW 345,000=5,000,000) → 모델 출력 형식 검증 → 재검증 → 지문 → 서명자 복구 → 방송 직전 재견적 → 영수증 4기준 | 전/후 diff(mode·budget·min·deadline) 계산, 후보 1개 = **0.6 USDT**(예산 안 조정은 코드), 이하 동일 | **후보 0 → 지급 불가 강제**(decline_reason "exact amount 1000000 + fee 0 exceeds budget 600000"). 미서명·주문·intent·승인 생성 없음 |
| AI(Kiln qwen3-32b)가 한 일 | AI_LIVE 00:56 call e851f510 (in 444/out 67): 후보 1개가 규칙에 맞는 이유 한 문장("지정된 수신 주소와 정확한 USDT 금액, TRX 수수료 한도 내… 유일한 옵션") | 01:18 call a5f4bba8 (in 813/out 61): "예산이 1.0 USDT에서 0.6 USDT로 줄어들었기 때문에, 후보는 최대 0.6 USDT를 보내는 것을 반영합니다." → 실행 단계 결함으로 그 flow 는 서명 전 종료 → **v6b 는 같은 판단을 이어받음(AI_CARRIED, 추가 호출 0, 서명 전 종료·실제 호출·원응답 재파싱·규칙/계획/지문 일치 검증)** | AI_LIVE 01:36 call df785a78 (in 319/out 53): "예산 0.6 USDT는 정확한 지급 금액 1 USDT보다 적어 결제가 불가능합니다." — **설명만**, 결정 권한·금액 제안 없음(형식 위반/다른 결정은 무시됨, 검사 있음) |
| AI 가 하지 않은 일 | 후보 생성·금액/주소/기한 변경·승인·서명·방송 | 금액 결정(0.6 은 코드), 승인·서명·방송 | 지급 여부 결정(코드가 이미 확정), 후보·금액 생성 |
| 사람 확인·서명 | 지문 ce741dfb… 대조 → Codex 서명 01:01:53 | 지문 dce84413… 대조 → Codex 서명 01:3x (전/후/이유 화면 표시) | 없음(서명 대상 없음) |
| 실행 결과(온체인) | tx `a7c6b7340fde03af6b9ac00158d916b1ec90595ed00c0e0ac548eea7e770ca24` 블록 71361508 **FINAL_CONFIRMED_SOLIDITY**, Transfer 1 USDT 자체 | tx `71a951bc63b73b3146f74d6b66b9b46b994be2f20a715ae57104acc0c83b4f88` 블록 71362119 **FINAL_CONFIRMED_SOLIDITY**, Transfer 0.6 USDT 자체 | **송금 0 · 주문 0 · 서명 0 · 방송 0**, 원장 불변(호출 전후 대조) |
| 실제 비용 | fee 345,000 sun = net_fee(과금 345 B 예측 일치), energy 14,650 소각 0 | 동일(345,000 sun) | 0 |
| 상한 대조 | 4기준 통과(≤5,000,000 / energy≤4,655,000 / net_fee≤345,000 / net_usage≤345) | 통과 | 해당 없음 |
| 증거 | PREPARE_ai_normal_v5.json · FLOW_ai_normal_v5.json · RESOLVE_ai_normal_v5.txt · EXECUTOR_ai_normal_v5.log · kiln_calls.jsonl(e851f510) | PREPARE_ai_adapt_v6.json · FLOW_ai_adapt_v6.json(실패, NOT_SUBMITTED) · FLOW_ai_adapt_v6b_reuse.json · RESOLVE_ai_adapt_v6b_reuse.txt · EXECUTOR_ai_adapt_v6b_reuse.log · kiln_calls.jsonl(a5f4bba8) | DECLINE_v7_precheck_no_call.json(호출 0 사전 검사) · DECLINE_ai_v7.json · kiln_calls.jsonl(df785a78) |
| a_demo_eligible | True | True (AI_CARRIED) | True |

모든 경로: `prep_20260914/evidence/20260928_nile_live/`. 원장: `guide/logs/nile_intents.jsonl`(v5·v6b CONFIRMED), `guide/logs/nile_approvals.jsonl`, `guide/logs/kiln_calls.jsonl`(6줄).

## 호출·비용
- Kiln 제품 호출 누적 **6**(9/28 3 + 9/29 3), 잔여 **0**. 추가 호출은 별도 승인. 이번 거절 흐름 실패 시 재시도 없음(성공했으므로 해당 없음). 유료 자문 호출 0.
- 실패 1건(v6 execute revision 지문 불일치)은 호출 1회를 소모했고, 재호출 대신 검증된 판단 이어받기로 처리했다(부사장 A 선택).

## 구분·한계(정직 표기)
- "AI 판단"은 ①②에서 후보 1개에 대한 **설명 + 선택 index**, ③에서 **거절 이유 설명**이다. 후보 계산·금액 조정·차단·검증·서명·방송은 전부 코드/사람이다. 데모에서 "AI 가 최적화/결정했다"고 말하지 않는다.
- ② 의 AI 기록은 원 호출(01:18)을 이어받은 것이며 v6b 를 위한 새 호출은 없었다(화면·기록에 AI_CARRIED·원 호출 ID 명시).
- 자체 전송(수취인 = 발신)이므로 잔액 순증가 없음. 에너지 14,650 이 지갑 TRX 소각 없이 처리된 원인은 미확인.
- /sign 최초 지갑 연결(권한 승인 팝업)은 미검증 — 이번 서명들은 기존 연결 상태. 연결 버튼 동작(eth_requestAccounts)은 기존 연결 상태에서만 확인.
- 거래소 안내(업비트→Binance)와 이 3흐름의 제품상 연결은 별도 검수 대상(PRODUCT_OPTIONS_REVIEW §4).
