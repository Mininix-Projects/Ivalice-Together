from __future__ import annotations

import struct
import unittest

from headless_menu_core import (
    BKEI_DEAD_FLAG,
    BKEI_DESPAWNED_FLAG,
    BKEI_INACTIVE_FLAG,
    BKEI_INACTIVE_UNTARGETABLE_FLAG,
    bkei_allows_native_target,
    decode_ff12_text,
    live_foes,
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


class NativeFoeLifecycleTests(unittest.TestCase):
    def test_live_enemy_name_preserves_native_punctuation(self):
        process = FakeProcess()
        address = 0x18000
        # B a ' G a m n a n, using FF12's compact battle-text glyph page.
        process.put(
            address,
            bytes((0x21, 0x3A, 0xAC, 0x26, 0x3A, 0x46, 0x47, 0x3A, 0x47, 0)),
        )

        self.assertEqual(decode_ff12_text(process, address), "Ba'Gamnan")

    def test_live_enemy_name_uses_the_shared_ascii_punctuation_page(self):
        process = FakeProcess()
        address = 0x18000
        process.put(
            address,
            bytes(
                (
                    0xA2,
                    0xA5,
                    0xA6,
                    0xA7,
                    0xA8,
                    0xA9,
                    0xAA,
                    0xAB,
                    0xAD,
                    0xAE,
                    0xAF,
                    0,
                )
            ),
        )

        self.assertEqual(
            decode_ff12_text(process, address),
            '/-+,.:;!"()',
        )

    def test_clear_and_unrelated_flags_remain_targetable(self):
        self.assertTrue(bkei_allows_native_target(0))
        self.assertTrue(bkei_allows_native_target(0x80))

    def test_every_native_lifecycle_blocker_rejects_target(self):
        for flag in (
            BKEI_DESPAWNED_FLAG,
            BKEI_DEAD_FLAG,
            BKEI_INACTIVE_FLAG,
            BKEI_INACTIVE_UNTARGETABLE_FLAG,
        ):
            with self.subTest(flag=flag):
                self.assertFalse(bkei_allows_native_target(flag))

    def build_foe(self, lifecycle_flags: int):
        """One attackable foe, in range and alive, carrying these BKEI flags."""

        process = FakeProcess()
        issuer = 0x20000
        actor = 0x30000
        bwei = 0x31000
        battle_keep = 0x32000
        extended = 0x33000
        name_text = 0x34000

        process.put(issuer + 0xD0, struct.pack("<3f", 0.0, 0.0, 0.0))
        # Active, not hidden.
        process.put(actor, struct.pack("<Q", 0x01))
        process.put(actor + 0x08, struct.pack("<I", 0x100042))
        process.put(actor + 0x10, struct.pack("<Q", bwei))
        process.put(actor + 0x698, struct.pack("<Q", battle_keep))
        process.put(actor + 0x6A0, struct.pack("<Q", extended))
        process.put(extended, bytes((lifecycle_flags,)))
        # Unit type 1 is an ordinary foe.
        process.put(bwei + 0x0E, bytes((1,)))
        process.put(bwei + 0xF8, struct.pack("<Q", name_text))
        process.put(name_text, bytes((0xFF,)))
        process.put(battle_keep + 0x48, struct.pack("<I", 500))

        actors = [(0x42, actor, 0, (2.0, 0.0, 2.0))]
        return process, issuer, actors

    def test_catalog_drops_a_foe_the_lifecycle_byte_blocks(self):
        """The predicate is not enough; the catalog has to actually ask it.

        Everything else about this actor says targetable: active, visible,
        in range, attackable type, HP above zero, so the lifecycle byte is
        the only thing that can keep it out.  This is the pre-spawn enemy that
        used to reach the menu before its event had materialized it.
        """

        process, issuer, actors = self.build_foe(0)
        self.assertEqual(
            len(live_foes(process, [], issuer_actor=issuer, active_actors=actors)),
            1,
            "a clean foe should be targetable",
        )

        for flag in (
            BKEI_DESPAWNED_FLAG,
            BKEI_DEAD_FLAG,
            BKEI_INACTIVE_FLAG,
            BKEI_INACTIVE_UNTARGETABLE_FLAG,
        ):
            with self.subTest(flag=flag):
                process, issuer, actors = self.build_foe(flag)
                self.assertEqual(
                    live_foes(process, [], issuer_actor=issuer, active_actors=actors),
                    [],
                )

    def test_catalog_drops_a_foe_without_a_battle_keep_extended(self):
        """A null BKEI is not yet a live unit either, so it cannot be read."""

        process, issuer, actors = self.build_foe(0)
        process.put(0x30000 + 0x6A0, struct.pack("<Q", 0))
        self.assertEqual(
            live_foes(process, [], issuer_actor=issuer, active_actors=actors), []
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
