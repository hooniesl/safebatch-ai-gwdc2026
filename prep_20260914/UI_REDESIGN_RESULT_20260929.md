# 휴대폰 UI 단순화 결과 · 2026-09-29 18:2x (전무 · 부사장 VP_UI_MINIMAL_REDESIGN 반영 · 검수용)

## 변경 요약(guide/phone.html 전면 재작성, guide/phone_client.js 에 화면 규칙 추가)
- **상태 하나·주 버튼 하나**: input(새 송금) → confirm(해석 확인: 큰 금액·수취인·전체 주소·발신 지갑·Nile·이 요청의 해석) → [수수료 확인] → presign(예상 수수료/상한/유효시간 별도 줄, 자세히 접힘) → [지갑에서 확인·서명] → processing(확정 확인 중) → done(송금 완료·실제 수수료·[영수증 보기]·**[새 송금]**) / unknown(결과 확인 필요·다시 보내지 마세요·[이어서 확인]).
- **[↻ 새로고침]**(상단 보조 버튼, aria-label): 지갑 상태·현재/보관 주문 status GET·최근 송금 GET 만. AI 호출·prepare·서명·서명본 POST·방송·hold 등록 없음. 연타 1회 처리, "조회 중…/갱신 시각/조회 실패 — 마지막 상태 유지". `location.reload()`·resume 미연결.
- **[새 송금]**: done(종결)·보관 주문 없음일 때만. 입력·제안·주문·결과·중복 안내 초기화, 지갑·최근 송금 유지, 입력칸 포커스. 호출·주문·서명·방송 0. unknown/서명 대기/보관 중에는 불가(토스트).
- **재서명 방지**: 서명 버튼은 `screen.canSign`(presign·현재 주문·같은 지갑)일 때만 활성, 핸들러도 같은 조건 재검사. 완료 뒤 이전 주문으로 실행 불가. `lockNewOrders(false)` 가 이전 주문 서명을 되살리지 않음(render 가 상태로만 결정).
- **늦은 응답·지갑 변경**: reqId 대조로 이전 chat/prepare 응답 무시, 다른 주문 결과는 현재 화면을 덮지 않음(resultBelongs), accountsChanged 시 이전 제안·주문 폐기 + 안내.
- **중복 15분 확인 유지**: dup 상태에서 [그래도 다시 보내기](활성화 결함 수정) 1개만 강조.
- **수수료 표기 통일**: `fee_known` 우선(없으면 fee_field_present) → 0 sun 도 "0 TRX"(미확인 아님), 근거는 영수증 상세.
- **AI 표시**: 연결 모드(health: Kiln 실호출 연결됨/규칙 모의)와 요청별 실제(실호출·call id / 폴백 / 재사용 / 모의) 분리.
- **긴 글자·목록**: `box-sizing:border-box`, grid `minmax(0)`, 주소/txID/주문ID `overflow-wrap:anywhere`, 일반 문장은 자연 줄바꿈. 최근 송금 = 흰 패널 하나·얇은 구분선 목록(상태 배지 · **금액** · 수취인 · 시각(created_at 있을 때만)) → 항목 탭/Enter 로 상세(전체 주소·수수료·txID·블록·탐색기·주문 ID + 복사 버튼). 색은 배지/상태 텍스트에만. `maximum-scale=1` 제거, safe-area padding, 버튼 48px.
- 음성: OS 키보드 받아쓰기 안내 한 줄만(가짜 마이크 버튼 없음).

## 검증
- jsc 모의 Q 21건(완료→서명 불가, 새 송금 초기화, 늦은 응답 무시, 지갑 변경 차단, unknown 잠금, NOT_SUBMITTED 무변화, fee_known 표기, 목록 행, AI 표시) + 기존 A~P 모두 PASS(`guide/tests/phone_client_mock_last.txt`). guide 250/250.
- 폭별 캡처·넘침 측정(모의 API·모의 지갑, 실제 호출 0): `evidence/20260929_phone_prototype/ui_redesign_1800/after/` — input/presign/done @320/360/390/430, confirm/dup/processing/unknown @360 → **모든 상태 scrollWidth ≤ innerWidth, 넘치는 요소 0**(`OVERFLOW_REPORT.txt`). 64자 txID·전체 주소·긴 주문 ID·긴 안내 포함(사장 캡처 재현 데이터). 변경 전 기준본 `before/`(phone.html·phone_client.js·사장 캡처 1000044056.jpg).
- 로컬 미리보기: `guide/tests/_preview_<state>.html`(생성기 `guide/tests/phone_ui_preview.py`).
- 실기기: A-V·A-T 는 구 UI 로 완료(기록 유지). I-V·I-T 는 새 UI 로 진행(버전 구분).

## 남은 것
- 실기기에서 [새 송금]·[새로고침]·목록 상세 열기/복사 확인(I-V/I-T 중 캡처). 다른 수취인 새 송금은 모의(Q)로만 검증(실송금 승인 범위 밖).
