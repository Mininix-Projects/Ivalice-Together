"""Third-player steering: resolution and camera-relative conversion."""

import math
import inspect
import struct
import unittest
from unittest import mock

import coop_runtime as movement


class FakeBindings:
    def __init__(self, axes):
        self.axes = axes

    def signed_axis(self, player, positive, negative):
        return self.axes.get((player, positive, negative), 0.0)


class RuntimeHotkeyTests(unittest.TestCase):
    def test_only_f8_remains_as_a_runtime_hotkey(self):
        source = inspect.getsource(movement.main)
        self.assertIn("key_down(VK_F8)", source)
        for name in ("VK_F4", "VK_F5", "VK_F6", "VK_F9", "VK_F10"):
            self.assertNotIn(name, source)

    def test_walk_toggle_is_independent_for_both_followers(self):
        source = inspect.getsource(movement.main)
        self.assertIn('bindings.edge("p2", "run_toggle")', source)
        self.assertIn('bindings.edge("p3", "run_toggle")', source)

    def test_manual_control_toggle_is_configurable_for_both_followers(self):
        source = inspect.getsource(movement.main)
        self.assertIn('bindings.edge("p2", "toggle_control")', source)
        self.assertIn('bindings.edge("p3", "toggle_control")', source)


class ManualControlStateTests(unittest.TestCase):
    class Memory:
        def __init__(self):
            self.data = {}

        def write(self, address, payload):
            for offset, value in enumerate(payload):
                self.data[address + offset] = value

        def read(self, address, size):
            return bytes(self.data.get(address + offset, 0) for offset in range(size))

        def u32(self, address):
            return struct.unpack("<I", self.read(address, 4))[0]

        def u64(self, address):
            return struct.unpack("<Q", self.read(address, 8))[0]

    def test_missing_publication_defaults_to_both_players_enabled(self):
        self.assertEqual(
            movement.read_manual_control_mask(self.Memory()),
            movement.DEFAULT_MANUAL_CONTROL_MASK,
        )

    def test_published_mask_round_trips_and_discards_unknown_bits(self):
        process = self.Memory()
        actual = movement.publish_manual_control_mask(
            process,
            movement.P2_MANUAL_CONTROL_BIT | 0x80,
        )
        self.assertEqual(actual, movement.P2_MANUAL_CONTROL_BIT)
        self.assertEqual(
            movement.read_manual_control_mask(process),
            movement.P2_MANUAL_CONTROL_BIT,
        )

    def test_initial_manual_ownership_requires_a_configured_source(self):
        class Bindings:
            def __init__(self, desktop_owner, devices):
                self.desktop_owner = desktop_owner
                self.devices = devices

            def owns_desktop(self, player):
                return player == self.desktop_owner

            def assigned_gamepad(self, player):
                return self.devices.get(player)

        cases = (
            (
                "current-session-p2-desktop-p3-empty",
                Bindings("p2", {"p1": 0, "p2": None, "p3": None}),
                movement.P2_MANUAL_CONTROL_BIT,
            ),
            (
                "both-followers-on-gamepads",
                Bindings(None, {"p2": 1, "p3": {"guid": "pad-3"}}),
                movement.DEFAULT_MANUAL_CONTROL_MASK,
            ),
            (
                "no-follower-source",
                Bindings("p1", {"p1": 0, "p2": None, "p3": None}),
                0,
            ),
        )
        for label, bindings, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(
                    movement.configured_follower_control_mask(bindings),
                    expected,
                )

    def test_disconnected_but_configured_gamepad_keeps_manual_ownership(self):
        bindings = mock.Mock()
        bindings.owns_desktop.return_value = False
        bindings.assigned_gamepad.side_effect = lambda player: (
            2 if player == "p3" else None
        )

        self.assertEqual(
            movement.configured_follower_control_mask(bindings),
            movement.P3_MANUAL_CONTROL_BIT,
        )

    def test_source_removal_releases_manual_ownership_and_source_gain_arms_it(self):
        both = movement.DEFAULT_MANUAL_CONTROL_MASK
        p2_only = movement.P2_MANUAL_CONTROL_BIT

        after_removal = movement.reconcile_follower_control_sources(
            both,
            both,
            p2_only,
        )
        after_reassignment = movement.reconcile_follower_control_sources(
            after_removal,
            p2_only,
            both,
        )

        self.assertEqual(after_removal, p2_only)
        self.assertEqual(after_reassignment, both)

    def test_unchanged_sources_do_not_reenable_an_explicitly_disabled_role(self):
        self.assertEqual(
            movement.reconcile_follower_control_sources(
                movement.P2_MANUAL_CONTROL_BIT,
                movement.DEFAULT_MANUAL_CONTROL_MASK,
                movement.DEFAULT_MANUAL_CONTROL_MASK,
            ),
            movement.P2_MANUAL_CONTROL_BIT,
        )

    def test_disabled_player_publishes_no_runtime_ownership(self):
        process = self.Memory()
        p2_actor = 0x71000000
        p3_actor = 0x72000000
        process.write(p2_actor + 0x08, struct.pack("<I", 0x2211))
        process.write(p2_actor + 0x10, struct.pack("<Q", 0x73100000))
        process.write(p3_actor + 0x08, struct.pack("<I", 0x4433))
        process.write(p3_actor + 0x10, struct.pack("<Q", 0x73200000))

        movement.publish_follower_role_metadata(
            process,
            active=True,
            p2_manual_control=False,
            p3_manual_control=True,
            p2_actor=p2_actor,
            p3_actor=p3_actor,
            p2_skeleton_keep=0x74100000,
            p3_skeleton_keep=0x74200000,
        )

        self.assertEqual(
            process.u32(movement.COMMAND_BLOCK + movement.P2_LOOT_HANDLE_OFFSET),
            0,
        )
        self.assertEqual(
            process.u64(movement.COMMAND_BLOCK + movement.P2_SKELETON_KEEP_OFFSET),
            0,
        )
        self.assertEqual(
            process.u64(movement.COMMAND_BLOCK + movement.P2_INTERACTION_ENTRY_OFFSET),
            0,
        )
        p3_block = movement.third_player_block()
        self.assertEqual(
            process.u32(p3_block + movement.P3_LOOT_HANDLE_OFFSET),
            0x4433,
        )
        self.assertEqual(
            process.u64(p3_block + movement.P3_SKELETON_KEEP_OFFSET),
            0x74200000,
        )
        self.assertEqual(
            process.u64(p3_block + movement.P3_INTERACTION_ENTRY_OFFSET),
            0x73200000,
        )

    def test_clear_commands_release_all_player_specific_ownership(self):
        process = self.Memory()
        for address, payload in (
            (movement.COMMAND_BLOCK + 0x10, struct.pack("<Q", 0x71000000)),
            (
                movement.COMMAND_BLOCK + movement.P2_SKELETON_KEEP_OFFSET,
                struct.pack("<Q", 0x71100000),
            ),
            (
                movement.third_player_block() + movement.P3_ACTOR_OFFSET,
                struct.pack("<Q", 0x72000000),
            ),
            (
                movement.third_player_block() + movement.P3_SKELETON_KEEP_OFFSET,
                struct.pack("<Q", 0x72100000),
            ),
        ):
            process.write(address, payload)

        movement.clear_second_player_command(process)
        movement.clear_third_player_command(process)

        self.assertEqual(process.u64(movement.COMMAND_BLOCK + 0x10), 0)
        self.assertEqual(
            process.u64(movement.COMMAND_BLOCK + movement.P2_SKELETON_KEEP_OFFSET),
            0,
        )
        self.assertEqual(
            process.u64(movement.third_player_block() + movement.P3_ACTOR_OFFSET),
            0,
        )
        self.assertEqual(
            process.u64(
                movement.third_player_block() + movement.P3_SKELETON_KEEP_OFFSET
            ),
            0,
        )


