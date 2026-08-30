import ctypes as ct
import itertools
import unittest
from unittest import mock

import coop_runtime as movement
import damage_meter
import headless_menu_core
import headless_menu_shell as headless
import loot_ownership
import native_area_target_circle_controller
import native_target_circle_router
import native_treasure_log
import native_ui_tick_mux
import native_wait_mode_bridge
import penelo_story_guard as party_guard
import story_removal_journal
import story_party_patch as story


def overlaps(left, right):
    return left[0] < right[1] and right[0] < left[1]


class RuntimeCaveLayoutTests(unittest.TestCase):
    def assert_pairwise_disjoint(self, regions):
        for (left_name, left), (right_name, right) in itertools.combinations(
            regions,
            2,
        ):
            self.assertFalse(
                overlaps(left, right),
                f"{left_name} overlaps {right_name}: {left!r} vs {right!r}",
            )

    def test_allocator_has_non_overlapping_rel32_fallback_ranges(self):
        candidates = list(movement.cave_allocation_candidates())
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertIn(movement.CAVE_ALLOCATION_BASE, candidates)
        self.assertIn(movement.REL32_ALLOCATION_LOW, candidates)
        self.assertIn(0x03000000, candidates)
        self.assertIn(0x70000000, candidates)
        self.assertIn(0x7F000000, candidates)
        self.assertLess(max(candidates), 0x80000000)

    def test_virtual_alloc2_contract_matches_the_windows_x64_abi(self):
        self.assertEqual(ct.sizeof(movement.MEM_ADDRESS_REQUIREMENTS), 24)
        self.assertEqual(ct.sizeof(movement.MEM_EXTENDED_PARAMETER), 16)
        self.assertEqual(ct.sizeof(movement.MEMORY_BASIC_INFORMATION), 48)
        self.assertEqual(movement.REL32_ALLOCATION_LOW, 0x10000)
        self.assertEqual(movement.REL32_ALLOCATION_HIGH, 0x7FFFFFFF)
        self.assertEqual(movement.ALLOCATION_GRANULARITY, 0x10000)

    def test_constrained_allocator_asks_windows_for_the_rel32_window(self):
        expected = 0x23450000
        with mock.patch.object(
            movement,
            "_virtual_alloc2",
            return_value=expected,
        ) as allocator:
            actual = movement.allocate_rel32_executable(0x1234, 0x10000)

        self.assertEqual(actual, expected)
        arguments = allocator.call_args.args
        self.assertEqual(arguments[0], 0x1234)
        self.assertIsNone(arguments[1])
        self.assertEqual(arguments[2], 0x10000)
        self.assertEqual(
            arguments[3],
            movement.MEM_COMMIT | movement.MEM_RESERVE,
        )
        self.assertEqual(arguments[4], movement.PAGE_EXECUTE_READWRITE)
        self.assertEqual(arguments[6], 1)

    def test_allocator_uses_the_remote_memory_map_after_virtual_alloc2_fails(self):
        expected = movement.CAVE_ALLOCATION_BASE

        def query(_handle, _address, buffer, _size):
            information = ct.cast(
                buffer,
                ct.POINTER(movement.MEMORY_BASIC_INFORMATION),
            ).contents
            information.BaseAddress = expected
            information.RegionSize = 0x20000
            information.State = movement.MEM_FREE
            return ct.sizeof(movement.MEMORY_BASIC_INFORMATION)

        with (
            mock.patch.object(movement, "_virtual_alloc2", None),
            mock.patch.object(
                movement.kernel32,
                "VirtualQueryEx",
                side_effect=query,
            ),
            mock.patch.object(
                movement,
                "_reserve_executable_at",
                return_value=expected,
            ) as reserve,
        ):
            actual = movement.allocate_rel32_executable(0x1234, 0x10000)

        self.assertEqual(actual, expected)
        reserve.assert_called_once_with(0x1234, expected, 0x10000)

    def test_magic_scanner_adopts_a_committed_native_reservation(self):
        expected = 0x23450000
        process = mock.Mock(handle=0x1234)
        process.read.side_effect = lambda address, size: (
            movement.CAVE_MAGIC[:size] if address == expected else b"\x00" * size
        )
        with mock.patch.object(
            movement,
            "committed_rel32_region_bases",
            return_value=iter((0x20000000, expected)),
        ):
            actual = movement.find_rel32_magic_allocation(
                process,
                movement.CAVE_MAGIC,
            )

        self.assertEqual(actual, expected)

    def test_primary_cave_adopts_native_magic_before_external_allocation(self):
        expected = 0x23450000
        process = movement.RemoteProcess.__new__(movement.RemoteProcess)
        process.handle = 0x1234
        process.read = mock.Mock(return_value=b"\x90" * 8)
        with (
            mock.patch.dict(
                movement.os.environ,
                {movement.runtime_paths.CAVE_BASE_ENV: ""},
                clear=False,
            ),
            mock.patch.object(
                movement,
                "find_rel32_magic_allocation",
                return_value=expected,
            ),
            mock.patch.object(
                movement,
                "allocate_rel32_executable",
                side_effect=AssertionError("native cave must be adopted"),
            ),
        ):
            actual = process.ensure_cave_allocation()

        self.assertEqual(actual, expected)

    def test_exact_reservation_can_fall_back_from_rwx_to_rw_then_protect(self):
        expected = 0x23450000
        with (
            mock.patch.object(
                movement.kernel32,
                "VirtualAllocEx",
                side_effect=(0, expected),
            ) as reserve,
            mock.patch.object(
                movement,
                "_make_remote_allocation_executable",
                return_value=True,
            ) as protect,
            mock.patch.object(movement, "_release_remote_allocation") as release,
        ):
            actual = movement._reserve_executable_at(
                0x1234,
                expected,
                0x10000,
            )

        self.assertEqual(actual, expected)
        self.assertEqual(reserve.call_count, 2)
        self.assertEqual(
            [call.args[4] for call in reserve.call_args_list],
            [movement.PAGE_EXECUTE_READWRITE, movement.PAGE_READWRITE],
        )
        protect.assert_called_once_with(0x1234, expected, 0x10000)
        release.assert_not_called()

    def test_allocator_retries_after_a_transiently_full_memory_map(self):
        expected = 0x34560000
        with (
            mock.patch.object(
                movement,
                "_allocate_rel32_virtual_alloc2",
                return_value=0,
            ),
            mock.patch.object(
                movement,
                "rel32_free_region_candidates",
                side_effect=(iter(()), iter((expected,))),
            ),
            mock.patch.object(
                movement,
                "_reserve_executable_at",
                return_value=expected,
            ),
            mock.patch.object(movement.time, "sleep") as sleep,
        ):
            actual = movement.allocate_rel32_executable(0x1234, 0x10000)

        self.assertEqual(actual, expected)
        sleep.assert_called_once_with(0.05)

    def test_child_adopts_the_bootstrap_cave_instead_of_allocating_again(self):
        expected = 0x23450000
        process = movement.RemoteProcess.__new__(movement.RemoteProcess)
        process.handle = 0x1234

        def read(address, size):
            if address in (movement.MOVEMENT_HOOK_SITE, movement.KEYBOARD_HOOK_SITE):
                return b"\x90" * size
            if address == expected:
                return movement.CAVE_MAGIC[:size]
            raise OSError("unmapped")

        process.read = read
        with (
            mock.patch.dict(
                movement.os.environ,
                {movement.runtime_paths.CAVE_BASE_ENV: hex(expected)},
                clear=False,
            ),
            mock.patch.object(
                movement,
                "allocate_rel32_executable",
                side_effect=AssertionError("child must not allocate"),
            ),
        ):
            actual = process.ensure_cave_allocation()

        self.assertEqual(actual, expected)

    def test_damage_theme_adopts_the_in_process_reservation(self):
        expected = 0x34560000
        process = movement.RemoteProcess.__new__(movement.RemoteProcess)
        process.handle = 0x1234

        def read(address, size):
            if address in (
                movement.DAMAGE_NUMBER_SOURCE_CALL_SITE,
                movement.DAMAGE_NUMBER_INIT_CALL_SITE,
            ):
                return b"\x90" * size
            if address == expected:
                return movement.DAMAGE_NUMBER_THEME_MAGIC[:size]
            raise OSError("unmapped")

        process.read = read
        with (
            mock.patch.dict(
                movement.os.environ,
                {movement.runtime_paths.DAMAGE_CAVE_BASE_ENV: hex(expected)},
                clear=False,
            ),
            mock.patch.object(
                movement,
                "allocate_rel32_executable",
                side_effect=AssertionError("published cave must be adopted"),
            ),
        ):
            actual = process.ensure_damage_number_theme_allocation()

        self.assertEqual(actual, expected)

    def test_damage_theme_is_recovered_through_its_meter_proxy(self):
        expected = 0x34560000
        proxy_entry = (
            expected
            + movement.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
            + movement.DAMAGE_METER_PROXY_CODE_OFFSET
        )
        source_call = b"\xe8" + movement.rel32(
            movement.DAMAGE_NUMBER_SOURCE_CALL_SITE,
            5,
            proxy_entry,
        )
        process = movement.RemoteProcess.__new__(movement.RemoteProcess)
        process.handle = 0x1234

        def read(address, size):
            if address == movement.DAMAGE_NUMBER_SOURCE_CALL_SITE:
                return source_call[:size]
            if address == expected:
                return movement.DAMAGE_NUMBER_THEME_MAGIC[:size]
            raise OSError("unmapped")

        process.read = read
        with mock.patch.object(
            movement,
            "allocate_rel32_executable",
            side_effect=AssertionError("meter proxy must recover the theme cave"),
        ):
            actual = process.ensure_damage_number_theme_allocation()

        self.assertEqual(actual, expected)

    def test_party_guard_workspace_is_inside_unused_damage_cave_space(self):
        occupied_damage_regions = (
            (
                movement.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET,
                movement.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET + 0x100,
            ),
            (
                movement.DAMAGE_NUMBER_INIT_WRAPPER_OFFSET,
                movement.DAMAGE_NUMBER_INIT_WRAPPER_OFFSET + 0x200,
            ),
            (
                movement.DAMAGE_NUMBER_STATE_OFFSET,
                movement.DAMAGE_NUMBER_BRIGHTNESS_OFFSET + 4,
            ),
            (
                movement.DAMAGE_NUMBER_INIT_WRAPPER_ALT_OFFSET,
                movement.DAMAGE_NUMBER_INIT_WRAPPER_ALT_OFFSET + 0x200,
            ),
        )
        party_guard = (
            movement.PARTY_GUARD_WORKSPACE_OFFSET,
            movement.PARTY_GUARD_WORKSPACE_OFFSET + movement.PARTY_GUARD_WORKSPACE_SIZE,
        )
        damage_meter_proxy = (
            movement.DAMAGE_METER_PROXY_WORKSPACE_OFFSET,
            movement.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
            + movement.DAMAGE_METER_PROXY_WORKSPACE_SIZE,
        )

        self.assertLessEqual(
            party_guard[1], movement.DAMAGE_NUMBER_THEME_ALLOCATION_SIZE
        )
        self.assertLessEqual(
            damage_meter_proxy[1],
            movement.DAMAGE_NUMBER_THEME_ALLOCATION_SIZE,
        )
        for occupied in occupied_damage_regions:
            self.assertFalse(overlaps(party_guard, occupied))
            self.assertFalse(overlaps(damage_meter_proxy, occupied))
        self.assertFalse(overlaps(damage_meter_proxy, party_guard))

    def test_party_guard_internal_code_data_regions_never_overlap(self):
        regions = {
            **{
                f"remote_call_{index}": (
                    offset,
                    offset + party_guard.REMOTE_CALL_REGION_SIZE,
                )
                for index, offset in enumerate(
                    party_guard.REMOTE_CALL_REGION_OFFSETS
                )
            },
            "remove_state": (
                party_guard.REMOVE_FILTER_STATE_OFFSET,
                party_guard.REMOVE_FILTER_STATE_OFFSET
                + party_guard.REMOVE_FILTER_STATE_CAPACITY,
            ),
            "remove_cave": (
                party_guard.REMOVE_FILTER_CAVE_OFFSET,
                party_guard.REMOVE_FILTER_CAVE_OFFSET
                + party_guard.REMOVE_FILTER_CAVE_CAPACITY,
            ),
            "journal_state": (
                story_removal_journal.DEFAULT_STATE_OFFSET,
                story_removal_journal.DEFAULT_STATE_OFFSET
                + story_removal_journal.STATE_REGION_SIZE,
            ),
            "journal_cave": (
                story_removal_journal.DEFAULT_CAVE_OFFSET,
                story_removal_journal.DEFAULT_CAVE_OFFSET
                + story_removal_journal.CAVE_REGION_SIZE,
            ),
        }
        for name, span in regions.items():
            self.assertGreaterEqual(span[0], 0, name)
            self.assertLessEqual(span[1], movement.PARTY_GUARD_WORKSPACE_SIZE, name)
        for index, (left_name, left) in enumerate(regions.items()):
            for right_name, right in tuple(regions.items())[index + 1 :]:
                self.assertFalse(
                    overlaps(left, right),
                    f"{left_name} overlaps {right_name}",
                )

    def test_every_portable_workspace_partitions_the_damage_allocation(self):
        regions = (
            (
                "party_guard",
                movement.PARTY_GUARD_WORKSPACE_OFFSET,
                movement.PARTY_GUARD_WORKSPACE_SIZE,
            ),
            (
                "target_router",
                movement.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET,
                movement.NATIVE_TARGET_ROUTER_WORKSPACE_SIZE,
            ),
            (
                "area_target",
                movement.NATIVE_AREA_TARGET_WORKSPACE_OFFSET,
                movement.NATIVE_AREA_TARGET_WORKSPACE_SIZE,
            ),
            (
                "wait_predicate",
                movement.NATIVE_WAIT_PREDICATE_WORKSPACE_OFFSET,
                movement.NATIVE_WAIT_PREDICATE_WORKSPACE_SIZE,
            ),
            (
                "loot_ownership",
                movement.LOOT_OWNERSHIP_WORKSPACE_OFFSET,
                movement.LOOT_OWNERSHIP_WORKSPACE_SIZE,
            ),
            (
                "story_extended",
                movement.STORY_EXTENDED_WORKSPACE_OFFSET,
                movement.STORY_EXTENDED_WORKSPACE_SIZE,
            ),
        )
        spans = {name: (offset, offset + size) for name, offset, size in regions}
        for name, span in spans.items():
            self.assertGreaterEqual(span[0], 0)
            self.assertLessEqual(
                span[1],
                movement.DAMAGE_NUMBER_THEME_ALLOCATION_SIZE,
                name,
            )
        for index, (left_name, left) in enumerate(spans.items()):
            for right_name, right in tuple(spans.items())[index + 1 :]:
                self.assertFalse(
                    overlaps(left, right),
                    f"{left_name} overlaps {right_name}",
                )

        self.assertEqual(
            native_target_circle_router.REGION_SIZE,
            movement.NATIVE_TARGET_ROUTER_WORKSPACE_SIZE,
        )
        self.assertEqual(
            native_area_target_circle_controller.REGION_SIZE,
            movement.NATIVE_AREA_TARGET_WORKSPACE_SIZE,
        )
        self.assertEqual(
            native_wait_mode_bridge.WAIT_PREDICATE_CAVE_SIZE,
            movement.NATIVE_WAIT_PREDICATE_WORKSPACE_SIZE,
        )
        self.assertEqual(
            loot_ownership.CAVE_SIZE,
            movement.LOOT_OWNERSHIP_WORKSPACE_SIZE,
        )
        self.assertEqual(
            story.EXTENDED_ALLOCATION_SIZE,
            movement.STORY_EXTENDED_WORKSPACE_SIZE,
        )
        self.assertLessEqual(
            damage_meter.PROXY_CODE_OFFSET
            + len(damage_meter.build_absolute_jump(0x123456789ABCDEF0)),
            movement.DAMAGE_METER_PROXY_WORKSPACE_SIZE,
        )

    def test_party_guard_branches_reach_its_reserved_workspace(self):
        cave_bases = (
            movement.REL32_ALLOCATION_LOW,
            movement.REL32_ALLOCATION_HIGH
            - movement.DAMAGE_NUMBER_THEME_ALLOCATION_SIZE
            + 1,
        )
        for cave_base in cave_bases:
            workspace = cave_base + movement.PARTY_GUARD_WORKSPACE_OFFSET
            movement.rel32(
                party_guard.REMOVE_PARTY_MEMBER,
                5,
                workspace + party_guard.REMOVE_FILTER_CAVE_OFFSET,
            )
            movement.rel32(
                story_removal_journal.SCRIPT_REMOVE_CALL,
                5,
                workspace + story_removal_journal.DEFAULT_CAVE_OFFSET,
            )

    def test_allocator_error_is_not_overwritten_by_the_final_magic_scan(self):
        process = movement.RemoteProcess.__new__(movement.RemoteProcess)
        process.handle = 0x1234
        process.read = mock.Mock(side_effect=OSError(299, "partial read"))
        with (
            mock.patch.dict(
                movement.os.environ,
                {
                    movement.runtime_paths.CAVE_BASE_ENV: "",
                },
                clear=False,
            ),
            mock.patch.object(
                movement,
                "allocate_rel32_executable",
                return_value=0,
            ),
            mock.patch.object(movement.ct, "get_last_error", return_value=5),
            mock.patch.object(
                movement,
                "cave_allocation_candidates",
                return_value=iter((0x10000000,)),
            ),
        ):
            with self.assertRaises(OSError) as caught:
                process.ensure_cave_allocation()

        self.assertEqual(caught.exception.errno, 5)

    def test_movement_extras_do_not_overlap_headless_dual_regions(self):
        movement_regions = {
            "follower_locomotion": (
                movement.FOLLOWER_LOCOMOTION_GUARD_CAVE_OFFSET,
                movement.FOLLOWER_LOCOMOTION_GUARD_CAVE_OFFSET + 0x100,
            ),
            "follower_gateway_collision": (
                movement.FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE_OFFSET,
                movement.FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE_OFFSET + 0x100,
            ),
            "camera_focus_v2": (
                movement.CAMERA_FOCUS_V2_CAVE_OFFSET,
                movement.CAMERA_FOCUS_V2_CAVE_OFFSET + 0x100,
            ),
            "follower_gateway_trigger": (
                movement.FOLLOWER_GATEWAY_TRIGGER_CAVE_OFFSET,
                movement.FOLLOWER_GATEWAY_TRIGGER_CAVE_OFFSET + 0x200,
            ),
        }
        headless_regions = {
            "transactional_interaction": (0xC000, 0xD000),
            "interaction_consumer": (
                headless.P2_INTERACTION_CONSUMER_CAVE_OFFSET,
                headless.P2_INTERACTION_CONSUMER_CAVE_OFFSET + 0x200,
            ),
            "interaction_mailbox": (
                headless.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET,
                headless.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET + 0x80,
            ),
            "axis": (headless.NATIVE_AXIS_CAVE_OFFSET, 0xE500),
            "p1_list": (headless.P1_LIST_INPUT_CAVE_OFFSET, 0xE800),
            "action_targetline_sound_dialog": (0xE800, 0xF000),
            "combat_log_target_gate": (
                headless.NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET,
                headless.NATIVE_COMBAT_LOG_GATE_REGION_END,
            ),
        }
        for movement_name, movement_region in movement_regions.items():
            for headless_name, headless_region in headless_regions.items():
                self.assertFalse(
                    overlaps(movement_region, headless_region),
                    f"{movement_name} overlaps {headless_name}",
                )

    def test_primary_e000_f000_partition_is_complete_and_pairwise_disjoint(self):
        regions = (
            (
                "UI tick callback",
                (
                    headless_menu_core.DISPATCH_CAVE_OFFSET,
                    headless_menu_core.DISPATCH_CAVE_END_OFFSET,
                ),
            ),
            (
                "axis callback",
                (
                    headless.NATIVE_AXIS_CAVE_OFFSET,
                    headless.NATIVE_INPUT_COMMAND_HELPER_CAVE_OFFSET,
                ),
            ),
            (
                "input command helper",
                (
                    headless.NATIVE_INPUT_COMMAND_HELPER_CAVE_OFFSET,
                    headless.P1_LIST_INPUT_CAVE_OFFSET,
                ),
            ),
            (
                "P1 list callback",
                (
                    headless.P1_LIST_INPUT_CAVE_OFFSET,
                    headless.P1_LIST_INPUT_CAVE_OFFSET
                    + headless.P1_LIST_INPUT_CAVE_SIZE,
                ),
            ),
            (
                "desktop button callback",
                (
                    headless.NATIVE_DESKTOP_BUTTON_CAVE_OFFSET,
                    headless.P1_LIST_INPUT_STATE_OFFSET,
                ),
            ),
            (
                "P1 list mailbox",
                (
                    headless.P1_LIST_INPUT_STATE_OFFSET,
                    headless.NATIVE_INPUT_STATE_OFFSET,
                ),
            ),
            (
                "input policy and event state",
                (
                    headless.NATIVE_INPUT_STATE_OFFSET,
                    headless.NATIVE_INPUT_STATE_OFFSET
                    + headless.NATIVE_INPUT_STATE_SIZE,
                ),
            ),
            (
                "action and Gambits mailbox",
                (
                    headless_menu_core.DISPATCH_STATE_OFFSET,
                    headless_menu_core.DISPATCH_STATE_OFFSET
                    + headless_menu_core.DISPATCH_STATE_SIZE,
                ),
            ),
            (
                "Gambits identity proof callback",
                (
                    headless_menu_core.INDICATOR_CAVE_OFFSET,
                    headless_menu_core.INDICATOR_CAVE_OFFSET
                    + headless_menu_core.INDICATOR_CAVE_SIZE,
                ),
            ),
            (
                "combat indicator mailbox",
                (
                    headless_menu_core.INDICATOR_STATE_OFFSET,
                    headless_menu_core.SOUND_CAVE_OFFSET,
                ),
            ),
            (
                "UI sound callback",
                (
                    headless_menu_core.SOUND_CAVE_OFFSET,
                    headless_menu_core.SOUND_CAVE_OFFSET
                    + headless_menu_core.SOUND_CAVE_SIZE,
                ),
            ),
            (
                "ordinary UI sound lane",
                (
                    headless_menu_core.SOUND_STATE_OFFSET,
                    headless_menu_core.SOUND_STATE_OFFSET + 4,
                ),
            ),
            (
                "priority treasure sound lane",
                (
                    native_treasure_log.TREASURE_SOUND_STATE_OFFSET,
                    native_treasure_log.TREASURE_SOUND_STATE_OFFSET + 4,
                ),
            ),
            (
                "sound mailbox reserved tail",
                (
                    native_treasure_log.TREASURE_SOUND_STATE_OFFSET + 4,
                    headless_menu_core.SOUND_STATE_OFFSET + 0x10,
                ),
            ),
            (
                "reserved primary padding",
                (
                    headless_menu_core.SOUND_STATE_OFFSET + 0x10,
                    headless.NATIVE_MOUSE_LOOK_CAVE_OFFSET,
                ),
            ),
            (
                "desktop mouse-look gate",
                (
                    headless.NATIVE_MOUSE_LOOK_CAVE_OFFSET,
                    headless.NATIVE_DESKTOP_WHEEL_CAVE_OFFSET,
                ),
            ),
            (
                "desktop wheel callback",
                (
                    headless.NATIVE_DESKTOP_WHEEL_CAVE_OFFSET,
                    headless.NATIVE_DIALOG_CAVE_OFFSET,
                ),
            ),
            (
                "dialog button callback",
                (
                    headless.NATIVE_DIALOG_CAVE_OFFSET,
                    headless.NATIVE_DIALOG_STATE_OFFSET,
                ),
            ),
            (
                "dialog mailbox",
                (
                    headless.NATIVE_DIALOG_STATE_OFFSET,
                    headless.NATIVE_DIALOG_STATE_OFFSET
                    + headless.NATIVE_DIALOG_STATE_SIZE,
                ),
            ),
            (
                "input event publisher",
                (headless.NATIVE_INPUT_EVENT_PUBLISHER_CAVE_OFFSET, 0xF000),
            ),
        )

        self.assertEqual(regions[0][1][0], 0xE000)
        self.assertEqual(regions[-1][1][1], 0xF000)
        for (_left_name, left), (_right_name, right) in itertools.pairwise(
            regions
        ):
            self.assertEqual(left[1], right[0])
        self.assert_pairwise_disjoint(regions)

    def test_ui_sound_shared_mailbox_abi_is_exact(self):
        start = headless_menu_core.SOUND_STATE_OFFSET
        end = start + 0x10
        priority = native_treasure_log.TREASURE_SOUND_STATE_OFFSET
        self.assertEqual((start, end), (0xED00, 0xED10))
        self.assertEqual(priority, start + 4)
        self.assertEqual(
            (
                (start, priority),
                (priority, priority + 4),
                (priority + 4, end),
            ),
            (
                (0xED00, 0xED04),
                (0xED04, 0xED08),
                (0xED08, 0xED10),
            ),
        )

    def test_mux_target_and_aoe_partitions_are_pairwise_disjoint(self):
        target_regions = (
            ("mux header", (0, native_ui_tick_mux.HEADER_SIZE)),
            (
                "mux callback",
                (
                    native_ui_tick_mux.CODE_OFFSET,
                    native_ui_tick_mux.REGION_SIZE,
                ),
            ),
            (
                "target service header",
                (
                    native_target_circle_router.SERVICE_OFFSET,
                    native_target_circle_router.CODE_OFFSET,
                ),
            ),
            (
                "target callback",
                (
                    native_target_circle_router.CODE_OFFSET,
                    native_target_circle_router.STATE_OFFSET,
                ),
            ),
            (
                "target mailbox",
                (
                    native_target_circle_router.STATE_OFFSET,
                    native_target_circle_router.REGION_SIZE,
                ),
            ),
        )
        aoe_regions = (
            (
                "AoE service header",
                (0, native_area_target_circle_controller.CODE_OFFSET),
            ),
            (
                "AoE callback",
                (
                    native_area_target_circle_controller.CODE_OFFSET,
                    native_area_target_circle_controller.STATE_OFFSET,
                ),
            ),
            (
                "AoE mailbox",
                (
                    native_area_target_circle_controller.STATE_OFFSET,
                    native_area_target_circle_controller.REGION_SIZE,
                ),
            ),
        )

        self.assertEqual(native_ui_tick_mux.REGION_SIZE, 0x200)
        self.assertEqual(
            native_target_circle_router.SERVICE_OFFSET,
            native_ui_tick_mux.REGION_SIZE,
        )
        self.assertEqual(target_regions[0][1][0], 0)
        self.assertEqual(
            target_regions[-1][1][1],
            movement.NATIVE_TARGET_ROUTER_WORKSPACE_SIZE,
        )
        self.assertEqual(aoe_regions[0][1][0], 0)
        self.assertEqual(
            aoe_regions[-1][1][1],
            movement.NATIVE_AREA_TARGET_WORKSPACE_SIZE,
        )
        for regions in (target_regions, aoe_regions):
            for (_left_name, left), (_right_name, right) in itertools.pairwise(
                regions
            ):
                self.assertEqual(left[1], right[0])
            self.assert_pairwise_disjoint(regions)

        workspaces = (
            (
                "target router workspace",
                (
                    movement.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET,
                    movement.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET
                    + movement.NATIVE_TARGET_ROUTER_WORKSPACE_SIZE,
                ),
            ),
            (
                "AoE router workspace",
                (
                    movement.NATIVE_AREA_TARGET_WORKSPACE_OFFSET,
                    movement.NATIVE_AREA_TARGET_WORKSPACE_OFFSET
                    + movement.NATIVE_AREA_TARGET_WORKSPACE_SIZE,
                ),
            ),
        )
        self.assertEqual(workspaces[0][1][1], workspaces[1][1][0])
        self.assert_pairwise_disjoint(workspaces)

    def test_gateway_trigger_owns_its_page_and_its_scratch(self):
        """The scratch has to sit past the code, inside the same page."""

        cave = movement.FOLLOWER_GATEWAY_TRIGGER_CAVE_OFFSET
        scratch = movement.FOLLOWER_GATEWAY_TRIGGER_SCRATCH_OFFSET
        self.assertLess(cave, scratch)
        self.assertLessEqual(
            scratch + movement.FOLLOWER_GATEWAY_TRIGGER_SCRATCH_SIZE,
            cave + 0x200,
        )
        self.assertGreater(cave, movement.CAMERA_COLLISION_FILTER_SCRATCH_OFFSET)
        self.assertLessEqual(cave + 0x200, 0xC000)

    def test_camera_focus_is_not_the_legacy_cursor_feed(self):
        self.assertEqual(movement.CAMERA_FOCUS_V2_CAVE_OFFSET, 0xB200)
        self.assertNotEqual(movement.CAMERA_FOCUS_V2_CAVE_OFFSET, 0xD00)

    def test_camera_frame_double_buffers_fit_the_reserved_state_page(self):
        sync = movement.camera_frame_sync
        self.assertEqual(sync.CAMERA_FRAME_STATE_OFFSET, 0x2000)
        self.assertEqual(movement.CAMERA_FRAME_STATE, movement.CAMERA_SMOOTH_STATE)
        self.assertEqual(sync.CAMERA_GOAL_SLOT_OFFSETS, (0x20, 0x60))
        self.assertEqual(sync.CAMERA_OUTPUT_SLOT_OFFSETS, (0xB0, 0xD0))
        self.assertLessEqual(
            sync.CAMERA_OUTPUT_SLOT_OFFSETS[-1] + sync.CAMERA_OUTPUT_SLOT_SIZE,
            sync.CAMERA_FRAME_STATE_SIZE,
        )


