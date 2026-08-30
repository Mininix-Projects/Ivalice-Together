"""The protected corridor must not grow into a tunnel across the level.

Camera collision is suppressed inside a capsule spanning the two players so
scenery between them stops shoving the shared camera.  The capsule used to be
as long as the players were apart, and a wide separation exempted enough
geometry for the camera to leave the room entirely.
"""

from __future__ import annotations

import math
import struct
import unittest

import coop_runtime as coop
import shared_camera as camera

LIMIT = 16.0


def span(first, second) -> float:
    return math.hypot(second[0] - first[0], second[2] - first[2])


class ClampTests(unittest.TestCase):
    def test_a_close_pair_is_left_alone(self):
        first, second = coop.clamp_formation_span((0, 0, 0), (4, 0, 0), LIMIT)
        self.assertEqual((first, second), ((0, 0, 0), (4, 0, 0)))

    def test_a_pair_exactly_at_the_limit_is_left_alone(self):
        first, second = coop.clamp_formation_span((0, 0, 0), (LIMIT, 0, 0), LIMIT)
        self.assertAlmostEqual(span(first, second), LIMIT, places=5)

    def test_a_wide_pair_is_shortened_to_the_limit(self):
        first, second = coop.clamp_formation_span((0, 0, 0), (40, 0, 0), LIMIT)
        self.assertAlmostEqual(span(first, second), LIMIT, places=5)

    def test_shortening_keeps_the_midpoint(self):
        first, second = coop.clamp_formation_span((-30, 0, 10), (30, 0, 70), LIMIT)
        self.assertAlmostEqual((first[0] + second[0]) / 2, 0.0, places=5)
        self.assertAlmostEqual((first[2] + second[2]) / 2, 40.0, places=5)

    def test_height_is_not_clamped(self):
        """A player above the other must keep their own height in the capsule."""

        first, second = coop.clamp_formation_span((0, 2, 0), (40, 9, 0), LIMIT)
        self.assertEqual((first[1], second[1]), (2, 9))

    def test_two_players_in_the_same_spot_do_not_divide_by_zero(self):
        first, second = coop.clamp_formation_span((5, 0, 5), (5, 0, 5), LIMIT)
        self.assertEqual((first, second), ((5, 0, 5), (5, 0, 5)))

    def test_a_disabled_limit_restores_the_old_behaviour(self):
        first, second = coop.clamp_formation_span((0, 0, 0), (99, 0, 0), 0.0)
        self.assertAlmostEqual(span(first, second), 99.0, places=5)


class PayloadTests(unittest.TestCase):
    def test_the_published_capsule_honours_the_limit(self):
        tuning = camera.CameraTuning()
        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0), (200.0, 0.0, 0.0), tuning, enabled=True
        )
        x1, _y1, z1, x2, _y2, z2 = struct.unpack_from("<6f", payload, 0)
        self.assertAlmostEqual(
            math.hypot(x2 - x1, z2 - z1),
            tuning.internal_collision_corridor_length,
            places=4,
        )

    def test_a_disabled_formation_still_publishes_a_blank_block(self):
        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0), (5.0, 0.0, 0.0), camera.CameraTuning(), enabled=False
        )
        self.assertEqual(payload, bytes(0x24))


class MinimumSeparationTests(unittest.TestCase):
    """A corridor is only worth protecting when there is something inside it.

    Measured in a small room, toggling between the two cameras in the same
    spot: the stock camera sat at boom 4.16 with FF12 reporting the wall on 95%
    of frames, while the co-op camera pinned the boom at 5.88 with the wall
    reported on 5%.  The corridor's radius is wider than the room, so the walls
    themselves were inside the volume the filter suppresses.
    """

    def setUp(self):
        self.tuning = camera.CameraTuning()

    def test_players_standing_together_publish_no_corridor(self):
        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0), (2.0, 0.0, 0.0), self.tuning, enabled=True
        )
        self.assertEqual(payload, bytes(0x24))

    def test_players_far_apart_still_publish_one(self):
        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0), (12.0, 0.0, 0.0), self.tuning, enabled=True
        )
        self.assertNotEqual(payload, bytes(0x24))

    def test_the_threshold_is_horizontal_only(self):
        """One player far above the other is still standing next to them."""

        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0), (1.0, 40.0, 0.0), self.tuning, enabled=True
        )
        self.assertEqual(payload, bytes(0x24))

    def test_the_threshold_clears_the_corridor_radius(self):
        """A corridor narrower than it is long would engulf its own room."""

        self.assertGreater(
            self.tuning.internal_collision_min_separation,
            self.tuning.internal_collision_corridor_radius,
        )


