import struct
import unittest

import coop_runtime as movement


class FakeProcess:
    def __init__(self):
        self.memory = {
            movement.CAMERA_FOCUS_V2_HOOK_SITE: movement.CAMERA_FOCUS_V2_ORIGINAL,
            movement.CAMERA_CONTROLLER_FOCUS_HOOK_SITE: movement.CAMERA_CONTROLLER_FOCUS_ORIGINAL,
        }

    def read(self, address, size):
        return self.memory.get(address, bytes(size))[:size]

    def write(self, address, data):
        self.memory[address] = bytes(data)

    def patch_code(self, address, data):
        self.write(address, data)


class CameraFocusV2HookTests(unittest.TestCase):
    def test_the_rescue_restores_the_boom_and_leaves_the_wall_bit(self):
        """A small room is a real wall, and the game's own bit has to survive.

        Clearing it here let the rescue push the camera out through the wall
        of any room short enough to look like a collapsed boom.
        """

        process = FakeProcess()
        movement.clear_internal_camera_wall_latch(
            process,
            8.25,
            movement.CAMERA_BACK_ON_WALL_MASK | 0x00001234,
        )
        self.assertAlmostEqual(
            struct.unpack(
                "<f",
                process.memory[movement.CAMERA_PRIMARY_RIG + 0x38],
            )[0],
            8.25,
        )
        self.assertNotIn(movement.CAMERA_PRIMARY_RIG + 0x40, process.memory)

    def test_the_old_behaviour_is_still_one_switch_away(self):
        process = FakeProcess()
        previous = movement.CAMERA_WALL_BIT_RELEASED_BY_RESCUE
        movement.CAMERA_WALL_BIT_RELEASED_BY_RESCUE = True
        try:
            movement.clear_internal_camera_wall_latch(
                process,
                8.25,
                movement.CAMERA_BACK_ON_WALL_MASK | 0x00001234,
            )
        finally:
            movement.CAMERA_WALL_BIT_RELEASED_BY_RESCUE = previous
        self.assertEqual(
            struct.unpack(
                "<I",
                process.memory[movement.CAMERA_PRIMARY_RIG + 0x40],
            )[0],
            0x00001234,
        )

    def test_install_consumes_only_a_committed_present_snapshot_and_restores(self):
        process = FakeProcess()
        movement.install_controller_shared_focus(process)

        patch = process.read(
            movement.CAMERA_CONTROLLER_FOCUS_HOOK_SITE,
            len(movement.CAMERA_CONTROLLER_FOCUS_ORIGINAL),
        )
        self.assertEqual(patch[:1], bytes([0xE9]))
        self.assertEqual(
            patch[5:],
            bytes([0x90]) * (len(movement.CAMERA_CONTROLLER_FOCUS_ORIGINAL) - 5),
        )

        cave = process.memory[movement.CAMERA_FOCUS_V2_CAVE]
        self.assertTrue(cave.startswith(movement.CAMERA_CONTROLLER_FOCUS_ORIGINAL))
        self.assertEqual(
            movement.CAMERA_CONTROLLER_FOCUS_HOOK_RETURN,
            movement.CAMERA_CONTROLLER_FOCUS_HOOK_SITE
            + len(movement.CAMERA_CONTROLLER_FOCUS_ORIGINAL),
        )
        self.assertLess(len(cave), 0x100)
        helper = process.memory[movement.CAMERA_SNAPSHOT_HELPER]
        self.assertLessEqual(len(helper), movement.CAMERA_SNAPSHOT_HELPER_SIZE)
        self.assertIn(
            bytes.fromhex("41 83 7A")
            + bytes([movement.CAMERA_ACTIVE_OFFSET])
            + bytes.fromhex("01 0F 85"),
            helper,
        )
        self.assertIn(
            bytes.fromhex("49 BA") + struct.pack("<Q", movement.CAMERA_FRAME_STATE),
            helper,
        )
        self.assertIn(
            struct.pack("<I", movement.camera_frame_sync.CAMERA_FRAME_MAGIC),
            helper,
        )
        self.assertIn(
            struct.pack(
                "<I", movement.camera_frame_sync.CAMERA_OUTPUT_SLOT_OFFSETS[0]
            ),
            helper,
        )
        commit = struct.pack(
            "<I", movement.camera_frame_sync.CAMERA_OUTPUT_COMMIT_OFFSET
        )
        self.assertIn(bytes.fromhex("4D 8B 9A") + commit, helper)
        self.assertIn(bytes.fromhex("4D 3B 9A") + commit, helper)
        self.assertIn(bytes.fromhex("E8"), cave)
        self.assertIn(bytes.fromhex("89 47 60 89 4F 64 89 57 68"), cave)
        self.assertIn(bytes.fromhex("C7 47 6C 00 00 80 3F"), cave)
        self.assertIn(bytes.fromhex("48 83 C4 20 5A 59 58 9D"), cave)

        movement.restore_controller_shared_focus(process)
        self.assertEqual(
            process.read(
                movement.CAMERA_CONTROLLER_FOCUS_HOOK_SITE,
                len(movement.CAMERA_CONTROLLER_FOCUS_ORIGINAL),
            ),
            movement.CAMERA_CONTROLLER_FOCUS_ORIGINAL,
        )

    def test_rejects_unknown_hook_site_bytes(self):
        process = FakeProcess()
        process.memory[movement.CAMERA_CONTROLLER_FOCUS_HOOK_SITE] = bytes(
            [0xCC]
        ) * len(movement.CAMERA_CONTROLLER_FOCUS_ORIGINAL)
        with self.assertRaises(RuntimeError):
            movement.install_controller_shared_focus(process)


