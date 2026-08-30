"""The bridge grants only the two published co-op loot handles.

Every inactive or unknown-handle path still reaches the stock ownership query.
Cleanup leaves immutable process-lifetime code resident and relies on the
runtime's COOP_ACTIVE publication to make it a native pass-through.
"""

from __future__ import annotations

import struct
import unittest
from types import SimpleNamespace

import coop_runtime as coop
import headless_menu_shell as menu
import loot_ownership as loot

CODE = 0x2C940100
BASE = CODE - loot.CAVE_CODE_OFFSET
BLOCK = 0x2C930200


class FakeProcess:
    def __init__(self, site_bytes=loot.STOCK_CALL):
        self.memory = {loot.OWNERSHIP_CALL_SITE: bytes(site_bytes)}
        self.patches = []

    def read(self, address, size):
        result = bytearray(size)
        found = False
        for base, payload in self.memory.items():
            start = max(address, base)
            end = min(address + size, base + len(payload))
            if start >= end:
                continue
            found = True
            result[start - address : end - address] = payload[
                start - base : end - base
            ]
        if found:
            return bytes(result)
        return bytes(result)

    def write(self, address, payload):
        self.memory[address] = bytes(payload)

    def patch_code(self, address, payload):
        self.patches.append((address, bytes(payload)))
        self.memory[address] = bytes(payload)

    def ensure_damage_number_theme_allocation(self):
        return BASE - coop.LOOT_OWNERSHIP_WORKSPACE_OFFSET


class TranslationShapeTests(unittest.TestCase):
    def setUp(self):
        self.code = loot.build_owner_translation(CODE, BLOCK)

    def test_it_ends_by_handing_over_to_the_stock_query(self):
        self.assertEqual(self.code[-5:-4], b"\xe9")
        destination = CODE + len(self.code) + struct.unpack("<i", self.code[-4:])[0]
        self.assertEqual(destination, loot.OWNERSHIP_QUERY)

    def test_exact_coop_handles_return_owned_without_leader_translation(self):
        self.assertEqual(self.code.count(b"\x3b\xc8"), 2)  # cmp ecx,eax
        self.assertIn(b"\xb8\x01\x00\x00\x00\xc3", self.code)

    def test_it_reads_both_published_player_handles(self):
        self.assertIn(b"\x41\x8b\x43" + bytes((coop.P2_LOOT_HANDLE_OFFSET,)), self.code)
        self.assertIn(b"\x41\x8b\x42" + bytes((coop.P3_LOOT_HANDLE_OFFSET,)), self.code)

    def test_it_builds_no_stack_frame(self):
        """A tail call needs none, and the stock routine expects the caller's."""

        self.assertNotIn(b"\x53", self.code[:1])  # push rbx
        self.assertNotIn(b"\x48\x83\xec", self.code)  # sub rsp, imm

    def test_the_site_patch_reaches_the_translation(self):
        patch = loot.call_site_patch(CODE)
        self.assertEqual(len(patch), len(loot.STOCK_CALL))
        destination = (
            loot.OWNERSHIP_CALL_SITE + len(patch) + struct.unpack("<i", patch[1:])[0]
        )
        self.assertEqual(destination, CODE)


