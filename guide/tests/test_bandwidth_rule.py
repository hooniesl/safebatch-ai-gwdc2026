"""9/28 부사장 8차 검수 회귀: 대역폭 과금 바이트 = 전송 바이트 + MAX_RESULT_SIZE_IN_TX(64)×계약 수 (공식 java-tron BandwidthProcessor, Nile 4.8.2.2 실측 일치).
옛 산식(전송 281 + 임의 4 = 285B)은 총 5 TRX 상한을 넘기므로(4,715,000 + 345,000 = 5,060,000) 실패해야 하고, 수정 후 산식은 상한 안에서 통과해야 한다.
합성 검사(가짜 노드). 실지갑·네트워크·서명·방송 없음."""
import argparse
import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import demo_flows as D  # noqa: E402
import nile_executor as NX  # noqa: E402
import run_nile_live as RL  # noqa: E402
from safebatch import nile_tx as NT  # noqa: E402
from tests.test_nile_path import FakeNode, sign_like_wallet, SENDER, OWNER, T0  # noqa: E402
from tests.test_nile_link import LinkEnv, rules  # noqa: E402

V3 = pathlib.Path(__file__).resolve().parents[2] / "prep_20260914" / "evidence" / "20260928_nile_live" / "PREPARE_selftransfer_v3_final.json"
OLD_TX_BYTES = (1 + 2 + 211) + (1 + 1 + 65) + 4        # 옛 signed_tx_size_bytes (임의 +4 포함) = 285
CAP = 5_000_000


