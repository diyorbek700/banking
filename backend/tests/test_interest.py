import unittest
from datetime import date

from autosave.interest import InterestAccrualService, daily_interest, is_month_end
from autosave.money import sum_
from autosave.payments import CardPaymentService, PaymentRequest

from helpers import fixed_clock, make_env


class DailyInterestFormulaTest(unittest.TestCase):
    def test_rates_between_12_and_20_percent(self):
        # 1 000 000 sum × rate / 365, rounded to the tiyin
        expected = {"12.00": "328.77", "15.00": "410.96", "18.00": "493.15", "20.00": "547.95"}
        for rate, per_day in expected.items():
            with self.subTest(rate=rate):
                self.assertEqual(daily_interest(sum_(1_000_000), rate), sum_(per_day))

    def test_fractional_rate(self):
        # 185 000 sum × 18.5% / 365 = 93.767... sum
        self.assertEqual(daily_interest(sum_(185_000), "18.50"), sum_("93.77"))

    def test_zero_balance_earns_nothing(self):
        self.assertEqual(daily_interest(0, "18.00"), 0)

    def test_small_balance_rounds_to_tiyin(self):
        # 1 600 sum × 18% / 365 = 0.789 sum -> 79 tiyin
        self.assertEqual(daily_interest(sum_(1600), "18.00"), 79)

    def test_bankers_rounding_on_exact_half(self):
        # 365 tiyin × 50% / 365 = 0.5 tiyin -> rounds to even (0); 1095 tiyin -> 1.5 -> 2
        self.assertEqual(daily_interest(365, "50"), 0)
        self.assertEqual(daily_interest(1095, "50"), 2)

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            daily_interest(-1, "18")
        with self.assertRaises(ValueError):
            daily_interest(100, "101")

    def test_month_end(self):
        self.assertTrue(is_month_end(date(2026, 9, 30)))
        self.assertTrue(is_month_end(date(2028, 2, 29)))
        self.assertFalse(is_month_end(date(2026, 9, 29)))


class EodAccrualTest(unittest.TestCase):
    def setUp(self):
        self.env = make_env(deposit_balance=1_000_000, rate="18.00", opened_on="2026-09-10")
        self.service = InterestAccrualService(self.env.conn, fixed_clock)

    def accruals(self):
        return self.env.conn.execute(
            "SELECT accrual_date, amount FROM interest_accruals ORDER BY accrual_date").fetchall()

    def test_single_day_accrues_without_touching_principal(self):
        report = self.service.run_eod(date(2026, 9, 10))
        dep = self.env.deposit()
        self.assertEqual(report.days_accrued, 1)
        self.assertEqual(dep["accrued_interest_balance"], sum_("493.15"))
        self.assertEqual(dep["balance"], sum_(1_000_000))
        self.assertEqual(dep["last_interest_accrual_date"], "2026-09-10")
        audit, = self.env.audit("INTEREST_ACCRUED")
        self.assertEqual((audit["amount"], audit["interest_rate_annual"]), (sum_("493.15"), "18.00"))

    def test_rerun_for_same_day_is_idempotent(self):
        self.service.run_eod(date(2026, 9, 10))
        report = self.service.run_eod(date(2026, 9, 10))
        self.assertEqual(report.days_accrued, 0)
        self.assertEqual(len(self.accruals()), 1)
        self.assertEqual(self.env.deposit()["accrued_interest_balance"], sum_("493.15"))

    def test_missed_days_are_caught_up(self):
        self.service.run_eod(date(2026, 9, 10))
        report = self.service.run_eod(date(2026, 9, 13))
        self.assertEqual(report.days_accrued, 3)
        self.assertEqual([r["accrual_date"] for r in self.accruals()],
                         ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13"])
        self.assertEqual(self.env.deposit()["accrued_interest_balance"], 4 * sum_("493.15"))

    def test_month_end_capitalizes_accrued_interest(self):
        report = self.service.run_eod(date(2026, 9, 30))  # 21 days: Sep 10..30
        dep = self.env.deposit()
        interest = 21 * sum_("493.15")
        self.assertEqual(report.interest_capitalized, interest)
        self.assertEqual(dep["balance"], sum_(1_000_000) + interest)
        self.assertEqual(dep["accrued_interest_balance"], 0)
        cap, = self.env.transactions("INTEREST_CAPITALIZATION")
        self.assertEqual(cap["amount"], interest)
        self.env.assert_ledger_balanced(self)
        self.assertEqual(len(self.env.audit("INTEREST_CAPITALIZED")), 1)

    def test_interest_compounds_after_capitalization(self):
        self.service.run_eod(date(2026, 10, 1))
        oct_1, = [r for r in self.accruals() if r["accrual_date"] == "2026-10-01"]
        new_balance = sum_(1_000_000) + 21 * sum_("493.15")
        self.assertEqual(oct_1["amount"], daily_interest(new_balance, "18.00"))
        self.assertGreater(oct_1["amount"], sum_("493.15"))

    def test_round_ups_raise_the_interest_base(self):
        CardPaymentService(self.env.conn, fixed_clock).process_card_payment(
            PaymentRequest(1, 1, "Korzinka", sum_(23400), "k"))
        self.service.run_eod(date(2026, 9, 10))
        self.assertEqual(self.accruals()[0]["amount"], daily_interest(sum_(1_001_600), "18.00"))

    def test_blocked_deposit_still_earns_interest(self):
        self.env.conn.execute("UPDATE deposit_accounts SET status = 'BLOCKED'")
        self.assertEqual(self.service.run_eod(date(2026, 9, 10)).days_accrued, 1)

    def test_nothing_accrues_before_opening_date(self):
        self.assertEqual(self.service.run_eod(date(2026, 9, 9)).days_accrued, 0)

    def test_failure_on_one_account_does_not_stop_others(self):
        self.env.conn.execute(
            """INSERT INTO deposit_accounts (id, user_id, account_number, balance, interest_rate_annual, opened_on)
               VALUES (2, 2, '20206000491100000002', ?, '12.00', '2026-09-10')""", (sum_(500_000),))
        # Pre-existing row for account 1 makes its INSERT violate the unique key.
        self.env.conn.execute(
            """INSERT INTO interest_accruals (deposit_account_id, accrual_date, balance_snapshot,
               interest_rate_annual, amount, created_at) VALUES (1, '2026-09-10', 0, '18.00', 0, 'x')""")
        report = self.service.run_eod(date(2026, 9, 10))
        self.assertEqual([e[0] for e in report.errors], [1])
        self.assertEqual(report.accounts_processed, 1)
        self.assertEqual(self.env.conn.execute(
            "SELECT accrued_interest_balance FROM deposit_accounts WHERE id = 2").fetchone()[0],
            daily_interest(sum_(500_000), "12.00"))
        self.assertIsNone(self.env.deposit()["last_interest_accrual_date"])
        self.assertEqual(len(self.env.audit("INTEREST_ACCRUAL_FAILED")), 1)


if __name__ == "__main__":
    unittest.main()
