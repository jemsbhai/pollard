import unittest

from calculator import clamp


class ClampTests(unittest.TestCase):
    def test_below_range_returns_lower_bound(self) -> None:
        self.assertEqual(clamp(-4, 0, 10), 0)

    def test_value_in_range_is_unchanged(self) -> None:
        self.assertEqual(clamp(6, 0, 10), 6)

    def test_above_range_returns_upper_bound(self) -> None:
        self.assertEqual(clamp(14, 0, 10), 10)

    def test_reversed_bounds_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            clamp(4, 10, 0)


if __name__ == "__main__":
    unittest.main()
