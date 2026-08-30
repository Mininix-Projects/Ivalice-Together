from __future__ import annotations

import struct
import time
import unittest

import coop_runtime as coop
from native_area_target_circle_controller import (
    AREA_CIRCLE_LIST_OFFSET,
    AREA_CIRCLE_APPEND,
    AREA_CIRCLE_FACTORY,
    P1_OWNER_TAG,
    P2_OWNER_TAG,
    P3_OWNER_TAG,
    CODE_OFFSET,
    STATE_OFFSET,
    build_create_dispatcher,
)
import native_area_target_circle_controller as area_circle


class NativeAreaTargetCircleControllerTests(unittest.TestCase):
    def test_render_gate_owns_only_the_native_area_bit(self) -> None:
        settings = 0x300000

        class FakeProcess:
            def __init__(self) -> None:
                self.values = {
                    area_circle.SETTINGS_GLOBAL: settings,
                    settings + area_circle.SETTINGS_FLAGS_OFFSET: 0x00A20000,
                }

            def u64(self, address: int) -> int:
                return self.values.get(address, 0)

            def u32(self, address: int) -> int:
                return self.values.get(address, 0)

            def write(self, address: int, data: bytes) -> None:
                self.values[address] = struct.unpack("<I", data)[0]

        process = FakeProcess()
        gate = area_circle.NativeAreaRenderGate(process)
        original = process.u32(settings + area_circle.SETTINGS_FLAGS_OFFSET)

        self.assertTrue(gate.sync(True))
        self.assertEqual(
            process.u32(settings + area_circle.SETTINGS_FLAGS_OFFSET),
            original | area_circle.AREA_RENDER_ENABLED,
        )

        gate.sync(False)
        self.assertEqual(
            process.u32(settings + area_circle.SETTINGS_FLAGS_OFFSET),
            original,
        )

    def test_render_gate_preserves_a_bit_that_was_already_enabled(self) -> None:
        settings = 0x300000

        class FakeProcess:
            def __init__(self) -> None:
                self.values = {
                    area_circle.SETTINGS_GLOBAL: settings,
                    settings + area_circle.SETTINGS_FLAGS_OFFSET: 0x12
                    | area_circle.AREA_RENDER_ENABLED,
                }

            def u64(self, address: int) -> int:
                return self.values.get(address, 0)

            def u32(self, address: int) -> int:
                return self.values.get(address, 0)

            def write(self, address: int, data: bytes) -> None:
                self.values[address] = struct.unpack("<I", data)[0]

        process = FakeProcess()
        gate = area_circle.NativeAreaRenderGate(process)
        original = process.u32(settings + area_circle.SETTINGS_FLAGS_OFFSET)

        gate.sync(True)
        gate.restore()
        self.assertEqual(
            process.u32(settings + area_circle.SETTINGS_FLAGS_OFFSET),
            original,
        )

    def test_render_gate_never_restores_through_a_stale_settings_pointer(self) -> None:
        old_settings = 0x300000
        new_settings = 0x310000

        class FakeProcess:
            def __init__(self) -> None:
                self.values = {
                    area_circle.SETTINGS_GLOBAL: old_settings,
                    old_settings + area_circle.SETTINGS_FLAGS_OFFSET: 0x12,
                    new_settings + area_circle.SETTINGS_FLAGS_OFFSET: 0x34,
                }
                self.writes = []

            def u64(self, address: int) -> int:
                return self.values.get(address, 0)

            def u32(self, address: int) -> int:
                return self.values.get(address, 0)

            def write(self, address: int, data: bytes) -> None:
                self.writes.append(address)
                self.values[address] = struct.unpack("<I", data)[0]

        process = FakeProcess()
        gate = area_circle.NativeAreaRenderGate(process)
        gate.sync(True)
        old_after_publish = process.u32(
            old_settings + area_circle.SETTINGS_FLAGS_OFFSET
        )
        process.values[area_circle.SETTINGS_GLOBAL] = new_settings
        process.writes.clear()

        gate.sync(False)

        self.assertEqual(process.writes, [])
        self.assertEqual(
            process.u32(old_settings + area_circle.SETTINGS_FLAGS_OFFSET),
            old_after_publish,
        )

    def test_render_gate_preserves_externally_changed_settings_word(self) -> None:
        settings = 0x300000

        class FakeProcess:
            def __init__(self) -> None:
                self.values = {
                    area_circle.SETTINGS_GLOBAL: settings,
                    settings + area_circle.SETTINGS_FLAGS_OFFSET: 0x12,
                }
                self.writes = []

            def u64(self, address: int) -> int:
                return self.values.get(address, 0)

            def u32(self, address: int) -> int:
                return self.values.get(address, 0)

            def write(self, address: int, data: bytes) -> None:
                self.writes.append(address)
                self.values[address] = struct.unpack("<I", data)[0]

        process = FakeProcess()
        gate = area_circle.NativeAreaRenderGate(process)
        gate.sync(True)
        address = settings + area_circle.SETTINGS_FLAGS_OFFSET
        process.values[address] |= 0x80
        changed = process.values[address]
        process.writes.clear()

        gate.restore()

        self.assertEqual(process.writes, [])
        self.assertEqual(process.u32(address), changed)

    def test_unchanged_live_circle_skips_remote_memory(self) -> None:
        class NoRemoteAccess:
            def __getattr__(self, name):
                raise AssertionError(name)

        controller = area_circle.NativeAreaTargetCircleController.__new__(
            area_circle.NativeAreaTargetCircleController
        )
        controller.process = NoRemoteAccess()
        controller.node = 0x400000
        controller.target = 0x100008
        controller.palette = 4
        controller.published_palette = 4
        controller.published_radius = 8.0
        controller.published_collision_radius = 0.5
        controller.next_maintenance = time.monotonic() + 1.0

        self.assertTrue(
            controller.sync(
                0x100008,
                8.0,
                palette=4,
                collision_radius=0.5,
            )
        )

    def test_player_tags_are_distinct_and_stable(self) -> None:
        self.assertEqual(P1_OWNER_TAG.to_bytes(4, "little"), b"AOP1")
        self.assertEqual(P2_OWNER_TAG.to_bytes(4, "little"), b"AOP2")
        self.assertEqual(P3_OWNER_TAG.to_bytes(4, "little"), b"AOP3")
        self.assertNotEqual(P1_OWNER_TAG, P2_OWNER_TAG)
        self.assertNotEqual(P2_OWNER_TAG, P3_OWNER_TAG)

    def test_dispatcher_is_owner_neutral_and_returns_to_the_permanent_mux(self) -> None:
        cave = 0x6A000100
        state = 0x6A000800
        code = build_create_dispatcher(cave, state, P2_OWNER_TAG)
        self.assertLess(len(code), 0x700)
        self.assertIn(struct.pack("<Q", AREA_CIRCLE_FACTORY), code)
        self.assertIn(struct.pack("<Q", AREA_CIRCLE_APPEND), code)
        for owner_tag in (P1_OWNER_TAG, P2_OWNER_TAG, P3_OWNER_TAG):
            self.assertIn(struct.pack("<I", owner_tag), code)
            self.assertEqual(
                build_create_dispatcher(cave, state, owner_tag),
                code,
            )
        # The permanent mux owns the stock tick.  Service callbacks return and
        # never embed or rewrite a root detour.
        self.assertTrue(code.endswith(b"\xc3"))

    def test_reserved_workspace_keeps_code_before_state(self) -> None:
        base = 0x30000000 + coop.NATIVE_AREA_TARGET_WORKSPACE_OFFSET
        code = build_create_dispatcher(
            base + CODE_OFFSET,
            base + STATE_OFFSET,
            P3_OWNER_TAG,
        )
        self.assertLessEqual(
            CODE_OFFSET + len(code),
            STATE_OFFSET,
        )
        self.assertLessEqual(
            STATE_OFFSET + area_circle.STATE_SIZE,
            coop.NATIVE_AREA_TARGET_WORKSPACE_SIZE,
        )

    def test_clear_reuses_node_and_restore_relinquishes_it(self) -> None:
        manager = 0x300000
        node = 0x400000

        class FakeProcess:
            def __init__(self) -> None:
                self.values = {
                    area_circle.TARGET_UI_MANAGER_GLOBAL: manager,
                    manager + AREA_CIRCLE_LIST_OFFSET: node,
                    node + 0x70: 0,
                    node + 0x50: 0x01,
                    node + 0x54: P2_OWNER_TAG,
                    node + 0x6C: 0x100008,
                }

            def u64(self, address: int) -> int:
                return self.values.get(address, 0)

            def u32(self, address: int) -> int:
                return self.values.get(address, 0)

            def write(self, address: int, data: bytes) -> None:
                self.values[address] = struct.unpack("<I", data)[0]

        process = FakeProcess()
        controller = area_circle.NativeAreaTargetCircleController.__new__(
            area_circle.NativeAreaTargetCircleController
        )
        controller.process = process
        controller.owner_tag = P2_OWNER_TAG
        controller.node = node
        controller.target = 0x100008
        controller.patched = False

        controller.clear()
        self.assertEqual(controller.node, node)
        self.assertEqual(process.u32(node + 0x54), P2_OWNER_TAG)
        self.assertEqual(process.u32(node + 0x6C), 0)
        self.assertEqual(process.u32(node + 0x50) & 0x01, 0)

        controller.restore()
        self.assertEqual(controller.node, 0)
        self.assertEqual(process.u32(node + 0x54), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
