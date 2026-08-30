"""The published heading has to follow the actor while the facing is released.

Captured live: with the facing hooks standing down for an action, the actor's
own heading turned from -0.18 to 2.78 while the published value stayed at
-0.18, and the character snapped back to -0.18 the instant the action ended and
the mod resumed ownership.
"""

from __future__ import annotations

import struct
import unittest

import coop_runtime as coop


class FakeProcess:
    def __init__(self):
        self.data = {}

    def put(self, address, payload):
        for offset, value in enumerate(payload):
            self.data[address + offset] = value

    def read(self, address, size):
        return bytes(self.data.get(address + offset, 0) for offset in range(size))

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


ACTOR = 0x30000
OWN_HEADING = 2.78
STALE = -0.18


def actor_in(state: int, target: int) -> FakeProcess:
    process = FakeProcess()
    process.put(ACTOR + 0x6B4, bytes((state,)))
    process.put(ACTOR + 0x710, struct.pack("<I", target))
    process.put(ACTOR + 0x15C, struct.pack("<f", OWN_HEADING))
    return process


class ActionFacingReleaseTests(unittest.TestCase):
    def test_an_action_with_a_target_publishes_the_actors_own_heading(self):
        if not coop.FOLLOWER_ACTION_FACING_RELEASE:
            self.skipTest("the release is switched off")
        self.assertAlmostEqual(
            coop.action_facing_release_heading(actor_in(4, 0x10000A), ACTOR, STALE),
            OWN_HEADING,
            places=5,
        )

    def test_an_idle_actor_keeps_the_computed_heading(self):
        self.assertEqual(
            coop.action_facing_release_heading(actor_in(1, 0), ACTOR, STALE), STALE
        )

    def test_an_action_without_a_target_keeps_the_computed_heading(self):
        """The stock turn-toward-the-leader request reaches state 4 with none,
        and the mod has to keep owning the heading through it."""

        self.assertEqual(
            coop.action_facing_release_heading(actor_in(4, 0), ACTOR, STALE), STALE
        )

    def test_no_actor_is_harmless(self):
        self.assertEqual(
            coop.action_facing_release_heading(actor_in(4, 1), 0, STALE), STALE
        )

    def test_a_failed_read_keeps_the_computed_heading(self):
        class Broken(FakeProcess):
            def read(self, address, size):
                raise OSError(5, "denied")

        self.assertEqual(
            coop.action_facing_release_heading(Broken(), ACTOR, STALE), STALE
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
