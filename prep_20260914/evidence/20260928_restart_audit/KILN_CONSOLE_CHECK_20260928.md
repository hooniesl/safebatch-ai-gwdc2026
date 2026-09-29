# Kiln 계정 직접 확인 · 2026-09-28 (전무, 기존 Chrome, 비밀값 미출력)

- 18:08 KST 메일 `[GWDC 2026 × Bricksum] Kiln API Account – Solo Builder` 본문을 Gmail 탭에서 읽었다(ID/Password 값은 화면·로그·이 문서에 남기지 않음). 발신 Derrick / Bricksum Operations. **Credits: $100 (preloaded)**, 로그인 URL https://kiln.bricksum.com, 문서 https://kiln.bricksum.com/docs/en. "첫 로그인 후 비밀번호 변경" 요청 문구 있음 — **변경하지 않았다**(사장 결정 필요).
- **로그인 성공(이메일+비밀번호 폼, JS 주입, 값 미출력).** 콘솔 `/app`: 조직 **team34**, 현재 잔액 **$100.00 USD**, 최근 7일 요청 0, 이번 달 비용 $0.00. 크레딧 사용 기간/만료는 개요 화면에 표시 없음(→ 아래 /app/credits 확인 결과 참조).
- `/app/models`(콘솔 카탈로그, 10개): **qwen3-32b 서비스 중(가용성 100%, 추론·툴 콜, 컨텍스트 32.8K, $0.08/$0.28 per 1M)**, deepseek-v4.1-flash 서비스 중($4/$8), **gpt-oss-120b coming soon**, gpt-oss-20b/exaone-4.5-33b/k-exaone-236b/llama-3.1-8b coming soon. → 9/20 "coming soon" 이던 qwen3-32b 는 **오늘 서비스 중**. 과제 문서의 gpt-oss-120b 는 여전히 미제공(주최 9/20·9/23 회신대로 Qwen3-32B 사용).
- 공식 문서(docs/en/api-reference): `/chat/completions` 가 대화·**tool calling 지원**(`tools` array supported, `tool_choice` supported — 강제 tool_choice 는 200 을 주지만 모델에 따라 적용되지 않을 수 있음). 컨텍스트 131,072 상한(요청+출력), 429 rate limit, usage 필드에 cost 포함. 인증 `Authorization: Bearer sk-bk-…`, base_url https://api.bricksum.com/v1.
- 메일의 필수 조건(부사장 검수와 동일 확인): Kiln 사용 + 온체인 tx 없으면 실격, 사전 제작 README 구분(미공개 시 실격), 제출 9/29 21:00~9/30 12:00 KST Google Form, 공개 GitHub·README 실행법·영상≤3분·PDF≤10쪽·흐름별 tx 해시+Kiln 호출 로그, Top3 9/30 15:00, 주최에 프롬프트/스킬/harness 튜닝 도움 요청 시 실격 가능(계정 접근·장애만 지원).
- 이번 확인에서 **API 키 생성·추론 호출·비밀번호 변경·결제 없음.**
