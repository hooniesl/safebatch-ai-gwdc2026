"""Signer fault-injection regressions. Temporary records, public fixture key, FakeNode only."""
import json
import os
import pathlib
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_tg_signer_exec import setup, make_awaiting, approve, order_of, SG, SENDER, MAC, USER, CONTACTS
import phone_intent
import phone_server


def trial_policy(tmp):
    return SG.SignerPolicy(allowed_receivers={MAC}, allowed_senders={SENDER}, exact_amount_sun=2_000_000,
                           lifetime_max_transactions=1, daily_total_max_sun=4_000_000, stop_file=pathlib.Path(tmp) / "stop")


def policy_json():
    return {"version": 1, "network": "nile", "chain_id": SG.NILE_CHAIN_ID, "sender": SENDER, "receiver": MAC,
            "exact_amount_sun": 2_000_000, "fee_cap_max_sun": 2_000_000, "lifetime_max_transactions": 1}


def interrupted(ext, flow, signer, policy, ledger, rec, clock, stage):
    order = order_of(ext, flow, rec)
    ledger.reserve(order, policy)
    rec["approval"] = {"key": "mock-approval", "t": clock.t, "state": "APPROVED"}
    rec["awaiting_approval"] = False
    if stage:
        rec["exec"] = {"stage": stage, "owner": "dead", "pid": 999999, "attempts": 1}
        if stage == "SIGNED":
            rec["exec"]["signed_tx"] = signer.sign(order["unsigned_tx"], order["user_eoa"])
    ext._write(rec)
    return order


