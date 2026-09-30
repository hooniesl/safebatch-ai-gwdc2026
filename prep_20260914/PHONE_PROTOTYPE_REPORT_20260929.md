# GWDC 9/29 첫 실행 보고 — 휴대폰 서명·맥북 처리 경로 최소 시제품 (전무, 04:40 KST)

지시: 사장 전달 부사장 지시 "[GWDC 9/29 첫 실행 지시]". 방향 = 휴대폰 대화 요청 → 휴대폰 최종 승인·서명 → 맥북 검증·처리 → 휴대폰 결과. 이번 범위 = 서명·처리 경로 최소 시제품 + 가능한 검증 + 대화·별칭 연결.
증거 폴더: `prep_20260914/evidence/20260929_phone_prototype/`. 실서명·방송·유료 자문 호출·Kiln 호출 모두 0.

## ① 실제로 된 것 / 미검증 / 막힌 것

**된 것(전무 직접 관측)**
- 공식 문서 확인(9/29 04:1x, 읽기 전용): TronLink 모바일 딥링크 `tronlinkoutside://pull.activity` 의 sign 액션은 **콜백에 transactionHash 만 돌려주고 TronLink 가 직접 방송**하며 callbackUrl 은 HTTPS 필수 → "맥북이 방송 전에 검증" 조건에 맞지 않아 제외. TronLink 앱 DApp 브라우저는 tronWeb/tronLink 주입(v4.3.4+, 공식 통합 문서). dApp 다중 서명 API `tronWeb.trx.multiSign(tx, undefined, permissionId)`. TRON 권한: Owner(0)/Active(2~9)/임계값·가중치, 권한 변경 `AccountPermissionUpdateContract` **100 TRX**(Nile getchainparameters 9/29 읽기: getUpdateAccountPermissionFee=100 TRX, getMultiSignFee=1 TRX, 신규 계정 활성 1 TRX+0.1 TRX, 대역폭 1000 sun/B).
- 선택한 첫 경로 구현: **휴대폰 TronLink DApp 브라우저 → 맥북 LAN 서버(8791) → 휴대폰 지갑 단일 서명(`tronWeb.trx.sign`, 방송 아님) → 맥북 검증·방송·같은 txID 조회 → 휴대폰 결과 조회.** 맥북은 서명하지 않는다.
- 대화·별칭 연결: "영훈이한테 트론 2개" → TRX 2개·별칭 '영훈' 으로 이해하되 **미등록이므로 질문**(추측 없음). 동명이인('민수' 2건)·미확인 주소('지연')·자산/수량 누락·상한 초과도 질문. 등록·사장 확인 별칭('맥북지갑'=TEQh…RHHz)만 제안. 규칙 기반 모의 응답이며 화면·응답에 `MOCK_RULES`(실제 AI 호출 아님) 표시.
- 합성 검사 13건 신규 통과(변조·다른 서명자·만료·내용 변경 재승인·중복 실행 차단·UNKNOWN→같은 txID 조회·노드 거절·체인 실패·수수료 상한 초과·HTTP 경계) + 자문 반영 6건(05:05) = 19건. 기존 검사 회귀: guide 179/179(기존 160+신규 19), safebatch 105/105.
- LAN 서버 실기동(PID 10241, `*:8791`, 04:24:43) + 실제 Nile 노드 읽기 prepare 1회: 미서명 TransferContract 133 B, 독립 디코드 19/19, 견적(과금 대역폭 267 B → 최악 0.267 TRX, 상한 2 TRX). 검사용 공개 키 주소로 만든 주문이므로 서명 없이 취소(CANCELLED). 토큰 없는 경로 404·다른 Host 403 확인.

**미검증**
- 실제 Android TronLink DApp 브라우저에서 LAN `http://` 페이지에 tronWeb 이 주입되는지, 서명 확인 창이 뜨는지, 서명본이 서버 검증을 통과해 방송·확정되는지(휴대폰 단말 시험 0회).
- GWDC-Phone 지갑의 Nile 설정·전체 주소·잔액(화면상 0 → Nile TRX 없으면 prepare 가 '잔액 부족'으로 거절).
- 집 밖 원격: 미구현. 맥북에 tailscale/ngrok/cloudflared 없음, 기존 승인된 원격 수단 기록 없음(MACBOOK_BASELINE 에 해당 항목 없음). 같은 Wi-Fi 시험만 가능.
- 실제 AI(Kiln qwen3-32b) 의도 파싱: 잔여 0 → 미호출, 모의 파서로 대체.

