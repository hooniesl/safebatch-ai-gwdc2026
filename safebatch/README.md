# SafeBatch AI · 로컬 검증 코어 (2026-09-15)

접속 대기 중 외부 연결 없이 만든 최소 기능. Python 3.9 표준 라이브러리만 사용, 설치 없음.

- `csvcheck.py` — CSV(recipient, amount, memo) 검증. 원본 행번호(물리 줄번호) 유지, 금액은 문자열→정수 최소단위(6자리) 변환으로 부동소수점 미사용, 누락·형식 오류·TRON 주소 체크섬 오류·중복 수신자 의심 표시, 상태별 건수·지급 가능 합계.
- `policy.py` — 지급 전 검사. 같은 payment_id 재요청은 첫 기록 반환(REPLAY)·내용 다르면 CONFLICT, 허용목록 밖 수신자 차단, 지급액+수수료 예약액이 남은 예산 초과 시 차단, 통과 시 `ACCEPTED_FOR_APPROVAL`(사람 확인·서명 대기, 전송 아님). 모든 결정은 사유코드와 함께 로그.
- `flow.py` — `run_batch(csv_text, policy, batch_id)`: CSV 검증 → OK 행만 지급 후보 → 정책 검사 → `ACCEPTED_FOR_APPROVAL`(사람 승인 대기)에서 종료. payment_id는 `batch_id:행번호`로 결정론적이라 같은 요청 재실행은 REPLAY로 끝난다. 결과 JSON에 모든 행의 행번호·메모·금액·판정·사유와 결정 로그를 담는다.
- `tests/test_local.py` — 합성 데이터 단위검사 20건(회귀 5건 포함). 대응 명세 C05 C07 C08 C09 C10 C27 C30 C33.
  헤더·CSV 구문 오류는 `REJECTED_INVALID_CSV`로 반환하며 배치 등록·행별 정책 처리를 하지 않는다. 응답의 `decision_log`·`batch_log`는 내부 기록과 독립된 사본이다.
  배치 변경 입력 차단: 최초 batch_id와 CSV sha256을 `policy.register_batch`로 보관하고, 같은 batch_id로 다른 CSV(빈 줄·순서·금액·행 추가 등 바이트 차이)가 오면 행별 처리 전 `REJECTED_BATCH_CONFLICT`로 전체 거부한다. 기존 기록·예약액·승인 대기는 그대로다. 헤더 오류 입력은 등록하지 않는다.
- `tests/test_flow.py` — 흐름 연결 4건 + 배치 차단 회귀 5건(빈 줄·순서 변경·금액 변경+행 추가·동일 CSV REPLAY·헤더오류 미등록). 표본 결과 `tests/SAMPLE_RESULT_20260915.json`.
- C29(로컬): `revise_batch(csv_text, policy, batch_id, base_revision, request_id)`가 명시적 수정 경로다. 접수되면 이전 revision의 승인 대기·모의 승인을 SUPERSEDED/INVALIDATED로 바꾸고(이력 보존) 그 배치의 예약액만 해제한 뒤 수정본을 재검사해 통과 행만 재예약한다. 무효 CSV도 revision으로 접수되며 새 예약은 0. 같은 request_id 재시도는 REVISION_REPLAY, 오래된 revision·동일 내용·미등록 배치는 거부. `approve_batch(policy, batch_id, revision, csv_sha256)`는 현재 revision·해시와 일치할 때만 유효한 **로컬 모의 승인**이다. payment_id는 `batch_id:r<revision>:행번호`.

## 9/28 추가 · GasFree 연결 계층 (서명·송금 없음)