class RadiusCeilingTests(unittest.TestCase):
    """The corridor must never reach as far from the focus as the camera is.

    Measured: in a small room the game held the camera at boom 4.16, while the
    corridor radius stayed at its configured 5.0.  The corridor therefore
    covered the camera's own position and the wall behind it, the wall stopped
    being reported, and nothing pulled the camera back in.
    """

    def setUp(self):
        self.tuning = camera.CameraTuning()

    def radius_of(self, payload):
        squared = struct.unpack_from("<f", payload, 24)[0]
        return math.sqrt(squared)

    def test_a_short_boom_narrows_the_corridor(self):
        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0),
            (12.0, 0.0, 0.0),
            self.tuning,
            enabled=True,
            native_boom=9.0,
        )
        self.assertLess(self.radius_of(payload), 9.0 * 0.9)

    def test_a_long_boom_leaves_the_configured_radius(self):
        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0),
            (12.0, 0.0, 0.0),
            self.tuning,
            enabled=True,
            native_boom=40.0,
        )
        self.assertAlmostEqual(
            self.radius_of(payload),
            self.tuning.internal_collision_corridor_radius,
            places=3,
        )

    def test_no_boom_keeps_the_previous_behaviour(self):
        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0), (12.0, 0.0, 0.0), self.tuning, enabled=True
        )
        self.assertAlmostEqual(
            self.radius_of(payload),
            self.tuning.internal_collision_corridor_radius,
            places=3,
        )

    def test_a_room_too_tight_publishes_no_corridor(self):
        """Any corridor at all silenced the wall in a long narrow room."""

        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0),
            (15.0, 0.0, 0.0),
            self.tuning,
            enabled=True,
            native_boom=4.93,
        )
        self.assertEqual(payload, bytes(0x24))


class HysteresisTests(unittest.TestCase):
    """The corridor must not chase its own tail.

    Publishing it lengthens the boom and withdrawing it shortens the boom, so a
    single threshold on the boom is a feedback loop: the camera reaches the
    line, flips, is pushed back across it and flips again.  Reported in play as
    the camera reaching a point, resetting, returning and resetting.
    """

    def setUp(self):
        self.tuning = camera.CameraTuning()

    def publica(self, boom, antes):
        payload = coop.camera_collision_formation_payload(
            (0.0, 0.0, 0.0),
            (15.0, 0.0, 0.0),
            self.tuning,
            enabled=True,
            native_boom=boom,
            previously_active=antes,
        )
        return payload != bytes(0x24)

    def test_it_takes_more_boom_to_start_than_to_keep(self):
        self.assertGreater(
            self.tuning.internal_collision_min_boom,
            self.tuning.internal_collision_boom_release,
        )

    def test_between_the_thresholds_the_answer_is_whatever_it_was(self):
        entre = (
            self.tuning.internal_collision_min_boom
            + self.tuning.internal_collision_boom_release
        ) / 2.0
        self.assertTrue(self.publica(entre, True))
        self.assertFalse(self.publica(entre, False))

    def test_far_above_it_always_publishes(self):
        alto = self.tuning.internal_collision_min_boom + 4.0
        self.assertTrue(self.publica(alto, False))
        self.assertTrue(self.publica(alto, True))

    def test_far_below_it_never_publishes(self):
        baixo = self.tuning.internal_collision_boom_release - 2.0
        self.assertFalse(self.publica(baixo, False))
        self.assertFalse(self.publica(baixo, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CorridorHysteresisAcrossTicksTests(unittest.TestCase):
    """The engage/release pair only exists if the flag survives the tick.

    Clearing camera_corridor_active at the top of every loop iteration made
    previously_active permanently False, so the lower release threshold was
    dead code and the corridor dropped out the moment FF12 collapsed the boom -
    which is the feedback loop that made the followed focus point collide.
    """

    def test_the_main_loop_does_not_clear_it_every_iteration(self):
        import inspect

        source = inspect.getsource(coop.main)
        loop = source.index("while True:")
        self.assertNotIn(
            "camera_corridor_active = False",
            source[loop:],
            "o flag voltou a ser zerado dentro do laco",
        )
        self.assertIn("camera_corridor_active = False", source[:loop])

    def test_release_is_lower_than_engage(self):
        tuning = camera.CameraTuning()
        self.assertLess(
            tuning.internal_collision_boom_release,
            tuning.internal_collision_min_boom,
        )

    def test_a_boom_between_release_and_engage_keeps_the_corridor(self):
        """This is the case the dead hysteresis was losing every tick."""

        tuning = camera.CameraTuning()
        middle = (
            tuning.internal_collision_boom_release + tuning.internal_collision_min_boom
        ) / 2.0
        far = tuning.internal_collision_min_separation + 2.0
        first = (0.0, 0.0, 0.0)
        second = (far, 0.0, 0.0)

        engaged = coop.camera_collision_formation_payload(
            first,
            second,
            tuning,
            enabled=True,
            native_boom=tuning.internal_collision_min_boom + 1.0,
            previously_active=False,
        )
        self.assertTrue(any(engaged), "devia engatar acima do limiar")

        kept = coop.camera_collision_formation_payload(
            first,
            second,
            tuning,
            enabled=True,
            native_boom=middle,
            previously_active=True,
        )
        self.assertTrue(any(kept), "devia continuar engatado acima do release")

        dropped = coop.camera_collision_formation_payload(
            first,
            second,
            tuning,
            enabled=True,
            native_boom=tuning.internal_collision_boom_release - 1.0,
            previously_active=True,
        )
        self.assertFalse(any(dropped), "devia soltar abaixo do release")