**막힌 것**
- (04:4x 시점) 자문 호출은 `prompt_review.py record` 의 사장 프롬프트 카드(--shown) 요구로 대기 → **04:5x 사장 카드 승인 후 Grok·Kimi 각 1회 발송·반영 완료**(§④). 게이트는 카드 질문문에 요청서 지문이 있어야 통과했다(첫 카드는 지문 없이 냈다가 재발급).
- Gemini(작동 증거·심사요건 검토): 실제 단말 작동 증거가 아직 없어 호출하지 않음(횟수 채우기 금지).

## ② 선택한 서명·통신 구조와 보안상 한계

| 구조 | 서명 주체 | 맥북 현장 서명 | 체인 권한 변경 | 상태 |
|---|---|---|---|---|
| **A. 휴대폰 단일 서명(이번 구현)** | 휴대폰 지갑 키 = 보내는 계정 | 없음(검증·방송·조회만) | 없음 | 구현·합성 검사 통과·단말 미검증 |
| C. 맥북 계정에 휴대폰 키를 Active 권한으로 추가 | 휴대폰 키 1개(임계값 1) | 권한 변경 1회만(맥북 Owner 서명, 100 TRX) 이후 없음 | 있음 | 후보(미실행). 계약 종류만 제한, 금액 제한 없음 |
| B. 2-of-2 다중 서명(휴대폰 키+맥북 키) | 둘 다 매 거래 | 맥북 TronLink 면 팝업 필요→**핵심 조건 미달**; 맥북 소프트웨어 키면 키 파일 보관 문제 | 있음(100 TRX) + 거래당 1 TRX | 후보(미실행). 분실 복구 없이는 부적합 |

- A 의 차이: 휴대폰 서명만 있으면 끝난다. 맥북 검증(수취인 등록·수량·수수료 상한·만료·중복)은 **우리 서버를 거칠 때만** 적용되고 체인이 강제하지 않는다. 휴대폰 지갑 앱에서 직접 보내는 송금은 못 막는다.
- 서버 침해 시 한계(9/29 VP 정정): 서버는 새 서명을 만들 수 없지만, **이미 받은 서명본은 만료 전까지 유효한 송금이라 침해된 서버가 방송할 수 있다**(우리가 격리해도 체인은 막지 않음). 또 침해된 서버(또는 같은 Wi-Fi 의 변조자, TLS 없음)는 **화면에 보이는 수취인·수량을 바꿔** 사용자가 지갑 창과 대조하지 않으면 다른 내용에 서명하게 할 수 있다. 방어는 사용자가 지갑 앱 확인 창의 주소·수량을 화면과 대조하는 것뿐이다. '거부·지연만 가능' 이라는 이전 표현은 틀렸다.
- 통신: 같은 Wi-Fi `http://192.168.0.251:8791/p/<실행마다 새 토큰>/`. 로그인·쿠키 없음, 토큰 없으면 404, Host/Origin 허용 목록, JSON 64KB. TLS 없음 → 같은 Wi-Fi 의 도청자가 토큰·주문 내용을 볼 수 있으나 서명 없이는 자금 이동 불가. 인터넷 공개·포트포워딩 없음.
- 기존 검증 규칙 이식: raw 바이트 동일·txID 재계산·서명 1개·서명자 복구=보내는 계정·만료·방송 직전 재견적·방송 예약(txID 잠금, 같은 계정 미해결 시 차단)·방송 1회·영수증(raw 동일·contractRet·수수료≤상한·수취인/수량 재디코드·solidity 동일)·UNKNOWN 은 같은 txID 조회만.
- 내용 변경 → 새 주문·이전 PENDING 취소·지문 불일치로 늦은 서명 거부. 서명 저장 뒤 취소는 "제출 안 함" 만 보장(서명 자체 무효화 불가).

## ③ 실행 방법·변경 파일·검사·근거

실행(이미 기동 중, PID 10241): `python3 guide/phone_server.py --host 0.0.0.0 --port 8791 --advertise 192.168.0.251` → URL(토큰 포함)은 `guide/logs/phone_session.json`(0600) 에만 있다. 휴대폰: TronLink 앱 → Nile → DApp 브라우저 주소창에 그 URL → [지갑 연결] → 대화 → [주문 만들기] → [휴대폰 지갑에서 서명].
검사: `(cd guide && python3 -m unittest discover -s tests)` 173 · `(cd safebatch && …)` 105 · `python3 -m unittest tests.test_phone_flow -v` 13.

