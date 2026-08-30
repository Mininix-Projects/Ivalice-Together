import struct
import unittest

import penelo_story_guard as guard_module
from penelo_story_guard import EMPTY, PeneloStoryGuard, VAAN_ID


class FakeProcess:
    def __init__(self):
        self.memory = {}

    def put(self, address, data):
        for index, value in enumerate(data):
            self.memory[address + index] = value

    def put_u32(self, address, value):
        self.put(address, struct.pack("<I", value))

    def put_u64(self, address, value):
        self.put(address, struct.pack("<Q", value))

    def read(self, address, size):
        return bytes(self.memory[address + index] for index in range(size))

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


class PeneloRepairGenerationTests(unittest.TestCase):
    def test_repair_window_validates_the_callers_captured_generation(self):
        process = FakeProcess()
        base = 0x500000
        control = 0x600000
        location = 0x700000
        process.put_u64(guard_module.USER_CONTROL_GLOBAL, control)
        process.put_u64(guard_module.LOCATION_SAVE_GLOBAL, location)
        process.put_u32(guard_module.NATIVE_MENU_STATE, 0)
        process.put(control + guard_module.USER_CONTROL_ENABLED_OFFSET, b"\x01")
        process.put_u32(location + guard_module.LOCATION_CURRENT_OFFSET, 227)
        process.put_u32(location + guard_module.LOCATION_UI_OFFSET, 227)

        live = (VAAN_ID, 1, 2, *([EMPTY] * 6))
        for layout_id in range(5):
            process.put(
                base
                + guard_module.PARTY_LAYOUT_OFFSET
                + layout_id * guard_module.PARTY_LAYOUT_STRIDE,
                struct.pack("<9H", *live),
            )

        guard = PeneloStoryGuard.__new__(PeneloStoryGuard)
        guard.process = process
        guard.controlled_actor_valid = lambda: True
        self.assertTrue(guard.repair_window_open(base))

        # This is the generation tick actually observed before the periodic
        # town repair: Default was present, while Current/UI were still empty.
        # The live process has already materialized a valid trio above. If the
        # guard re-reads it instead of honoring this snapshot, it wrongly
        # approves partyAllRead and resets Vaan's run animation for one frame.
        stale = [live for _layout_id in range(5)]
        stale[guard_module.CURRENT_LAYOUT] = (EMPTY,) * 9
        stale[guard_module.UI_LAYOUT] = (EMPTY,) * 9
        self.assertFalse(guard.repair_window_open(base, tuple(stale)))


if __name__ == "__main__":
    unittest.main()
