"""휴대폰 AI 파싱 계층(모의 Kiln) 검사 — 실호출 0. 정상 / 적응(예산·기한 변경) / 거절(명백한 제약 위반) / 정보 확인(미등록 별칭은 거절 아님) / 출력 검증·폴백."""
import pathlib
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import phone_ai as AI  # noqa: E402

MAC = "TEQh4L9pabnbW4UpHmxXveY31Q3FLsRHHz"
CONTACTS = [{"alias": "맥북지갑", "aliases": ["맥북"], "address": MAC, "network": "nile", "confirmed_by_owner": True},
            {"alias": "민수", "address": None, "confirmed_by_owner": False}, {"alias": "민수", "address": None, "confirmed_by_owner": False},
            {"alias": "나", "address": "TJ1aFHjZsTyDtpUkwHXkPEC8Ay4w6ixHFY", "network": "nile", "confirmed_by_owner": True, "self_wallet": True}]


class MockKilnFlows(unittest.TestCase):
    def test_normal_exact_amount_not_lowered(self):
        r = AI.decide("맥북지갑한테 트론 2개 보내줘", CONTACTS)
        self.assertEqual((r["kind"], r["kind_detail"]), ("proposal", "normal")); self.assertEqual(r["proposal"]["amount_trx"], "2"); self.assertEqual(r["proposal"]["address"], MAC)
        self.assertEqual(r["ai"]["mode"], "MOCK_KILN"); self.assertEqual(r["ai"]["calls"], 0)

    def test_adapt_on_explicit_budget_or_deadline_change(self):
        r = AI.decide("맥북지갑한테 트론 2개, 예산 5 트론 10분 안에", CONTACTS)
        self.assertEqual((r["kind"], r["kind_detail"]), ("proposal", "adapt")); self.assertEqual(r["proposal"]["amount_trx"], "2")   # 수량 유지
        self.assertEqual(r["adapt"]["budget_trx"], "5"); self.assertEqual(r["adapt"]["deadline_minutes"], 10); self.assertIn("다시 확인·서명", r["next"])
        # 예산이 수량+상한보다 작으면 감액하지 않고 거절
        d = AI.decide("맥북지갑한테 트론 2개 예산 1", CONTACTS)
        self.assertEqual((d["kind"], d["reason_code"]), ("decline", "BUDGET_BELOW_REQUEST")); self.assertFalse(d["order_created"]); self.assertIn("임의로 낮추지", d["explain"])

    def test_decline_only_on_clear_violations(self):
        d = AI.decide("맥북지갑한테 트론 1000개", CONTACTS); self.assertEqual((d["kind"], d["reason_code"]), ("decline", "OVER_PER_REQUEST_CAP"))
        d = AI.decide("맥북지갑한테 USDT 2개", CONTACTS); self.assertEqual((d["kind"], d["reason_code"]), ("decline", "UNSUPPORTED_ASSET"))
        d = AI.decide("나한테 트론 2개", CONTACTS); self.assertEqual((d["kind"], d["reason_code"]), ("decline", "SELF_TRANSFER"))
        for d in (AI.decide("맥북지갑한테 트론 1000개", CONTACTS),):
            self.assertFalse(d["order_created"])

    def test_unregistered_alias_is_info_check_not_decline(self):
        r = AI.decide("영훈이한테 트론 2개", CONTACTS)
        self.assertEqual((r["kind"], r["kind_detail"], r["reason"]), ("question", "info_check", "UNREGISTERED_ALIAS"))
        r = AI.decide("민수한테 트론 2개", CONTACTS); self.assertEqual((r["kind"], r["reason"]), ("question", "AMBIGUOUS_ALIAS"))

    def test_model_output_validation_and_fallback(self):
        self.assertIsNone(AI.validate_model_output('{"alias":"x","amount":"2","asset":"TRX","address":"Txxx"}')[0])   # 주소 필드 금지
        self.assertIsNone(AI.validate_model_output('{"alias":"x","amount":"-1","asset":"TRX"}')[0])
        self.assertIsNone(AI.validate_model_output('{"alias":"x","amount":"2","asset":"TRX","change":{"foo":1}}')[0])
        self.assertIsNone(AI.validate_model_output('not json')[0])
        self.assertIsNotNone(AI.validate_model_output('{"alias":"맥북지갑","amount":"2","asset":"TRX","change":{"budget_trx":"5"}}')[0])

        class Bad:
            provider = "MOCK_KILN"; model = "qwen3-32b(mock)"
            def structure(self, text): return {"raw": '{"alias":"맥북지갑","amount":"1","asset":"TRX","address":"Tzzz"}', "calls": 0}
        r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=Bad())
        self.assertEqual(r["kind"], "proposal"); self.assertEqual(r["proposal"]["amount_trx"], "2"); self.assertIn("규칙 파서", r["ai"]["fallback"])   # 검증 실패 → 규칙, 수량 2 유지

        class Lower:
            provider = "MOCK_KILN"; model = "qwen3-32b(mock)"
            def structure(self, text): return {"raw": '{"alias":"맥북지갑","amount":"1","asset":"TRX"}', "calls": 0}
        r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=Lower())
        self.assertEqual(r["proposal"]["amount_trx"], "2"); self.assertIn("불일치", r["ai"]["fallback"])                                   # 모델이 1로 낮춰도 코드는 2 유지


