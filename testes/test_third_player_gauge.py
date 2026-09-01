import struct
import unittest

import coop_runtime as movement


class FakeProcess:
    def __init__(self):
        self.writes = {}
        self.patches = {}

    def read(self, address, size):
        if address == movement.GAUGE_COLOR_HOOK_SITE:
            return b"\x85\xc9\x0f\x84\x35\x01\x00\x00"
        raise AssertionError(f"unexpected read {address:#x}")

    def write(self, address, data):
        self.writes[address] = bytes(data)

    def patch_code(self, address, data):
        self.patches[address] = bytes(data)


class ThirdPlayerGaugeTests(unittest.TestCase):
    def test_foreign_gauge_detour_is_rejected_before_theme_or_cave_writes(self):
        class ForeignProcess(FakeProcess):
            def read(self, address, size):
                if address == movement.GAUGE_COLOR_HOOK_SITE:
                    return (b"\xe9\x01\x02\x03\x04" + b"\x90" * 3)[:size]
                return super().read(address, size)

        process = ForeignProcess()
        with self.assertRaisesRegex(RuntimeError, "unexpected bytes"):
            movement.install_p2_gauge_color(process)
        self.assertEqual(process.writes, {})
        self.assertEqual(process.patches, {})

    def test_dispatcher_checks_p3_between_p2_and_p1(self):
        process = FakeProcess()
        movement.install_p2_gauge_color(process)
        cave = process.writes[movement.GAUGE_COLOR_CAVE]
        p3_actor_load = b"\x49\x8b\x83" + struct.pack(
            "<i",
            movement.P3_COMMAND_BLOCK_OFFSET - 0x200 + movement.P3_ACTOR_OFFSET,
        )
        self.assertIn(p3_actor_load, cave)
        self.assertEqual(cave.count(b"\x48\x3b\x47\x30"), 3)
        self.assertLessEqual(len(cave), 0x100)

    def test_player_identity_precedes_the_native_gauge_family_dispatch(self):
        process = FakeProcess()
        movement.install_p2_gauge_color(process)
        cave = process.writes[movement.GAUGE_COLOR_CAVE]

        first_actor_compare = cave.index(b"\x48\x3b\x47\x30")
        native_family_test = cave.index(b"\x85\xc9")
        last_actor_compare = cave.rindex(b"\x48\x3b\x47\x30")

        self.assertLess(first_actor_compare, native_family_test)
        self.assertLess(last_actor_compare, native_family_test)
        self.assertEqual(cave.count(b"\x85\xc9"), 1)

    def test_unmatched_native_gauge_palettes_are_not_overwritten(self):
        process = FakeProcess()
        movement.install_p2_gauge_color(process)

        self.assertNotIn(movement.SIDE_GAUGE_PALETTE, process.writes)
        self.assertNotIn(movement.OTHER_GAUGE_PALETTE, process.writes)

    def test_p3_has_private_palette_and_interpolator(self):
        process = FakeProcess()
        movement.install_p2_gauge_color(process)
        expected_palette = struct.pack("<8i", 127, 127, 58, 127, 127, 108, 18, 127)
        self.assertEqual(
            process.writes[movement.GAUGE_P3_PALETTE],
            expected_palette,
        )
        p3_interpolator = process.writes[movement.GAUGE_P3_COLOR_CAVE]
        self.assertEqual(
            p3_interpolator[2:10],
            struct.pack("<Q", movement.GAUGE_P3_PALETTE),
        )

    def test_p3_theme_data_stays_inside_its_reserved_block(self):
        base = movement.P3_COMMAND_BLOCK_OFFSET
        end = base + movement.P3_COMMAND_BLOCK_SIZE
        for offset, size in (
            (movement.P3_GAUGE_PALETTE_OFFSET, 32),
            (movement.P3_HUD_TEXT_THEME_COLOR_OFFSET, 12),
            (movement.P3_MENU_THEME_COLOR_OFFSET, 12),
        ):
            self.assertGreaterEqual(base + offset, base)
            self.assertLessEqual(base + offset + size, end)


if __name__ == "__main__":
    unittest.main(verbosity=2)
