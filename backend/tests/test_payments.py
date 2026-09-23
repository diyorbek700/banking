"""Integration tests: card payment + Me-to-Me round-up on a real SQLite database."""
import unittest
from datetime import timedelta
from unittest import mock

from autosave.money import sum_
from autosave.outbox import PushDispatcher, RoundUpRetryWorker
from autosave.payments import (CardPaymentService, IdempotencyConflict, PaymentDeclined, PaymentRequest,
                               RoundUpEngine, RoundUpStatus, SkipReason)

from helpers import NOW, fixed_clock, make_env


def pay(env, amount_sum, key="p-1", merchant="Korzinka"):
    service = CardPaymentService(env.conn, fixed_clock)
    return service.process_card_payment(PaymentRequest(env.user_id, env.card_id, merchant, sum_(amount_sum), key))


class SuccessfulRoundUpTest(unittest.TestCase):
    def setUp(self):
        self.env = make_env(card_balance=300_000, deposit_balance=183_400, step=5000)
        self.result = pay(self.env, 23400)

    def test_purchase_and_round_up_are_both_applied(self):
        self.assertEqual(self.result.round_up_status, RoundUpStatus.COMPLETED)
        self.assertEqual(self.result.round_up_amount, sum_(1600))
        self.assertEqual(self.env.card_balance(), sum_(300_000 - 23_400 - 1_600))
        self.assertEqual(self.env.deposit()["balance"], sum_(185_000))
        self.assertEqual(self.result.deposit_balance, sum_(185_000))

    def test_round_up_transaction_is_linked_to_the_payment(self):
        payment, = self.env.transactions("CARD_PAYMENT_ACQUIRING")
        round_up, = self.env.transactions("SAVINGS_ROUND_UP_ME_TO_ME")
        self.assertEqual(payment["amount"], sum_(23400))
        self.assertEqual(payment["merchant_name"], "Korzinka")
        self.assertEqual(round_up["parent_transaction_id"], payment["id"])
        self.assertEqual(round_up["status"], "COMPLETED")
        self.assertEqual(round_up["amount"], sum_(1600))

    def test_ledger_is_double_entry(self):
        self.env.assert_ledger_balanced(self)
        entries = self.env.conn.execute(
            "SELECT account_type, direction, amount FROM ledger_entries WHERE transaction_id = ?",
            (self.result.round_up_transaction_id,)).fetchall()
        self.assertEqual(sorted(map(tuple, entries)),
                         [("CARD", "DEBIT", sum_(1600)), ("DEPOSIT", "CREDIT", sum_(1600))])

    def test_one_combined_push_is_queued(self):
        pushes = self.env.outbox("PUSH_NOTIFICATION")
        self.assertEqual(len(pushes), 1)
        self.assertEqual(
            pushes[0]["payload"]["text"],
            "Оплата 23 400 сум. В копилку (18% годовых): +1 600 сум 🎯 (Баланс копилки: 185 000 сум)")

    def test_push_dispatcher_delivers_it_once(self):
        sent = []
        dispatcher = PushDispatcher(self.env.conn, lambda user, text: sent.append((user, text)), fixed_clock)
        dispatcher.run_once()
        dispatcher.run_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], self.env.user_id)
        self.assertEqual(self.env.outbox("PUSH_NOTIFICATION", "PENDING"), [])

    def test_success_is_audited(self):
        completed, = self.env.audit("ROUND_UP_COMPLETED")
        self.assertEqual(completed["round_up_amount"], sum_(1600))


class SafetyThresholdTest(unittest.TestCase):
    def test_round_up_skipped_when_card_would_drop_below_threshold(self):
        # 70 000 - 23 400 - 1 600 = 45 000 < 50 000
        env = make_env(card_balance=70_000, safety_threshold=50_000)
        result = pay(env, 23400)

        self.assertEqual(result.round_up_status, RoundUpStatus.SKIPPED)
        self.assertEqual(result.skip_reason, SkipReason.BELOW_SAFETY_THRESHOLD)
        self.assertEqual(env.card_balance(), sum_(70_000 - 23_400))  # purchase went through
        self.assertEqual(env.deposit()["balance"], sum_(183_400))    # deposit untouched
        self.assertEqual(env.transactions("SAVINGS_ROUND_UP_ME_TO_ME"), [])
        skipped, = env.audit("ROUND_UP_SKIPPED")
        self.assertEqual(skipped["reason"], "BELOW_SAFETY_THRESHOLD")
        self.assertEqual(skipped["card_balance_after_payment"], sum_(46_600))
        text = env.outbox("PUSH_NOTIFICATION")[0]["payload"]["text"]
        self.assertEqual(text, "Оплата 23 400 сум. Округление в копилку пропущено: "
                               "на карте должно оставаться не меньше 50 000 сум.")

    def test_landing_exactly_on_threshold_is_allowed(self):
        env = make_env(card_balance=75_000, safety_threshold=50_000)
        result = pay(env, 23400)
        self.assertEqual(result.round_up_status, RoundUpStatus.COMPLETED)
        self.assertEqual(env.card_balance(), sum_(50_000))

    def test_one_tiyin_below_threshold_is_skipped(self):
        env = make_env(card_balance="74999.99", safety_threshold=50_000)
        self.assertEqual(pay(env, 23400).skip_reason, SkipReason.BELOW_SAFETY_THRESHOLD)

    def test_enough_for_purchase_but_not_for_round_up(self):
        env = make_env(card_balance=24_000, safety_threshold=0)
        result = pay(env, 23400)
        self.assertEqual(result.skip_reason, SkipReason.BELOW_SAFETY_THRESHOLD)
        self.assertEqual(env.card_balance(), sum_(600))


