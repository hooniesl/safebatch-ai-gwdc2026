"""9/28 20:34 부사장 실행 연결 검수 3항목: ①사용자 기한=서명 기한·지갑 대기 중 만료 ②정수 금액/CSV 안전 ③진짜 재시작·동시 요청."""
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import executor as X  # noqa: E402
import demo_flows as D  # noqa: E402
from safebatch.policy import PaymentPolicy  # noqa: E402
from safebatch.intent_log import IntentLog  # noqa: E402
from safebatch.tests.test_gasfree_order import RECV, USDT, PROV, tokens_info, FakeClient  # noqa: E402
from test_executor import Env, rules, ok_model, ok_client, T0, real_sign, EOA, address_info  # noqa: E402


class DeadlineBinding(unittest.TestCase):
    def test_order_deadline_equals_user_deadline_not_600(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            f = env.run(rules(deadline_ts=T0 + 30))
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED")
            self.assertEqual(int(env.client.submitted[0]["deadline"]), T0 + 30)      # 600초로 연장되지 않음

    def test_wallet_signature_after_deadline_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            def slow_sign(order):
                env.clock["t"] = T0 + 31                                         # 기한 T+30, 지갑 응답 T+31
                return real_sign(order)
            env.sign = slow_sign
            f = env.run(rules(deadline_ts=T0 + 30))
            self.assertEqual(f["outcome"], "NOT_SUBMITTED"); self.assertIn("deadline", f["execution"]["detail"]["reason"])
            self.assertEqual(env.client.submitted, [])

    def test_short_remaining_time_is_not_extended(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            def confirm(s): env.clock["t"] = T0 + 29; return "owner"              # 남은 1초
            f = env.run(rules(deadline_ts=T0 + 30), confirm=confirm)
            if env.client.submitted:
                self.assertEqual(int(env.client.submitted[0]["deadline"]), T0 + 30)
            else:
                self.assertIn(f["outcome"], ("NOT_SUBMITTED", "DECLINED"))


class ExactAmountsAndMemo(unittest.TestCase):
    def test_large_amount_exact_no_float(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client(), fee=10_000_000)
            env.policy = PaymentPolicy(budget_units=10**15, fee_units=20_000_000, allowlist=frozenset({RECV}))
            big = 123_456_789_012_345                                           # 123,456,789.012345 USDT
            env_client_trace = env.client.trace_resp["body"]["data"]; env_client_trace["txnAmount"] = big; env_client_trace["txnTotalCost"] = big + 10_000_000
            env.quote = lambda: X.Quote(address_info(nonce=3, balance=big + 10_000_000), tokens_info(), PROV, env.clock["t"], 10_000_000)  # 잔액 충분
            f = env.run(rules(amount_units=big, budget_total_units=big + 10_000_000, min_receive_units=1))
            self.assertEqual(env.client.submitted[0]["value"], big)             # 보인 정수 = 제출 정수
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED")

    def test_memo_with_comma_quote_newline_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            goal = 'a,b "quoted" \n newline; plan="x"'
            seen = {}
            f = env.run(rules(goal=goal), confirm=lambda s: seen.update(s) or "owner")
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED")
            body = env.client.submitted[0]
            self.assertEqual(body["receiver"], RECV); self.assertEqual(body["value"], 1_500_000); self.assertEqual(body["maxFee"], 10_000_000)
            self.assertEqual(seen["value_units"], body["value"]); self.assertEqual(seen["fee_units"], body["maxFee"])
            self.assertEqual(env.policy.records[f["execution"]["detail"]["payment_id"]].memo[:5], goal[:5])


class RealRestartAndConcurrency(unittest.TestCase):
    def test_restart_with_fresh_policy_is_closed_by_intent_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, ok_client())
            self.assertEqual(env.run()["outcome"], "EXECUTED_CONFIRMED")
            env2 = Env(tmp, ok_client())                                       # 새 프로세스: 정책 원장 미복원(새 PaymentPolicy), intent/approval 원장만 영속
            f = env2.run()
            self.assertEqual(env2.client.submitted, [])
            self.assertIn(f["outcome"], ("NOT_SUBMITTED", "BLOCKED_APPROVAL_INVALID"))
            self.assertEqual(env2.intent_log.state("b-f1:r1:2"), "CONFIRMED")   # 닫힌 상태 유지

    def test_two_concurrent_requests_exactly_one_submission_and_one_explicit_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ok_client(); env = Env(tmp, client)
            barrier = threading.Barrier(2); results, errors = [], []
            def worker():
                try:
                    barrier.wait(); results.append(env.run()["outcome"])
                except Exception as e:
                    errors.append(repr(e))
            ts = [threading.Thread(target=worker) for _ in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]
            self.assertEqual(errors, [])
            self.assertEqual(len(client.submitted), 1, results)
            self.assertEqual(sorted(results).count("EXECUTED_CONFIRMED"), 1, results)
            other = [r for r in results if r != "EXECUTED_CONFIRMED"]
            self.assertEqual(len(other), 1); self.assertIn(other[0], ("BLOCKED_APPROVAL_INVALID", "NOT_SUBMITTED", "DECLINED"))

    def test_approval_reserve_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = X.ApprovalLedger(pathlib.Path(tmp) / "a.jsonl")
            barrier = threading.Barrier(4); wins = []
            def w():
                barrier.wait(); wins.append(led.reserve("ap-1"))
            ts = [threading.Thread(target=w) for _ in range(4)]; [t.start() for t in ts]; [t.join() for t in ts]
            self.assertEqual(wins.count(True), 1)


if __name__ == "__main__":
    unittest.main()