class RecoverySafetyTests(unittest.TestCase):
    def test_every_recoverable_stage_rechecks_stop_expiry_and_fingerprint(self):
        for stage in (None, "RESERVED", "SIGNED"):
            for mutation in ("stop", "expiry", "fingerprint", "summary", "sender"):
                with self.subTest(stage=stage, mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                    clock, node, flow, ext, signer, policy, ledger = setup(tmp)
                    rec = make_awaiting(ext, flow)
                    order = interrupted(ext, flow, signer, policy, ledger, rec, clock, stage)
                    before = signer.sign_count
                    if mutation == "stop":
                        policy.stop_file.write_text("stop")
                    elif mutation == "expiry":
                        clock.t = rec["approval_expires_at"] + 1
                    else:
                        if mutation == "fingerprint": rec["fingerprint"] = "f" * 64
                        elif mutation == "summary": rec["summary"]["amount_trx"] = "1"
                        else: rec["sender"] = MAC
                        ext._write(rec)
                    result = ext.resume_approved(flow, signer, policy, intent_id=rec["intent_id"], ledger=ledger)
                    self.assertEqual(result["state"], "POLICY_REFUSED", result)
                    self.assertEqual(signer.sign_count, before)
                    self.assertEqual(node.broadcasts, [])
                    self.assertEqual(ledger.state_of(order["payment_id"])["state"], "RESERVED")

    def test_unsigned_bytes_are_checked_before_signing(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp)
            rec = make_awaiting(ext, flow)
            stored = flow.store.get(rec["payment_id"])
            stored["order"]["unsigned_tx"]["raw_data"]["contract"][0]["parameter"]["value"]["amount"] = 3_000_000
            flow.store._write(flow.store._path(rec["payment_id"]), stored)
            result = approve(ext, flow, signer, policy, ledger, rec)
            self.assertEqual(result["state"], "POLICY_REFUSED")
            self.assertEqual(signer.sign_count, 0)
            self.assertEqual(node.broadcasts, [])

    def test_unknown_signer_failure_keeps_reservation_and_never_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, _, ledger = setup(tmp)
            policy = trial_policy(tmp); rec = make_awaiting(ext, flow)
            original = signer.sign
            def sign_then_lose_response(unsigned, sender):
                original(unsigned, sender)
                raise RuntimeError("mock response loss after signature")
            signer.sign = sign_then_lose_response
            result = approve(ext, flow, signer, policy, ledger, rec)
            self.assertEqual(result["state"], "INVESTIGATE")
            self.assertEqual(ledger.state_of(rec["payment_id"])["state"], "RESERVED")
            again = ext.resume_approved(flow, signer, policy, intent_id=rec["intent_id"], ledger=ledger)
            self.assertEqual(again["state"], "INVESTIGATE")
            self.assertEqual(signer.sign_count, 1)
            self.assertEqual(node.broadcasts, [])

    def test_stop_or_expiry_during_signing_blocks_submission(self):
        for mutation in ("stop", "expiry"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                clock, node, flow, ext, signer, policy, ledger = setup(tmp)
                rec = make_awaiting(ext, flow)
                def change():
                    if mutation == "stop": policy.stop_file.write_text("stop")
                    else: clock.t = rec["approval_expires_at"] + 1
                signer.delay_fn = change
                result = approve(ext, flow, signer, policy, ledger, rec)
                self.assertEqual(result["state"], "POLICY_REFUSED")
                self.assertEqual(signer.sign_count, 1)
                self.assertEqual(node.broadcasts, [])
                self.assertIn("signed_tx", ext.get(rec["intent_id"])["exec"])
                self.assertEqual(ledger.state_of(rec["payment_id"])["state"], "RESERVED")

    def test_unknown_stage_or_missing_reservation_cannot_sign(self):
        for stage in ("UNKNOWN", "RESERVED", "SIGNED"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                clock, node, flow, ext, signer, policy, ledger = setup(tmp)
                rec = make_awaiting(ext, flow)
                rec["approval"] = {"key": "mock", "t": clock.t, "state": "APPROVED"}
                rec["exec"] = {"stage": stage, "owner": "dead", "pid": 999999}
                ext._write(rec)
                result = ext.resume_approved(flow, signer, policy, intent_id=rec["intent_id"], ledger=ledger)
                self.assertIn(result["state"], ("INVESTIGATE", "LIMIT_UNKNOWN"))
                self.assertEqual(signer.sign_count, 0)
                self.assertEqual(node.broadcasts, [])

    def test_existing_result_is_status_only_even_with_stop_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp)
            rec = make_awaiting(ext, flow)
            self.assertTrue(approve(ext, flow, signer, policy, ledger, rec)["ok"])
            saved = ext.get(rec["intent_id"]); saved["exec"]["stage"] = "SUBMITTING"; saved["exec"]["owner"] = None; ext._write(saved)
            clock.t += 86400; policy.stop_file.write_text("stop")
            result = ext.resume_approved(flow, signer, policy, intent_id=rec["intent_id"], ledger=ledger)
            self.assertTrue(result["status_only"])
            self.assertEqual(signer.sign_count, 1)
            self.assertEqual(len(node.broadcasts), 1)

    def test_changes_during_requote_are_checked_at_actual_broadcast_boundary(self):
        for mutation in ("stop", "expiry", "approval", "policy", "ledger"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                clock, node, flow, ext, signer, policy, ledger = setup(tmp)
                rec = make_awaiting(ext, flow); account = node.account; changed = []
                def change_during_requote(address):
                    if not changed:
                        changed.append(True)
                        if mutation == "stop": policy.stop_file.write_text("stop")
                        elif mutation == "expiry": clock.t = rec["approval_expires_at"] + 1
                        elif mutation == "approval":
                            saved = ext.get(rec["intent_id"]); saved["approval"]["state"] = "REVOKED"; ext._write(saved)
                        elif mutation == "policy": policy.allowed_receivers.clear()
                        else: ledger.init_marker.unlink()
                    return account(address)
                node.account = change_during_requote
                result = approve(ext, flow, signer, policy, ledger, rec)
                self.assertTrue(changed, "fault injected inside the real submission requote")
                self.assertEqual(result["state"], "NOT_SUBMITTED", result)
                self.assertTrue(result["result"]["broadcast_guard_refused"])
                self.assertEqual(signer.sign_count, 1)
                self.assertEqual(node.broadcasts, [])
                saved = ext.get(rec["intent_id"])
                self.assertIn("signed_tx", saved["exec"])
                self.assertEqual(flow.store.get(rec["payment_id"])["state"], "SIGNED_REFUSED_NOT_BROADCAST")
                last = json.loads(ledger.path.read_text().splitlines()[-1])
                self.assertEqual(last["state"], "RESERVED")
                again = ext.resume_approved(flow, signer, policy, intent_id=rec["intent_id"], ledger=ledger)
                self.assertEqual(again["state"], "NOT_SUBMITTED")
                direct = flow.submit_signed(rec["payment_id"], rec["fingerprint"], saved["exec"]["signed_tx"])
                self.assertEqual(direct["state"], "SIGNATURE_HELD")
                self.assertEqual(signer.sign_count, 1)
                self.assertEqual(node.broadcasts, [])


class TrialLimitTests(unittest.TestCase):
    def test_one_transfer_for_lifetime_including_next_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, _, ledger = setup(tmp)
            policy = trial_policy(tmp); first = make_awaiting(ext, flow)
            self.assertTrue(approve(ext, flow, signer, policy, ledger, first)["ok"])
            clock.t += 86400
            second = make_awaiting(ext, flow, key="second-day")
            result = approve(ext, flow, signer, policy, ledger, second, key="approval-next-day")
            self.assertEqual(result["state"], "POLICY_REFUSED")
            self.assertIn("lifetime", result["error"])
            self.assertEqual(signer.sign_count, 1)
            self.assertEqual(len(node.broadcasts), 1)

    def test_sender_receiver_amount_and_fee_constraints(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, _, ledger = setup(tmp)
            policy = trial_policy(tmp); rec = make_awaiting(ext, flow); order = order_of(ext, flow, rec)
            for changes in ({"user_eoa": MAC}, {"receiver": SENDER}, {"amount_sun": 1_000_000}, {"amount_sun": 3_000_000}, {"fee_cap_sun": 2_000_001}):
                with self.subTest(changes=changes): self.assertFalse(ledger.reserve({**order, **changes}, policy)["ok"])
            self.assertEqual(ledger._read(), {})

    def test_release_does_not_reset_lifetime_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, _, ledger = setup(tmp)
            policy = trial_policy(tmp); order = order_of(ext, flow, make_awaiting(ext, flow))
            ledger.reserve(order, policy); ledger.release(order["payment_id"], "mock no signing attempted")
            clock.t += 86400
            result = ledger.reserve({**order, "payment_id": "new-order", "tx_id": "b" * 64}, policy)
            self.assertFalse(result["ok"])
            self.assertIn("lifetime", result["error"])

    def test_concurrent_new_reservations_share_lifetime_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, _, ledger = setup(tmp)
            policy = trial_policy(tmp); order = order_of(ext, flow, make_awaiting(ext, flow)); results = []
            def reserve(n): results.append(ledger.reserve({**order, "payment_id": "order-" + str(n)}, policy))
            threads = [threading.Thread(target=reserve, args=(n,)) for n in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(sum(bool(r["ok"]) for r in results), 1)
            self.assertEqual(len(ledger._read()), 1)

    def test_valid_json_missing_day_or_changed_cost_blocks(self):
        for mutation in ("day", "amount", "binding", "state"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                clock, node, flow, ext, signer, policy, ledger = setup(tmp)
                order = order_of(ext, flow, make_awaiting(ext, flow)); ledger.reserve(order, policy); ledger.consume(order["payment_id"])
                lines = [json.loads(line) for line in ledger.path.read_text().splitlines()]
                if mutation == "day": del lines[-1]["day"]
                elif mutation == "amount": lines[-1]["cost_sun"] = 1
                elif mutation == "binding": lines[-1]["receiver"] = SENDER
                else: lines[0]["state"] = "CONSUMED"
                ledger.path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
                with self.assertRaises(SG.LedgerError): ledger.reserve({**order, "payment_id": "next"}, policy)


class DurableStorageAndConfigTests(unittest.TestCase):
    def test_intent_write_is_private_and_fsyncs_before_and_after_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext = phone_intent.IntentStore(pathlib.Path(tmp)); events = []
            fsync = os.fsync; replace = os.replace
            def synced(fd):
                events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
                fsync(fd)
            def replaced(src, dest):
                self.assertEqual(stat.S_IMODE(os.stat(src).st_mode), 0o600)
                events.append("replace"); replace(src, dest)
            with patch.object(phone_intent.os, "fsync", side_effect=synced), patch.object(phone_intent.os, "replace", side_effect=replaced):
                ext._write({"intent_id": "durable", "exec": {"stage": "SIGNED", "signed_tx": {"fixture": True}}})
            self.assertEqual(events, ["file", "replace", "directory"])
            self.assertEqual(stat.S_IMODE(ext._path("durable").stat().st_mode), 0o600)

    def test_signing_checkpoint_failure_never_calls_signer(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp)
            rec = make_awaiting(ext, flow); write = ext._write
            def fail_checkpoint(value):
                if (value.get("exec") or {}).get("stage") == "SIGNING":
                    with patch.object(phone_intent.os, "fsync", side_effect=OSError("mock fsync failure")):
                        return write(value)
                return write(value)
            with patch.object(ext, "_write", side_effect=fail_checkpoint), self.assertRaises(OSError):
                approve(ext, flow, signer, policy, ledger, rec)
            self.assertEqual(signer.sign_count, 0)
            self.assertEqual(node.broadcasts, [])
            self.assertEqual(ledger.state_of(rec["payment_id"])["state"], "RESERVED")

    def test_unsigned_signer_release_error_is_visible_and_keeps_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, node, flow, ext, signer, policy, ledger = setup(tmp, signer=SG.NoSigner())
            rec = make_awaiting(ext, flow)
            with patch.object(ledger, "release", side_effect=SG.LedgerError("mock disk failure")):
                result = approve(ext, flow, signer, policy, ledger, rec)
            self.assertEqual(result["state"], "SIGN_FAILED")
            self.assertIn("reservation retained", result["intent"]["exec"]["ledger_note"])
            self.assertEqual(ledger.state_of(rec["payment_id"])["state"], "RESERVED")
            self.assertEqual(node.broadcasts, [])

    def test_local_config_requires_exact_explicit_policy_before_key_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "trial.json"; path.write_text(json.dumps(policy_json()))
            policy = phone_server.local_trial_policy(str(path), CONTACTS)
            self.assertTrue(policy.is_one_shot_trial())
            with self.assertRaises(ValueError): SG.load_trial_policy(None, stop_file=pathlib.Path(tmp) / "stop")
            for change in ({"lifetime_max_transactions": 2}, {"exact_amount_sun": 1_000_000}, {"network": "mainnet"}, {"version": True}):
                path.write_text(json.dumps({**policy_json(), **change}))
                with self.assertRaises(ValueError): phone_server.local_trial_policy(str(path), CONTACTS)
            path.write_text(json.dumps(policy_json()))
            with self.assertRaises(ValueError): phone_server.local_trial_policy(str(path), [])
            with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["phone_server", "--signer", "local"]), patch.object(SG, "build_signer") as build:
                with self.assertRaises(SystemExit) as exc: phone_server.main()
                self.assertEqual(exc.exception.code, 2)
                build.assert_not_called()

    def test_private_files_reject_open_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "fixture"; SG._write_private_new(path, b"public-test-fixture")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(SG._read_private(path), b"public-test-fixture")
            path.chmod(0o644)
            with self.assertRaises(RuntimeError): SG._read_private(path)
            path.chmod(0o600); link = pathlib.Path(tmp) / "link"; link.symlink_to(path)
            with self.assertRaises(OSError): SG._read_private(link)
            with self.assertRaises(FileExistsError): SG._write_private_new(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"public-test-fixture")


if __name__ == "__main__":
    unittest.main()
