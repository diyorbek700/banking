import unittest

from autosave.money import sum_
from autosave.roundup import ALLOWED_STEPS, calculate_round_up


class CalculateRoundUpTest(unittest.TestCase):
    def test_spec_example(self):
        # 23 400 sum with step 5 000 -> ceiling 25 000 -> 1 600 sum
        self.assertEqual(calculate_round_up(sum_(23400), sum_(5000)), sum_(1600))

    def test_each_product_step(self):
        cases = {1000: 600, 5000: 1600, 10000: 6600}
        for step, expected in cases.items():
            with self.subTest(step=step):
                self.assertEqual(calculate_round_up(sum_(23400), sum_(step)), sum_(expected))

    def test_amount_already_multiple_of_step_gives_zero(self):
        for amount, step in [(5000, 5000), (25000, 5000), (1000, 1000), (10000, 1000), (100000, 10000)]:
            with self.subTest(amount=amount, step=step):
                self.assertEqual(calculate_round_up(sum_(amount), sum_(step)), 0)

    def test_one_tiyin_above_a_multiple_rounds_up_almost_a_full_step(self):
        self.assertEqual(calculate_round_up(sum_("25000.01"), sum_(5000)), sum_("4999.99"))

    def test_one_tiyin_below_a_multiple_rounds_up_one_tiyin(self):
        self.assertEqual(calculate_round_up(sum_("24999.99"), sum_(5000)), 1)

    def test_amount_smaller_than_step(self):
        self.assertEqual(calculate_round_up(sum_(700), sum_(1000)), sum_(300))
        self.assertEqual(calculate_round_up(1, sum_(1000)), sum_(1000) - 1)

    def test_amount_with_tiyin(self):
        self.assertEqual(calculate_round_up(sum_("46350.55"), sum_(1000)), sum_("649.45"))

    def test_large_amount(self):
        self.assertEqual(calculate_round_up(sum_(987_654_321), sum_(10000)), sum_(5679))

    def test_result_always_reaches_next_multiple_and_is_less_than_step(self):
        for step in ALLOWED_STEPS:
            for amount in range(1, 3 * step, 997):
                r = calculate_round_up(amount, step)
                self.assertTrue(0 <= r < step)
                self.assertEqual((amount + r) % step, 0)

    def test_rejects_non_positive_amount(self):
        for amount in (0, -1, -sum_(100)):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                calculate_round_up(amount, sum_(1000))

    def test_rejects_non_positive_step(self):
        with self.assertRaises(ValueError):
            calculate_round_up(sum_(100), 0)

    def test_rejects_floats(self):
        with self.assertRaises(TypeError):
            calculate_round_up(23400.5, sum_(1000))
        with self.assertRaises(TypeError):
            sum_(23400.5)


if __name__ == "__main__":
    unittest.main()