class AdaptiveCameraRigHookTests(unittest.TestCase):
    def make_process(self):
        process = FakeProcess()
        process.memory[movement.CAMERA_DISTANCE_MUL1_SITE] = (
            movement.CAMERA_RIG_BUILD1_ORIGINAL
        )
        process.memory[movement.CAMERA_DISTANCE_MUL2_SITE] = (
            movement.CAMERA_RIG_BUILD2_ORIGINAL
        )
        return process

    def test_foreign_midpoint_detour_is_rejected_without_cave_write(self):
        process = FakeProcess()
        foreign = b"\xe9\x01\x02\x03\x04\x90\x90"
        process.memory[movement.CAMERA_HOOK_SITE] = foreign
        before = dict(process.memory)

        with self.assertRaisesRegex(RuntimeError, "unexpected bytes"):
            movement.install_camera_midpoint(process)

        self.assertEqual(process.memory, before)

    def test_install_publishes_bounded_angles_without_mutating_rig(self):
        process = self.make_process()
        movement.install_collision_aware_camera_zoom(process)

        for site, original in (
            (movement.CAMERA_DISTANCE_MUL1_SITE, movement.CAMERA_RIG_BUILD1_ORIGINAL),
            (movement.CAMERA_DISTANCE_MUL2_SITE, movement.CAMERA_RIG_BUILD2_ORIGINAL),
        ):
            patch = process.read(site, len(original))
            self.assertEqual(patch[:1], bytes([0xE9]))
            self.assertEqual(patch[5:], bytes([0x90]) * (len(original) - 5))

        for cave_address in (
            movement.CAMERA_DISTANCE_CAVE1,
            movement.CAMERA_DISTANCE_CAVE2,
        ):
            cave = process.memory[cave_address]
            self.assertIn(bytes.fromhex("F3 0F 58 54 24 14"), cave)
            self.assertIn(bytes.fromhex("F3 0F 58 4C 24 10"), cave)
            # There must be no store to persistent rig yaw/pitch.
            self.assertNotIn(bytes.fromhex("F3 0F 11 4E 30"), cave)
            self.assertNotIn(bytes.fromhex("F3 0F 11 56 34"), cave)

        second_cave = process.memory[movement.CAMERA_DISTANCE_CAVE2]
        # The controller already published the symmetric focus into the rig,
        # so active and native builders share the ordinary RSI+0x20 input.
        self.assertIn(bytes.fromhex("48 8D 4E 20"), second_cave)
        self.assertIn(bytes.fromhex("48 89 7C 24 58"), second_cave)

    def test_rejects_legacy_five_byte_zoom_hook_without_hot_repair(self):
        process = self.make_process()
        original = movement.CAMERA_RIG_BUILD1_ORIGINAL
        process.memory[movement.CAMERA_DISTANCE_MUL1_SITE] = (
            bytes([0xE9, 0, 0, 0, 0]) + original[5:]
        )
        before = dict(process.memory)

        with self.assertRaisesRegex(RuntimeError, "unexpected bytes"):
            movement.install_collision_aware_camera_zoom(process)

        self.assertEqual(process.memory, before)


