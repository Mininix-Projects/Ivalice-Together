from __future__ import annotations

import struct
import unittest

import headless_menu_core as legacy


class FakeProcess:
    def __init__(self) -> None:
        self.memory: dict[int, int] = {}
        self.patches: list[tuple[int, bytes]] = []

    def seed(self, address: int, data: bytes) -> None:
        for offset, value in enumerate(data):
            self.memory[address + offset] = value

    def read(self, address: int, size: int) -> bytes:
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address: int, data: bytes) -> None:
        self.seed(address, data)

    def patch_code(self, address: int, data: bytes) -> None:
        self.patches.append((address, data))
        self.seed(address, data)

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]


def jump(source: int, target: int) -> bytes:
    return b"\xe9" + struct.pack("<i", target - (source + 5))


class OrphanedUiTickRecoveryTests(unittest.TestCase):
    @staticmethod
    def seed_legacy_target(
        process: FakeProcess,
        dispatcher_state: int,
    ) -> tuple[int, int, bytes]:
        region = 0x60002000
        cave = region + 0x100
        state = region + 0x800
        root = jump(legacy.UI_MANAGER_TICK_CALL, cave)
        process.seed(legacy.UI_MANAGER_TICK_CALL, root)
        process.seed(region, b"FF12NCR1")
        process.seed(cave, b"\x48\xb8" + struct.pack("<Q", state))
        process.seed(state, struct.pack("<I", dispatcher_state) + b"\xaa" * 60)
        return cave, state, root

    @staticmethod
    def seed_legacy_area(
        process: FakeProcess,
        dispatcher_state: int,
    ) -> tuple[int, int, bytes]:
        region = 0x60003000
        cave = region + 0x100
        state = region + 0x800
        root = jump(legacy.UI_MANAGER_TICK_CALL, cave)
        process.seed(legacy.UI_MANAGER_TICK_CALL, root)
        process.seed(region, b"FF12NAO1")
        process.seed(cave, b"\x48\xb8" + struct.pack("<Q", state))
        process.seed(state, struct.pack("<I", dispatcher_state) + b"\xaa" * 60)
        return cave, state, root

    def test_preserves_armed_or_claimed_dispatcher_until_native_completion(self):
        for dispatcher_state in (1, 2):
            with self.subTest(dispatcher_state=dispatcher_state):
                process = FakeProcess()
                base = 0x28660000
                cave = base + legacy.DISPATCH_CAVE_OFFSET
                state = base + legacy.DISPATCH_STATE_OFFSET
                root = jump(legacy.UI_MANAGER_TICK_CALL, cave)
                process.seed(legacy.UI_MANAGER_TICK_CALL, root)
                process.seed(cave, b"\x48\xb8" + struct.pack("<Q", state))
                process.seed(
                    state,
                    struct.pack("<I", dispatcher_state) + b"\xaa" * 60,
                )

                recovered = legacy.recover_orphaned_ui_tick_dispatcher(process, base)

                self.assertEqual(recovered, "action:in-flight")
                self.assertEqual(process.read(legacy.UI_MANAGER_TICK_CALL, 5), root)
                self.assertEqual(process.u32(state), dispatcher_state)
                self.assertEqual(process.patches, [])

    def test_terminal_action_dispatcher_requires_restart_without_mutation(self):
        process = FakeProcess()
        base = 0x28660000
        cave = base + legacy.DISPATCH_CAVE_OFFSET
        state = base + legacy.DISPATCH_STATE_OFFSET
        process.seed(
            legacy.UI_MANAGER_TICK_CALL,
            jump(legacy.UI_MANAGER_TICK_CALL, cave),
        )
        process.seed(cave, b"\x48\xb8" + struct.pack("<Q", state))
        process.seed(state, struct.pack("<I", 2) + b"\xaa" * 60)

        self.assertEqual(
            legacy.recover_orphaned_ui_tick_dispatcher(process, base),
            "action:in-flight",
        )
        process.seed(state, struct.pack("<I", 3) + b"\xbb" * 60)

        root = process.read(legacy.UI_MANAGER_TICK_CALL, 5)
        terminal = process.read(state, 0x40)
        with self.assertRaisesRegex(RuntimeError, "process-resident"):
            legacy.recover_orphaned_ui_tick_dispatcher(process, base)

        self.assertEqual(process.read(legacy.UI_MANAGER_TICK_CALL, 5), root)
        self.assertEqual(process.read(state, 0x40), terminal)
        self.assertEqual(process.patches, [])

    def test_terminal_combat_indicator_requires_restart_without_mutation(self):
        process = FakeProcess()
        base = 0x28660000
        cave = base + legacy.INDICATOR_CAVE_OFFSET
        state = base + legacy.INDICATOR_STATE_OFFSET
        process.seed(
            legacy.UI_MANAGER_TICK_CALL,
            jump(legacy.UI_MANAGER_TICK_CALL, cave),
        )
        process.seed(cave, b"\x48\xb8" + struct.pack("<Q", state))
        process.seed(state, struct.pack("<I", 3) + b"\xaa" * 60)

        root = process.read(legacy.UI_MANAGER_TICK_CALL, 5)
        terminal = process.read(state, 0x40)
        with self.assertRaisesRegex(RuntimeError, "process-resident"):
            legacy.recover_orphaned_ui_tick_dispatcher(process, base)

        self.assertEqual(process.read(legacy.UI_MANAGER_TICK_CALL, 5), root)
        self.assertEqual(process.read(state, 0x40), terminal)
        self.assertEqual(process.patches, [])

    def test_legacy_target_armed_or_claimed_remains_quarantined(self):
        for dispatcher_state in (1, 2):
            with self.subTest(dispatcher_state=dispatcher_state):
                process = FakeProcess()
                _cave, state, root = self.seed_legacy_target(
                    process,
                    dispatcher_state,
                )

                recovered = legacy.recover_orphaned_ui_tick_dispatcher(
                    process,
                    0x50000000,
                )

                self.assertEqual(recovered, "target-circle:in-flight")
                self.assertEqual(process.read(legacy.UI_MANAGER_TICK_CALL, 5), root)
                self.assertEqual(process.u32(state), dispatcher_state)
                self.assertEqual(process.patches, [])

    def test_legacy_target_terminal_requires_restart_without_mutation(self):
        for dispatcher_state in (3, 4):
            with self.subTest(dispatcher_state=dispatcher_state):
                process = FakeProcess()
                _cave, state, root = self.seed_legacy_target(
                    process,
                    dispatcher_state,
                )
                terminal = process.read(state, 0x40)

                with self.assertRaisesRegex(RuntimeError, "process-resident"):
                    legacy.recover_orphaned_ui_tick_dispatcher(
                        process,
                        0x50000000,
                    )

                self.assertEqual(process.read(legacy.UI_MANAGER_TICK_CALL, 5), root)
                self.assertEqual(process.read(state, 0x40), terminal)
                self.assertEqual(process.patches, [])

    def test_current_permanent_mux_at_legacy_offset_is_not_misclassified(self):
        process = FakeProcess()
        region = 0x60002000
        cave = region + 0x100
        root = jump(legacy.UI_MANAGER_TICK_CALL, cave)
        process.seed(legacy.UI_MANAGER_TICK_CALL, root)
        process.seed(region, b"FF12UT1")
        process.seed(cave, b"\x51\x52\x41\x50\x41\x51")

        recovered = legacy.recover_orphaned_ui_tick_dispatcher(
            process,
            0x50000000,
        )

        self.assertIsNone(recovered)
        self.assertEqual(process.read(legacy.UI_MANAGER_TICK_CALL, 5), root)
        self.assertEqual(process.patches, [])

    def test_legacy_area_armed_or_claimed_remains_quarantined(self):
        for dispatcher_state in (1, 2):
            with self.subTest(dispatcher_state=dispatcher_state):
                process = FakeProcess()
                _cave, state, root = self.seed_legacy_area(
                    process,
                    dispatcher_state,
                )

                recovered = legacy.recover_orphaned_ui_tick_dispatcher(
                    process,
                    0x50000000,
                )

                self.assertEqual(recovered, "area-circle:in-flight")
                self.assertEqual(process.read(legacy.UI_MANAGER_TICK_CALL, 5), root)
                self.assertEqual(process.u32(state), dispatcher_state)
                self.assertEqual(process.patches, [])

    def test_legacy_area_terminal_requires_restart_without_mutation(self):
        for dispatcher_state in (3, 4):
            with self.subTest(dispatcher_state=dispatcher_state):
                process = FakeProcess()
                _cave, state, root = self.seed_legacy_area(
                    process,
                    dispatcher_state,
                )
                terminal = process.read(state, 0x40)

                with self.assertRaisesRegex(RuntimeError, "process-resident"):
                    legacy.recover_orphaned_ui_tick_dispatcher(
                        process,
                        0x50000000,
                    )

                self.assertEqual(process.read(legacy.UI_MANAGER_TICK_CALL, 5), root)
                self.assertEqual(process.read(state, 0x40), terminal)
                self.assertEqual(process.patches, [])

    def test_preserves_unrecognized_ui_tick_hook(self):
        process = FakeProcess()
        base = 0x28660000
        foreign = 0x30000000
        patch = jump(legacy.UI_MANAGER_TICK_CALL, foreign)
        process.seed(legacy.UI_MANAGER_TICK_CALL, patch)
        process.seed(foreign, b"\x48\xb8" + struct.pack("<Q", 0x30001000))

        recovered = legacy.recover_orphaned_ui_tick_dispatcher(process, base)

        self.assertIsNone(recovered)
        self.assertEqual(
            process.read(legacy.UI_MANAGER_TICK_CALL, len(patch)),
            patch,
        )
        self.assertEqual(process.patches, [])

    def test_preserves_known_destination_with_wrong_state_signature(self):
        process = FakeProcess()
        base = 0x28660000
        cave = base + legacy.INDICATOR_CAVE_OFFSET
        process.seed(
            legacy.UI_MANAGER_TICK_CALL,
            jump(legacy.UI_MANAGER_TICK_CALL, cave),
        )
        process.seed(cave, b"\x48\xb8" + struct.pack("<Q", base + 0x1234))

        recovered = legacy.recover_orphaned_ui_tick_dispatcher(process, base)

        self.assertIsNone(recovered)
        self.assertEqual(process.patches, [])

    def test_stock_tick_is_a_noop(self):
        process = FakeProcess()
        process.seed(legacy.UI_MANAGER_TICK_CALL, legacy.ORIGINAL_TICK_CALL)

        recovered = legacy.recover_orphaned_ui_tick_dispatcher(process, 0x28660000)

        self.assertIsNone(recovered)
        self.assertEqual(process.patches, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