class FollowerActionTransitionReservationTests(unittest.TestCase):
    def test_mailboxes_and_helper_own_only_the_audited_dual_gap(self):
        state = (
            movement.DUAL_INTERACTION_STATE_OFFSET,
            movement.DUAL_INTERACTION_STATE_OFFSET + 0x600,
        )
        mailboxes = (
            movement.FOLLOWER_ACTION_MAILBOX_OFFSET,
            movement.FOLLOWER_ACTION_MAILBOX_OFFSET
            + movement.FOLLOWER_ACTION_MAILBOX_REGION_SIZE,
        )
        helper = (
            movement.FOLLOWER_ACTION_TRANSITION_HELPER_OFFSET,
            movement.FOLLOWER_ACTION_TRANSITION_HELPER_OFFSET
            + movement.FOLLOWER_ACTION_TRANSITION_HELPER_SIZE,
        )
        next_native_reservation = (
            movement.FOLLOWER_NPC_ENTRY_GUARD_CAVE_OFFSET,
            movement.FOLLOWER_NPC_ENTRY_GUARD_CAVE_OFFSET + 0x100,
        )

        self.assertEqual(mailboxes[0], state[1])
        self.assertLessEqual(mailboxes[1], helper[0])
        self.assertLessEqual(helper[1], next_native_reservation[0])
        for left, right in (
            (state, mailboxes),
            (state, helper),
            (mailboxes, helper),
            (mailboxes, next_native_reservation),
            (helper, next_native_reservation),
        ):
            self.assertFalse(overlaps(left, right), (left, right))

    def test_mailbox_abi_and_role_addresses_are_exact(self):
        previous_base = movement.DUAL_INTERACTION_BASE
        base = 0x51000000
        try:
            movement.set_dual_interaction_base(base)
            self.assertEqual(
                movement.FOLLOWER_ACTION_MAILBOX_STRUCT.size,
                movement.FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE,
            )
            self.assertEqual(
                movement.FOLLOWER_ACTION_MAILBOX_REGION_SIZE,
                movement.FOLLOWER_ACTION_MAILBOX_ROLE_COUNT
                * movement.FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE,
            )
            self.assertEqual(
                movement.P2_FOLLOWER_ACTION_MAILBOX,
                base + movement.FOLLOWER_ACTION_MAILBOX_OFFSET,
            )
            self.assertEqual(
                movement.P3_FOLLOWER_ACTION_MAILBOX,
                movement.P2_FOLLOWER_ACTION_MAILBOX
                + movement.FOLLOWER_ACTION_MAILBOX_ROLE_STRIDE,
            )
            self.assertEqual(
                movement.FOLLOWER_ACTION_TRANSITION_HELPER,
                base + movement.FOLLOWER_ACTION_TRANSITION_HELPER_OFFSET,
            )
        finally:
            movement.set_dual_interaction_base(previous_base)

    def test_native_helper_fits_its_reserved_budget(self):
        previous_cave_base = movement.COMMAND_BLOCK - 0x200
        previous_dual_base = movement.DUAL_INTERACTION_BASE
        try:
            movement.set_cave_base(0x50000000)
            movement.set_dual_interaction_base(0x51000000)
            helper = movement.build_follower_action_transition_helper(
                movement.FOLLOWER_ACTION_TRANSITION_HELPER,
                movement.P2_FOLLOWER_ACTION_MAILBOX,
            )
        finally:
            movement.set_cave_base(previous_cave_base)
            movement.set_dual_interaction_base(previous_dual_base)

        self.assertGreater(len(helper), 0)
        self.assertLessEqual(
            len(helper),
            movement.FOLLOWER_ACTION_TRANSITION_HELPER_SIZE,
        )