class InternalCameraCollisionFilterTests(unittest.TestCase):
    def make_process(self):
        old_base = movement.DUAL_INTERACTION_BASE
        self.addCleanup(movement.set_dual_interaction_base, old_base)
        movement.set_dual_interaction_base(0x50000000)
        process = FakeProcess()
        for site in movement.CAMERA_COLLISION_CALL_SITES:
            process.memory[site] = movement._native_camera_collision_call(site)
        return process

    def test_install_wraps_all_native_camera_queries_and_restores(self):
        process = self.make_process()
        movement.install_shared_camera_internal_collision_filter(process)

        for site in movement.CAMERA_COLLISION_CALL_SITES:
            self.assertEqual(
                process.read(site, 5),
                movement._shared_camera_collision_call(site),
            )

        cave = process.memory[movement.CAMERA_COLLISION_FILTER_CAVE]
        self.assertLess(
            len(cave),
            movement.CAMERA_COLLISION_FILTER_SCRATCH
            - movement.CAMERA_COLLISION_FILTER_CAVE,
        )
        self.assertIn(struct.pack("<Q", movement.COMMAND_BLOCK), cave)
        self.assertIn(
            struct.pack("<Q", movement.CAMERA_COLLISION_FILTER_SCRATCH),
            cave,
        )
        self.assertIn(
            struct.pack("<Q", movement.CAMERA_COLLISION_QUERY),
            cave,
        )
        self.assertNotIn(
            struct.pack("<Q", 0x1111111122222222),
            cave,
        )
        self.assertNotIn(
            struct.pack("<Q", 0x3333333344444444),
            cave,
        )
        self.assertNotIn(
            struct.pack("<Q", 0x5555555566666666),
            cave,
        )
        self.assertIn(
            bytes.fromhex("41 83 7A 40 01"),
            cave,
        )
        self.assertIn(
            bytes.fromhex("41 83 7A 44 01"),
            cave,
        )
        self.assertIn(
            bytes.fromhex("41 0F 2E 42 48"),
            cave,
        )
        self.assertIn(bytes.fromhex("41 FF 42 4C"), cave)
        self.assertIn(
            bytes.fromhex("C7 44 24 50 00 00 00 00"),
            cave,
        )

        movement.restore_shared_camera_internal_collision_filter(process)
        for site in movement.CAMERA_COLLISION_CALL_SITES:
            self.assertEqual(
                process.read(site, 5),
                movement._native_camera_collision_call(site),
            )

    def test_rejects_foreign_camera_query_patch(self):
        process = self.make_process()
        first = movement.CAMERA_COLLISION_CALL_SITES[0]
        process.memory[first] = b"\xcc" * 5
        with self.assertRaises(RuntimeError):
            movement.install_shared_camera_internal_collision_filter(process)

    def test_formation_payload_is_member_symmetric_and_height_limited(self):
        tuning = movement.SharedCameraController().tuning
        forward = movement.camera_collision_formation_payload(
            (-4.0, 2.0, 1.0),
            (6.0, 4.0, 3.0),
            tuning,
            enabled=True,
        )
        swapped = movement.camera_collision_formation_payload(
            (6.0, 4.0, 3.0),
            (-4.0, 2.0, 1.0),
            tuning,
            enabled=True,
        )
        values = struct.unpack("<3f3fffI", forward)
        swapped_values = struct.unpack("<3f3fffI", swapped)
        self.assertEqual(values[:3], swapped_values[3:6])
        self.assertEqual(values[3:6], swapped_values[:3])
        self.assertAlmostEqual(values[1], 2.0 + tuning.target_height)
        self.assertAlmostEqual(
            values[6],
            tuning.internal_collision_corridor_radius**2,
        )
        self.assertAlmostEqual(
            values[7],
            tuning.internal_collision_vertical_radius**2,
        )
        self.assertEqual(values[8], 1)
        self.assertEqual(
            movement.camera_collision_formation_payload((), (), tuning, enabled=False),
            b"\x00" * 0x24,
        )

    def test_focus_escape_also_protects_a_two_player_midpoint(self):
        tuning = movement.SharedCameraController().tuning
        payload = movement.camera_collision_focus_escape_payload(
            ((0.0, 0.0, 0.0), (8.0, 0.0, 0.0)),
            (4.0, tuning.target_height, 0.0),
            tuning,
            enabled=True,
        )
        active, distance_squared = struct.unpack("<If", payload)
        self.assertEqual(active, 1)
        self.assertAlmostEqual(
            distance_squared,
            tuning.focus_collision_escape_max_hit_distance**2,
            places=5,
        )

    def test_focus_escape_stays_disabled_for_one_player(self):
        tuning = movement.SharedCameraController().tuning
        payload = movement.camera_collision_focus_escape_payload(
            ((0.0, 0.0, 0.0),),
            (4.0, tuning.target_height, 0.0),
            tuning,
            enabled=True,
        )
        self.assertEqual(payload, b"\x00" * 8)

    def test_focus_escape_rejects_a_real_actor_focus(self):
        tuning = movement.SharedCameraController().tuning
        party = (
            (0.0, 0.0, 0.0),
            (4.0, 0.0, 0.0),
            (0.0, 0.0, 4.0),
        )
        payload = movement.camera_collision_focus_escape_payload(
            party,
            (0.0, tuning.target_height, 0.0),
            tuning,
            enabled=True,
        )
        self.assertEqual(payload, b"\x00" * 8)

    def test_focus_escape_publishes_only_the_measured_origin_sphere(self):
        tuning = movement.SharedCameraController().tuning
        party = (
            (0.0, 0.0, 0.0),
            (4.0, 0.0, 0.0),
            (0.0, 0.0, 4.0),
        )
        payload = movement.camera_collision_focus_escape_payload(
            party,
            (2.0, tuning.target_height, 2.0),
            tuning,
            enabled=True,
        )
        active, distance_squared = struct.unpack("<If", payload)
        self.assertEqual(active, 1)
        self.assertAlmostEqual(
            distance_squared,
            tuning.focus_collision_escape_max_hit_distance**2,
            places=5,
        )


class CameraEyeSmoothingHookTests(unittest.TestCase):
    def test_explicit_reset_invalidates_the_complete_frame_bridge(self):
        process = FakeProcess()
        movement.reset_camera_eye_smoothing(process)
        self.assertEqual(
            process.memory[movement.CAMERA_FRAME_STATE],
            b"\x00" * movement.camera_frame_sync.CAMERA_FRAME_STATE_SIZE,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
