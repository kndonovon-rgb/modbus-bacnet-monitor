"""The rules, driven through the three ways naive monitoring fails."""
import unittest

from dcmon.rules import ALARM, OK, STALE, UNKNOWN, Rule, RuleSet, render


def settle(rule, value, times=3, now=100.0, ts=100.0):
    out = None
    for _ in range(times):
        out = rule.evaluate(value, ts=ts, now=now)
    return out


class Confirmation(unittest.TestCase):
    def test_a_rule_starts_unknown_not_ok(self):
        """Nothing has been measured. Claiming health would be a guess."""
        r = Rule("p", trip=80, clear=77)
        self.assertEqual(r.state, UNKNOWN)

    def test_one_breach_does_not_alarm(self):
        r = Rule("p", trip=80, clear=77, strikes=2)
        state, transitioned, _ = r.evaluate(85, ts=1.0, now=1.0)
        self.assertNotEqual(state, ALARM)
        self.assertFalse(transitioned)

    def test_the_second_consecutive_breach_alarms(self):
        r = Rule("p", trip=80, clear=77, strikes=2)
        r.evaluate(85, ts=1.0, now=1.0)
        state, transitioned, detail = r.evaluate(86, ts=2.0, now=2.0)
        self.assertEqual(state, ALARM)
        self.assertTrue(transitioned)
        self.assertIn("2 consecutive", detail)

    def test_an_interruption_resets_the_count(self):
        """Two breaches either side of a good reading are not consecutive."""
        r = Rule("p", trip=80, clear=77, strikes=3)
        r.evaluate(85, ts=1.0, now=1.0)
        r.evaluate(70, ts=2.0, now=2.0)
        state, _, _ = r.evaluate(85, ts=3.0, now=3.0)
        self.assertNotEqual(state, ALARM)

    def test_the_strike_counter_does_not_run_away(self):
        """It once reported '7 of 2', which reads as a bug to an operator."""
        r = Rule("p", trip=80, clear=77, strikes=2)
        settle(r, 90, times=8)
        _, _, detail = r.evaluate(90, ts=1.0, now=1.0)
        self.assertNotIn(" of 2", detail)
        self.assertIn("still", detail)

    def test_a_settled_ok_point_reports_just_its_value(self):
        r = Rule("p", trip=80, clear=77, strikes=2)
        settle(r, 70, times=6)
        _, transitioned, detail = r.evaluate(70, ts=1.0, now=1.0)
        self.assertFalse(transitioned)
        self.assertEqual(detail, "70")


class Hysteresis(unittest.TestCase):
    """One threshold flaps; a flapping point gets muted; muted is unmonitored."""

    def setUp(self):
        self.r = Rule("p", trip=80, clear=77, strikes=1)
        self.r.evaluate(85, ts=1.0, now=1.0)
        self.assertEqual(self.r.state, ALARM)

    def test_inside_the_deadband_holds_the_alarm(self):
        state, transitioned, detail = self.r.evaluate(78.5, ts=2.0, now=2.0)
        self.assertEqual(state, ALARM)
        self.assertFalse(transitioned)
        self.assertIn("deadband", detail)

    def test_passing_the_clear_level_clears(self):
        state, transitioned, _ = self.r.evaluate(76, ts=2.0, now=2.0)
        self.assertEqual(state, OK)
        self.assertTrue(transitioned)

    def test_oscillating_across_the_trip_point_produces_one_transition(self):
        transitions = 0
        for v in (79.9, 80.1, 79.8, 80.2, 79.5, 80.5):
            _, t, _ = self.r.evaluate(v, ts=3.0, now=3.0)
            transitions += 1 if t else 0
        self.assertEqual(transitions, 0, "a single threshold would have flapped")

    def test_deadband_is_reported(self):
        self.assertAlmostEqual(self.r.deadband, 3.0)

    def test_a_below_rule_works_the_other_way(self):
        r = Rule("v", trip=456, clear=465, direction="below", strikes=1)
        self.assertEqual(r.evaluate(450, ts=1.0, now=1.0)[0], ALARM)
        self.assertEqual(r.evaluate(460, ts=2.0, now=2.0)[0], ALARM)   # deadband
        self.assertEqual(r.evaluate(470, ts=3.0, now=3.0)[0], OK)

    def test_an_inverted_deadband_is_refused_at_construction(self):
        """Clearing above the trip point can never clear. Fail loudly."""
        with self.assertRaises(ValueError):
            Rule("p", trip=80, clear=85, direction="above")
        with self.assertRaises(ValueError):
            Rule("v", trip=456, clear=450, direction="below")

    def test_an_unknown_direction_is_refused(self):
        with self.assertRaises(ValueError):
            Rule("p", trip=1, clear=1, direction="sideways")