NORTH_BASIS = ((0.0, 1.0), (1.0, 0.0))  # forward = +Z, right = +X


class InteractionNpcScanAssemblyTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self):
            self.writes = []

        def read(self, address, size):
            if address == movement.INTERACTION_DUAL_SCAN_CALL_SITE:
                return movement.INTERACTION_DUAL_SCAN_CALL_ORIGINAL
            return b"\x00" * size

        def write(self, address, data):
            self.writes.append((address, bytes(data)))

        def patch_code(self, address, data):
            self.writes.append((address, bytes(data)))

    def scan_cave(self, *, npc_enabled=True):
        # Production assigns these addresses after reserving the dedicated
        # interaction page.  Keep this test independent of module import
        # order: at a pristine import both derived globals are zero, causing
        # the first write lookup below to mistake the leader bridge for the
        # dual-scan cave.
        if movement.DUAL_INTERACTION_SCAN_CAVE == movement.NATIVE_LEADER_BRIDGE_CAVE:
            movement.set_dual_interaction_base(0x60000000)
        process = self.FakeProcess()
        with mock.patch.object(
            movement,
            "FOLLOWER_NPC_INTERACTIONS_ENABLED",
            npc_enabled,
        ):
            movement.install_dual_interaction_scan(process)
        return next(
            data
            for address, data in process.writes
            if address == movement.DUAL_INTERACTION_SCAN_CAVE
        )

    def direct_calls(self, code, base=None):
        if base is None:
            base = movement.DUAL_INTERACTION_SCAN_CAVE
        calls = []
        for offset in range(len(code) - 4):
            if code[offset] != 0xE8:
                continue
            displacement = struct.unpack_from("<i", code, offset + 1)[0]
            calls.append(base + offset + 5 + displacement)
        return calls

    def exact_cave(self):
        movement.set_dual_interaction_base(0x60000000)
        return movement._build_follower_npc_exact_scan_cave()

    def test_generic_scan_never_runs_the_exact_npc_producer(self):
        calls = self.direct_calls(self.scan_cave())
        self.assertEqual(
            calls.count(movement.INTERACTION_NATIVE_INDICATOR_SCAN),
            0,
        )
        self.assertEqual(calls.count(movement.INTERACTION_NATIVE_SCAN), 3)

    def test_private_scans_resolve_live_entries_from_generation_handles(self):
        """Poisoned published entry pointers can never reach native scanners."""

        p3_block = movement.third_player_block()
        cases = (
            (self.scan_cave(), movement.DUAL_INTERACTION_SCAN_CAVE),
            (self.exact_cave(), movement.FOLLOWER_NPC_EXACT_SCAN_CAVE),
        )
        for code, base in cases:
            with self.subTest(base=base):
                calls = self.direct_calls(code, base)
                self.assertEqual(calls.count(movement.ENTITY_ENTRY_RESOLVER), 2)
                self.assertEqual(calls.count(movement.ENTITY_HANDLE_RESOLVER), 0)
                for handle_address in (
                    movement.COMMAND_BLOCK + movement.P2_LOOT_HANDLE_OFFSET,
                    p3_block + movement.P3_LOOT_HANDLE_OFFSET,
                ):
                    self.assertIn(
                        b"\x48\xb8"
                        + struct.pack("<Q", handle_address)
                        + b"\x8b\x08",
                        code,
                    )
                for poisoned_entry_address in (
                    movement.COMMAND_BLOCK + movement.P2_INTERACTION_ENTRY_OFFSET,
                    p3_block + movement.P3_INTERACTION_ENTRY_OFFSET,
                ):
                    self.assertNotIn(
                        b"\x48\xb8" + struct.pack("<Q", poisoned_entry_address),
                        code,
                    )
                # Both resolver results are null-checked before being published
                # as the synchronous active entry consumed by the native scan.
                self.assertGreaterEqual(code.count(b"\x48\x85\xc0\x74"), 2)

    def test_exact_wrapper_runs_two_private_owners_then_stock_p1(self):
        code = self.exact_cave()
        calls = self.direct_calls(code, movement.FOLLOWER_NPC_EXACT_SCAN_CAVE)
        self.assertEqual(
            calls.count(movement.INTERACTION_NATIVE_INDICATOR_SCAN),
            3,
        )
        self.assertEqual(
            calls.count(movement.INTERACTION_BUILD_EXACT_HANDLE),
            2,
        )

    def test_private_exact_descriptor_is_zeroed_without_native_clear_helpers(self):
        code = self.exact_cave()
        calls = self.direct_calls(code, movement.FOLLOWER_NPC_EXACT_SCAN_CAVE)
        self.assertEqual(calls.count(movement.INTERACTION_CLEAR_CAN_USE), 0)
        self.assertEqual(calls.count(movement.INTERACTION_CLEAR_DESCRIPTOR), 0)
        clear_descriptor = (
            b"\x48\xbf"
            + struct.pack("<Q", movement.NATIVE_INTERACTION_DESCRIPTOR)
            + b"\x31\xc0\xb9\x03\x00\x00\x00\xf3\x48\xab"
        )
        self.assertEqual(code.count(clear_descriptor), 2)
        # Only the private P2/P3 passes need immediate handle finalization.
        self.assertEqual(
            calls.count(movement.INTERACTION_BUILD_EXACT_HANDLE),
            2,
        )

    def test_p1_complete_exact_contract_survives_the_private_owner_scans(self):
        code = self.exact_cave()
        save_descriptor = (
            b"\x48\xbe"
            + struct.pack("<Q", movement.NATIVE_INTERACTION_DESCRIPTOR)
            + b"\x48\x8d\x7c\x24\x20"
            + b"\xb9\x03\x00\x00\x00\xf3\x48\xa5"
        )
        restore_descriptor = (
            b"\x48\x8d\x74\x24\x20"
            + b"\x48\xbf"
            + struct.pack("<Q", movement.NATIVE_INTERACTION_DESCRIPTOR)
            + b"\xb9\x03\x00\x00\x00\xf3\x48\xa5"
        )
        save_can_use = (
            b"\x48\xb8"
            + struct.pack("<Q", movement.NATIVE_INTERACTION_CAN_USE_GATE)
            + b"\x8a\x00\x88\x44\x24\x38"
        )
        restore_can_use = (
            b"\x8a\x44\x24\x38\x48\xbf"
            + struct.pack("<Q", movement.NATIVE_INTERACTION_CAN_USE_GATE)
            + b"\x88\x07"
        )
        for pattern in (
            save_descriptor,
            restore_descriptor,
            save_can_use,
            restore_can_use,
        ):
            self.assertEqual(code.count(pattern), 1)
        self.assertLess(code.index(save_descriptor), code.index(restore_descriptor))

    def test_disabled_npc_feature_leaves_process_global_descriptor_to_p1(self):
        code = self.scan_cave(npc_enabled=False)
        calls = self.direct_calls(code)
        self.assertEqual(
            calls.count(movement.INTERACTION_NATIVE_INDICATOR_SCAN),
            0,
        )
        self.assertEqual(calls.count(movement.INTERACTION_CLEAR_CAN_USE), 0)
        self.assertEqual(calls.count(movement.INTERACTION_CLEAR_DESCRIPTOR), 0)
        self.assertEqual(
            calls.count(movement.INTERACTION_BUILD_EXACT_HANDLE),
            0,
        )

    def test_each_private_exact_snapshot_has_an_independent_destination(self):
        code = self.exact_cave()
        for snapshot_offset in (
            movement.DUAL_INTERACTION_P2_SNAPSHOT_OFFSET,
            movement.DUAL_INTERACTION_P3_SNAPSHOT_OFFSET,
        ):
            destination = (
                movement.DUAL_INTERACTION_STATE
                + snapshot_offset
                + movement.DUAL_PUBLISH_EXACT_DESCRIPTOR_OFFSET
            )
            self.assertGreaterEqual(
                code.count(b"\x48\xbf" + struct.pack("<Q", destination)),
                2,
            )

    def test_exact_wrapper_resets_private_owner_before_final_p1_call(self):
        code = self.exact_cave()
        calls = []
        for offset in range(len(code) - 4):
            if code[offset] != 0xE8:
                continue
            displacement = struct.unpack_from("<i", code, offset + 1)[0]
            if (
                movement.FOLLOWER_NPC_EXACT_SCAN_CAVE + offset + 5 + displacement
                == movement.INTERACTION_NATIVE_INDICATOR_SCAN
            ):
                calls.append(offset)
        reset = (
            b"\x49\xbb"
            + struct.pack("<Q", movement.DUAL_INTERACTION_STATE)
            + b"\x41\xc7\x03\x00\x00\x00\x00"
        )
        self.assertEqual(len(calls), 3)
        self.assertLess(code.index(reset), calls[-1])

    def test_exact_wrapper_stays_inside_its_reserved_page_range(self):
        self.assertLessEqual(
            len(self.exact_cave()),
            movement.FOLLOWER_NPC_EXACT_SCAN_CAVE_LIMIT,
        )

    def test_main_installs_guards_and_source_before_exact_wrapper(self):
        source = inspect.getsource(movement._install_native_code)
        guards = source.index("install_follower_npc_private_object_guards(process)")
        source_hook = source.index("install_p2_interaction_indicator_source(process)")
        exact_scan = source.index("install_follower_npc_exact_scan(process)")
        dual_scan = source.index("install_dual_interaction_scan(process)")
        self.assertLess(guards, source_hook)
        self.assertLess(source_hook, exact_scan)
        self.assertLess(exact_scan, dual_scan)