class OfficialRule(unittest.TestCase):
    def test_constants_match_java_tron(self):
        self.assertEqual(NT.MAX_RESULT_SIZE_IN_TX, 64); self.assertEqual(NT.PER_SIGN_LENGTH, 65)

    def test_transmitted_vs_charged_bytes(self):
        raw = "00" * NT.RAW_LEN_TRANSFER
        self.assertEqual(NT.signed_tx_size_bytes(raw, 1), 281)                    # 전송: raw 214 + 서명 67, 임의 여유 없음
        self.assertEqual(NT.charged_bandwidth_bytes(raw, 1, 1), 345)              # 과금: 281 + 64
        self.assertEqual(NT.charged_bandwidth_bytes(raw, 1, 2), 281 + 128)        # 계약 수만큼 64 가산(우리는 1개만 허용)
        self.assertEqual(NT.charged_bandwidth_bytes(raw, 2, 1), 281 + 67 + 64)    # 서명 수 반영
        self.assertEqual(NX.BANDWIDTH_BYTES_TRANSFER, 345)

    def test_real_v3_raw_bytes_charge_345(self):
        art = json.loads(V3.read_text(encoding="utf-8"))
        raw_hex = art["unsigned"]["raw_data_hex"]
        self.assertEqual(len(raw_hex) // 2, NT.RAW_LEN_TRANSFER)
        self.assertEqual(NT.signed_tx_size_bytes(raw_hex, 1), 281); self.assertEqual(NT.charged_bandwidth_bytes(raw_hex, 1, 1), 345)

    def test_nile_observed_sizes_match_rule(self):
        # 9/28 22:4x Nile 실측 3건(TriggerSmartContract·서명 1개): raw 597/378/244B → 영수증 net 731/512/378B (= 전송+64). 규칙 함수가 같은 값을 내야 한다.
        for raw_len, observed in ((597, 731), (378, 512), (244, 378)):
            self.assertEqual(NT.charged_bandwidth_bytes("00" * raw_len, 1, 1), observed)


class OldFormulaFailsNewPasses(unittest.TestCase):
    def test_old_formula_breaks_5trx_cap_new_plan_fits(self):
        old_bw = OLD_TX_BYTES * 1000; old_fee_limit = CAP - old_bw                 # v3: 285,000 / 4,715,000
        self.assertEqual((old_bw, old_fee_limit), (285_000, 4_715_000))
        self.assertGreater(old_fee_limit + 345 * 1000, CAP)                        # 5,060,000 > 5,000,000: 옛 산식의 누락 항목
        art = RL.prepare_artifact(FakeNode(), rules(), SENDER, now=T0); p = art["plan"]
        self.assertEqual((p["trx_bandwidth_max_sun"], p["trx_fee_limit_sun"], p["trx_total_max_sun"]), (345_000, 4_655_000, CAP))
        self.assertEqual(NT.decode_raw(art["unsigned"]["raw_data_hex"])["fee_limit"], 4_655_000)   # 체인 fee_limit 도 줄어든 Energy 한도
        self.assertGreaterEqual(p["trx_fee_limit_sun"], p["trx_est_sun"])
        self.assertEqual(art["artifact_version"], 3); self.assertTrue(art["bandwidth_basis_check"]["pass"])
        self.assertEqual(art["bandwidth_basis_check"]["bandwidth_bytes_actual"], 345); self.assertEqual(art["quote"]["bandwidth_bytes"], 345)
        self.assertIn("결과 상한 64 B", art["human_readable"]["Bandwidth 최대"]); self.assertIn("대역폭 산정 근거", art["human_readable"])
        self.assertEqual(art["confirm_payload"]["bandwidth_max_sun"], 345_000); self.assertEqual(art["confirm_payload"]["energy_fee_limit_sun"], 4_655_000)
        self.assertEqual(art["confirm_payload"]["bandwidth_rule"], NT.BANDWIDTH_RULE)

    def test_v3_artifact_plan_exceeds_cap_under_official_rule(self):
        art = json.loads(V3.read_text(encoding="utf-8")); p = art["plan"]
        charged = NT.charged_bandwidth_bytes(art["unsigned"]["raw_data_hex"], 1, 1) * art["quote"]["bandwidth_price_sun"]
        self.assertEqual(p["trx_fee_limit_sun"] + charged, 5_060_000); self.assertGreater(p["trx_fee_limit_sun"] + charged, art["rules"]["trx_fee_cap_sun"])

    def test_v3_artifact_refused_by_execute_before_any_network(self):
        art = json.loads(V3.read_text(encoding="utf-8")); self.assertEqual(art["artifact_version"], 2)
        a = argparse.Namespace(artifact=str(V3), confirm_digest=art["confirm_sha256"], confirmed_by="Codex", batch_id="b", flow_id="f", out="/dev/null",
                               ai_record=None, kiln=False, manual_tech_check=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            RL.cmd_execute(a)
        self.assertIn("[중단] 아티팩트 형식", buf.getvalue())

    def test_old_basis_artifact_gets_no_approval_and_no_signature(self):
        # 옛 산식으로 만든 아티팩트(285,000 / 4,715,000)를 그대로 execute 에 넣으면 현재 후보(345,000 / 4,655,000)와 달라 승인·서명 없이 끝난다.
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(); good = RL.prepare_artifact(node, rules(), SENDER, now=T0)
            old = copy.deepcopy(good); old["plan"].update(trx_bandwidth_max_sun=285_000, trx_fee_limit_sun=4_715_000)
            spec = NT.TransferSpec(sender=SENDER, receiver=SENDER, token=NT.NILE_USDT, amount_units=1_000_000, fee_limit_sun=4_715_000, expire_at_ms=rules().deadline_ts * 1000)
            u = NT.build_unsigned(node, spec, now_ms=T0 * 1000); old["unsigned"] = {k: u[k] for k in ("txID", "raw_data", "raw_data_hex")}
            r = rules(); pl = D.Plan(**old["plan"])
            old["confirm_sha256"] = NX.confirm_digest(r, pl, SENDER, u, 1, None)
            env = LinkEnv(tmp, node, artifact=old); f = env.run()
            self.assertNotEqual(f["outcome"], "EXECUTED_CONFIRMED"); self.assertFalse(f.get("executed")); self.assertEqual(env.signed, 0)
            self.assertEqual(len(node.broadcasts), 0)


class FourStagesSameBasis(unittest.TestCase):
    def test_verify_signed_reports_transmitted_and_charged(self):
        art = RL.prepare_artifact(FakeNode(), rules(), SENDER, now=T0); u = {**art["unsigned"]}
        spec = NT.TransferSpec(sender=SENDER, receiver=SENDER, token=NT.NILE_USDT, amount_units=1_000_000, fee_limit_sun=art["plan"]["trx_fee_limit_sun"], expire_at_ms=rules().deadline_ts * 1000)
        body = NT.verify_signed(u, sign_like_wallet(u, OWNER), spec, now_ms=T0 * 1000 + 1000)
        self.assertEqual((body["signed_size_bytes"], body["bandwidth_bytes"]), (281, 345))

    def test_receipt_net_usage_checked_against_charged_bytes(self):
        art = RL.prepare_artifact(FakeNode(), rules(), SENDER, now=T0); u = art["unsigned"]
        spec = NT.TransferSpec(sender=SENDER, receiver=SENDER, token=NT.NILE_USDT, amount_units=1_000_000, fee_limit_sun=4_655_000, expire_at_ms=rules().deadline_ts * 1000)
        node = FakeNode(); signed = sign_like_wallet(u, OWNER); node.chain.append(signed)
        rec = NT.fetch_receipt(node, u["txID"])                                    # FakeNode 영수증 net_usage=345 (실측 형태)
        ok = NT.reconcile_receipt(u, spec, rec, total_cap_sun=CAP, bandwidth_max_sun=345_000, bandwidth_bytes=345)
        self.assertTrue(ok["fee_within_limit"]); self.assertTrue(ok["net_usage_within_bound"]); self.assertEqual(ok["verdict"], "CONFIRMED")
        bad = NT.reconcile_receipt(u, spec, rec, total_cap_sun=CAP, bandwidth_max_sun=281_000, bandwidth_bytes=281)   # 옛 전송 바이트 기준이면 실측 345 초과
        self.assertFalse(bad["net_usage_within_bound"]); self.assertFalse(bad["fee_within_limit"]); self.assertEqual(bad["verdict"], "MISMATCH")

    def test_full_flow_with_free_bandwidth_exhausted_uses_charged_bytes_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = FakeNode(free_net=0); env = LinkEnv(tmp, node); f = env.run()
            self.assertEqual(f["outcome"], "EXECUTED_CONFIRMED"); q = f["quote"]; p = f["chosen"]["plan"]
            self.assertEqual((q["bandwidth_bytes"], q["tx_size_bytes"]), (345, 281)); self.assertEqual(q["expected_total_sun"], q["est_energy_sun"] + 345_000)
            self.assertEqual((p["trx_bandwidth_max_sun"], p["trx_fee_limit_sun"], p["trx_total_max_sun"]), (345_000, 4_655_000, CAP))
            od = f["order"]; self.assertEqual((od["bandwidth_bytes"], od["tx_size_bytes"], od["bandwidth_rule"]), (345, 281, NT.BANDWIDTH_RULE))
            rc = f["execution"]["detail"]["receipt"]; self.assertEqual((rc["bandwidth_bytes"], rc["bandwidth_max_sun"]), (345, 345_000))
            self.assertEqual(len(node.broadcasts), 1)

    def test_free_bandwidth_between_transmitted_and_charged_counts_as_exhausted(self):
        # 무료 대역폭 300B: 전송 281B 기준이면 '무료로 충분'으로 오판, 과금 345B 기준이면 부족 → 예상 총비용에 Bandwidth 포함
        q = NX.quote_from_node(FakeNode(free_net=300), SENDER, SENDER, 1_000_000, now_fn=lambda: T0)
        self.assertEqual(q.expected_total_sun, q.est_energy_sun + 345_000)
        q2 = NX.quote_from_node(FakeNode(free_net=345), SENDER, SENDER, 1_000_000, now_fn=lambda: T0)
        self.assertEqual(q2.expected_total_sun, q2.est_energy_sun)


if __name__ == "__main__":
    unittest.main()