class Staleness(unittest.TestCase):
    """The failure this whole project is about.

    A device that stops answering leaves its last good value in place. It
    looks like a measurement, it is inside every limit, and a monitor that
    does not age it will report health indefinitely.
    """

    def test_a_fresh_reading_is_evaluated_normally(self):
        r = Rule("p", trip=80, clear=77, strikes=1, max_age=60)
        state, _, _ = r.evaluate(70, ts=1000.0, now=1010.0)
        self.assertEqual(state, OK)

    def test_an_old_reading_is_stale_however_healthy_it_looks(self):
        r = Rule("p", trip=80, clear=77, strikes=1, max_age=60)
        r.evaluate(70, ts=1000.0, now=1000.0)
        self.assertEqual(r.state, OK)
        state, transitioned, detail = r.evaluate(70, ts=1000.0, now=1200.0)
        self.assertEqual(state, STALE)
        self.assertTrue(transitioned)
        self.assertIn("old", detail)

    def test_stale_is_never_reported_as_ok(self):
        r = Rule("p", trip=80, clear=77, strikes=1, max_age=10)
        r.evaluate(70, ts=0.0, now=0.0)
        for age in (11, 50, 5000):
            state, _, _ = r.evaluate(70, ts=0.0, now=age)
            self.assertNotEqual(state, OK)
            self.assertEqual(state, STALE)

    def test_recovery_from_stale_still_needs_confirmation(self):
        r = Rule("p", trip=80, clear=77, strikes=2, max_age=10)
        r.evaluate(70, ts=0.0, now=20.0)
        self.assertEqual(r.state, STALE)
        state, _, _ = r.evaluate(70, ts=21.0, now=21.0)
        self.assertEqual(state, STALE, "one good poll should not clear it")
        state, transitioned, _ = r.evaluate(70, ts=22.0, now=22.0)
        self.assertEqual(state, OK)
        self.assertTrue(transitioned)

    def test_a_breach_arriving_out_of_staleness_still_needs_strikes(self):
        r = Rule("p", trip=80, clear=77, strikes=2, max_age=10)
        r.evaluate(70, ts=0.0, now=20.0)
        state, _, _ = r.evaluate(99, ts=21.0, now=21.0)
        self.assertNotEqual(state, OK)
        state, _, _ = r.evaluate(99, ts=22.0, now=22.0)
        self.assertEqual(state, ALARM)

    def test_a_failed_poll_is_not_a_value_of_zero(self):
        """Treating a failed read as 0 would clear a high-limit alarm."""
        r = Rule("p", trip=80, clear=77, strikes=1, max_age=60)
        r.evaluate(99, ts=1000.0, now=1000.0)
        self.assertEqual(r.state, ALARM)
        state, _, _ = r.evaluate(None, ts=1000.0, now=1001.0, ok=False)
        self.assertEqual(state, ALARM, "a dropped frame must not clear an alarm")

    def test_without_a_staleness_horizon_a_failed_poll_is_unknown(self):
        """No max_age means we cannot age the value, so we cannot vouch for it."""
        r = Rule("p", trip=80, clear=77, strikes=1)
        r.evaluate(70, ts=1.0, now=1.0)
        state, _, _ = r.evaluate(None, ts=1.0, now=2.0, ok=False)
        self.assertEqual(state, UNKNOWN)


class Rendering(unittest.TestCase):
    def test_a_failed_poll_never_renders_as_a_bare_ok(self):
        self.assertEqual(render(OK, ok=True), OK)
        self.assertIn("no-read", render(OK, ok=False))
        self.assertIn("no-read", render(STALE, ok=False))
        self.assertEqual(render(None, ok=False), "no-read")


class Collections(unittest.TestCase):
    def test_in_alarm_counts_stale_as_needing_attention(self):
        rs = RuleSet([
            Rule("a", trip=80, clear=77, strikes=1, max_age=10),
            Rule("b", trip=80, clear=77, strikes=1, max_age=10),
        ])
        rs.evaluate("a", 99, ts=0.0, now=0.0)
        rs.evaluate("b", 10, ts=0.0, now=100.0)
        self.assertEqual(rs.in_alarm(), ["a", "b"])

    def test_an_unconfigured_point_evaluates_to_nothing(self):
        self.assertIsNone(RuleSet().evaluate("nope", 1.0))


if __name__ == "__main__":
    unittest.main()