class FollowerNpcObjectGuardAssemblyTests(unittest.TestCase):
    def setUp(self):
        movement.set_dual_interaction_base(0x60000000)

    @staticmethod
    def jump_targets(code, cave_address):
        targets = []
        for offset in range(len(code) - 4):
            if code[offset] != 0xE9:
                continue
            relative = struct.unpack_from("<i", code, offset + 1)[0]
            targets.append(cave_address + offset + 5 + relative)
        return targets

    def test_measured_stock_bytes_are_pinned(self):
        self.assertEqual(
            movement.FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL,
            bytes.fromhex("80 62 0C FD 48 8B FA"),
        )
        self.assertEqual(
            movement.FOLLOWER_NPC_ACCEPT_GUARD_ORIGINAL,
            bytes.fromhex("80 4F 0C 02 0F B6 47 0B"),
        )
        self.assertEqual(
            movement.FOLLOWER_NPC_CLEANUP_GUARD_ORIGINAL,
            bytes.fromhex("0F B6 47 0B A8 08"),
        )

    def test_entry_guard_skips_only_the_object_bit_clear_for_private_scan(self):
        code = movement._build_follower_npc_entry_guard_cave()
        self.assertIn(
            b"\x48\xb8"
            + struct.pack("<Q", movement.DUAL_INTERACTION_STATE)
            + b"\x83\x38\x00",
            code,
        )
        self.assertEqual(code.count(movement.FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL), 1)
        self.assertIn(b"\x48\x8b\xfa", code)
        self.assertEqual(
            self.jump_targets(code, movement.FOLLOWER_NPC_ENTRY_GUARD_CAVE),
            [
                movement.FOLLOWER_NPC_ENTRY_GUARD_RETURN,
                movement.FOLLOWER_NPC_ENTRY_GUARD_RETURN,
            ],
        )

    def test_accept_guard_bypasses_all_post_geometry_side_effects(self):
        code = movement._build_follower_npc_accept_guard_cave()
        self.assertEqual(code.count(movement.FOLLOWER_NPC_ACCEPT_GUARD_ORIGINAL), 1)
        self.assertEqual(
            self.jump_targets(code, movement.FOLLOWER_NPC_ACCEPT_GUARD_CAVE),
            [
                movement.FOLLOWER_NPC_ACCEPT_GUARD_RETURN,
                movement.FOLLOWER_NPC_ACCEPT_GUARD_PRIVATE_RETURN,
            ],
        )

    def test_cleanup_guard_keeps_p1_cleanup_and_skips_private_mutation(self):
        code = movement._build_follower_npc_cleanup_guard_cave()
        self.assertEqual(code.count(movement.FOLLOWER_NPC_CLEANUP_GUARD_ORIGINAL), 1)
        self.assertEqual(
            self.jump_targets(code, movement.FOLLOWER_NPC_CLEANUP_GUARD_CAVE),
            [
                movement.FOLLOWER_NPC_CLEANUP_GUARD_RETURN,
                movement.FOLLOWER_NPC_CLEANUP_GUARD_PRIVATE_RETURN,
            ],
        )

    def test_main_installs_object_guards_before_private_indicator(self):
        source = inspect.getsource(movement._install_native_code)
        guard = source.index("install_follower_npc_private_object_guards")
        indicator = source.index("install_p2_interaction_indicator_source")
        self.assertLess(guard, indicator)


