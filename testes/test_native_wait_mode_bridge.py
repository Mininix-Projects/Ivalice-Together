import struct
import unittest

import coop_runtime as coop
from native_wait_mode_bridge import (
    BATTLE_MODE_ACTIVE_MASK,
    COMMAND_SELECTION_ACTIVE_MASK,
    COMMAND_SELECTION_FLAGS_OFFSET,
    NativeWaitModeBridge,
    SETTINGS_POINTER_GLOBAL,
    UI_MANAGER_GLOBAL,
    WAIT_MENU_APPLIED_OFFSET,
    WAIT_MENU_DESIRED_OFFSET,
    WAIT_PREDICATE_CAVE_CODE_OFFSET,
    WAIT_PREDICATE_CAVE_FLAG_OFFSET,
    WAIT_PREDICATE_CAVE_MAGIC,
    WAIT_PREDICATE_HOOK_SITE,
    WAIT_PREDICATE_ORIGINAL_FUNCTION,
    WAIT_PREDICATE_STATE_HOOK_SITE,
    WAIT_PREDICATE_STATE_ORIGINAL,
    NativeWaitPredicateHook,
    command_selection_value,
    native_battle_mode_is_wait,
    wait_predicate_cave,
    wait_predicate_site_patch,
)


class FakeProcess:
    def __init__(self):
        self.data = {}
        self.writes = []

    def read(self, address, size):
        return bytes(self.data.get(address + offset, 0) for offset in range(size))

    def write(self, address, payload):
        payload = bytes(payload)
        self.writes.append((address, payload))
        for offset, value in enumerate(payload):
            self.data[address + offset] = value

    def patch_code(self, address, payload):
        self.write(address, payload)

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


class FakeWaitPredicateHook:
    def __init__(self):
        self.requests = []
        self.restored = False

    def maintain(self, desired):
        self.requests.append(bool(desired))
        return True

    def restore(self):
        self.restored = True


class NativeWaitModeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.process = FakeProcess()
        self.cave_base = 0x50000000
        if self.cave_base + 0x200 == coop.COMMAND_BLOCK:
            self.cave_base += 0x10000
        self.command_block = self.cave_base + 0x200
        self.manager = 0x70000000
        self.address = self.manager + COMMAND_SELECTION_FLAGS_OFFSET
        self.process.write(UI_MANAGER_GLOBAL, struct.pack("<Q", self.manager))
        self.process.write(self.address, b"\x21")
        self.settings = 0x72000000
        self.process.write(SETTINGS_POINTER_GLOBAL, struct.pack("<Q", self.settings))
        self.process.write(self.settings, struct.pack("<I", 0))
        self.process.writes.clear()
        self.pause_hook = FakeWaitPredicateHook()
        self.bridge = NativeWaitModeBridge(
            self.process,
            self.cave_base,
            wait_predicate_hook=self.pause_hook,
        )

    @property
    def desired_address(self):
        return self.command_block + WAIT_MENU_DESIRED_OFFSET

    @property
    def applied_address(self):
        return self.command_block + WAIT_MENU_APPLIED_OFFSET

    def test_bit_transform_preserves_every_other_flag(self):
        opened = command_selection_value(0xA1, True)
        self.assertEqual(opened, 0xA1 | COMMAND_SELECTION_ACTIVE_MASK)
        self.assertEqual(
            command_selection_value(opened, False),
            0xA1 & ~COMMAND_SELECTION_ACTIVE_MASK,
        )

    def test_open_and_close_publish_native_lifecycle_edges(self):
        self.assertTrue(self.bridge.maintain(True))
        self.assertEqual(self.process.read(self.desired_address, 1), b"\x01")
        self.assertEqual(
            self.process.read(coop.COMMAND_BLOCK + WAIT_MENU_DESIRED_OFFSET, 1),
            b"\x00",
        )
        self.assertEqual(
            self.process.read(self.address, 1)[0] & COMMAND_SELECTION_ACTIVE_MASK,
            COMMAND_SELECTION_ACTIVE_MASK,
        )
        writes = len(self.process.writes)
        self.assertTrue(self.bridge.maintain(True))
        self.assertEqual(len(self.process.writes), writes)
        self.assertTrue(self.bridge.maintain(False))
        self.assertEqual(self.process.read(self.desired_address, 1), b"\x00")
        self.assertEqual(self.process.read(self.address, 1), b"\x21")
        self.assertEqual(self.pause_hook.requests, [True, True, False])

    def test_treasure_release_keeps_wait_but_closes_native_presentation(self):
        self.bridge.maintain(True)
        self.process.write(self.applied_address, b"\x01")

        self.assertTrue(self.bridge.maintain(True, native_presentation=False))
        self.assertEqual(self.process.read(self.desired_address, 1), b"\x00")
        self.assertEqual(self.process.read(self.address, 1), b"\x21")
        self.assertEqual(self.pause_hook.requests, [True, True])

    def test_presentation_ownership_survives_issuer_rewrite_and_close_edge(self):
        self.bridge.maintain(True)
        self.process.write(self.applied_address, b"\x01")
        self.process.write(coop.NATIVE_COMMAND_ACTOR_GLOBAL, struct.pack("<I", 4))
        self.process.write(coop.NATIVE_COMMAND_FLAG_GLOBAL, struct.pack("<I", 1))

        self.assertTrue(self.bridge.owns_native_presentation())

        # Python publishes CLOSE before the game-thread consumer acknowledges
        # it.  The still-applied presentation remains ours for that one frame.
        self.process.write(self.desired_address, b"\x00")
        self.assertTrue(self.bridge.owns_native_presentation())

    def test_presentation_ownership_requires_every_applied_native_signal(self):
        self.process.write(self.applied_address, b"\x01")
        self.process.write(coop.NATIVE_COMMAND_ACTOR_GLOBAL, struct.pack("<I", 4))
        self.process.write(coop.NATIVE_COMMAND_FLAG_GLOBAL, struct.pack("<I", 1))
        self.assertTrue(self.bridge.owns_native_presentation())

        for address, payload in (
            (self.applied_address, b"\x00"),
            (coop.NATIVE_COMMAND_ACTOR_GLOBAL, struct.pack("<I", 0)),
            (coop.NATIVE_COMMAND_FLAG_GLOBAL, struct.pack("<I", 0)),
        ):
            original = self.process.read(address, len(payload))
            self.process.write(address, payload)
            self.assertFalse(self.bridge.owns_native_presentation())
            self.process.write(address, original)

    def test_lifecycle_close_waits_for_the_game_thread_applied_byte(self):
        self.process.write(self.applied_address, b"\x01")
        self.assertFalse(self.bridge.lifecycle_close_complete())
        self.process.write(self.applied_address, b"\x00")
        self.assertTrue(self.bridge.lifecycle_close_complete())

    def test_constructor_releases_stale_desired_but_preserves_applied(self):
        self.process.write(self.desired_address, b"\x01")
        self.process.write(self.applied_address, b"\x01")
        NativeWaitModeBridge(
            self.process,
            self.cave_base,
            wait_predicate_hook=FakeWaitPredicateHook(),
        )
        self.assertEqual(self.process.read(self.desired_address, 1), b"\x00")
        self.assertEqual(self.process.read(self.applied_address, 1), b"\x01")

    def test_ui_generation_change_never_restores_old_pointer(self):
        self.bridge.maintain(True)
        new_manager = 0x71000000
        new_address = new_manager + COMMAND_SELECTION_FLAGS_OFFSET
        self.process.write(UI_MANAGER_GLOBAL, struct.pack("<Q", new_manager))
        self.process.write(new_address, b"\x40")
        self.bridge.maintain(True)
        self.assertEqual(
            self.process.read(new_address, 1),
            bytes((0x40 | COMMAND_SELECTION_ACTIVE_MASK,)),
        )

    def test_native_newer_value_is_not_overwritten_on_close(self):
        self.bridge.maintain(True)
        self.process.write(self.address, b"\x6d")
        self.bridge.maintain(False)
        self.assertEqual(self.process.read(self.address, 1), b"\x6d")
        self.assertEqual(self.process.read(self.desired_address, 1), b"\x00")

    def test_active_battle_mode_never_requests_wait_pause(self):
        self.process.write(
            self.settings,
            struct.pack("<I", BATTLE_MODE_ACTIVE_MASK),
        )
        self.assertTrue(self.bridge.maintain(True))
        self.assertEqual(self.pause_hook.requests, [False])

    def test_battle_mode_reader(self):
        self.assertTrue(native_battle_mode_is_wait(self.process))
        self.process.write(
            self.settings,
            struct.pack("<I", BATTLE_MODE_ACTIVE_MASK),
        )
        self.assertFalse(native_battle_mode_is_wait(self.process))

    def test_restore_disables_predicate_hook(self):
        self.bridge.restore()
        self.assertTrue(self.pause_hook.restored)