def _usage(completion: int, reasoning: int, prompt: int = 380):
    return {"completion_tokens": completion, "completion_tokens_details": {"reasoning_tokens": reasoning}, "prompt_tokens": prompt,
            "total_tokens": prompt + completion, "cost": 5.584e-05}


def _resp(**kw):
    base = {"ok": True, "http": 200, "content": None, "tool_calls": None, "reasoning_content": None, "usage": _usage(30, 30), "model": "qwen3-32b",
            "request_id": None, "elapsed_ms": 1, "error": None, "finish_reason": "stop", "call_id": "t3st0001"}
    base.update(kw); return base


ARGS_NORMAL = '{"alias":"맥북지갑","amount":"2","asset":"TRX","change":null,"missing":[],"reason":"ok"}'


class KilnLiveRecovery(unittest.TestCase):
    """9/29 11:28 실호출 4건 실패(모두 completion_tokens == reasoning_tokens, finish stop, tool_calls 없음 → 'not json' 폴백)의 모의 재현과 수정 검증. 실호출 0."""

    def _live(self, resp):
        seen = {}
        def fake_chat(messages, **kw):
            seen["messages"], seen["kw"] = messages, kw; return resp
        return AI.KilnLive(chat_fn=fake_chat, flow_id="phone_test"), seen

    def test_request_shape_matches_0928_success_path(self):
        live, seen = self._live(_resp(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": ARGS_NORMAL}}]))
        live.structure("맥북지갑한테 트론 2개")
        self.assertEqual(seen["kw"]["tool_choice"], "auto")                                     # 함수명 강제(9/29 실패 형태) 아님
        self.assertTrue(seen["messages"][0]["content"].startswith("/no_think "))                # 9/28 성공 형태: system 맨 앞
        self.assertNotIn("/no_think", seen["messages"][1]["content"])                          # user 끝에 붙이지 않음
        self.assertEqual(seen["kw"]["max_tokens"], 300); self.assertEqual(len(seen["kw"]["tools"]), 1)

    def test_replay_0929_failure_shape_is_fallback_with_diagnosis(self):
        # 저장 로그와 같은 형태: 도구 호출 없음·본문 없음·reasoning 만 소비. 수정 뒤에도 '성공'으로 둔갑하지 않고 원인 문구를 남긴다.
        live, _ = self._live(_resp(usage=_usage(92, 92), finish_reason="stop"))
        r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=live)
        self.assertEqual(r["kind_detail"], "fallback"); self.assertEqual(r["ai"]["calls"], 1); self.assertEqual(r["proposal"]["amount_trx"], "2")
        self.assertIn("no valid tool call", r["ai"]["error"]); self.assertIn("reasoning=92", r["ai"]["error"]); self.assertEqual(r["ai"]["source"], "none")

    def test_reasoning_or_content_json_is_diagnostic_only_not_success(self):
        # VP 9/29 R2: 성공은 tool_calls 1건(함수명 일치)뿐. reasoning_content/content 의 JSON 은 진단 기록만 남기고 규칙 폴백(성공 아님).
        live, _ = self._live(_resp(reasoning_content=ARGS_NORMAL))
        r = AI.decide("맥북지갑한테 트론 2개 보내줘", CONTACTS, provider=live)
        self.assertEqual(r["kind_detail"], "fallback"); self.assertEqual(r["ai"]["source"], "none"); self.assertEqual(r["ai"]["calls"], 1)
        self.assertIn("reasoning_json", r["ai"]["diagnostic"]); self.assertIn("no valid tool call", r["ai"]["error"]); self.assertEqual(r["proposal"]["amount_trx"], "2")
        wrapped = '<think>\n\n</think>\n\n<tool_call>\n{"name": "parse_transfer_request", "arguments": {"alias": "맥북지갑", "amount": "2", "asset": "TRX"}}\n</tool_call>'
        live, _ = self._live(_resp(content=wrapped))
        r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=live)
        self.assertEqual(r["kind_detail"], "fallback"); self.assertIn("content_json", r["ai"]["diagnostic"])

    def test_strict_tool_call_shape(self):
        good = [{"function": {"name": "parse_transfer_request", "arguments": ARGS_NORMAL}}]
        self.assertEqual(AI.extract_tool_call(_resp(tool_calls=good))["source"], "tool_calls")
        # 함수명 불일치 / 2건 / 빈 인자 → 성공 아님
        self.assertEqual(AI.extract_tool_call(_resp(tool_calls=[{"function": {"name": "other_tool", "arguments": ARGS_NORMAL}}]))["source"], "none")
        two = AI.extract_tool_call(_resp(tool_calls=good + good)); self.assertEqual((two["source"], two["tool_calls_n"]), ("none", 2))
        self.assertEqual(AI.extract_tool_call(_resp(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": ""}}], reasoning_content=ARGS_NORMAL))["source"], "none")
        self.assertEqual(AI.extract_tool_call(_resp(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": {}}}]))["source"], "none")

    def test_adapt_via_real_tool_call_applies_constraints(self):
        args = {"alias": "맥북지갑", "amount": "2", "asset": "TRX", "change": {"budget_trx": "3", "deadline_minutes": 5}, "missing": [], "reason": "x"}
        live, _ = self._live(_resp(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": args}}], finish_reason="tool_calls"))
        r = AI.decide("맥북지갑한테 트론 2개, 예산 3 트론 5분 안에", CONTACTS, provider=live)
        self.assertEqual((r["kind"], r["kind_detail"], r["ai"]["source"]), ("proposal", "adapt", "tool_calls"))
        self.assertEqual(r["constraints"], {"budget_sun": 3000000, "deadline_s": 300, "fee_cap_sun": 1000000}); self.assertEqual(r["proposal"]["amount_trx"], "2"); self.assertNotIn("fallback", r["ai"])

    def test_condition_must_be_in_budget_or_deadline_context(self):
        # 수량 숫자(2)와 같은 예산 2 를 모델이 지어내도 '예산 2' 문맥이 없으면 무시. 기한 2 도 '2분' 이 없으면 무시.
        args = {"alias": "맥북지갑", "amount": "2", "asset": "TRX", "change": {"budget_trx": "2", "deadline_minutes": 2}}
        live, _ = self._live(_resp(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": args}}]))
        r = AI.decide("맥북지갑한테 트론 2개 보내줘", CONTACTS, provider=live)
        self.assertEqual(r["kind_detail"], "normal"); self.assertEqual(r["ai"]["ignored_model_conditions"], {"budget_trx": "2", "deadline_minutes": 2})
        self.assertTrue(AI._condition_stated("budget_trx", "3", "트론 2개, 예산 3 트론 5분 안에")); self.assertTrue(AI._condition_stated("deadline_minutes", 5, "예산 3 트론 5분 안에"))
        self.assertFalse(AI._condition_stated("budget_trx", "5", "예산 3 트론 5분 안에")); self.assertFalse(AI._condition_stated("deadline_minutes", 3, "예산 3 트론 5분 안에"))
        self.assertTrue(AI._condition_stated("budget_trx", "3.0", "예산 3 트론"))

    def test_tool_call_arguments_as_object_and_empty_string(self):
        live, _ = self._live(_resp(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": {"alias": "맥북지갑", "amount": "1000", "asset": "TRX"}}}]))
        d = AI.decide("맥북지갑한테 트론 1000개", CONTACTS, provider=live)
        self.assertEqual((d["kind"], d["reason_code"], d["ai"]["source"]), ("decline", "OVER_PER_REQUEST_CAP", "tool_calls")); self.assertFalse(d["order_created"])
        # arguments "" + reasoning_content 에 JSON → R2: 성공 아님(진단만)
        live, _ = self._live(_resp(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": ""}}], reasoning_content=ARGS_NORMAL))
        r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=live); self.assertEqual(r["ai"]["source"], "none"); self.assertEqual(r["kind_detail"], "fallback")

    def test_garbage_or_error_stays_fallback_and_model_cannot_lower_amount(self):
        live, _ = self._live(_resp(content="죄송합니다, 처리할 수 없습니다.", reasoning_content="생각 중..."))
        r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=live); self.assertEqual(r["kind_detail"], "fallback"); self.assertEqual(r["ai"]["source"], "none")
        live, _ = self._live(_resp(ok=False, http=429, error="rate limited"))
        r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=live); self.assertEqual(r["kind_detail"], "fallback"); self.assertEqual(r["ai"]["error"], "rate limited"); self.assertEqual(r["ai"]["calls"], 1)
        live, _ = self._live(_resp(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": '{"alias":"맥북지갑","amount":"1","asset":"TRX","change":{"budget_trx":"9"}}'}}]))
        r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=live)
        self.assertEqual(r["proposal"]["amount_trx"], "2"); self.assertIn("불일치", r["ai"]["fallback"]); self.assertEqual(r["ai"]["ignored_model_conditions"], {"budget_trx": "9"})   # 지어낸 예산 무시

    def test_extract_helper_direct(self):
        self.assertEqual(AI.extract_tool_arguments({"ok": False}), ("", "none"))
        self.assertEqual(AI.extract_tool_arguments(_resp(content='앞말 {"alias":"a","amount":"2","asset":"TRX"} 뒷말'))[1], "none")          # R2: content JSON 은 성공 아님
        self.assertIn("content_json", AI.extract_tool_call(_resp(content='앞말 {"alias":"a","amount":"2","asset":"TRX"} 뒷말'))["diagnostic"])
        self.assertEqual(AI.extract_tool_arguments(_resp(content="{broken"))[1], "none")


GOOD_TC = [{"function": {"name": "parse_transfer_request", "arguments": ARGS_NORMAL}}]


def _live_ok(**kw):
    base = dict(tool_calls=GOOD_TC, finish_reason="tool_calls", raw_saved=True, raw_error=None, raw_file="guide/logs/kiln_raw/t3st0001.json", cost_usd=5.584e-05, cost_status="server_usage")
    base.update(kw); return _resp(**base)


class BudgetGateTests(unittest.TestCase):
    """R2: 실패·타임아웃도 횟수 포함, 실패 뒤 후속 호출 중단, 기록 실패·비용 미확인은 성공 아님(0 처리 없음). 실호출 0."""
    def _prov(self, td, resps):
        it = iter(resps); calls = {"n": 0}
        def fake_chat(messages, **kw):
            calls["n"] += 1; return next(it)
        b = AI.KilnBudget(path=pathlib.Path(td) / "budget.json", max_calls=4, max_usd=0.01); b.init("test approval")
        return AI.BudgetedKiln(budget=b, flow_id="t", chat_fn=fake_chat), b, calls

    def test_missing_budget_file_is_not_a_fresh_approval(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            calls = {"n": 0}
            def fake_chat(messages, **kw):
                calls["n"] += 1; return _live_ok()
            b = AI.KilnBudget(path=pathlib.Path(td) / "none.json")                    # init 하지 않음
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=AI.BudgetedKiln(budget=b, flow_id="t", chat_fn=fake_chat))
            self.assertEqual(r["ai"]["calls"], 0); self.assertIn("budget file missing", r["ai"]["error"]); self.assertEqual(calls["n"], 0); self.assertFalse((pathlib.Path(td) / "none.json").exists())

    def test_reservation_persists_before_call_and_exception_consumes_it(self):
        import tempfile, json as _j
        with tempfile.TemporaryDirectory() as td:
            b = AI.KilnBudget(path=pathlib.Path(td) / "budget.json"); b.init("x")
            seen = {}
            def boom(messages, **kw):
                seen["during"] = _j.loads(b.path.read_text())                            # 호출 중 파일에 이미 used=1·pending 예약이 있어야 한다
                raise RuntimeError("process interrupted")
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=AI.BudgetedKiln(budget=b, flow_id="t", chat_fn=boom))
            self.assertEqual(seen["during"]["used"], 1); self.assertTrue(seen["during"]["calls"][0]["pending"])
            d = b.load(); self.assertEqual(d["used"], 1); self.assertFalse(d["calls"][0]["pending"]); self.assertIn("exception", d["halted"]); self.assertEqual(d["unknown_cost_calls"], 1)
            self.assertEqual(r["kind_detail"], "fallback"); self.assertEqual(r["ai"]["calls"], 1); self.assertIn("RuntimeError", r["ai"]["error"])

    def test_concurrent_threads_never_exceed_cap(self):
        import tempfile, threading
        with tempfile.TemporaryDirectory() as td:
            b = AI.KilnBudget(path=pathlib.Path(td) / "budget.json", max_calls=4); b.init("x")
            n = {"calls": 0}; lock = threading.Lock(); gate = threading.Barrier(8)
            def fake_chat(messages, **kw):
                with lock:
                    n["calls"] += 1
                return _live_ok()
            def worker():
                gate.wait(); AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=AI.BudgetedKiln(budget=b, flow_id="t", chat_fn=fake_chat))
            ts = [threading.Thread(target=worker) for _ in range(8)]
            [x.start() for x in ts]; [x.join() for x in ts]
            d = b.load(); self.assertEqual(n["calls"], d["used"]); self.assertLessEqual(d["used"], 4); self.assertGreaterEqual(d["used"], 1)   # 동시 요청은 pending 이 있는 동안 거부(불변식 1) → 실제 호출 수 == 소비 횟수 ≤ 상한
            self.assertFalse(any(c["pending"] for c in d["calls"])); self.assertIsNone(d["halted"])
            for _ in range(8):                                                                   # 순차 요청은 상한까지만 호출된다
                AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=AI.BudgetedKiln(budget=b, flow_id="t", chat_fn=fake_chat))
            d = b.load(); self.assertEqual((n["calls"], d["used"]), (4, 4)); self.assertEqual(len(d["calls"]), 4)

    def test_invariant_a_reserve_then_crash_blocks_after_restart(self):
        """(a) reserve 직후 프로세스 중단 → 재시작한 새 제공자는 예약 못 함(실제 제공자 호출 0). pending 은 소비 횟수·미확인 비용으로 보존."""
        import tempfile, subprocess, sys as _s, json as _j
        with tempfile.TemporaryDirectory() as td:
            b = AI.KilnBudget(path=pathlib.Path(td) / "budget.json", max_calls=4); b.init("x")
            code = f"""
import sys, pathlib, os; sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[1])!r})
import phone_ai as AI
b = AI.KilnBudget(path=pathlib.Path({str(b.path)!r}), max_calls=4)
rid, why = b.reserve('crash'); print(rid, flush=True); os._exit(9)      # settle 없이 강제 종료(flush 뒤 즉시 종료)
"""
            out = subprocess.run([_s.executable, "-c", code], capture_output=True).stdout.decode().strip(); self.assertTrue(out.startswith("r"))
            d = b.load(); self.assertEqual(d["used"], 1); self.assertTrue(d["calls"][0]["pending"]); self.assertEqual(d["calls"][0]["cost_status"], "pending")
            calls = {"n": 0}
            def fake_chat(messages, **kw):
                calls["n"] += 1; return _live_ok()
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=AI.BudgetedKiln(budget=AI.KilnBudget(path=b.path, max_calls=4), flow_id="restart", chat_fn=fake_chat))
            self.assertEqual(r["ai"]["calls"], 0); self.assertIn("pending reservation", r["ai"]["error"]); self.assertEqual(calls["n"], 0)
            d2 = b.load(); self.assertEqual(d2["used"], 1); self.assertTrue(d2["calls"][0]["pending"]); self.assertIsNone(d2["calls"][0]["ok"])   # 재시작·시간 경과로 바뀌지 않음

    def test_invariant_b_slow_first_request_blocks_second_thread_and_process(self):
        """(b) 느린 첫 요청 중 두 번째 스레드·프로세스 요청 → 관문 거부(실제 제공자 호출 0)."""
        import tempfile, threading, subprocess, sys as _s
        with tempfile.TemporaryDirectory() as td:
            b = AI.KilnBudget(path=pathlib.Path(td) / "budget.json", max_calls=4); b.init("x")
            started, release = threading.Event(), threading.Event(); n = {"calls": 0}
            def slow_chat(messages, **kw):
                n["calls"] += 1; started.set(); release.wait(10); return _live_ok()
            res = {}
            t1 = threading.Thread(target=lambda: res.update(first=AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=AI.BudgetedKiln(budget=b, flow_id="slow", chat_fn=slow_chat))))
            t1.start(); self.assertTrue(started.wait(5))
            second = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=AI.BudgetedKiln(budget=b, flow_id="second", chat_fn=slow_chat))
            self.assertEqual(second["ai"]["calls"], 0); self.assertIn("pending reservation", second["ai"]["error"])
            code = f"""
import sys, pathlib; sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[1])!r})
import phone_ai as AI
print(AI.KilnBudget(path=pathlib.Path({str(b.path)!r}), max_calls=4).reserve('proc')[0])
"""
            out = subprocess.run([_s.executable, "-c", code], capture_output=True).stdout.decode().strip(); self.assertEqual(out, "None")
            release.set(); t1.join(5)
            self.assertEqual(res["first"]["kind_detail"], "normal"); self.assertEqual(n["calls"], 1)
            d = b.load(); self.assertEqual(d["used"], 1); self.assertFalse(d["calls"][0]["pending"]); self.assertTrue(d["calls"][0]["ok"]); self.assertIsNone(d["halted"])

    def test_invariant_c_semantic_failure_after_response_finalizes_as_failure(self):
        """(c) 응답 수신 뒤 의미 검증 실패 → 예약이 실패로 확정·halted, 후속 실제 제공자 호출 0. 성공은 검증 후에만 확정."""
        import tempfile, json as _j
        with tempfile.TemporaryDirectory() as td:
            args = {"alias": "맥북지갑", "amount": "2", "asset": "TRX", "change": {"budget_trx": "9"}}
            prov, b, calls = self._prov(td, [_live_ok(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": args}}]), _live_ok()])
            phases = []
            orig = prov.budget.settle
            def spy(rid, resp, ok, why="", final=False):
                d = orig(rid, resp, ok, why, final); phases.append((d["calls"][-1]["pending"], d["calls"][-1]["phase"])); return d
            prov.budget.settle = spy
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov)
            self.assertEqual(phases, [(True, "verifying")])                             # 네트워크 단계 뒤에는 아직 확정 아님
            d = b.load(); self.assertFalse(d["calls"][0]["pending"]); self.assertFalse(d["calls"][0]["ok"]); self.assertIn("invented", d["halted"])
            AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(calls["n"], 1)
        with tempfile.TemporaryDirectory() as td:                                     # 성공 경로: 검증 뒤 확정 ok=True, pending 없음
            prov, b, calls = self._prov(td, [_live_ok()])
            AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); d = b.load(); self.assertEqual((d["calls"][0]["pending"], d["calls"][0]["ok"], d["calls"][0]["phase"]), (False, True, "final"))

    def test_cross_process_lock_never_exceeds_cap(self):
        import tempfile, subprocess, sys as _s, json as _j
        with tempfile.TemporaryDirectory() as td:
            b = AI.KilnBudget(path=pathlib.Path(td) / "budget.json", max_calls=5); b.init("x")
            code = f"""
import sys, pathlib; sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[1])!r})
import phone_ai as AI
b = AI.KilnBudget(path=pathlib.Path({str(b.path)!r}), max_calls=5)
got = 0; denied_pending = 0
for _ in range(200):
    rid, why = b.reserve('p')
    if rid: got += 1; b.settle(rid, {{"cost_usd": 1e-6, "cost_status": "server_usage", "raw_saved": True, "source": "tool_calls"}}, True, final=True)
    elif 'pending reservation' in why: denied_pending += 1        # 다른 프로세스가 호출 중 → 거부(불변식 1), 잠시 뒤 재시도
    else: break                                                     # call cap reached
print(got)
"""
            ps = [subprocess.Popen([_s.executable, "-c", code], stdout=subprocess.PIPE) for _ in range(3)]
            got = sum(int(p.communicate()[0].decode().strip() or 0) for p in ps)
            d = _j.loads(b.path.read_text()); self.assertEqual(got, 5); self.assertEqual(d["used"], 5); self.assertEqual(len(d["calls"]), 5); self.assertAlmostEqual(d["cost_usd"], 5e-6)

    def test_schema_or_semantic_failure_halts_server_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            # 스키마 실패("{}") → 폴백 + 후속 차단
            prov, b, calls = self._prov(td, [_live_ok(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": "{}"}}]), _live_ok()])
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(r["kind_detail"], "fallback"); self.assertIn("missing required keys", r["ai"]["fallback"])
            self.assertIn("schema", b.load()["halted"]); AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(calls["n"], 1)
        with tempfile.TemporaryDirectory() as td:
            # 의미 실패(지어낸 예산) → 제안은 만들되 후속 차단
            args = {"alias": "맥북지갑", "amount": "2", "asset": "TRX", "change": {"budget_trx": "9"}}
            prov, b, calls = self._prov(td, [_live_ok(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": args}}]), _live_ok()])
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(r["kind_detail"], "normal"); self.assertIn("semantic", b.load()["halted"])
            AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(calls["n"], 1)
        with tempfile.TemporaryDirectory() as td:
            # 수량 불일치 → 폴백 + 차단
            args = {"alias": "맥북지갑", "amount": "1", "asset": "TRX"}
            prov, b, calls = self._prov(td, [_live_ok(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": args}}]), _live_ok()])
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertIn("불일치", r["ai"]["fallback"]); self.assertIn("amount", b.load()["halted"])

    def test_missing_info_is_question_not_fallback_and_not_halt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            args = {"alias": "맥북지갑", "amount": None, "asset": "TRX", "missing": ["amount"], "reason": "수량 없음"}
            prov, b, calls = self._prov(td, [_live_ok(tool_calls=[{"function": {"name": "parse_transfer_request", "arguments": args}}])])
            r = AI.decide("맥북지갑한테 트론 보내줘", CONTACTS, provider=prov)
            self.assertEqual((r["kind"], r["kind_detail"], r["reason"], r["missing"]), ("question", "info_check", "MISSING_INFO", ["amount"])); self.assertIn("수량", r["question"])
            self.assertNotIn("fallback", r["ai"]); self.assertEqual(r["ai"]["source"], "tool_calls"); self.assertIsNone(b.load()["halted"]); self.assertFalse(r["order_created"])
        self.assertIsNone(AI.validate_model_output("{}")[0]); self.assertEqual(AI.validate_model_output("{}")[1], "missing required keys ['alias', 'amount', 'asset']")
        self.assertIsNone(AI.validate_model_output('{"alias":"x","amount":"2"}')[0])
        self.assertIsNotNone(AI.validate_model_output('{"alias":"x","amount":null,"asset":"TRX"}')[0])

    def test_success_counts_and_cost_accumulates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            prov, b, calls = self._prov(td, [_live_ok(), _live_ok()])
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual((r["kind_detail"], r["ai"]["source"], r["ai"]["raw_saved"]), ("normal", "tool_calls", True))
            self.assertEqual(r["ai"]["budget"]["used"], 1); self.assertAlmostEqual(b.load()["cost_usd"], 5.584e-05)
            AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(b.load()["used"], 2); self.assertIsNone(b.load()["halted"]); self.assertEqual(calls["n"], 2)

    def test_failure_counts_and_halts_following_calls(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            prov, b, calls = self._prov(td, [_resp(usage=_usage(30, 30), raw_saved=True, cost_usd=2.4e-05, cost_status="server_usage"), _live_ok()])
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(r["kind_detail"], "fallback"); self.assertEqual(r["ai"]["calls"], 1)
            d = b.load(); self.assertEqual(d["used"], 1); self.assertIn("no valid tool call", d["halted"])
            r2 = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov)              # 후속 호출은 관문에서 막힘: 실호출 없음(calls=0)
            self.assertEqual(r2["ai"]["calls"], 0); self.assertIn("halted", r2["ai"]["error"]); self.assertEqual(calls["n"], 1); self.assertEqual(b.load()["used"], 1)

    def test_transport_error_counts_and_halts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            prov, b, calls = self._prov(td, [_resp(ok=False, http=0, error="curl exit 28: timeout", cost_status="unknown"), _live_ok()])
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(r["ai"]["calls"], 1); self.assertEqual(r["kind_detail"], "fallback")
            d = b.load(); self.assertEqual((d["used"], d["unknown_cost_calls"]), (1, 1)); self.assertIn("timeout", d["halted"])
            AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(calls["n"], 1)

    def test_raw_save_failure_and_unknown_cost_are_not_success(self):
        import tempfile
        import phone_kiln_run as R
        with tempfile.TemporaryDirectory() as td:
            prov, b, _ = self._prov(td, [_live_ok(raw_saved=False, raw_error="PermissionError: x", raw_file=None)])
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov)
            self.assertEqual(r["kind_detail"], "normal")                                     # 의미상 제안은 만들어지지만
            self.assertFalse(r["ai"]["raw_saved"]); self.assertIn("raw not saved", b.load()["halted"])     # 기록 실패 → 러너 판정 실패·후속 중단
            self.assertFalse(R.judge(r, {"kind": "proposal", "kind_detail": "normal", "amount_trx": "2"})[0])
        with tempfile.TemporaryDirectory() as td:
            prov, b, _ = self._prov(td, [_live_ok(cost_usd=None, cost_status="unknown(no usage.cost)")])
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov)
            d = b.load(); self.assertEqual(d["unknown_cost_calls"], 1); self.assertEqual(d["cost_usd"], 0.0); self.assertIn("cost unknown", d["halted"])
            self.assertIsNone(r["ai"]["cost_usd"])
            ok, why = R.judge(r, {"kind": "proposal"}); self.assertFalse(ok); self.assertIn("cost unknown", why)

    def test_cap_reached_blocks_without_calling(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            prov, b, calls = self._prov(td, [_live_ok()] * 5)
            for _ in range(4):
                AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov)
            r = AI.decide("맥북지갑한테 트론 2개", CONTACTS, provider=prov); self.assertEqual(r["ai"]["calls"], 0); self.assertIn("call cap", r["ai"]["error"]); self.assertEqual(calls["n"], 4)

    def test_runner_judge_semantics(self):
        import phone_kiln_run as R
        prov_ok = _live_ok()
        live = AI.KilnLive(chat_fn=lambda m, **kw: prov_ok, flow_id="t")
        r = AI.decide("맥북지갑한테 트론 2개 보내줘", CONTACTS, provider=live)
        self.assertTrue(R.judge(r, {"kind": "proposal", "kind_detail": "normal", "amount_trx": "2"})[0])
        self.assertFalse(R.judge(r, {"kind": "decline"})[0])
        self.assertIsNone(R.est_cost({"prompt_tokens": 1000, "completion_tokens": 1000}))     # 서버 cost 없으면 None(0 아님)
        self.assertEqual(R.est_cost({"cost": 5.584e-05}), 5.584e-05); self.assertAlmostEqual(R.estimate_only({"prompt_tokens": 1000, "completion_tokens": 1000}), 0.0002)


class KilnClientRawSave(unittest.TestCase):
    def test_save_raw_message_keeps_choices_only(self):
        import json, tempfile
        import kiln_client as KC
        with tempfile.TemporaryDirectory() as td:
            old = KC.RAW_DIR; KC.RAW_DIR = pathlib.Path(td)
            try:
                KC.save_raw_message("abcd1234", {"id": "x", "model": "qwen3-32b", "choices": [{"message": {"content": None, "reasoning_content": "{...}"}, "finish_reason": "stop"}],
                                                 "usage": {"completion_tokens": 3}, "authorization": "SHOULD_NOT_BE_SAVED"})
                saved = json.loads((pathlib.Path(td) / "abcd1234.json").read_text(encoding="utf-8"))
            finally:
                KC.RAW_DIR = old
        self.assertEqual(saved["choices"][0]["message"]["reasoning_content"], "{...}"); self.assertNotIn("authorization", saved)

    def test_save_raw_message_reports_failure(self):
        import kiln_client as KC
        old = KC.RAW_DIR; KC.RAW_DIR = pathlib.Path("/dev/null/notadir")
        try:
            ok, err = KC.save_raw_message("abcd1234", {"choices": []})
        finally:
            KC.RAW_DIR = old
        self.assertFalse(ok); self.assertTrue(err)

    def test_runner_cost_prefers_server_usage_cost(self):
        import phone_kiln_run as R
        self.assertEqual(R.est_cost({"prompt_tokens": 380, "completion_tokens": 92, "cost": 5.584e-05}), 5.584e-05)
        self.assertIsNone(R.est_cost({"prompt_tokens": 1000, "completion_tokens": 1000}))


if __name__ == "__main__":
    unittest.main()