class BridgeTests(unittest.TestCase):
    def test_menu_retries_a_transient_foreign_detour_without_losing_the_bridge(self):
        class Bridge:
            def __init__(self):
                self.installed = False
                self.attempts = 0

            def maintain(self, enabled):
                self.assert_enabled(enabled)
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("loader is still publishing its trampoline")
                self.installed = True

            @staticmethod
            def assert_enabled(enabled):
                if enabled is not True:
                    raise AssertionError("bridge must be enabled")

        bridge = Bridge()
        runtime = SimpleNamespace(
            loot_ownership=bridge,
            loot_ownership_retry_at=0.0,
            loot_ownership_last_error=None,
        )
        menu.CoopMenuRuntime.maintain_loot_ownership(runtime, 1.0)
        self.assertEqual(bridge.attempts, 1)
        self.assertEqual(runtime.loot_ownership_retry_at, 1.5)

        menu.CoopMenuRuntime.maintain_loot_ownership(runtime, 1.25)
        self.assertEqual(bridge.attempts, 1)
        menu.CoopMenuRuntime.maintain_loot_ownership(runtime, 1.5)
        self.assertTrue(bridge.installed)
        self.assertEqual(bridge.attempts, 2)

    def test_it_refuses_a_site_someone_else_already_owns(self):
        process = FakeProcess(b"\xe8\x11\x22\x33\x44")
        bridge = loot.LootOwnershipBridge(process, BLOCK)
        with self.assertRaises(RuntimeError):
            bridge.install()
        self.assertEqual(process.patches, [])

    def test_restore_preserves_owned_process_lifetime_call(self):
        process = FakeProcess()
        bridge = loot.LootOwnershipBridge(process, BLOCK)
        bridge.code = CODE
        process.patch_code(loot.OWNERSHIP_CALL_SITE, loot.call_site_patch(CODE))
        bridge.installed = True
        bridge.owns_static = True
        process.patches.clear()
        self.assertFalse(bridge.restore())
        self.assertEqual(
            process.read(loot.OWNERSHIP_CALL_SITE, len(loot.STOCK_CALL)),
            loot.call_site_patch(CODE),
        )
        self.assertEqual(process.patches, [])

    def test_restore_on_a_stock_site_changes_nothing(self):
        process = FakeProcess()
        bridge = loot.LootOwnershipBridge(process, BLOCK)
        self.assertFalse(bridge.restore())
        self.assertEqual(process.patches, [])

    def test_restore_leaves_a_stranger_alone(self):
        process = FakeProcess(b"\xe8\x11\x22\x33\x44")
        bridge = loot.LootOwnershipBridge(process, BLOCK)
        bridge.code = CODE
        self.assertFalse(bridge.restore())
        self.assertEqual(process.patches, [])

    def test_exact_resident_bridge_is_adopted_and_never_unpatched(self):
        process = FakeProcess(loot.call_site_patch(CODE))
        process.memory[BASE] = loot.CAVE_MAGIC
        process.memory[CODE] = loot.build_owner_translation(CODE, BLOCK)
        bridge = loot.LootOwnershipBridge(process, BLOCK)

        self.assertFalse(bridge.install())
        self.assertTrue(bridge.installed)
        self.assertFalse(bridge.owns_static)
        process.patches.clear()

        self.assertFalse(bridge.restore())
        self.assertEqual(process.patches, [])
        self.assertEqual(
            process.read(loot.OWNERSHIP_CALL_SITE, len(loot.STOCK_CALL)),
            loot.call_site_patch(CODE),
        )

    def test_resident_cave_mismatch_aborts_without_hot_repair(self):
        process = FakeProcess(loot.call_site_patch(CODE))
        process.memory[BASE] = loot.CAVE_MAGIC
        body = loot.build_owner_translation(CODE, BLOCK)
        process.memory[CODE] = bytes((body[0] ^ 1,)) + body[1:]
        memory_before = dict(process.memory)

        with self.assertRaisesRegex(RuntimeError, "cave mismatch"):
            loot.LootOwnershipBridge(process, BLOCK).install()

        self.assertEqual(process.memory, memory_before)
        self.assertEqual(process.patches, [])

    def test_fresh_install_commits_full_page_before_root(self):
        process = FakeProcess()
        bridge = loot.LootOwnershipBridge(process, BLOCK)

        self.assertTrue(bridge.install())

        self.assertEqual(process.memory[BASE], bridge._page_image(BASE))
        self.assertEqual(
            process.read(loot.OWNERSHIP_CALL_SITE, len(loot.STOCK_CALL)),
            loot.call_site_patch(CODE),
        )
        self.assertEqual(process.patches[-1][0], loot.OWNERSHIP_CALL_SITE)

    def test_stock_root_with_old_page_aborts_without_overwrite(self):
        process = FakeProcess()
        process.memory[BASE] = b"old reachable cave"
        memory_before = dict(process.memory)

        with self.assertRaisesRegex(RuntimeError, "non-empty loot-ownership page"):
            loot.LootOwnershipBridge(process, BLOCK).install()

        self.assertEqual(process.memory, memory_before)
        self.assertEqual(process.patches, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
