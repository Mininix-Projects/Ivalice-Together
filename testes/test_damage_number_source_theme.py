import unittest
from types import SimpleNamespace
from unittest import mock

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import coop_runtime as coop


class DamageNumberSourceThemeTests(unittest.TestCase):
    def setUp(self):
        self.wrapper_base = 0x31000000
        self.source_state = self.wrapper_base + 0x500
        self.command_block = 0x32000200
        self.p3_block = 0x32008200
        self.p1_theme = 0x32000E30
        self.p2_theme = 0x32000B30
        self.p3_theme = 0x32008270
        self.brightness_state = self.wrapper_base + 0x508
        self.md = Cs(CS_ARCH_X86, CS_MODE_64)

    def init_wrapper(self, address=None):
        return coop.build_damage_number_init_wrapper(
            self.wrapper_base + 0x300 if address is None else address,
            self.source_state,
            self.command_block,
            self.p3_block,
            self.p1_theme,
            self.p2_theme,
            self.p3_theme,
            self.brightness_state,
        )

    def instructions(self, code, address):
        return [
            (instruction.mnemonic, instruction.op_str)
            for instruction in self.md.disasm(code, address)
        ]

    def test_source_wrapper_scopes_the_resolved_actor_to_native_factory(self):
        address = self.wrapper_base + 0x100
        code = coop.build_damage_number_source_wrapper(
            address,
            self.source_state,
        )
        instructions = self.instructions(code, address)
        self.assertIn(
            ("mov", "qword ptr [r11], rbp"),
            instructions,
        )
        self.assertIn(
            ("call", f"{coop.DAMAGE_NUMBER_FACTORY:#x}"),
            instructions,
        )
        self.assertIn(
            ("mov", "qword ptr [r11], 0"),
            instructions,
        )
        self.assertEqual(instructions[-1], ("ret", ""))

    def test_initializer_matches_all_three_controlled_actor_pointers(self):
        address = self.wrapper_base + 0x300
        code = self.init_wrapper(address)
        instructions = self.instructions(code, address)
        self.assertIn(("cmp", "rax, qword ptr [r10 + 0x18]"), instructions)
        self.assertIn(("cmp", "rax, qword ptr [r10 + 0x10]"), instructions)
        self.assertIn(("cmp", "rax, qword ptr [r9 + 8]"), instructions)
        for theme in (self.p1_theme, self.p2_theme, self.p3_theme):
            self.assertIn(("movabs", f"r8, {theme:#x}"), instructions)

    def test_initializer_changes_only_primary_rgb_and_font_clut(self):
        address = self.wrapper_base + 0x300
        code = self.init_wrapper(address)
        instructions = self.instructions(code, address)
        writes = [
            operands
            for mnemonic, operands in instructions
            if mnemonic == "mov" and operands.startswith("byte ptr [rbx")
        ]
        self.assertEqual(writes.count("byte ptr [rbx + 0xc], al"), 1)
        self.assertEqual(writes.count("byte ptr [rbx + 0xd], al"), 1)
        self.assertEqual(writes.count("byte ptr [rbx + 0xe], al"), 1)
        self.assertEqual(writes.count("byte ptr [rbx + 0x3d], al"), 1)
        self.assertNotIn("byte ptr [rbx + 0xf], al", writes)
        self.assertNotIn("byte ptr [rbx + 0x10], al", writes)

    def test_initializer_validates_font_clut_before_any_theme_write(self):
        address = self.wrapper_base + 0x300
        instructions = self.instructions(self.init_wrapper(address), address)
        load = ("mov", "eax, dword ptr [r8 + 0xc]")
        limit = ("cmp", f"eax, {coop.DAMAGE_NUMBER_PALETTE_MAX:#x}")
        self.assertEqual(instructions.count(load), 2)
        load_index = instructions.index(load)
        limit_index = instructions.index(limit, load_index)
        self.assertEqual(instructions[limit_index + 1][0], "ja")
        first_write = instructions.index(("mov", "byte ptr [rbx + 0xc], al"))
        clut_write = instructions.index(("mov", "byte ptr [rbx + 0x3d], al"))
        self.assertLess(limit_index, first_write)
        self.assertGreater(clut_write, first_write)

    def test_initializer_leaves_recovery_numbers_on_the_native_green_path(self):
        address = self.wrapper_base + 0x300
        instructions = self.instructions(self.init_wrapper(address), address)
        recovery_test = (
            "test",
            "byte ptr [rsi + 0xd], 1",
        )
        self.assertIn(recovery_test, instructions)
        index = instructions.index(recovery_test)
        self.assertEqual(instructions[index + 1][0], "jne")

    def test_initializer_rejects_non_numeric_battle_text_descriptors(self):
        address = self.wrapper_base + 0x300
        instructions = self.instructions(self.init_wrapper(address), address)
        descriptor_limit = (
            "cmp",
            "byte ptr [rsi + 0xd], "
            f"{coop.DAMAGE_NUMBER_DESCRIPTOR_PALETTE_MAX}",
        )
        self.assertIn(descriptor_limit, instructions)
        index = instructions.index(descriptor_limit)
        self.assertEqual(instructions[index + 1][0], "ja")

    def test_initializer_distinguishes_controlled_allies_from_enemies(self):
        address = self.wrapper_base + 0x300
        code = self.init_wrapper(address)
        instructions = self.instructions(code, address)
        self.assertIn(("mov", "ecx, dword ptr [rbx + 8]"), instructions)
        self.assertIn(
            ("movabs", f"r11, {coop.CONTROLLED_HANDLE_GLOBAL:#x}"),
            instructions,
        )
        self.assertIn(("cmp", "ecx, dword ptr [r11]"), instructions)
        self.assertIn(
            (
                "cmp",
                f"ecx, dword ptr [r10 + {coop.P2_LOOT_HANDLE_OFFSET:#x}]",
            ),
            instructions,
        )
        self.assertIn(
            (
                "cmp",
                f"ecx, dword ptr [r9 + {coop.P3_LOOT_HANDLE_OFFSET:#x}]",
            ),
            instructions,
        )

    def test_poisoned_actor_mailboxes_are_comparison_only(self):
        """A stale actor pointer must never become a receiver-address base."""

        address = self.wrapper_base + 0x300
        instructions = self.instructions(self.init_wrapper(address), address)
        actor_loads = {
            "qword ptr [r10 + 0x18]",
            "qword ptr [r10 + 0x10]",
            "qword ptr [r9 + 8]",
        }
        for mnemonic, operands in instructions:
            if any(actor_load in operands for actor_load in actor_loads):
                self.assertEqual(mnemonic, "cmp")
                self.assertTrue(operands.startswith("rax, "))
            self.assertNotIn("[rdx + 8]", operands)

    def test_initializer_lerps_each_channel_towards_the_native_neutral(self):
        address = self.wrapper_base + 0x300
        code = self.init_wrapper(address)
        instructions = self.instructions(code, address)
        self.assertIn(("movabs", f"r10, {self.brightness_state:#x}"), instructions)
        self.assertIn(("mov", "r10d, dword ptr [r10]"), instructions)
        # The distance to neutral is scaled, never the channel itself; the
        # retired multiply pushed channels past 0x7f, where the modulate only
        # clips and the hue is lost.
        self.assertEqual(instructions.count(("sub", "ecx, eax")), 3)
        self.assertEqual(instructions.count(("imul", "ecx, r10d")), 3)
        # Signed: a channel published above neutral must survive intact.
        self.assertEqual(instructions.count(("sar", "ecx, 8")), 3)
        self.assertNotIn(("shr", "ecx, 8"), instructions)
        self.assertEqual(instructions.count(("sub", "eax, ecx")), 3)
        self.assertNotIn(("sar", "ecx, 1"), instructions)
        self.assertNotIn(("add", "eax, ecx"), instructions)
        self.assertNotIn(("mov", "eax, 0x97"), instructions)

    def test_initializer_bounds_the_brightness_word_and_every_channel(self):
        address = self.wrapper_base + 0x300
        code = self.init_wrapper(address)
        instructions = self.instructions(code, address)
        ceiling = coop.DAMAGE_NUMBER_BRIGHTNESS_UNCHANGED
        neutral = coop.DAMAGE_NUMBER_NEUTRAL_CHANNEL
        channel_ceiling = coop.DAMAGE_NUMBER_CHANNEL_CEILING
        self.assertIn(("cmp", f"r10d, {ceiling:#x}"), instructions)
        self.assertIn(("mov", f"r10d, {ceiling:#x}"), instructions)
        # The byte bounds the channel; neutral is only the lerp target.
        self.assertEqual(
            instructions.count(("cmp", f"eax, {channel_ceiling:#x}")),
            3,
        )
        self.assertEqual(
            instructions.count(("mov", f"eax, {channel_ceiling:#x}")),
            3,
        )
        self.assertEqual(
            instructions.count(("mov", f"eax, {neutral:#x}")),
            3,
        )
        self.assertEqual(
            instructions.count(("mov", f"ecx, {neutral:#x}")),
            3,
        )

    def test_published_style_uses_a_tint_and_native_core_outline_clut(self):
        import menu_themes

        for name in menu_themes.PRESETS:
            drawn = menu_themes.damage_number_color(name)
            style = menu_themes.damage_number_style(name)
            self.assertEqual(len(drawn), 3)
            self.assertEqual(len(style), 4)
            self.assertEqual(drawn, menu_themes.DAMAGE_NUMBER_PALETTE[name])
            self.assertEqual(style[:3], drawn)
            self.assertEqual(style[3], menu_themes.damage_number_clut(name))
            self.assertIn(style[3], range(coop.DAMAGE_NUMBER_PALETTE_MAX + 1))
            for channel in drawn:
                self.assertGreaterEqual(channel, 0)
                self.assertLessEqual(channel, coop.DAMAGE_NUMBER_CHANNEL_CEILING)

        amethyst = menu_themes.damage_number_color("amethyst")
        pink = menu_themes.damage_number_color("pink")
        self.assertEqual(
            amethyst,
            (coop.DAMAGE_NUMBER_NEUTRAL_CHANNEL,) * 3,
        )
        self.assertEqual(
            pink,
            (coop.DAMAGE_NUMBER_NEUTRAL_CHANNEL,) * 3,
        )

        self.assertNotEqual(
            menu_themes.damage_number_style("amethyst"),
            menu_themes.damage_number_style("pink"),
        )
        self.assertNotEqual(
            menu_themes.damage_number_clut("pink"),
            menu_themes.damage_number_clut("ruby"),
        )
        self.assertEqual(
            {
                name: menu_themes.damage_number_style(name)
                for name in menu_themes.PRESETS
            },
            {
                "sapphire": (127, 127, 127, 108),
                "gold": (127, 127, 127, 109),
                "emerald": (127, 127, 127, 110),
                "amethyst": (127, 127, 127, 111),
                "pink": (127, 127, 127, 112),
                "ruby": (127, 127, 127, 113),
                "bronze": (127, 127, 127, 114),
            },
        )

        self.assertEqual(
            sorted(
                menu_themes.damage_number_clut(name) for name in menu_themes.PRESETS
            ),
            list(range(108, 115)),
        )

        brightness = coop.DAMAGE_NUMBER_BRIGHTNESS_DEFAULT
        neutral = coop.DAMAGE_NUMBER_NEUTRAL_CHANNEL
        for raw in amethyst + pink:
            bright = neutral - (((neutral - raw) * brightness) >> 8)
            self.assertGreaterEqual(bright, raw)

    def test_brightness_arithmetic_never_leaves_a_writable_byte(self):
        neutral = coop.DAMAGE_NUMBER_NEUTRAL_CHANNEL
        ceiling = coop.DAMAGE_NUMBER_BRIGHTNESS_UNCHANGED
        channel_ceiling = coop.DAMAGE_NUMBER_CHANNEL_CEILING
        samples = (0, 18, 74, 108, 127, 168, 190, 255)
        for brightness in (
            1,
            0x40,
            coop.DAMAGE_NUMBER_BRIGHTNESS_DEFAULT,
            ceiling,
        ):
            for value in samples:
                # Python's >> on a negative int is arithmetic, matching sar.
                drawn = neutral - (((neutral - value) * brightness) >> 8)
                self.assertGreaterEqual(drawn, 0)
                self.assertLessEqual(drawn, channel_ceiling)
        # The knob ceiling is the identity, so any published channel, above or
        # below neutral, can always reach the game verbatim.
        for value in samples:
            self.assertEqual(
                neutral - (((neutral - value) * ceiling) >> 8),
                value,
            )

    def test_wrappers_fit_their_private_pages(self):
        source = coop.build_damage_number_source_wrapper(
            self.wrapper_base + 0x100,
            self.source_state,
        )
        initializer = self.init_wrapper()
        alternate = self.init_wrapper(self.wrapper_base + 0x700)
        self.assertLessEqual(len(source), 0x100)
        self.assertLessEqual(len(initializer), 0x200)
        self.assertLessEqual(len(alternate), 0x200)
        self.assertLessEqual(
            coop.DAMAGE_NUMBER_STATE_OFFSET + 8,
            coop.DAMAGE_NUMBER_BRIGHTNESS_OFFSET,
        )
        self.assertLessEqual(
            coop.DAMAGE_NUMBER_BRIGHTNESS_OFFSET + 4,
            coop.DAMAGE_NUMBER_INIT_WRAPPER_ALT_OFFSET,
        )
        self.assertLessEqual(
            coop.DAMAGE_NUMBER_INIT_WRAPPER_ALT_OFFSET + 0x200,
            coop.DAMAGE_NUMBER_THEME_ALLOCATION_SIZE,
        )

    def test_the_two_initializer_slots_never_overlap(self):
        self.assertNotEqual(
            coop.DAMAGE_NUMBER_INIT_WRAPPER_OFFSET,
            coop.DAMAGE_NUMBER_INIT_WRAPPER_ALT_OFFSET,
        )
        self.assertLessEqual(
            coop.DAMAGE_NUMBER_INIT_WRAPPER_OFFSET + 0x200,
            coop.DAMAGE_NUMBER_STATE_OFFSET,
        )

    def test_verified_meter_proxy_is_inspected_without_detaching_live_code(self):
        theme_base = 0x31000000
        coop.set_damage_number_theme_base(theme_base)
        proxy_base = theme_base + coop.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
        proxy_entry = proxy_base + coop.DAMAGE_METER_PROXY_CODE_OFFSET
        proxy_call = b"\xe8" + coop.rel32(
            coop.DAMAGE_NUMBER_SOURCE_CALL_SITE,
            5,
            proxy_entry,
        )
        previous_call = b"\xe8" + coop.rel32(
            coop.DAMAGE_NUMBER_SOURCE_CALL_SITE,
            5,
            coop.DAMAGE_NUMBER_SOURCE_WRAPPER,
        )

        class Process:
            def __init__(self):
                self.patches = []

            def patch_code(self, address, data):
                self.patches.append((address, bytes(data)))

        process = Process()
        resident = SimpleNamespace(previous_call=previous_call)
        with mock.patch(
            "damage_meter.inspect_resident_damage_hook",
            return_value=resident,
        ) as inspect:
            restored = coop.detach_damage_meter_proxy(process, proxy_call)

        self.assertEqual(restored, previous_call)
        self.assertEqual(process.patches, [])
        inspect.assert_called_once_with(
            process,
            theme_base,
            current_call=proxy_call,
        )


if __name__ == "__main__":
    unittest.main()
