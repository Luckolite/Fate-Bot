"""Mute duration parsing preserves units, zero timers, and the remaining reason."""

import unittest

from cogs.moderation.mod import _parse_mute_duration


class MuteDurationChecks(unittest.TestCase):
    def test_multiple_units_are_combined_in_input_order(self):
        self.assertEqual(
            _parse_mute_duration("Spamming 1d 2h 3m 4s"),
            (93784, "1 day, 2 hours, 3 minutes, 4 seconds", "Spamming"),
        )

    def test_zero_timer_is_distinct_from_an_indefinite_mute(self):
        self.assertEqual(_parse_mute_duration("0s"), (0, "0 seconds", ""))
        self.assertEqual(_parse_mute_duration("Spamming"), (None, None, "Spamming"))

    def test_repeated_adjacent_and_embedded_tokens_keep_existing_behavior(self):
        self.assertEqual(_parse_mute_duration("2m2m"), (240, "2 minutes, 2 minutes", ""))
        self.assertEqual(_parse_mute_duration("wait2mplease"), (120, "2 minutes", "waitplease"))
        self.assertEqual(_parse_mute_duration("1h1m"), (3660, "1 hour, 1 minute", ""))

    def test_leading_zeros_case_and_whitespace_are_preserved(self):
        self.assertEqual(_parse_mute_duration("  01m  reason  "), (60, "01 minutes", "reason"))
        self.assertEqual(_parse_mute_duration("2H"), (None, None, "2H"))
        self.assertEqual(_parse_mute_duration("\t2m\treason\t"), (120, "2 minutes", "\t\treason\t"))


if __name__ == "__main__":
    unittest.main()