class ThirdPlayerReservationTests(unittest.TestCase):
    """Guard the P3 block against the collision class that cost a session.

    The follower guard once landed on the menu control block at +0x1D00 because
    a free offset was picked by eye.  Every known reservation is enumerated
    here so the next one fails a test instead of corrupting live state.
    """

    def p3_region(self):
        return (
            movement.P3_COMMAND_BLOCK_OFFSET,
            movement.P3_COMMAND_BLOCK_OFFSET + movement.P3_COMMAND_BLOCK_SIZE,
        )

    def known_reservations(self):
        """Every cave offset the runtime reserves, as (name, start, end)."""

        regions = [
            ("command_block", 0x200, 0x300),
            ("keyboard", 0x300, 0x400),
            ("facing", 0x400, 0x600),
            ("visual_facing", 0x1000, 0x1200),
            (
                "visual_fallback_helper",
                movement.VISUAL_FALLBACK_HELPER_OFFSET,
                movement.VISUAL_FALLBACK_HELPER_OFFSET
                + movement.VISUAL_FALLBACK_HELPER_SIZE,
            ),
            (
                "treasure_no_actor",
                native_treasure_log.TREASURE_NO_ACTOR_CAVE_OFFSET,
                native_treasure_log.TREASURE_NO_ACTOR_CAVE_OFFSET
                + native_treasure_log.TREASURE_NO_ACTOR_CAVE_SIZE,
            ),
            (
                "treasure_sound",
                native_treasure_log.TREASURE_SOUND_CAVE_OFFSET,
                native_treasure_log.TREASURE_SOUND_CAVE_OFFSET
                + native_treasure_log.TREASURE_SOUND_CAVE_SIZE,
            ),
            ("camera", 0x600, 0x700),
            ("camera_distance_1", 0x700, 0x800),
            ("camera_distance_2", 0x800, 0x900),
            ("gauge_colour", 0x900, 0xB00),
            ("gauge_palette_and_theme", 0xB00, 0xC00),
            ("focus_pause", 0xC00, 0xD00),
            ("legacy_cursor_feed", 0xD00, 0xE00),
            ("gauge_p1", 0xE00, 0x1000),
            ("native_cursor_filter", 0x1A00, 0x1B00),
            ("follower_action_filter", 0x1B00, 0x1C00),
            ("phys_facing_guard", 0x1C00, 0x1D00),
            ("legacy_menu_control", 0x1D00, 0x1E00),
            (
                "early_menu_feature_gate",
                story.MENU_FEATURE_GATE_CAVE_OFFSET,
                story.MENU_FEATURE_GATE_CAVE_OFFSET + story.MENU_FEATURE_GATE_CAVE_SIZE,
            ),
            (
                "camera_frame_state",
                movement.camera_frame_sync.CAMERA_FRAME_STATE_OFFSET,
                movement.camera_frame_sync.CAMERA_FRAME_STATE_OFFSET
                + movement.camera_frame_sync.CAMERA_FRAME_STATE_SIZE,
            ),
            (
                "camera_snapshot_helper",
                movement.CAMERA_SNAPSHOT_HELPER_OFFSET,
                movement.CAMERA_SNAPSHOT_HELPER_OFFSET
                + movement.CAMERA_SNAPSHOT_HELPER_SIZE,
            ),
            ("retired_menu_clone", 0x2000, 0x81F0),
            (
                "native_leader_field_bridge",
                movement.NATIVE_LEADER_BRIDGE_CAVE_OFFSET,
                movement.NATIVE_LEADER_BRIDGE_CAVE_OFFSET + 0x100,
            ),
            (
                "p3_gauge_interpolator",
                movement.P3_GAUGE_COLOR_CAVE_OFFSET,
                movement.P3_GAUGE_COLOR_CAVE_OFFSET + 0x100,
            ),
            (
                "combat_log_target_gate",
                headless.NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET,
                headless.NATIVE_COMBAT_LOG_GATE_REGION_END,
            ),
            (
                "renderer_presentation_gate",
                headless.RENDER_PRESENTATION_SUPPRESSED_OFFSET,
                headless.RENDER_PRESENTATION_SUPPRESSED_OFFSET + 4,
            ),
            (
                "p1_berserk_movement_state",
                movement.P1_BERSERK_MOVEMENT_STATE_OFFSET,
                movement.P1_BERSERK_MOVEMENT_STATE_OFFSET
                + movement.P1_BERSERK_MOVEMENT_STATE_SIZE,
            ),
            (
                "p1_berserk_movement_helper",
                movement.P1_BERSERK_MOVEMENT_HELPER_OFFSET,
                movement.P1_BERSERK_MOVEMENT_HELPER_OFFSET
                + movement.P1_BERSERK_MOVEMENT_HELPER_SIZE,
            ),
            (
                "movement_mailboxes",
                movement.MOVEMENT_MAILBOX_REGION_OFFSET,
                movement.MOVEMENT_MAILBOX_REGION_OFFSET
                + movement.MOVEMENT_MAILBOX_REGION_SIZE,
            ),
            (
                "steering_fallback_helper",
                movement.STEERING_FALLBACK_HELPER_OFFSET,
                movement.STEERING_FALLBACK_HELPER_OFFSET
                + movement.STEERING_FALLBACK_HELPER_SIZE,
            ),
            (
                "movement_cave",
                movement.MOVEMENT_CAVE_OFFSET,
                movement.MOVEMENT_CAVE_OFFSET + movement.MOVEMENT_CAVE_SIZE,
            ),
            ("camera_focus_v2", 0xB200, 0xB300),
            ("follower_action_approach_gate", 0xB300, 0xB400),
            (
                "xinput_filter_and_leader_bridge",
                movement.P2_DEVICE_FILTER_CAVE_OFFSET,
                movement.P2_DEVICE_FILTER_STATE_OFFSET
                + movement.P2_DEVICE_FILTER_STATE_SIZE,
            ),
            (
                "p1_sdl_xinput_bridge",
                movement.P1_SDL_XINPUT_CAVE_OFFSET,
                movement.P1_SDL_XINPUT_CAVE_OFFSET + movement.P1_SDL_XINPUT_CAVE_SIZE,
            ),
            (
                "p1_sdl_xinput_state",
                movement.P1_SDL_XINPUT_STATE_OFFSET,
                movement.P1_SDL_XINPUT_STATE_OFFSET + movement.P1_SDL_XINPUT_STATE_SIZE,
            ),
            (
                "dinput_gamepad_filter",
                movement.DINPUT_GAMEPAD_FILTER_CAVE_OFFSET,
                movement.DINPUT_GAMEPAD_FILTER_CAVE_OFFSET
                + movement.DINPUT_GAMEPAD_FILTER_CAVE_SIZE,
            ),
            (
                "steam_gamepad_filter",
                movement.STEAM_GAMEPAD_FILTER_CAVE_OFFSET,
                movement.STEAM_GAMEPAD_FILTER_CAVE_OFFSET
                + movement.STEAM_GAMEPAD_FILTER_CAVE_SIZE,
            ),
            ("headless_and_guards", 0xC000, 0x10000),
        ]
        return regions

    def test_atomic_movement_mailboxes_and_expanded_cave_own_audited_gaps(self):
        mailboxes = (
            movement.MOVEMENT_MAILBOX_REGION_OFFSET,
            movement.MOVEMENT_MAILBOX_REGION_OFFSET
            + movement.MOVEMENT_MAILBOX_REGION_SIZE,
        )
        cave = (
            movement.MOVEMENT_CAVE_OFFSET,
            movement.MOVEMENT_CAVE_OFFSET + movement.MOVEMENT_CAVE_SIZE,
        )
        self.assertEqual(movement.P2_MOVEMENT_MAILBOX_OFFSET, mailboxes[0])
        self.assertEqual(
            movement.P3_MOVEMENT_MAILBOX_OFFSET,
            mailboxes[0] + movement.MOVEMENT_MAILBOX_ROLE_STRIDE,
        )
        self.assertLessEqual(mailboxes[1], cave[0])
        self.assertLessEqual(cave[1], 0x9000)
        for name, start, end in self.known_reservations():
            if name in {"movement_mailboxes", "movement_cave"}:
                continue
            self.assertFalse(
                overlaps(mailboxes, (start, end)),
                f"movement mailboxes {mailboxes} overlap {name} {(start, end)}",
            )
            self.assertFalse(
                overlaps(cave, (start, end)),
                f"movement cave {cave} overlaps {name} {(start, end)}",
            )

    def test_heading_fallback_helpers_end_at_their_audited_boundaries(self):
        self.assertEqual(
            movement.STEERING_FALLBACK_HELPER_OFFSET,
            movement.MOVEMENT_MAILBOX_REGION_OFFSET
            + movement.MOVEMENT_MAILBOX_REGION_SIZE,
        )
        self.assertLessEqual(
            movement.STEERING_FALLBACK_HELPER_OFFSET
            + movement.STEERING_FALLBACK_HELPER_SIZE,
            movement.MOVEMENT_CAVE_OFFSET,
        )
        self.assertEqual(movement.VISUAL_FALLBACK_HELPER_OFFSET, 0x1200)
        self.assertLessEqual(
            movement.VISUAL_FALLBACK_HELPER_OFFSET
            + movement.VISUAL_FALLBACK_HELPER_SIZE,
            0x1300,
        )

    def test_combat_log_gate_owns_the_audited_gap_after_p3(self):
        gate = (
            headless.NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET,
            headless.NATIVE_COMBAT_LOG_GATE_REGION_END,
        )
        self.assertGreaterEqual(
            gate[0],
            movement.P3_GAUGE_COLOR_CAVE_OFFSET + 0x100,
        )
        self.assertLessEqual(gate[1], 0x9000)
        self.assertLess(
            headless.NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET,
            headless.NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET,
        )
        self.assertLess(
            headless.NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET,
            headless.NATIVE_COMBAT_LOG_GATE_STATE_OFFSET,
        )
        for name, start, end in self.known_reservations():
            if name == "combat_log_target_gate":
                continue
            self.assertFalse(
                overlaps(gate, (start, end)),
                f"Combat Log gate {gate} overlaps {name} {(start, end)}",
            )

    def test_renderer_presentation_gate_is_outside_p3_and_hook_regions(self):
        gate = (
            headless.RENDER_PRESENTATION_SUPPRESSED_OFFSET,
            headless.RENDER_PRESENTATION_SUPPRESSED_OFFSET + 4,
        )
        self.assertGreaterEqual(gate[0], headless.NATIVE_COMBAT_LOG_GATE_REGION_END)
        self.assertLessEqual(gate[1], 0x9000)
        self.assertFalse(overlaps(gate, self.p3_region()))

    def test_early_menu_feature_gate_has_an_isolated_cave_slot(self):
        gate = (
            story.MENU_FEATURE_GATE_CAVE_OFFSET,
            story.MENU_FEATURE_GATE_CAVE_OFFSET + story.MENU_FEATURE_GATE_CAVE_SIZE,
        )
        self.assertGreaterEqual(story.MENU_FEATURE_GATE_STATE_OFFSET, gate[0])
        self.assertLess(story.MENU_FEATURE_GATE_STATE_OFFSET, gate[1])
        for name, start, end in self.known_reservations():
            if name == "early_menu_feature_gate":
                continue
            self.assertFalse(
                overlaps(gate, (start, end)),
                f"menu feature gate {gate[0]:#x}..{gate[1]:#x} overlaps "
                f"{name} {start:#x}..{end:#x}",
            )

    def test_the_p3_block_collides_with_nothing_known(self):
        p3 = self.p3_region()
        for name, start, end in self.known_reservations():
            self.assertFalse(
                overlaps(p3, (start, end)),
                f"P3 block {p3[0]:#x}..{p3[1]:#x} overlaps {name} {start:#x}..{end:#x}",
            )

    def test_the_p3_block_clears_the_retired_menu_clone(self):
        # The clone is retired, but its old range is still referenced by RE
        # tooling and old traces; staying past it keeps those readable.
        self.assertGreaterEqual(movement.P3_COMMAND_BLOCK_OFFSET, 0x81F0)

    def test_the_p3_block_fits_inside_the_allocation(self):
        self.assertLessEqual(self.p3_region()[1], 0x10000)

    def test_p3_fields_stay_inside_their_block(self):
        fields = (
            movement.P3_HEADING_OFFSET,
            movement.P3_MAGNITUDE_OFFSET,
            movement.P3_ACTOR_OFFSET,
            movement.P3_PHYS_OFFSET,
            movement.P3_MOVEMENT_ACTIVE_OFFSET,
            movement.P3_HUD_PARTY_SLOT_OFFSET,
            movement.P3_VISUAL_HEADING_OFFSET,
            movement.P3_LOOT_HANDLE_OFFSET,
            movement.P3_INTERACTION_ENTRY_OFFSET,
            movement.P3_INTERACTION_SCAN_ENTRY_OFFSET,
            movement.P3_INTERACTION_STATE_POINTER_OFFSET,
            movement.MANUAL_CONTROL_STATE_OFFSET,
            movement.EXTERNAL_CAMERA_HOLD_OFFSET,
            movement.MOVEMENT_HELPER_LIVENESS_OFFSET,
        )
        for field in fields:
            self.assertGreaterEqual(field, 0)
            self.assertLess(field + 8, movement.P3_COMMAND_BLOCK_SIZE)
        self.assertLessEqual(
            movement.MOVEMENT_HELPER_LIVENESS_OFFSET
            + movement.MOVEMENT_HELPER_LIVENESS_SIZE,
            movement.P3_COMMAND_BLOCK_SIZE,
        )

    def test_p3_pointer_fields_are_eight_byte_aligned(self):
        for field in (
            movement.P3_ACTOR_OFFSET,
            movement.P3_PHYS_OFFSET,
            movement.P3_INTERACTION_ENTRY_OFFSET,
            movement.P3_INTERACTION_SCAN_ENTRY_OFFSET,
            movement.P3_INTERACTION_STATE_POINTER_OFFSET,
        ):
            self.assertEqual(field % 8, 0)

    def test_p3_fields_do_not_overlap_each_other(self):
        sized = [
            (movement.P3_HEADING_OFFSET, 4),
            (movement.P3_MAGNITUDE_OFFSET, 4),
            (movement.P3_ACTOR_OFFSET, 8),
            (movement.P3_PHYS_OFFSET, 8),
            (movement.P3_MOVEMENT_ACTIVE_OFFSET, 4),
            (movement.P3_HUD_PARTY_SLOT_OFFSET, 4),
            (movement.P3_VISUAL_HEADING_OFFSET, 4),
            (movement.P3_LOOT_HANDLE_OFFSET, 4),
            (movement.P3_INTERACTION_ENTRY_OFFSET, 8),
            (movement.P3_INTERACTION_SCAN_ENTRY_OFFSET, 8),
            (movement.P3_INTERACTION_STATE_POINTER_OFFSET, 8),
            (movement.MANUAL_CONTROL_STATE_OFFSET, 8),
            (
                movement.EXTERNAL_CAMERA_HOLD_OFFSET,
                movement.EXTERNAL_CAMERA_HOLD_SIZE,
            ),
            (
                movement.MOVEMENT_HELPER_LIVENESS_OFFSET,
                movement.MOVEMENT_HELPER_LIVENESS_SIZE,
            ),
        ]
        spans = [(start, start + size) for start, size in sized]
        for index, first in enumerate(spans):
            for second in spans[index + 1 :]:
                self.assertFalse(
                    overlaps(first, second),
                    f"P3 fields {first} and {second} overlap",
                )