class SteeringTests(unittest.TestCase):
    def test_main_refreshes_camera_basis_before_p2_axis_branch(self):
        source = inspect.getsource(movement.main)
        refresh = source.index("camera_basis = horizontal_camera_basis(")
        p2_axis_branch = source.index("if forward_input or right_input:")
        p3_steering = source.index("third_player_steering(")
        self.assertLess(refresh, p2_axis_branch)
        self.assertLess(refresh, p3_steering)

    def steer(self, forward=0.0, right=0.0, walk=False, allowed=True):
        bindings = FakeBindings(
            {
                ("p3", "move_up", "move_down"): forward,
                ("p3", "move_right", "move_left"): right,
            }
        )
        return movement.third_player_steering(bindings, NORTH_BASIS, walk, allowed)

    def test_no_input_keeps_the_previous_facing(self):
        heading, magnitude, moving = self.steer()
        self.assertIsNone(heading)
        self.assertEqual(magnitude, 0.0)
        self.assertFalse(moving)

    def test_forward_input_faces_the_camera_forward(self):
        heading, magnitude, moving = self.steer(forward=1.0)
        self.assertTrue(moving)
        self.assertAlmostEqual(heading, 0.0)
        self.assertAlmostEqual(magnitude, movement.RUN_STEP)

    def test_right_input_faces_ninety_degrees_across(self):
        heading, _magnitude, _moving = self.steer(right=1.0)
        self.assertAlmostEqual(heading, math.pi / 2)

    def test_back_input_faces_away(self):
        heading, _magnitude, _moving = self.steer(forward=-1.0)
        self.assertAlmostEqual(abs(heading), math.pi)

    def test_walk_mode_uses_the_walk_step(self):
        _heading, magnitude, _moving = self.steer(forward=1.0, walk=True)
        self.assertAlmostEqual(magnitude, movement.WALK_STEP)

    def test_diagonal_input_is_not_faster_than_straight(self):
        _h, straight, _m = self.steer(forward=1.0)
        _h, diagonal, _m = self.steer(forward=1.0, right=1.0)
        self.assertLessEqual(diagonal, straight + 1e-9)

    def test_partial_input_follows_the_native_curve_not_the_stick(self):
        """Half a stick is not half a step.

        FF12 never issues half its run command: measured off the leader, a
        stick at half deflection is still inside the walk ramp.  This used to
        assert the straight-through mapping, which is what let the followers
        outrun the leader by more than double at full deflection.
        """

        _heading, magnitude, _moving = self.steer(forward=0.5)
        self.assertAlmostEqual(
            magnitude,
            movement.RUN_STEP * movement.native_stick_fraction(0.5),
        )
        self.assertLess(magnitude, movement.RUN_STEP / 3.0)

    def test_a_stick_past_the_run_threshold_gives_the_whole_step(self):
        _heading, magnitude, _moving = self.steer(forward=0.95)
        self.assertAlmostEqual(magnitude, movement.RUN_STEP)

    def test_disallowed_movement_produces_nothing(self):
        heading, magnitude, moving = self.steer(forward=1.0, allowed=False)
        self.assertIsNone(heading)
        self.assertEqual(magnitude, 0.0)
        self.assertFalse(moving)

    def test_it_matches_the_second_player_conversion(self):
        # Both players must feel identical: same basis, same step.
        forward_input, right_input = 0.6, -0.8
        heading, magnitude, _moving = self.steer(
            forward=forward_input, right=right_input
        )
        forward, right = NORTH_BASIS
        world_x = forward_input * forward[0] + right_input * right[0]
        world_z = forward_input * forward[1] + right_input * right[1]
        self.assertAlmostEqual(heading, math.atan2(world_x, world_z))
        strength = min(1.0, math.hypot(forward_input, right_input))
        self.assertAlmostEqual(magnitude, movement.RUN_STEP * strength)


if __name__ == "__main__":
    unittest.main()
