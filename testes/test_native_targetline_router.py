import inspect
import struct
import unittest

import headless_menu_shell as menu
import menu_themes


class FakeProcess:
    def __init__(self):
        self.memory = {}
        self.seed(menu.ACTION_LINE_RENDER_SITE, menu.ACTION_LINE_RENDER_ORIGINAL)
        self.seed(
            menu.ACTION_TARGETLINE_RENDER_SITE,
            menu.ACTION_TARGETLINE_RENDER_ORIGINAL,
        )

    def seed(self, address, data):
        for offset, value in enumerate(bytes(data)):
            self.memory[address + offset] = value

    def read(self, address, size):
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address, data):
        self.seed(address, data)

    def patch_code(self, address, data):
        self.write(address, data)

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]


class NativeActionLineThemeRouterTests(unittest.TestCase):
    BASE = 0x28650000

    def test_install_hooks_arrow_and_real_targetline_renderers(self):
        process = FakeProcess()
        router = menu.NativeActionLineThemeRouter(process, self.BASE)
        router.install()

        patch = process.read(menu.ACTION_LINE_RENDER_SITE, len(router.patch))
        self.assertEqual(patch, router.patch)
        self.assertEqual(patch[0], 0xE9)
        patch_target = (
            menu.ACTION_LINE_RENDER_SITE + 5 + struct.unpack("<i", patch[1:5])[0]
        )
        self.assertEqual(patch_target, router.cave)

        cave = menu.build_action_line_theme_cave(router.cave, router.state)
        self.assertIn(b"\x83\xf8\x04", cave)
        self.assertIn(b"\x8b\x96\xd0\x02\x00\x00", cave)
        self.assertIn(b"\x41\x81\x4a\x0c\x00\x01\x00\x00", cave)
        self.assertIn(b"\x41\x81\x4a\x14\x00\x01\x00\x00", cave)
        self.assertIn(b"\x41\x81\x4a\x1c\x00\x01\x00\x00", cave)
        self.assertIn(b"\x41\x83\x7a\x0c\x04", cave)
        self.assertIn(b"\x41\x83\x7a\x14\x04", cave)
        self.assertIn(b"\x41\x83\x7a\x1c\x04", cave)
        self.assertEqual(cave[-13:-5], menu.ACTION_LINE_RENDER_ORIGINAL)
        self.assertEqual(cave[-5], 0xE9)
        return_target = router.cave + len(cave) + struct.unpack("<i", cave[-4:])[0]
        self.assertEqual(return_target, menu.ACTION_LINE_RENDER_RETURN)

        targetline_patch = process.read(
            menu.ACTION_TARGETLINE_RENDER_SITE,
            len(router.targetline_patch),
        )
        self.assertEqual(targetline_patch, router.targetline_patch)
        self.assertEqual(targetline_patch[0], 0xE9)
        targetline_patch_target = (
            menu.ACTION_TARGETLINE_RENDER_SITE
            + 5
            + struct.unpack("<i", targetline_patch[1:5])[0]
        )
        self.assertEqual(targetline_patch_target, router.targetline_cave)

        targetline_cave = menu.build_action_targetline_theme_cave(
            router.targetline_cave, router.state
        )
        self.assertTrue(targetline_cave.startswith(b"\x49\xba"))
        self.assertIn(b"\x45\x8b\x87\x10\x08\x00\x00", targetline_cave)
        self.assertEqual(targetline_cave.count(b"\xa9\x00\x01\x00\x00"), 3)
        self.assertIn(b"\xb8\x04\x00\x00\x00", targetline_cave)
        self.assertEqual(
            targetline_cave[-14:-5],
            menu.ACTION_TARGETLINE_RENDER_ORIGINAL,
        )
        self.assertEqual(targetline_cave[-5], 0xE9)
        targetline_return = (
            router.targetline_cave
            + len(targetline_cave)
            + struct.unpack("<i", targetline_cave[-4:])[0]
        )
        self.assertEqual(targetline_return, menu.ACTION_TARGETLINE_RENDER_RETURN)

    def test_publish_uses_three_character_handles_and_is_atomic(self):
        process = FakeProcess()
        router = menu.NativeActionLineThemeRouter(process, self.BASE)
        router.install()

        changed = router.publish(
            (
                (0x00400021, 0),
                (0x00400022, 2),
                (0x00400023, 1),
            )
        )
        self.assertTrue(changed)
        self.assertEqual(process.u32(router.state), 3)
        self.assertEqual(
            process.u32(router.state + 4),
            menu.ACTION_LINE_THEME_STATE_MAGIC,
        )
        self.assertEqual(process.u32(router.state + 0x08), 0x00400021)
        self.assertEqual(process.u32(router.state + 0x0C), 0)
        self.assertEqual(process.u32(router.state + 0x10), 0x00400022)
        self.assertEqual(process.u32(router.state + 0x14), 2)
        self.assertEqual(process.u32(router.state + 0x18), 0x00400023)
        self.assertEqual(process.u32(router.state + 0x1C), 1)
        self.assertFalse(router.publish(router.published_entries))

    def test_character_theme_mapping_follows_identity_not_party_slot(self):
        selection = menu_themes.ThemeSelection(
            character_overrides=((0, "emerald"), (1, "pink"), (4, "fire"))
        )
        party = [
            (4, "Basch", 0x41, 0x1000),
            (0, "Vaan", 0x42, 0x2000),
            (1, "Ashe", 0x43, 0x3000),
        ]
        self.assertEqual(
            menu.action_line_theme_entries(party, selection),
            ((0x41, 3), (0x42, 3), (0x43, 2)),
        )

    def test_balthier_uses_repurposed_brown_palette_for_all_action_lines(self):
        selection = menu_themes.ThemeSelection(character_overrides=((3, "dark_brown"),))
        party = [(3, "Balthier", 0x44, 0x4000)]
        self.assertEqual(
            menu.action_line_theme_entries(party, selection),
            ((0x44, 6),),
        )

    def test_player_theme_mapping_follows_handle_owner_after_role_swap(self):
        selection = menu_themes.ThemeSelection(
            player_1="blue",
            player_2="pink",
            player_3="emerald",
            assignment_mode="player",
            # Deliberately disagree with the player colours.
            character_overrides=((0, "ruby"), (1, "gold"), (4, "fire")),
        )
        party = [
            # An unowned guest comes first and must not consume one router row.
            (14, "Reddas", 0x40, 0x0800),
            (4, "Basch", 0x41, 0x1000),
            (0, "Vaan", 0x42, 0x2000),
            (1, "Ashe", 0x43, 0x3000),
        ]
        before = menu.action_line_theme_entries(
            party,
            selection,
            player_by_handle={0x41: 1, 0x42: 2, 0x43: 3},
        )
        after = menu.action_line_theme_entries(
            party,
            selection,
            player_by_handle={0x41: 1, 0x42: 3, 0x43: 2},
        )
        self.assertEqual(before, ((0x41, 0), (0x42, 2), (0x43, 3)))
        self.assertEqual(after, ((0x41, 0), (0x42, 3), (0x43, 2)))

    def test_player_mode_without_a_proven_owner_leaves_action_line_native(self):
        selection = menu_themes.ThemeSelection(assignment_mode="player")
        party = [(4, "Basch", 0x41, 0x1000)]
        self.assertEqual(
            menu.action_line_theme_entries(party, selection),
            (),
        )

    def test_live_publish_uses_role_handles_before_theme_signature_shortcut(self):
        source = inspect.getsource(menu.CoopMenuRuntime.sync_presenter_themes)
        self.assertIn("player_by_handle=player_by_handle", source)
        self.assertLess(
            source.index("native_action_line_theme_router.publish"),
            source.index("if signature == self.menu_theme_signature"),
        )

    def test_restore_disarms_state_but_keeps_process_lifetime_renderers(self):
        process = FakeProcess()
        router = menu.NativeActionLineThemeRouter(process, self.BASE)
        router.install()
        router.publish(((0x00400021, 5),))
        roots = (
            process.read(menu.ACTION_LINE_RENDER_SITE, len(router.patch)),
            process.read(
                menu.ACTION_TARGETLINE_RENDER_SITE,
                len(router.targetline_patch),
            ),
        )
        router.restore()
        self.assertEqual(
            process.read(
                menu.ACTION_LINE_RENDER_SITE,
                len(router.patch),
            ),
            roots[0],
        )
        self.assertEqual(
            process.read(
                menu.ACTION_TARGETLINE_RENDER_SITE,
                len(router.targetline_patch),
            ),
            roots[1],
        )
        self.assertEqual(process.u32(router.state), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