class MovementHelperLivenessTests(unittest.TestCase):
    class Memory:
        def __init__(self):
            self.bytes = {}
            self.writes = []

        def write(self, address, payload):
            payload = bytes(payload)
            self.writes.append((address, payload))
            for offset, value in enumerate(payload):
                self.bytes[address + offset] = value

        def read(self, address, size):
            return bytes(self.bytes.get(address + offset, 0) for offset in range(size))

    def test_liveness_publication_is_seqlocked_and_round_trips(self):
        process = self.Memory()
        previous_base = movement.COMMAND_BLOCK - 0x200
        base = 0x54000000
        try:
            movement.set_cave_base(base)
            sequence = movement.publish_movement_helper_liveness(
                process,
                0,
                owner_pid=4321,
                heartbeat_ms=123456,
            )
            snapshot = movement.read_movement_helper_liveness(process, base=base)
        finally:
            movement.set_cave_base(previous_base)

        address = (
            base
            + movement.P3_COMMAND_BLOCK_OFFSET
            + movement.MOVEMENT_HELPER_LIVENESS_OFFSET
        )
        self.assertEqual(sequence, 2)
        self.assertEqual([write[0] for write in process.writes], [address, address + 4, address])
        self.assertEqual(snapshot, (True, 2, movement.MOVEMENT_HELPER_LIVENESS_MAGIC, 1, 4321, 123456))

    def test_clear_invalidates_the_complete_liveness_record(self):
        process = self.Memory()
        base = 0x54000000
        address = (
            base
            + movement.P3_COMMAND_BLOCK_OFFSET
            + movement.MOVEMENT_HELPER_LIVENESS_OFFSET
        )
        process.write(address, b"\xff" * movement.MOVEMENT_HELPER_LIVENESS_SIZE)

        movement.clear_movement_helper_liveness(process, base=base)

        self.assertEqual(
            process.read(address, movement.MOVEMENT_HELPER_LIVENESS_SIZE),
            b"\x00" * movement.MOVEMENT_HELPER_LIVENESS_SIZE,
        )
        self.assertFalse(movement.read_movement_helper_liveness(process, base=base)[0])


