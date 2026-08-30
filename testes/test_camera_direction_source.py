"""The collision direction must come from an input, not from the answer.

Taking it from the rendered frame closed a loop: we publish, FF12 answers, we
read its answer and publish again on top of it.  Loops with a few frames of
delay oscillate, and this one did -- 0.05 units of wobble at twenty-five hertz
on the exclusion interval, while the party positions feeding it were smooth.
"""

from __future__ import annotations

import math
import unittest

import coop_runtime as coop


class CameraDirectionFromYawTests(unittest.TestCase):
    def test_it_reproduces_the_rendered_direction(self):
        """Checked live against eye minus focus and agreeing exactly.

        The rig held 2.7669 rad while the drawn direction measured the same to
        four decimals, so this is a substitution rather than an approximation.
        """

        yaw = 2.7669
        direction = coop.camera_direction_from_yaw(yaw)
        self.assertAlmostEqual(math.atan2(direction[0], direction[2]), yaw, places=6)

    def test_it_is_a_unit_direction_on_the_horizontal_plane(self):
        for yaw in (0.0, 1.0, -2.5, math.pi, 6.0):
            x, y, z = coop.camera_direction_from_yaw(yaw)
            self.assertAlmostEqual(math.hypot(x, z), 1.0)
            self.assertEqual(y, 0.0)

    def test_it_never_collapses_the_way_a_difference_can(self):
        """The old source vanished when the eye sat on top of the focus.

        That needed a length guard, and the guard silently kept the previous
        direction through the very frames the camera was moving most.  An angle
        cannot collapse, so the guard is gone with it.
        """

        for yaw in (0.0, 0.001, math.pi / 2.0):
            direction = coop.camera_direction_from_yaw(yaw)
            self.assertGreater(math.hypot(direction[0], direction[2]), 0.99)

    def test_a_missing_or_broken_yaw_declines_to_answer(self):
        self.assertIsNone(coop.camera_direction_from_yaw(None))
        self.assertIsNone(coop.camera_direction_from_yaw(float("nan")))
        self.assertIsNone(coop.camera_direction_from_yaw(float("inf")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