class SkipAndDeclineTest(unittest.TestCase):
    def test_insufficient_funds_declines_the_purchase(self):
        env = make_env(card_balance=10_000)
        with self.assertRaises(PaymentDeclined) as ctx:
            pay(env, 23400)
        self.assertEqual(ctx.exception.reason, "INSUFFICIENT_FUNDS")
        self.assertEqual(env.card_balance(), sum_(10_000))
        self.assertEqual(env.transactions(), [])
        self.assertEqual(env.audit("CARD_PAYMENT_DECLINED")[0]["reason"], "INSUFFICIENT_FUNDS")

    def test_disabled_feature_skips_round_up(self):
        env = make_env(enabled=False)
        result = pay(env, 23400)
        self.assertEqual(result.skip_reason, SkipReason.DISABLED)
        self.assertEqual(env.card_balance(), sum_(300_000 - 23_400))
        self.assertEqual(env.outbox("PUSH_NOTIFICATION")[0]["payload"]["text"], "Оплата 23 400 сум.")

    def test_blocked_deposit_skips_round_up(self):
        env = make_env(deposit_status="BLOCKED")
        result = pay(env, 23400)
        self.assertEqual(result.skip_reason, SkipReason.DEPOSIT_BLOCKED)
        self.assertEqual(env.deposit()["balance"], sum_(183_400))

    def test_purchase_multiple_of_step_has_no_round_up(self):
        env = make_env(step=5000)
        result = pay(env, 25000)
        self.assertEqual(result.skip_reason, SkipReason.AMOUNT_MULTIPLE_OF_STEP)
        self.assertEqual(env.transactions("SAVINGS_ROUND_UP_ME_TO_ME"), [])

    def test_deposit_of_another_user_is_never_credited(self):
        env = make_env()
        env.conn.execute("UPDATE deposit_accounts SET user_id = 2 WHERE id = 1")
        result = pay(env, 23400)
        self.assertEqual(result.skip_reason, SkipReason.NO_TARGET_DEPOSIT)
        self.assertEqual(env.deposit()["balance"], sum_(183_400))


class IdempotencyTest(unittest.TestCase):
    def test_repeated_request_charges_once(self):
        env = make_env()
        first = pay(env, 23400, key="same")
        second = pay(env, 23400, key="same")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.payment_transaction_id, first.payment_transaction_id)
        self.assertEqual(second.round_up_status, RoundUpStatus.COMPLETED)
        self.assertEqual(env.card_balance(), sum_(300_000 - 25_000))
        self.assertEqual(len(env.transactions()), 2)

    def test_replay_of_skipped_round_up_keeps_reason(self):
        env = make_env(card_balance=70_000)
        pay(env, 23400, key="k")
        self.assertEqual(pay(env, 23400, key="k").skip_reason, SkipReason.BELOW_SAFETY_THRESHOLD)

    def test_key_reused_for_different_payment_is_rejected(self):
        env = make_env()
        pay(env, 23400, key="k")
        with self.assertRaises(IdempotencyConflict):
            pay(env, 99000, key="k")