class NativeLeaderBridgeReservationTests(unittest.TestCase):
    def test_executable_bridge_ends_before_its_state(self):
        self.assertEqual(movement.P2_DEVICE_FILTER_CAVE_OFFSET, 0xD000)
        self.assertGreaterEqual(movement.P2_DEVICE_FILTER_STATE_OFFSET, 0xD200)

    def test_role_commit_guard_bridge_and_journal_are_disjoint(self):
        self.assertLessEqual(
            movement.NATIVE_PARTY_LEADER_GUARD_CAVE_OFFSET + 0x40,
            movement.NATIVE_LEADER_BRIDGE_CAVE_OFFSET,
        )
        self.assertLessEqual(
            movement.NATIVE_LEADER_BRIDGE_CAVE_OFFSET
            + movement.NATIVE_LEADER_BRIDGE_CAVE_SIZE,
            movement.NATIVE_LEADER_ROLE_JOURNAL_OFFSET,
        )
        self.assertLessEqual(
            movement.NATIVE_LEADER_ROLE_JOURNAL_OFFSET
            + movement.NATIVE_LEADER_ROLE_JOURNAL_SIZE,
            movement.NATIVE_LEADER_ROLE_COMMIT_CAVE_OFFSET,
        )
        self.assertLessEqual(
            movement.NATIVE_LEADER_ROLE_COMMIT_CAVE_OFFSET
            + movement.NATIVE_LEADER_ROLE_COMMIT_CAVE_SIZE,
            movement.FOLLOWER_NPC_ENTRY_GUARD_CAVE_OFFSET,
        )

    def test_native_leader_code_fits_its_independent_reservations(self):
        movement.set_cave_base(0x50000000)
        movement.set_dual_interaction_base(0x51000000)
        bridge_end = movement.NATIVE_LEADER_BRIDGE_CAVE_OFFSET + len(
            movement.build_native_leader_bridge_cave()
        )
        guard_end = movement.NATIVE_PARTY_LEADER_GUARD_CAVE_OFFSET + len(
            movement.build_native_party_leader_guard_cave()
        )

        self.assertLessEqual(
            bridge_end,
            movement.NATIVE_LEADER_BRIDGE_CAVE_OFFSET
            + movement.NATIVE_LEADER_BRIDGE_CAVE_SIZE,
        )
        self.assertLessEqual(
            guard_end,
            movement.NATIVE_LEADER_BRIDGE_CAVE_OFFSET,
        )
        self.assertLessEqual(
            len(movement.build_native_leader_role_commit_cave()),
            movement.NATIVE_LEADER_ROLE_COMMIT_CAVE_SIZE,
        )

    def test_native_leader_mailbox_fields_fit_in_state_block(self):
        for field in (
            movement.NATIVE_LEADER_MEMBER_OFFSET,
            movement.NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET,
            movement.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
            movement.NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET,
        ):
            self.assertGreaterEqual(field, 0x10)
            self.assertLessEqual(field + 4, 0x20)
        for field in (
            movement.NATIVE_LEADER_ROLE_P1_OFFSET,
            movement.NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET,
            movement.NATIVE_LEADER_ROLE_JOURNAL_SEQUENCE_OFFSET,
            movement.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        ):
            self.assertGreaterEqual(field, 0x40)
            self.assertLessEqual(field + 4, movement.P2_DEVICE_FILTER_STATE_SIZE)


if __name__ == "__main__":
    unittest.main()