- `gasfree_client.py` — GasFree Open API(Nile 전용). 헤더 `Timestamp` + `Authorization: ApiKey {key}:{base64(HMAC-SHA256(secret, METHOD+PATH+TS))}`(docs.gasfree.io §5). 키는 환경변수/`.env`에서만 읽고 출력하지 않는다. 기본 전송은 curl(시스템 python3.9 LibreSSL 2.8.3 이 이 호스트와 TLS 실패). 재시도 없음, 전송 오류는 예외(UNKNOWN). `tokens/providers/address/trace/submit`.
- `tools/gasfree_probe.py` — 읽기 전용 인증 조회 3건을 evidence JSON 으로. 9/28 결과: **401 "Apikey not found."**(키 미인식).
- `gasfree_order.py` — 로컬 승인 행 → PermitTransfer typed data(Nile chainId 3448148188, verifyingContract THQG…). `build_order` 서명 직전 재검증 9종(revision·CSV 해시·승인·행 일치·allowSubmit·토큰 지원·잔액−frozen·수수료·금액), `human_summary` 와 서명 메시지는 같은 Order. `verify_signed` 는 되돌아온 message 바이트 대조·deadline 확인. `submit_and_track` 은 전송 오류를 UNKNOWN 으로(재제출 없음), 제공자 거절은 REJECTED, 수락 후 trace 로 SUCCEED/FAILED. `reconcile` 은 trace(txnHash·txnAmount·txnTotalFee)를 원본 행·메모·승인과 대사. `resolve_unknown` 은 조회로만 푼다.
- `intent_log.py` — append-only JSONL 지급 intent 원장. 전이 DRAFTED→AWAITING_HUMAN→SIGNED→SUBMITTED→UNKNOWN/ACCEPTED→CONFIRMED/FAILED/REJECTED. 같은 EOA nonce 잠금, UNKNOWN/ACCEPTED/CONFIRMED 재제출 거부(`can_submit`). 손상 행은 세어 보고.
- `export.py` — 배치 하나의 단일 export(JSON) + CSV 대사표: 모든 행의 분류·사유, 승인 범위, 차단, 사람 중단, intent 이력, 요청ID→traceId→txHash→실제 수신액/수수료. `replay_check` 는 export 만으로 금액 일치·차단 사유·승인 revision 을 재계산.
- 검사: `tests/test_gasfree_order.py` 16건, `tests/test_intent_export.py` 8건, `tests/test_restart_fixes.py` 19건(가짜 응답, 네트워크 없음).
- 9/28 GPT·Grok 자문 반영: 승인은 UI 가 보여준 화면 지문(`displayed_sha256`)을 받아 대조. 제출 직전 policy 재검증·MOCK 무조건 거절·본문 재생성 대조·deadline 재검사. 수수료는 행 예약값 기준, decimal=6·주소 체크섬·nonce 엄격. intent 원장은 fsync·손상 시 fail-closed·flock 원자 예약(`reserve_submit`)·계정 단위 OPEN 잠금(scope 포함). 확정은 저장된 traceId 조회 + `reconcile` RECONCILED 일 때만. 400 은 문서화된 사유 9종만 REJECTED. replay 는 중단/차단/외부 제출을 실패로. 검사 `tests/test_gpt_findings.py` 14건, `tests/test_grok_findings.py` 9건 → 전체 105.
- 9/28 재점검 수정(R1~R5): `approve_batch(..., confirmed_by=…)` 만 HUMAN_CONFIRMED(화면 지문 `display_digest` 고정) — `build_order` 기본값은 이 승인만 받는다. Order 스냅샷 지문·domain/types 대조(서명자 복구는 지갑/provider 책임, 미구현). `reconcile` 은 nonce·traceId·user 일치 필수. provider 응답 필수값을 기본값으로 채우지 않음, 정책 수수료 예약액 ≥ provider maxFee. `submit_and_track` 은 intent_log 필수, 5xx/비JSON 은 UNKNOWN.

실행:

```
cd AI_CONTEST/gwdc_2026 && python3 -m unittest safebatch.tests.test_local safebatch.tests.test_flow safebatch.tests.test_gasfree_order safebatch.tests.test_intent_export -v
python3 safebatch/tools/gasfree_probe.py --owner <EOA> --out <evidence.json>
```

아닌 것: Kiln 연동, TronLink 서명, 송금, 실제 수수료, GasFree 인증 성공. `fee_units`는 사람이 준 상한 추정값이다. 로컬 통과는 서비스 연동 성공이 아니다.
