"""The followers must issue the walk command FF12 issues, not one of their own.

Every number here was read off the native leader's physObj+0xD0 while one stick
was ramped from its lightest hold to the stop.  The game emits four things and
nothing between them, and the co-op used to emit everything between them.
"""

from __future__ import annotations

import math
import unittest

import coop_runtime as coop


class NativeStickResponseTests(unittest.TestCase):
    def test_the_stick_is_ignored_below_the_native_deadzone(self):
        # The leader stood still through every bucket up to four tenths while
        # the followers were already walking away from him.
        for strength in (0.0, 0.10, 0.25, 0.35, 0.40):
            self.assertEqual(coop.native_stick_fraction(strength), 0.0)

    def test_a_held_walk_is_a_third_of_the_run_command(self):
        # 0.03333 against a run command of 0.10, flat from 55% to 90%.
        for strength in (0.55, 0.65, 0.75, 0.85, 0.90):
            self.assertAlmostEqual(
                coop.native_stick_fraction(strength) * coop.RUN_STEP,
                0.03333,
                places=4,
            )

    def test_past_the_run_threshold_the_stick_stops_mattering(self):
        # The measured run plateau is exactly the run command, and holding the
        # stick harder does nothing at all -- the behaviour the leader shows.
        for strength in (0.92, 0.95, 1.0):
            self.assertEqual(
                coop.native_stick_fraction(strength) * coop.RUN_STEP,
                coop.RUN_STEP,
            )

    def test_nothing_is_issued_between_the_two_plateaus(self):
        # Above the walk plateau the only other value the game ever publishes
        # is the run one.  A reading in between must round to one of them.
        issued = {
            round(coop.native_stick_fraction(strength / 100.0), 6)
            for strength in range(55, 101)
        }
        self.assertEqual(issued, {round(1.0 / 3.0, 6), 1.0})

    def test_the_ramp_climbs_without_overshooting_the_walk(self):
        previous = 0.0
        for strength in (0.42, 0.45, 0.48, 0.51, 0.54):
            current = coop.native_stick_fraction(strength)
            self.assertGreater(current, previous)
            self.assertLessEqual(current, 1.0 / 3.0)
            previous = current

    def test_a_keyboard_press_still_means_run(self):
        # Digital input arrives as full deflection and must be unaffected.
        self.assertEqual(coop.native_stick_fraction(1.0), 1.0)


class NativeTurnRateTests(unittest.TestCase):
    """Turn the body at the rate the game turns the leader's.

    Measured by sliding the follower heading series against the leader's: the
    command arrived two frames late and the body four.  The command lag is the
    cost of writing from outside the process and cannot be spent; the extra two
    were our own smoothing running slower than the game's.
    """

    def test_the_visual_turn_matches_the_measured_native_cap(self):
        degrees_per_frame = math.degrees(coop.TURN_RATE_RADIANS) / 60.0
        self.assertAlmostEqual(
            degrees_per_frame, coop.NATIVE_TURN_DEGREES_PER_FRAME, places=6
        )

    def test_it_is_faster_than_the_rate_it_replaced(self):
        # The old 11.25 rad/s turned the body at 10.74 degrees a frame, which
        # is below what the leader does and is where the extra lag came from.
        self.assertGreater(coop.TURN_RATE_RADIANS, 11.25)

    def test_an_action_still_turns_faster_than_locomotion(self):
        # Execution motions deliberately snap harder; raising locomotion must
        # not have overtaken them.
        self.assertGreater(coop.ACTION_TURN_RATE_RADIANS, coop.TURN_RATE_RADIANS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