class WaitPredicateCodeTests(unittest.TestCase):
    def test_cave_calls_stock_predicate_and_reads_remote_flag(self):
        code_address = 0x30000100
        flag_address = 0x30000200
        code = wait_predicate_cave(code_address, flag_address)

        self.assertEqual(code[0], 0xE8)
        call_target = code_address + 5 + struct.unpack("<i", code[1:5])[0]
        self.assertEqual(call_target, WAIT_PREDICATE_ORIGINAL_FUNCTION)
        self.assertEqual(code[5:11], bytes.fromhex("85 C0 75 0A 80 3D"))
        flag_target = code_address + 16 + struct.unpack("<i", code[11:15])[0]
        self.assertEqual(flag_target, flag_address)
        self.assertEqual(code[15:], bytes.fromhex("00 0F 95 C0 C3"))

    def test_both_site_patches_call_the_shared_cave(self):
        code_address = 0x30000100
        for site_address in (
            WAIT_PREDICATE_HOOK_SITE,
            WAIT_PREDICATE_STATE_HOOK_SITE,
        ):
            patch = wait_predicate_site_patch(code_address, site_address)
            self.assertEqual(len(patch), 5)
            self.assertEqual(patch[0], 0xE8)
            target = site_address + 5 + struct.unpack("<i", patch[1:5])[0]
            self.assertEqual(target, code_address)

    def test_partial_live_hook_aborts_without_hot_repair(self):
        process = FakeProcess()
        base = 0x30000000
        code_address = base + WAIT_PREDICATE_CAVE_CODE_OFFSET
        flag_address = base + WAIT_PREDICATE_CAVE_FLAG_OFFSET
        process.write(base, WAIT_PREDICATE_CAVE_MAGIC)
        process.write(
            code_address,
            wait_predicate_cave(code_address, flag_address),
        )
        process.write(flag_address, b"\x01")
        process.write(
            WAIT_PREDICATE_HOOK_SITE,
            wait_predicate_site_patch(
                code_address,
                WAIT_PREDICATE_HOOK_SITE,
            ),
        )
        process.write(
            WAIT_PREDICATE_STATE_HOOK_SITE,
            WAIT_PREDICATE_STATE_ORIGINAL,
        )

        process.writes.clear()
        memory_before = dict(process.data)

        hook = NativeWaitPredicateHook(process)
        with self.assertRaisesRegex(RuntimeError, "partial native Wait-predicate"):
            hook.maintain(False)

        self.assertFalse(hook.installed)
        self.assertFalse(hook.complete)
        self.assertEqual(process.data, memory_before)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.read(flag_address, 1), b"\x01")
        self.assertEqual(
            process.read(WAIT_PREDICATE_STATE_HOOK_SITE, 5),
            WAIT_PREDICATE_STATE_ORIGINAL,
        )

    def test_exact_live_hook_is_adopted_without_executable_write(self):
        process = FakeProcess()
        base = 0x30000000
        code_address = base + WAIT_PREDICATE_CAVE_CODE_OFFSET
        flag_address = base + WAIT_PREDICATE_CAVE_FLAG_OFFSET
        process.write(base, WAIT_PREDICATE_CAVE_MAGIC)
        process.write(
            code_address,
            wait_predicate_cave(code_address, flag_address),
        )
        process.write(flag_address, b"\x00")
        for site_address, _original in (
            (WAIT_PREDICATE_HOOK_SITE, b""),
            (WAIT_PREDICATE_STATE_HOOK_SITE, b""),
        ):
            process.write(
                site_address,
                wait_predicate_site_patch(code_address, site_address),
            )
        process.writes.clear()

        hook = NativeWaitPredicateHook(process)
        self.assertTrue(hook.install())

        self.assertTrue(hook.installed)
        self.assertTrue(hook.complete)
        self.assertEqual(process.writes, [])


if __name__ == "__main__":
    unittest.main()
