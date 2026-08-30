"""Heading and independent-stick behavior used by follower movement."""

from __future__ import annotations

import struct
import unittest

import coop_runtime as coop

PHYS = 0x1000
ACTOR = 0x2000


class IndependentStickSteeringTests(unittest.TestCase):
    def steer(self):
        heading, magnitude, moving = coop.third_player_steering(
            _StickBindings(0.5),
            ((0.0, 1.0), (1.0, 0.0)),
            False,
            True,
        )
        return magnitude

    def test_each_follower_uses_its_own_stick_curve(self):
        self.assertAlmostEqual(
            self.steer(),
            coop.RUN_STEP * coop.native_stick_fraction(0.5),
        )


class _StickBindings:
    def __init__(self, strength):
        self.strength = strength

    def signed_axis(self, player, positive, negative):
        return self.strength if positive == "move_up" else 0.0


class LeaderHeadingCopyTests(unittest.TestCase):
    """Copy the leader's facing instead of matching its turn rate.

    Matching the rate was the wrong move and made it worse: the followers
    stopped snapping, so the two frames it takes to get a command into the game
    stayed visible for the whole turn instead of being erased in three frames.
    Copying leaves nothing to match -- rate, easing and curve arrive together.
    """

    def pair(self, commanded, visible, **kwargs):
        return coop.leader_heading_pair(
            _HeadingProcess(commanded, visible, **kwargs), PHYS
        )

    def test_both_headings_are_copied_verbatim(self):
        # Compared loosely on purpose: the game stores these as 32-bit floats,
        # so a value round-trips as 1.100000023841858 and an exact match would
        # be testing Python's literals rather than the copy.
        commanded, visible = self.pair(1.25, 1.10)
        self.assertAlmostEqual(commanded, 1.25, places=6)
        self.assertAlmostEqual(visible, 1.10, places=6)

    def test_an_unreadable_leader_declines_to_answer(self):
        self.assertIsNone(self.pair(1.25, 1.10, fail=True))
        self.assertIsNone(coop.leader_heading_pair(None, 0))

    def test_a_nonsense_heading_declines_to_answer(self):
        self.assertIsNone(self.pair(float("nan"), 1.10))
        self.assertIsNone(self.pair(1.25, float("inf")))


class InitialFollowerHeadingTests(unittest.TestCase):
    def test_new_owner_generation_preserves_its_own_orientation(self):
        process = _GenerationHeadingProcess(
            {
                PHYS: (1.25, 1.10),
                PHYS + 0x1000: (-2.0, -1.75),
            }
        )
        commanded, visible = coop.initial_follower_heading_pair(
            process,
            PHYS,
            PHYS + 0x1000,
        )
        self.assertAlmostEqual(commanded, -2.0, places=6)
        self.assertAlmostEqual(visible, -1.75, places=6)

    def test_follower_orientation_is_the_load_safe_fallback(self):
        process = _GenerationHeadingProcess({PHYS + 0x1000: (-2.0, -1.75)})
        commanded, visible = coop.initial_follower_heading_pair(
            process,
            0,
            PHYS + 0x1000,
        )
        self.assertAlmostEqual(commanded, -2.0, places=6)
        self.assertAlmostEqual(visible, -1.75, places=6)

    def test_missing_generation_has_no_heading_to_publish(self):
        self.assertIsNone(
            coop.initial_follower_heading_pair(
                _GenerationHeadingProcess({}),
                0,
                0,
            )
        )


class _HeadingProcess:
    def __init__(self, commanded, visible, fail=False):
        self.commanded = commanded
        self.visible = visible
        self.fail = fail

    def read(self, address, size):
        if self.fail:
            raise OSError(5, "denied")
        return struct.pack("<2f", self.commanded, self.visible)


class _GenerationHeadingProcess:
    def __init__(self, headings):
        self.headings = headings

    def read(self, address, size):
        phys = address - 0xA4
        if size != 8 or phys not in self.headings:
            raise OSError(5, "missing generation")
        return struct.pack("<2f", *self.headings[phys])


if __name__ == "__main__":
    unittest.main(verbosity=2)
