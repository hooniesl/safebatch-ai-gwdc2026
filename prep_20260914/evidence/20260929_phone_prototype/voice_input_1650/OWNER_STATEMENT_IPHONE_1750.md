# 아이폰 키보드 한국어 음성 입력 확인 — 사장 진술 · 2026-09-29 17:5x KST
- 사장 보고(채팅): 아이폰(GWDC-iphone)에서 GWDC 입력창에 한국어 음성 입력 성공. 실제 인식문 **"MacBook 지갑에 트론 2개"**. 체감: 안드로이드보다 조금 더 잘 알아듣는다(정량 비교 아님).
- 표기: **아이폰 키보드 음성 입력으로 텍스트 작성 확인**. 요청 버튼·AI·주문·서명·송금 없음(서버 로그에 /api/chat 없음 → 전무 17:5x 확인). 캡처 파일 미수신.
- 인식 차이: 영문 혼용("MacBook"), 띄어쓰기("MacBook 지갑"), 조사 "에". 대응: 사장 지시로 등록 별칭 `MacBook지갑`(+`맥북 지갑`, `MacBook 지갑`)을 맥북지갑 항목에 **명시 등록**(주소·확인 상태 불변), 조사 '에' 를 등록 별칭 앞에서만 수취인 조사로 인정, 영문 대소문자 무시. 미등록("MacBook", "Mac 지갑")은 질문.
- 검사: PARSE_IPHONE_VOICE_BEFORE_1750.txt(수정 전 질문) → PARSE_IPHONE_VOICE_AFTER_1755.txt, tests/test_phone_chat_voice.py IphoneVoiceText.
