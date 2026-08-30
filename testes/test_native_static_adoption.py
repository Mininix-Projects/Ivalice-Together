import unittest

import headless_menu_core as core
import headless_menu_shell as shell
import native_action_line_theme as action_line
import native_target_circle_controller as target_circle


class CountingProcess:
    def __init__(self):
        self.memory = {}
        self.writes = []
        self.patches = []

    def seed(self, address, payload):
        for offset, value in enumerate(bytes(payload)):
            self.memory[int(address) + offset] = value

    def read(self, address, size):
        return bytes(
            self.memory.get(int(address) + offset, 0) for offset in range(int(size))
        )

    def write(self, address, payload):
        payload = bytes(payload)
        self.writes.append((int(address), payload))
        self.seed(address, payload)

    def patch_code(self, address, payload):
        payload = bytes(payload)
        self.patches.append((int(address), payload))
        self.seed(address, payload)

    def u32(self, address):
        return int.from_bytes(self.read(address, 4), "little")

    def clear_activity(self):
        self.writes.clear()
        self.patches.clear()


class NativeStaticAdoptionTests(unittest.TestCase):
    BASE = 0x28650000

    def test_action_line_router_exact_adopt_is_read_only(self):
        process = CountingProcess()
        process.seed(
            action_line.ACTION_LINE_RENDER_SITE,
            action_line.ACTION_LINE_RENDER_ORIGINAL,
        )
        process.seed(
            action_line.ACTION_TARGETLINE_RENDER_SITE,
            action_line.ACTION_TARGETLINE_RENDER_ORIGINAL,
        )
        owner = action_line.NativeActionLineThemeRouter(process, self.BASE)
        owner.install()
        process.clear_activity()

        adopted = action_line.NativeActionLineThemeRouter(process, self.BASE)
        adopted.install()

        self.assertTrue(adopted.installed)
        self.assertFalse(adopted.owns_static)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_action_line_router_cave_mismatch_aborts_read_only(self):
        for cave_name in ("cave", "targetline_cave"):
            with self.subTest(cave=cave_name):
                process = CountingProcess()
                process.seed(
                    action_line.ACTION_LINE_RENDER_SITE,
                    action_line.ACTION_LINE_RENDER_ORIGINAL,
                )
                process.seed(
                    action_line.ACTION_TARGETLINE_RENDER_SITE,
                    action_line.ACTION_TARGETLINE_RENDER_ORIGINAL,
                )
                owner = action_line.NativeActionLineThemeRouter(process, self.BASE)
                owner.install()
                cave = getattr(owner, cave_name)
                poisoned = bytearray(process.read(cave, 1))
                poisoned[0] ^= 0x80
                process.seed(cave, poisoned)
                process.clear_activity()

                adopted = action_line.NativeActionLineThemeRouter(process, self.BASE)
                with self.assertRaisesRegex(RuntimeError, "cave mismatch"):
                    adopted.install()

                self.assertFalse(adopted.installed)
                self.assertFalse(adopted.owns_static)
                self.assertEqual(process.writes, [])
                self.assertEqual(process.patches, [])

    def test_ui_sound_player_exact_adopt_is_read_only(self):
        process = CountingProcess()
        process.seed(core.SOUND_PUMP_HOOK, core.SOUND_PUMP_ORIGINAL)
        core.NativeUISoundPlayer(process, self.BASE)
        process.clear_activity()

        adopted = core.NativeUISoundPlayer(process, self.BASE)

        self.assertTrue(adopted.patched)
        self.assertFalse(adopted.owns_static)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_ui_sound_player_cave_mismatch_aborts_read_only(self):
        process = CountingProcess()
        process.seed(core.SOUND_PUMP_HOOK, core.SOUND_PUMP_ORIGINAL)
        owner = core.NativeUISoundPlayer(process, self.BASE)
        poisoned = bytearray(process.read(owner.cave, 1))
        poisoned[0] ^= 0x80
        process.seed(owner.cave, poisoned)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "cave mismatch"):
            core.NativeUISoundPlayer(process, self.BASE)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_ui_sound_owner_cleanup_disarms_without_unpatching(self):
        process = CountingProcess()
        process.seed(core.SOUND_PUMP_HOOK, core.SOUND_PUMP_ORIGINAL)
        owner = core.NativeUISoundPlayer(process, self.BASE)
        root = process.read(core.SOUND_PUMP_HOOK, len(owner.patch))
        process.clear_activity()

        owner.restore()

        self.assertEqual(
            process.read(core.SOUND_PUMP_HOOK, len(owner.patch)),
            root,
        )
        self.assertEqual(process.patches, [])
        self.assertEqual(process.writes, [(owner.state, b"\x00" * 0x10)])

    def test_ui_sound_stock_root_with_old_cave_aborts_read_only(self):
        process = CountingProcess()
        process.seed(core.SOUND_PUMP_HOOK, core.SOUND_PUMP_ORIGINAL)
        process.seed(self.BASE + core.SOUND_CAVE_OFFSET, b"old code")
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "non-empty native UI sound cave"):
            core.NativeUISoundPlayer(process, self.BASE)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_p1_list_bridge_exact_adopt_is_read_only(self):
        process = CountingProcess()
        process.seed(shell.P1_LIST_CALLBACK_SITE, shell.P1_LIST_CALLBACK_ORIGINAL)
        owner = shell.NativeP1ListInputBridge(process, self.BASE)
        owner.install()
        process.clear_activity()

        adopted = shell.NativeP1ListInputBridge(process, self.BASE)
        adopted.install()

        self.assertTrue(adopted.installed)
        self.assertFalse(adopted.owns_static)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_p1_list_static_image_stops_before_dialog_desktop_button_cave(self):
        self.assertLessEqual(
            shell.P1_LIST_INPUT_CAVE_OFFSET + shell.P1_LIST_INPUT_CAVE_SIZE,
            shell.NATIVE_DESKTOP_BUTTON_CAVE_OFFSET,
        )
        process = CountingProcess()
        process.seed(shell.P1_LIST_CALLBACK_SITE, shell.P1_LIST_CALLBACK_ORIGINAL)
        desktop_cave = self.BASE + shell.NATIVE_DESKTOP_BUTTON_CAVE_OFFSET
        sentinel = b"dialog-desktop-button"
        process.seed(desktop_cave, sentinel)

        owner = shell.NativeP1ListInputBridge(process, self.BASE)
        owner.install()

        self.assertEqual(process.read(desktop_cave, len(sentinel)), sentinel)

    def test_p1_list_bridge_cave_mismatch_aborts_read_only(self):
        process = CountingProcess()
        process.seed(shell.P1_LIST_CALLBACK_SITE, shell.P1_LIST_CALLBACK_ORIGINAL)
        owner = shell.NativeP1ListInputBridge(process, self.BASE)
        owner.install()
        poisoned = bytearray(process.read(owner.cave, 1))
        poisoned[0] ^= 0x80
        process.seed(owner.cave, poisoned)
        process.clear_activity()

        adopted = shell.NativeP1ListInputBridge(process, self.BASE)
        with self.assertRaisesRegex(RuntimeError, "cave mismatch"):
            adopted.install()

        self.assertFalse(adopted.installed)
        self.assertFalse(adopted.owns_static)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_p1_list_owner_cleanup_disarms_without_unpatching(self):
        process = CountingProcess()
        process.seed(shell.P1_LIST_CALLBACK_SITE, shell.P1_LIST_CALLBACK_ORIGINAL)
        owner = shell.NativeP1ListInputBridge(process, self.BASE)
        owner.install()
        root = process.read(shell.P1_LIST_CALLBACK_SITE, 5)
        process.clear_activity()

        owner.restore()

        self.assertEqual(process.read(shell.P1_LIST_CALLBACK_SITE, 5), root)
        self.assertEqual(process.patches, [])
        self.assertEqual(process.writes, [(owner.state, b"\x00" * 4)])

    def test_p1_list_stock_root_with_old_cave_aborts_read_only(self):
        process = CountingProcess()
        process.seed(shell.P1_LIST_CALLBACK_SITE, shell.P1_LIST_CALLBACK_ORIGINAL)
        process.seed(self.BASE + shell.P1_LIST_INPUT_CAVE_OFFSET, b"old code")
        process.clear_activity()

        bridge = shell.NativeP1ListInputBridge(process, self.BASE)
        with self.assertRaisesRegex(RuntimeError, "non-empty native P1 list"):
            bridge.install()

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_target_circle_capacity_adopted_cleanup_preserves_resident_patch(self):
        process = CountingProcess()
        process.seed(
            target_circle.TARGET_CIRCLE_CAPACITY_CHECK,
            target_circle.TARGET_CIRCLE_CAPACITY_TWO,
        )
        owner = target_circle.NativeTargetCircleCapacity(process)
        owner.maintain()
        self.assertTrue(owner.owns_static)
        process.clear_activity()

        adopted = target_circle.NativeTargetCircleCapacity(process)
        adopted.maintain()
        self.assertTrue(adopted.installed)
        self.assertFalse(adopted.owns_static)
        adopted.restore()

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])
        self.assertEqual(
            process.read(
                target_circle.TARGET_CIRCLE_CAPACITY_CHECK,
                len(target_circle.TARGET_CIRCLE_CAPACITY_THREE),
            ),
            target_circle.TARGET_CIRCLE_CAPACITY_THREE,
        )

    def test_target_circle_capacity_owner_cleanup_keeps_resident_patch(self):
        process = CountingProcess()
        process.seed(
            target_circle.TARGET_CIRCLE_CAPACITY_CHECK,
            target_circle.TARGET_CIRCLE_CAPACITY_TWO,
        )
        owner = target_circle.NativeTargetCircleCapacity(process)
        owner.maintain()
        process.clear_activity()

        owner.restore()

        self.assertEqual(process.patches, [])
        self.assertEqual(
            process.read(
                target_circle.TARGET_CIRCLE_CAPACITY_CHECK,
                len(target_circle.TARGET_CIRCLE_CAPACITY_THREE),
            ),
            target_circle.TARGET_CIRCLE_CAPACITY_THREE,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