class GracefulDegradationTest(unittest.TestCase):
    """A failing deposit credit must never fail the purchase."""

    def setUp(self):
        self.env = make_env(card_balance=300_000)
        with mock.patch.object(RoundUpEngine, "post", side_effect=RuntimeError("deposit core timeout")):
            self.result = pay(self.env, 23400)

    def test_purchase_commits_and_round_up_is_rolled_back(self):
        self.assertEqual(self.result.round_up_status, RoundUpStatus.FAILED)
        self.assertEqual(self.env.card_balance(), sum_(300_000 - 23_400))
        self.assertEqual(self.env.deposit()["balance"], sum_(183_400))
        payment, = self.env.transactions("CARD_PAYMENT_ACQUIRING")
        self.assertEqual(payment["status"], "COMPLETED")
        failed, = self.env.transactions("SAVINGS_ROUND_UP_ME_TO_ME")
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("deposit core timeout", failed["failure_reason"])
        self.assertEqual(self.env.conn.execute(
            "SELECT COUNT(*) FROM ledger_entries WHERE transaction_id = ?", (failed["id"],)).fetchone()[0], 0)

    def test_partial_posting_inside_savepoint_is_undone(self):
        env = make_env(card_balance=300_000)
        real_post = RoundUpEngine.post

        def post_then_crash(engine, conn, **kwargs):
            real_post(engine, conn, **kwargs)  # both balances already changed...
            raise RuntimeError("crash after posting")  # ...then the savepoint must undo them

        with mock.patch.object(RoundUpEngine, "post", post_then_crash):
            result = pay(env, 23400)
        self.assertEqual(result.round_up_status, RoundUpStatus.FAILED)
        self.assertEqual(env.card_balance(), sum_(300_000 - 23_400))
        self.assertEqual(env.deposit()["balance"], sum_(183_400))
        self.assertEqual(env.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status = 'COMPLETED' AND type = 'SAVINGS_ROUND_UP_ME_TO_ME'"
        ).fetchone()[0], 0)
        env.assert_ledger_balanced(self)

    def test_customer_still_gets_payment_push_and_retry_is_queued(self):
        self.assertEqual(self.env.outbox("PUSH_NOTIFICATION")[0]["payload"]["text"], "Оплата 23 400 сум.")
        retry, = self.env.outbox("ROUND_UP_RETRY", "PENDING")
        self.assertEqual(retry["payload"]["payment_transaction_id"], self.result.payment_transaction_id)
        self.assertEqual(len(self.env.audit("ROUND_UP_FAILED")), 1)

    def test_retry_worker_completes_round_up_later(self):
        report = RoundUpRetryWorker(self.env.conn, fixed_clock).run_once()
        self.assertEqual(report.succeeded, 1)
        self.assertEqual(self.env.card_balance(), sum_(300_000 - 25_000))
        self.assertEqual(self.env.deposit()["balance"], sum_(185_000))
        completed = [t for t in self.env.transactions("SAVINGS_ROUND_UP_ME_TO_ME") if t["status"] == "COMPLETED"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["parent_transaction_id"], self.result.payment_transaction_id)
        self.assertEqual(self.env.outbox("PUSH_NOTIFICATION")[-1]["payload"]["text"],
                         "В копилку (18% годовых): +1 600 сум 🎯 (Баланс копилки: 185 000 сум)")
        # Running again does nothing: the event is done and the unique index forbids a second credit.
        self.assertEqual(RoundUpRetryWorker(self.env.conn, fixed_clock).run_once().processed, 0)
        self.env.assert_ledger_balanced(self)

    def test_retry_rechecks_safety_threshold(self):
        # The client spent money after the purchase: 276 600 -> 51 000, so +1 600 would break 50 000.
        self.env.conn.execute("UPDATE card_accounts SET balance = ?", (sum_(51_000),))
        report = RoundUpRetryWorker(self.env.conn, fixed_clock).run_once()
        self.assertEqual(report.skipped, 1)
        self.assertEqual(self.env.card_balance(), sum_(51_000))
        self.assertEqual(self.env.outbox("ROUND_UP_RETRY", "PENDING"), [])
        self.assertTrue(self.env.audit("ROUND_UP_SKIPPED")[-1]["on_retry"])

    def test_retry_backs_off_and_gives_up(self):
        clock_now = [NOW]
        worker = RoundUpRetryWorker(self.env.conn, lambda: clock_now[0], max_attempts=2,
                                    backoff=timedelta(minutes=1))
        with mock.patch.object(RoundUpEngine, "post", side_effect=RuntimeError("still down")):
            self.assertEqual(worker.run_once().failed, 1)
            self.assertEqual(worker.run_once().processed, 0)  # backoff not elapsed yet
            clock_now[0] = NOW + timedelta(minutes=5)
            self.assertEqual(worker.run_once().failed, 1)
        event, = self.env.outbox("ROUND_UP_RETRY")
        self.assertEqual((event["status"], event["attempts"]), ("FAILED", 2))
        self.assertEqual(self.env.card_balance(), sum_(300_000 - 23_400))


if __name__ == "__main__":
    unittest.main()
