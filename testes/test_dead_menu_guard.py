import struct
import unittest

from headless_menu_shell import (
    controlled_actor_alive,
    custom_menu_input_allowed,
)


class FakeProcess:
    def __init__(self):
        self.data = {}

    def write(self, address, payload):
        for offset, value in enumerate(payload):
            self.data[address + offset] = value

    def read(self, address, size):
        return bytes(self.data.get(address + offset, 0) for offset in range(size))

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


class DeadMenuGuardTests(unittest.TestCase):
    def setUp(self):
        self.process = FakeProcess()
        self.actor = 0x71000000
        self.battle_keep = 0x72000000
        self.process.write(
            self.actor + 0x698,
            struct.pack("<Q", self.battle_keep),
        )

    def set_state(self, hp, status, permanent_status=0):
        self.process.write(
            self.battle_keep + 0x48,
            struct.pack("<I", hp),
        )
        self.process.write(
            self.battle_keep + 0x64,
            struct.pack("<I", status),
        )
        self.process.write(
            self.battle_keep + 0x3C,
            struct.pack("<I", permanent_status),
        )

    def test_living_actor_can_open_when_session_allows_it(self):
        self.set_state(100, 0)
        alive = controlled_actor_alive(self.process, self.actor)
        self.assertTrue(alive)
        self.assertTrue(custom_menu_input_allowed(True, alive))

    def test_zero_hp_actor_cannot_open_custom_menu(self):
        self.set_state(0, 0)
        alive = controlled_actor_alive(self.process, self.actor)
        self.assertFalse(alive)
        self.assertFalse(custom_menu_input_allowed(True, alive))

    def test_ko_status_actor_cannot_open_custom_menu(self):
        self.set_state(100, 1)
        alive = controlled_actor_alive(self.process, self.actor)
        self.assertFalse(alive)
        self.assertFalse(custom_menu_input_allowed(True, alive))

    def test_permanent_ko_actor_cannot_open_custom_menu(self):
        self.set_state(100, 0, permanent_status=1)
        alive = controlled_actor_alive(self.process, self.actor)
        self.assertFalse(alive)
        self.assertFalse(custom_menu_input_allowed(True, alive))

    def test_unavailable_generation_is_never_treated_as_alive(self):
        alive = controlled_actor_alive(self.process, 0)
        self.assertIsNone(alive)
        self.assertFalse(custom_menu_input_allowed(True, alive))

    def test_session_gate_still_wins_for_living_actor(self):
        self.set_state(100, 0)
        alive = controlled_actor_alive(self.process, self.actor)
        self.assertFalse(custom_menu_input_allowed(False, alive))


if __name__ == "__main__":
    unittest.main()
