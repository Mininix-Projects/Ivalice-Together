import ast
import os
import struct
import unittest
from pathlib import Path
from unittest import mock

import coop_runtime as coop




def find_game_executable() -> Path:
    configured = os.environ.get("FF12_GAME_ROOT")
    if configured:
        return Path(configured) / "x64" / "FFXII_TZA.exe"
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "x64" / "FFXII_TZA.exe"
        if candidate.is_file():
            return candidate
    return Path("__missing_FFXII_TZA.exe")


GAME_EXE = find_game_executable()
IMAGE_BASE = 0x120000


class RuntimeImage:
    CAVE_BASE = 0x30000000
    DUAL_BASE = 0x31000000
    DAMAGE_BASE = 0x32000000
    MANIFEST_BASE = 0x50000000

    def __init__(self, *, preallocated=()):
        if not GAME_EXE.is_file():
            raise unittest.SkipTest("executavel do jogo indisponivel")
        self.image = GAME_EXE.read_bytes()
        pe = struct.unpack_from("<I", self.image, 0x3C)[0]
        count = struct.unpack_from("<H", self.image, pe + 6)[0]
        optional_size = struct.unpack_from("<H", self.image, pe + 20)[0]
        table = pe + 24 + optional_size
        self.sections = []
        for index in range(count):
            entry = table + index * 40
            virtual_size, virtual_address, raw_size, pointer = struct.unpack_from(
                "<IIII", self.image, entry + 8
            )
            self.sections.append(
                (virtual_address, max(virtual_size, raw_size), raw_size, pointer)
            )

        self.memory = {}
        self.writes = []
        self.patches = []
        self.allocations = {}
        self.fail_manifest_allocation = False
        self.fail_patch_address = None
        self.fail_write_address = None
        for name in preallocated:
            if name == "main":
                self._reserve(self.CAVE_BASE, coop.CAVE_ALLOCATION_SIZE)
                self.put(self.CAVE_BASE, coop.CAVE_MAGIC)
            elif name == "dual":
                self._reserve(self.DUAL_BASE, coop.DUAL_INTERACTION_ALLOCATION_SIZE)
                self.put(self.DUAL_BASE, coop.DUAL_INTERACTION_MAGIC)
            elif name == "damage":
                self._reserve(self.DAMAGE_BASE, coop.DAMAGE_NUMBER_THEME_ALLOCATION_SIZE)
                self.put(self.DAMAGE_BASE, coop.DAMAGE_NUMBER_THEME_MAGIC)
            else:
                raise AssertionError(name)

    def _reserve(self, address, size):
        self.allocations[int(address)] = int(size)

    def put(self, address, payload):
        for offset, value in enumerate(bytes(payload)):
            self.memory[int(address) + offset] = value

    def _read_image(self, address, size):
        rva = int(address) - IMAGE_BASE
        for virtual_address, virtual_size, raw_size, pointer in self.sections:
            if (
                virtual_address <= rva
                and rva + size <= virtual_address + virtual_size
            ):
                relative = rva - virtual_address
                available = max(0, min(size, raw_size - relative))
                payload = bytearray(size)
                if available:
                    payload[:available] = self.image[
                        pointer + relative : pointer + relative + available
                    ]
                return bytes(payload)
        return None

    def read(self, address, size):
        address = int(address)
        payload = bytearray(size)
        image = self._read_image(address, size)
        if image is not None:
            payload[:] = image
        elif not any(
            base <= address and address + size <= base + length
            for base, length in self.allocations.items()
        ):
            raise OSError(f"unmapped test read at {address:#x}")
        for offset in range(size):
            value = self.memory.get(address + offset)
            if value is not None:
                payload[offset] = value
        return bytes(payload)

    def write(self, address, payload):
        payload = bytes(payload)
        if int(address) == self.fail_write_address:
            raise OSError(f"injected write failure at {int(address):#x}")
        self.writes.append((int(address), payload))
        self.put(address, payload)

    def patch_code(self, address, payload):
        payload = bytes(payload)
        if int(address) == self.fail_patch_address:
            raise OSError(f"injected patch failure at {int(address):#x}")
        self.patches.append((int(address), payload))
        self.put(address, payload)

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def _ensure(self, *, base, size, magic, setter, allow_allocate):
        if base in self.allocations:
            setter(base)
            return base
        if not allow_allocate:
            return 0
        self._reserve(base, size)
        setter(base)
        self.write(base, magic)
        return base

    def ensure_cave_allocation(self, *, allow_allocate=True):
        return self._ensure(
            base=self.CAVE_BASE,
            size=coop.CAVE_ALLOCATION_SIZE,
            magic=coop.CAVE_MAGIC,
            setter=coop.set_cave_base,
            allow_allocate=allow_allocate,
        )

    def ensure_dual_interaction_allocation(self, *, allow_allocate=True):
        return self._ensure(
            base=self.DUAL_BASE,
            size=coop.DUAL_INTERACTION_ALLOCATION_SIZE,
            magic=coop.DUAL_INTERACTION_MAGIC,
            setter=coop.set_dual_interaction_base,
            allow_allocate=allow_allocate,
        )

    def ensure_damage_number_theme_allocation(self, *, allow_allocate=True):
        return self._ensure(
            base=self.DAMAGE_BASE,
            size=coop.DAMAGE_NUMBER_THEME_ALLOCATION_SIZE,
            magic=coop.DAMAGE_NUMBER_THEME_MAGIC,
            setter=coop.set_damage_number_theme_base,
            allow_allocate=allow_allocate,
        )

    def allocate_runtime_manifest(self, size):
        if self.fail_manifest_allocation:
            raise OSError("injected manifest allocation failure")
        self._reserve(self.MANIFEST_BASE, size)
        return self.MANIFEST_BASE

    def clear_activity(self):
        self.writes.clear()
        self.patches.clear()