변경/신규 파일
- 신규 `safebatch/trx_tx.py`(TRX TransferContract 작성·독립 디코드·서명 검증·견적·영수증), `guide/phone_chat.py`(대화 파서·별칭, MOCK_RULES), `guide/phone_contacts.json`(별칭 목록), `guide/phone_flow.py`(주문 수명주기), `guide/phone_server.py`(LAN 서버), `guide/phone.html`(휴대폰 화면), `guide/tests/test_phone_flow.py`(13건).
- 수정 `guide/order_store.py`(kind `nile_trx` 검증·소비 분기, 사본 `/tmp/order_store_before_20260929.py`), `safebatch/intent_log.py`(reserve_broadcast 에 scope 인자, 기본값 기존 유지).
- 기존 안내 서버(8765, PID 1688)·주문·기록·제출물·public_release 는 손대지 않음. 포트 8765 는 다른 프로세스(PID 740, stem_server.py)가 `*:8765` 로도 듣고 있어 휴대폰용은 8791 사용.

근거: `evidence/20260929_phone_prototype/` — LIVE_LAN_CHECKS_0426.txt, LIVE_NODE_PREPARE_0428.txt, TESTS_*_0435.txt, phone_server_log_0435.txt, phone_intents_0435.jsonl. 서버 로그 `guide/logs/phone_server.log`, 주문 `guide/pending/phone_trx_*.json`, 원장 `guide/logs/phone_intents.jsonl`.

## ④ AI별 오늘 누적·잔여(원장 대조)와 반영

비용 원장(avengers_logs) 9/29 행 대조(전무 직접, 04:1x): Kimi 02:20:32 kimi-k3(동결 52eaad1f) · Grok 02:23:10 grok-4.7(2d6732d3) · Gemini 02:25:17 gemini-pro-latest(72e4be3c). 실패·재시도 행 없음. 9/28 행(Kimi1/GPT2/Grok2/Gemini2/Claude 실패2)은 별도 일자.
이번 세션(04:5x~05:0x, 사장 카드 "승인 — [02f86e15fa97d04a][3317d133150dac4e] 2건 그대로 보내라"): Grok 1회(grok-4.7=실제 모델, 동결 a470ad8f, 3,135/1,525 토큰 +추론 22,516, cost_in_usd_ticks 1,487,880,000) · Kimi 1회(kimi-k3, 동결 001a85ed, 1,933/3,543). 재시도 0.
→ **9/29 누적 Kimi 2 · Grok 2 · Gemini 1, 잔여 3·3·4.** Kiln 누적 6·잔여 0·호출 0. Grok 최신 모델 공식 재확인 04:3x docs.x.ai: grok-4.7 flagship 유지.
반영 내용(상세 `ADVISOR_REVIEWS_PHONE_20260929.md`): Grok 7건 중 채택 5(격리·철회 뒤 같은 계정 잠금(만료+60s), UNKNOWN 종결은 노드 블록 시각 만료+2회 연속 NOT_FOUND 만, 같은 전송 15분 내 재확인([그래도 다시 보내기]), ACCEPTED→NOT_FOUND 는 UNKNOWN 강등, fee 필드 누락 불합격) · 기각 2(취소 경합·주소 표기 — 기존 동작을 검사로 증명). Kimi 8건 모두 문구 채택. 검사 phone_flow 19/19, guide 179/179, safebatch 105/105. 서버 05:07 재기동(새 토큰).

## ⑤ 사장 손이 필요한 다음 행동 하나

휴대폰 TronLink 앱에서 **Me → 네트워크 Nile 전환 → DApp 브라우저(Discover) 주소창에 맥북 URL(`guide/logs/phone_session.json` 의 url, 같은 Wi-Fi) 입력 → [지갑 연결] → 상태줄("연결됨 · 계정 · 노드")이 보이는 화면 캡처 전달.** 복구문구·개인키 화면은 보내지 않는다. 이 한 번으로 '모바일 주입·네트워크·통신' 미검증 3건이 해소된다. (그다음: 휴대폰 주소로 Nile faucet TRX 받기 → "맥북지갑한테 트론 2개" 실서명 1회.)

## ⑥ 부사장 판단 쟁점

1. **프롬프트 카드 규칙 vs 오늘 '허가 재질문 금지'**: 게이트는 요청서 최종본 카드(`--shown`)를 요구한다. 선택지 (a) 카드 1장에 Grok·Kimi 요청서 2건을 함께 보여 확인받고 발송 (b) 오늘은 자문 없이 진행. **추천 (a)** — 반례 검토는 실서명 전에 가치가 크고 비용 소액.
2. **시연용 서명 구조**: A(휴대폰 단일 서명) 로 9/30 12:00 전 실증 → 발표에서는 C(Active 권한) 를 '다음 단계' 로만 설명. B(2-of-2)는 맥북 현장 서명 조건 미달·복구 미설계로 이번엔 제외. **추천 A 실증 우선**, C 는 권한 변경(100 TRX·맥북 Owner 서명) 승인 뒤에만.
3. **Gemini 호출 시점**: 실제 단말 캡처·1회 실서명 증거가 생긴 뒤 심사요건·시연 설명 검토. 지금은 보류 추천.
