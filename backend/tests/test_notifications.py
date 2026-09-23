import unittest

from autosave.money import sum_
from autosave.notifications import format_rate, format_sum, payment_push_text


class FormattingTest(unittest.TestCase):
    def test_format_sum(self):
        self.assertEqual(format_sum(sum_(23400)), "23 400 сум")
        self.assertEqual(format_sum(sum_(185000)), "185 000 сум")
        self.assertEqual(format_sum(sum_("1599.50")), "1 599,50 сум")
        self.assertEqual(format_sum(sum_(0)), "0 сум")
        self.assertEqual(format_sum(sum_(1_250_000)), "1 250 000 сум")

    def test_format_rate(self):
        self.assertEqual(format_rate("18.00"), "18")
        self.assertEqual(format_rate("20"), "20")
        self.assertEqual(format_rate("18.50"), "18,5")
        self.assertEqual(format_rate("12.25"), "12,25")


class PaymentPushTest(unittest.TestCase):
    def test_combined_text_from_spec(self):
        text = payment_push_text(sum_(23400), round_up_amount=sum_(1600), interest_rate_annual="18.00",
                                 deposit_balance=sum_(185000))
        self.assertEqual(text, "Оплата 23 400 сум. В копилку (18% годовых): +1 600 сум 🎯 (Баланс копилки: 185 000 сум)")

    def test_payment_only(self):
        self.assertEqual(payment_push_text(sum_(23400)), "Оплата 23 400 сум.")

    def test_never_mentions_card_balance(self):
        text = payment_push_text(sum_(23400), round_up_amount=sum_(1600), interest_rate_annual="18.00",
                                 deposit_balance=sum_(185000))
        self.assertNotIn("карт", text.lower())


if __name__ == "__main__":
    unittest.main()