def marker_fields(process):
    payloads = tuple(
        process.read(address, coop.RUNTIME_INSTALL_MARKER_STRUCT.size)
        for address in coop.runtime_install_marker_addresses()
    )
    return payloads, coop.RUNTIME_INSTALL_MARKER_STRUCT.unpack(payloads[0])


def manifest_regions(process):
    _payloads, fields = marker_fields(process)
    manifest_address = fields[5]
    manifest_size = fields[6]
    manifest_count = fields[4]
    payload = process.read(manifest_address, manifest_size)
    return coop._parse_runtime_manifest(payload, expected_count=manifest_count)


class RuntimeHotReinstallTests(unittest.TestCase):
    def install(self, process, *, upstream=False, preserve_dynamic=False):
        return coop.install_runtime(
            process,
            pid=15000,
            active=True,
            upstream_camera_v2=upstream,
            preserve_dynamic_state_on_reuse=preserve_dynamic,
        )

    def test_marker_owns_only_each_allocation_header_gap(self):
        start = coop.RUNTIME_INSTALL_MARKER_OFFSET
        end = start + coop.RUNTIME_INSTALL_MARKER_STRUCT.size
        self.assertGreaterEqual(start, len(coop.CAVE_MAGIC))
        self.assertGreaterEqual(start, len(coop.DUAL_INTERACTION_MAGIC))
        self.assertGreaterEqual(start, len(coop.DAMAGE_NUMBER_THEME_MAGIC))
        self.assertLessEqual(end, 0x60)

    def test_atomic_role_transfer_layout_revision_is_published(self):
        self.assertEqual(
            coop.RUNTIME_NATIVE_LAYOUT_ID.hex(),
            "16acde4d60574c358eb568df833a0d7a",
        )

    def test_legacy_executable_restores_only_belong_to_first_install_graph(self):
        restore_names = {
            "restore_p2_interaction_indicator_source",
            "restore_follower_npc_exact_scan",
            "restore_p2_interaction_actor_filter",
            "restore_phys_facing_guard",
            "restore_follower_action_approach_gate",
            "restore_follower_action_chase_guard",
            "restore_follower_arbitrator_guard",
            "restore_follower_turn_guard",
            "restore_follower_list_guard",
            "restore_visual_facing_update",
            "restore_follower_gateway_trigger",
            "restore_follower_gateway_collision_filter",
            "restore_follower_locomotion_animation_guard",
            "restore_follower_action_filter",
            "restore_camera_midpoint",
            "restore_upstream_camera_focus_v2",
            "restore_controller_shared_focus",
            "restore_shared_camera_internal_collision_filter",
            "restore_camera_eye_smoothing",
            "restore_p2_hud_text_color",
            "restore_pad_background_input",
            "restore_berserk_manual_command_gate",
            "restore_native_cursor",
        }
        tree = ast.parse(Path(coop.__file__).read_text(encoding="utf-8"))
        callers = {name: set() for name in restore_names}

        class CallGraph(ast.NodeVisitor):
            def __init__(self):
                self.function = None

            def visit_FunctionDef(self, node):
                previous = self.function
                self.function = node.name
                self.generic_visit(node)
                self.function = previous

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id in callers:
                    callers[node.func.id].add(self.function)
                self.generic_visit(node)

        CallGraph().visit(tree)
        self.assertTrue(all(callers.values()))
        self.assertEqual(
            {caller for values in callers.values() for caller in values},
            {"_install_native_code"},
        )

    def test_read_only_inspection_exposes_adopt_and_reuse_modes(self):
        process = RuntimeImage(preallocated=("dual",))
        mode, bases, regions = coop.inspect_native_install(
            process,
            upstream_camera_v2=False,
        )
        self.assertEqual(mode, "adopt")
        self.assertEqual(bases, (0, process.DUAL_BASE, 0))
        self.assertIsNone(regions)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

        self.install(process)
        process.clear_activity()
        mode, bases, regions = coop.inspect_native_install(
            process,
            upstream_camera_v2=False,
        )
        self.assertEqual(mode, "reuse")
        self.assertEqual(
            bases,
            (process.CAVE_BASE, process.DUAL_BASE, process.DAMAGE_BASE),
        )
        self.assertGreaterEqual(len(regions), 70)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_first_install_commits_complete_triple_manifest(self):
        process = RuntimeImage()
        self.assertEqual(self.install(process), "installed")

        payloads, fields = marker_fields(process)
        self.assertEqual(len(set(payloads)), 1)
        self.assertEqual(fields[0], coop.RUNTIME_INSTALL_MARKER_MAGIC)
        self.assertEqual(fields[1], coop.RUNTIME_INSTALL_MARKER_VERSION)
        self.assertEqual(fields[2], coop.RUNTIME_INSTALL_COMMITTED)

        regions = manifest_regions(process)
        roots = {
            address
            for kind, address, _size, _digest in regions
            if kind == coop.RUNTIME_INSTALL_REGION_ROOT
        }
        executable = {
            address
            for kind, address, _size, _digest in regions
            if kind == coop.RUNTIME_INSTALL_REGION_EXECUTABLE
        }
        self.assertGreaterEqual(len(roots), 35)
        self.assertGreaterEqual(len(executable), 30)
        self.assertTrue({address for address, _payload in process.patches} <= roots)
        written_executable = {
            address
            for address, _payload in process.writes
            if address in coop._runtime_executable_write_addresses()
        }
        self.assertEqual(written_executable, executable)
        self.assertIn(coop.FOLLOWER_LIST_GUARD_CAVE, executable)
        self.assertIn(coop.FOLLOWER_ACTION_TRANSITION_HELPER, executable)
        self.assertNotIn(coop.P2_FOLLOWER_ACTION_MAILBOX, executable)
        self.assertNotIn(coop.P3_FOLLOWER_ACTION_MAILBOX, executable)

    def test_exact_restart_clears_action_mailboxes_without_rewriting_code(self):
        process = RuntimeImage()
        self.install(process)
        regions = manifest_regions(process)
        executable_ranges = tuple(
            (address, address + size)
            for kind, address, size, _digest in regions
            if kind == coop.RUNTIME_INSTALL_REGION_EXECUTABLE
        )
        process.put(
            coop.P2_FOLLOWER_ACTION_MAILBOX,
            b"\xa5" * coop.FOLLOWER_ACTION_MAILBOX_REGION_SIZE,
        )
        process.clear_activity()

        self.assertEqual(self.install(process), "reused")

        self.assertEqual(process.patches, [])
        self.assertEqual(
            process.read(
                coop.P2_FOLLOWER_ACTION_MAILBOX,
                coop.FOLLOWER_ACTION_MAILBOX_REGION_SIZE,
            ),
            b"\x00" * coop.FOLLOWER_ACTION_MAILBOX_REGION_SIZE,
        )
        for address, payload in process.writes:
            end = address + len(payload)
            self.assertFalse(
                any(address < limit and base < end for base, limit in executable_ranges),
                hex(address),
            )

    def test_exact_restart_resets_state_but_never_writes_executable_memory(self):
        process = RuntimeImage()
        self.install(process)
        regions = manifest_regions(process)
        executable_ranges = tuple(
            (address, address + size)
            for kind, address, size, _digest in regions
            if kind == coop.RUNTIME_INSTALL_REGION_EXECUTABLE
        )
        markers = set(coop.runtime_install_marker_addresses())
        process.clear_activity()

        self.assertEqual(self.install(process), "reused")
        self.assertEqual(process.patches, [])
        self.assertFalse(markers.intersection(address for address, _ in process.writes))
        self.assertNotIn(process.MANIFEST_BASE, {address for address, _ in process.writes})
        for address, payload in process.writes:
            end = address + len(payload)
            self.assertFalse(
                any(address < limit and base < end for base, limit in executable_ranges),
                hex(address),
            )

    def test_replacement_reuse_preserves_committed_dynamic_state_byte_for_byte(self):
        process = RuntimeImage()
        self.install(process)
        sentinels = {
            coop.COMMAND_BLOCK + coop.COOP_ACTIVE_OFFSET: struct.pack("<I", 1),
            coop.COMMAND_BLOCK + coop.MOVEMENT_ACTIVE_OFFSET: struct.pack("<I", 1),
            coop.COMMAND_BLOCK + coop.CAMERA_ACTIVE_OFFSET: struct.pack("<I", 1),
            coop.COMMAND_BLOCK
            + coop.INTERACTION_ROUTING_ACTIVE_OFFSET: struct.pack("<I", 1),
            coop.COMMAND_BLOCK
            + coop.KEYBOARD_FILTER_ACTIVE_OFFSET: struct.pack("<I", 1),
            coop.COMMAND_BLOCK
            + coop.P1_HUD_PARTY_SLOT_OFFSET: struct.pack("<2I", 1, 0),
            coop.third_player_block()
            + coop.P3_HUD_PARTY_SLOT_OFFSET: struct.pack("<I", 2),
        }
        for address, payload in sentinels.items():
            process.put(address, payload)
        before = {
            address: process.read(address, len(payload))
            for address, payload in sentinels.items()
        }
        process.clear_activity()

        self.assertEqual(
            self.install(process, preserve_dynamic=True),
            "reused",
        )

        self.assertEqual(
            {
                address: process.read(address, len(payload))
                for address, payload in sentinels.items()
            },
            before,
        )
        self.assertEqual(process.patches, [])
        self.assertEqual(
            {address for address, _payload in process.writes},
            {coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE},
        )

    def test_detached_root_is_refused_without_reattach_or_any_write(self):
        process = RuntimeImage()
        self.install(process)
        root = coop.FACING_HOOK_SITE
        root_size = next(
            size
            for kind, address, size, _digest in manifest_regions(process)
            if kind == coop.RUNTIME_INSTALL_REGION_ROOT and address == root
        )
        original = process._read_image(root, root_size)
        process.put(root, original)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "hook mismatch"):
            self.install(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_poisoned_cave_is_refused_without_any_write(self):
        process = RuntimeImage()
        self.install(process)
        region = next(
            item
            for item in manifest_regions(process)
            if item[0] == coop.RUNTIME_INSTALL_REGION_EXECUTABLE
        )
        _kind, address, size, _digest = region
        poisoned = bytearray(process.read(address, size))
        poisoned[size // 2] ^= 0x80
        process.put(address, poisoned)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "cave mismatch"):
            self.install(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_poisoned_action_transition_helper_is_refused_before_any_write(self):
        process = RuntimeImage()
        self.install(process)
        _kind, address, size, _digest = next(
            item
            for item in manifest_regions(process)
            if item[0] == coop.RUNTIME_INSTALL_REGION_EXECUTABLE
            and item[1] == coop.FOLLOWER_ACTION_TRANSITION_HELPER
        )
        poisoned = bytearray(process.read(address, size))
        poisoned[size // 2] ^= 0x80
        process.put(address, poisoned)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "cave mismatch"):
            self.install(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_mismatched_triple_marker_aborts_before_any_write(self):
        process = RuntimeImage()
        self.install(process)
        marker_address = coop.runtime_install_marker_addresses()[1]
        marker = bytearray(
            process.read(marker_address, coop.RUNTIME_INSTALL_MARKER_STRUCT.size)
        )
        marker[-1] ^= 0x40
        process.put(marker_address, marker)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "triple commit"):
            self.install(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_same_corrupt_build_id_aborts_before_any_write(self):
        process = RuntimeImage()
        self.install(process)
        payloads, _fields = marker_fields(process)
        marker = bytearray(payloads[0])
        marker[-1] ^= 0x40
        for address in coop.runtime_install_marker_addresses():
            process.put(address, marker)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "build differs"):
            self.install(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_uncommitted_live_runtime_is_never_rewritten(self):
        process = RuntimeImage()
        self.install(process)
        payloads, fields = marker_fields(process)
        prepared = coop._runtime_install_marker(
            state=coop.RUNTIME_INSTALL_PREPARED,
            feature_bits=fields[3],
            manifest_address=fields[5],
            manifest_size=fields[6],
            manifest_count=fields[4],
            manifest_digest=fields[7],
        )
        for address in coop.runtime_install_marker_addresses():
            process.put(address, prepared)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "uncommitted"):
            self.install(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_unknown_stock_root_mismatch_fails_preflight_before_allocation_write(self):
        process = RuntimeImage()
        process.put(coop.MOVEMENT_HOOK_SITE, b"\xCC" * 5)

        with self.assertRaisesRegex(RuntimeError, "movement hook"):
            self.install(process)

        self.assertEqual(process.allocations, {})
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_legitimate_auxiliary_preallocation_can_be_bound_by_first_install(self):
        process = RuntimeImage(preallocated=("dual",))
        self.assertEqual(self.install(process), "installed")
        self.assertEqual(
            set(coop.runtime_install_marker_addresses()),
            {
                process.CAVE_BASE + coop.RUNTIME_INSTALL_MARKER_OFFSET,
                process.DUAL_BASE + coop.RUNTIME_INSTALL_MARKER_OFFSET,
                process.DAMAGE_BASE + coop.RUNTIME_INSTALL_MARKER_OFFSET,
            },
        )

    def test_feature_choice_is_bound_and_disabled_root_is_still_validated(self):
        process = RuntimeImage()
        with mock.patch.object(coop, "FOLLOWER_GATEWAY_TRIGGER_ENABLED", False):
            self.install(process)
            regions = manifest_regions(process)
            roots = {address for kind, address, _size, _digest in regions if kind == 1}
            executable = {
                address for kind, address, _size, _digest in regions if kind == 2
            }
            self.assertIn(coop.GATEWAY_TRIGGER_SITE, roots)
            self.assertNotIn(coop.FOLLOWER_GATEWAY_TRIGGER_CAVE, executable)

        process.clear_activity()
        with self.assertRaisesRegex(RuntimeError, "feature layout differs"):
            self.install(process)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_every_feature_dependent_install_path_commits_and_reuses(self):
        preflight_only = {
            "FOLLOWER_ACTION_NATIVE_APPROACH_RELEASE",
            "MOVEMENT_HOLDS_LEADER",
            "FOLLOWER_LOCOMOTION_ANIMATION_GUARD_ENABLED",
        }
        for name in coop.RUNTIME_NATIVE_FEATURE_NAMES:
            with self.subTest(feature=name):
                changed = not bool(getattr(coop, name))
                with mock.patch.object(coop, name, changed):
                    process = RuntimeImage()
                    if name in preflight_only:
                        with self.assertRaises(RuntimeError):
                            self.install(process)
                        self.assertEqual(process.allocations, {})
                        self.assertEqual(process.writes, [])
                        self.assertEqual(process.patches, [])
                        continue
                    self.assertEqual(self.install(process), "installed")
                    regions = manifest_regions(process)
                    self.assertGreaterEqual(
                        sum(kind == coop.RUNTIME_INSTALL_REGION_ROOT for kind, *_ in regions),
                        40,
                    )
                    self.assertGreaterEqual(
                        sum(
                            kind == coop.RUNTIME_INSTALL_REGION_EXECUTABLE
                            for kind, *_ in regions
                        ),
                        30,
                    )
                    process.clear_activity()
                    self.assertEqual(self.install(process), "reused")
                    self.assertEqual(process.patches, [])

        process = RuntimeImage()
        # This test validates the atomic install/reuse transaction, not the
        # generated assembly template.  Keep it runnable from an asset-free
        # source checkout where native build products are intentionally absent.
        with mock.patch.object(
            coop,
            "_build_shared_camera_collision_filter",
            return_value=b"\x90",
        ):
            self.assertEqual(self.install(process, upstream=True), "installed")
            process.clear_activity()
            self.assertEqual(self.install(process, upstream=True), "reused")
            self.assertEqual(process.patches, [])

    def test_manifest_allocation_failure_leaves_uncommitted_runtime_fail_closed(self):
        process = RuntimeImage()
        process.fail_manifest_allocation = True
        with self.assertRaisesRegex(OSError, "manifest allocation"):
            self.install(process)

        process.fail_manifest_allocation = False
        process.clear_activity()
        with self.assertRaisesRegex(RuntimeError, "uncommitted native runtime hook"):
            self.install(process)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_zero_marker_legacy_downstream_detour_aborts_before_any_mutation(self):
        process = RuntimeImage(preallocated=("main",))
        legacy_camera_cave = process.CAVE_BASE + 0x600
        legacy_patch = (
            b"\xe9"
            + coop.rel32(coop.CAMERA_HOOK_SITE, 5, legacy_camera_cave)
            + b"\x90\x90"
        )
        process.put(coop.CAMERA_HOOK_SITE, legacy_patch)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "legacy detour"):
            self.install(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])
        self.assertEqual(
            process.read(coop.CAMERA_HOOK_SITE, len(legacy_patch)),
            legacy_patch,
        )

    def test_legacy_focus_nops_are_not_hot_repaired_during_preflight(self):
        process = RuntimeImage()
        legacy_patch = b"\x90" * 6
        process.put(coop.FOCUS_CLASSIFY_SITE, legacy_patch)

        with self.assertRaisesRegex(RuntimeError, "legacy broad focus classifier"):
            self.install(process)

        self.assertEqual(process.allocations, {})
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])
        self.assertEqual(process.read(coop.FOCUS_CLASSIFY_SITE, 6), legacy_patch)

    def test_exact_steering_topology_is_adopted_into_the_global_manifest(self):
        process = RuntimeImage(preallocated=("main",))
        process.ensure_cave_allocation()
        self.assertTrue(coop.install_steering_heading_override(process))
        process.clear_activity()

        self.assertEqual(self.install(process), "installed")

        self.assertNotIn(
            coop.STEERING_HEADING_CAVE,
            {address for address, _payload in process.writes},
        )
        self.assertNotIn(
            coop.STEERING_HEADING_STORE_SITE,
            {address for address, _payload in process.patches},
        )
        regions = manifest_regions(process)
        region_keys = {(kind, address) for kind, address, _size, _digest in regions}
        self.assertIn(
            (coop.RUNTIME_INSTALL_REGION_ROOT, coop.STEERING_HEADING_STORE_SITE),
            region_keys,
        )
        self.assertIn(
            (coop.RUNTIME_INSTALL_REGION_EXECUTABLE, coop.STEERING_HEADING_CAVE),
            region_keys,
        )
        self.assertIn(
            (coop.RUNTIME_INSTALL_REGION_EXECUTABLE, coop.STEERING_FALLBACK_HELPER),
            region_keys,
        )

    def test_mid_install_patch_failure_cannot_trigger_hot_repair(self):
        process = RuntimeImage()
        process.fail_patch_address = coop.FACING_HOOK_SITE
        with self.assertRaisesRegex(OSError, "injected patch failure"):
            self.install(process)

        process.fail_patch_address = None
        process.clear_activity()
        with self.assertRaisesRegex(RuntimeError, "uncommitted native runtime hook"):
            self.install(process)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_interrupted_triple_commit_is_read_only_on_next_start(self):
        process = RuntimeImage()
        coop.set_cave_base(process.CAVE_BASE)
        coop.set_dual_interaction_base(process.DUAL_BASE)
        coop.set_damage_number_theme_base(process.DAMAGE_BASE)
        process.fail_write_address = coop.runtime_install_marker_addresses()[1]
        with self.assertRaisesRegex(OSError, "injected write failure"):
            self.install(process)

        process.fail_write_address = None
        process.clear_activity()
        with self.assertRaisesRegex(RuntimeError, "triple commit"):
            self.install(process)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])


if __name__ == "__main__":
    unittest.main()
