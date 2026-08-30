from __future__ import annotations

import struct
import unittest

import coop_runtime as coop
import session_state
from headless_menu_shell import targetable_party_actor_slots


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


class PartyTargetResolutionTests(unittest.TestCase):
    def make_party_actor(self, *, actor_id=1, party_slot=1):
        process = FakeProcess()
        manager = 0x10000
        bwei = 0x11000
        actor = 0x12000
        root = 0x13000
        phys = 0x14000
        battle_keep = 0x15000
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
        process.put(manager, struct.pack("<I", actor_id + 1))
        process.put(
            manager + 8 + actor_id * 8,
            struct.pack("<Q", bwei),
        )
        process.put(bwei + 0x30, struct.pack("<Q", actor))
        process.put(bwei + 0x12, bytes((party_slot,)))
        process.put(actor + 0x08, struct.pack("<I", 0x100000 | actor_id))
        process.put(actor + 0x10, struct.pack("<Q", root))
        process.put(root + 0xB8, struct.pack("<Q", phys))
        process.put(actor + 0x698, struct.pack("<Q", battle_keep))
        process.put(actor + 0xD0, struct.pack("<3f", 1.0, 2.0, 3.0))
        return process, actor

    def test_ko_actor_missing_from_movement_scan_remains_targetable(self):
        process, actor = self.make_party_actor()
        layout = (
            0,
            5,
            *([session_state.EMPTY_PARTY_ID] * 7),
        )
        resolved = targetable_party_actor_slots(process, {}, layout)
        self.assertIn(1, resolved)
        self.assertEqual(resolved[1][1], actor)

    def test_empty_current_slot_cannot_resurrect_stale_actor(self):
        process, _actor = self.make_party_actor()
        layout = (0,) + (session_state.EMPTY_PARTY_ID,) * 8
        self.assertEqual(
            targetable_party_actor_slots(process, {}, layout),
            {},
        )

    def test_actor_manager_generation_must_match_handle(self):
        process, actor = self.make_party_actor(actor_id=2)
        process.put(actor + 0x08, struct.pack("<I", 0x100001))
        layout = (
            0,
            5,
            *([session_state.EMPTY_PARTY_ID] * 7),
        )
        self.assertEqual(
            targetable_party_actor_slots(process, {}, layout),
            {},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
