"""The forged interaction descriptor must not flatten FF12's own state.

Captured live, standing against a Mimic in the Dalmasca Westersand: the game
publishes state 10 for that chest, while the mod forged a flat 2.  The consumer
took the handle either way: the log printed CONSUMED and RELEASE for each
click, and the Mimic never woke.
"""

from __future__ import annotations

import struct
import unittest

import headless_menu_core as legacy
from headless_menu_shell import (
    NATIVE_INTERACTION_MIMIC_STATE,
    NATIVE_INTERACTION_MINIMUM_STATE,
    forced_interaction_state,
    sleeping_mimic_state,
)


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


def world_with(unit_type: int, bkei_flags: int, handle: int = 0x200017):
    """One actor behind ``handle``, of this unit type and lifecycle state."""

    process = FakeProcess()
    manager, bwei, actor, extended = 0x10000, 0x11000, 0x12000, 0x13000
    actor_id = handle & 0xFFFF
    process.put(legacy.coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
    process.put(manager, struct.pack("<I", actor_id + 1))
    process.put(manager + 8 + actor_id * 8, struct.pack("<Q", bwei))
    process.put(bwei + 0x30, struct.pack("<Q", actor))
    process.put(bwei + 0x0E, bytes((unit_type,)))
    process.put(actor + 0x08, struct.pack("<I", handle))
    process.put(actor + 0x6A0, struct.pack("<Q", extended))
    process.put(extended, bytes((bkei_flags,)))
    return process


class SleepingMimicStateTests(unittest.TestCase):
    def test_a_sleeping_mimic_gets_the_measured_state(self):
        """The follower cannot observe this value, so it is recovered here."""

        process = world_with(unit_type=4, bkei_flags=0x22)
        self.assertEqual(
            sleeping_mimic_state(process, 0x200017), NATIVE_INTERACTION_MIMIC_STATE
        )

    def test_an_awake_mimic_is_left_alone(self):
        """Once it has woken it is a foe, and the world prompt is not its own."""

        process = world_with(unit_type=4, bkei_flags=0x02)
        self.assertIsNone(sleeping_mimic_state(process, 0x200017))

    def test_ordinary_objects_keep_their_own_state(self):
        """A chest, a crystal or an NPC must not be handed a Mimic's state."""

        for unit_type in (0, 1, 7):
            with self.subTest(unit_type=unit_type):
                process = world_with(unit_type=unit_type, bkei_flags=0x22)
                self.assertIsNone(sleeping_mimic_state(process, 0x200017))

    def test_a_stale_or_empty_handle_is_refused(self):
        process = world_with(unit_type=4, bkei_flags=0x22)
        self.assertIsNone(sleeping_mimic_state(process, 0))
        # Same actor index, different generation: not the actor we resolved.
        self.assertIsNone(sleeping_mimic_state(process, 0x300017))


def descriptor(state: int, handle: int) -> bytes:
    return struct.pack("<6I", 0x3F1999AC, 0, handle & 0xFFFF, state, handle, 0)


class ForcedInteractionStateTests(unittest.TestCase):
    def test_live_state_is_kept_for_the_same_handle(self):
        """The Mimic's own 10 survives instead of being flattened to 2."""

        self.assertEqual(
            forced_interaction_state(descriptor(10, 0x200017), 0x200017), 10
        )

    def test_a_state_for_another_object_is_not_borrowed(self):
        """A descriptor about something else says nothing about this handle."""

        self.assertEqual(
            forced_interaction_state(descriptor(10, 0x200018), 0x200017),
            NATIVE_INTERACTION_MINIMUM_STATE,
        )

    def test_state_below_the_usable_floor_is_raised(self):
        """The consumer rejects anything under 2, so 2 is still the floor."""

        for state in (0, 1):
            with self.subTest(state=state):
                self.assertEqual(
                    forced_interaction_state(descriptor(state, 0x200017), 0x200017),
                    NATIVE_INTERACTION_MINIMUM_STATE,
                )

    def test_ordinary_ready_object_is_unchanged(self):
        """Chests and NPCs that already worked keep the exact value they had."""

        self.assertEqual(
            forced_interaction_state(descriptor(2, 0x10000F), 0x10000F),
            NATIVE_INTERACTION_MINIMUM_STATE,
        )

    def test_a_short_or_missing_descriptor_falls_back(self):
        """A failed read must not raise inside the interaction path."""

        self.assertEqual(
            forced_interaction_state(b"", 0x200017),
            NATIVE_INTERACTION_MINIMUM_STATE,
        )
        self.assertEqual(
            forced_interaction_state(b"\x00" * 8, 0x200017),
            NATIVE_INTERACTION_MINIMUM_STATE,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
