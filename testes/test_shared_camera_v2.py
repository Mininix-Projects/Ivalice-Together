import math
import unittest

from shared_camera import (
    CameraTuning,
    InternalCollisionLatchGuard,
    SharedCameraController,
)


class SharedCameraV2Tests(unittest.TestCase):
    def test_internal_latch_guard_suppresses_delayed_internal_collapse(self):
        guard = InternalCollisionLatchGuard()
        self.assertFalse(guard.observe((10, 20, 0, 2), 8.0, False, 0.016).suppress)
        self.assertFalse(guard.observe((11, 22, 0, 2), 8.2, False, 0.016).suppress)
        decision = guard.observe((11, 22, 0, 2), 4.5, True, 0.016)
        self.assertTrue(decision.suppress)
        self.assertTrue(decision.activated)
        self.assertAlmostEqual(decision.restored_native_boom, 8.2)

    def test_internal_latch_guard_never_suppresses_an_external_hit(self):
        guard = InternalCollisionLatchGuard()
        guard.observe((10, 20, 0, 2), 8.0, False, 0.016)
        guard.observe((11, 22, 0, 2), 8.2, False, 0.016)
        decision = guard.observe((12, 23, 0, 3), 4.5, True, 0.016)
        self.assertFalse(decision.suppress)

    def test_internal_latch_guard_expires_stale_internal_evidence(self):
        guard = InternalCollisionLatchGuard()
        guard.observe((10, 20, 0, 2), 8.0, False, 0.016)
        guard.observe((11, 22, 0, 2), 8.2, False, 0.016)
        for _ in range(20):
            guard.observe((11, 22, 0, 2), 8.2, False, 0.016)
        decision = guard.observe((11, 22, 0, 2), 4.5, True, 0.016)
        self.assertFalse(decision.suppress)

    def test_internal_latch_guard_releases_after_native_recovery(self):
        guard = InternalCollisionLatchGuard()
        guard.observe((10, 20, 0, 2), 8.0, False, 0.016)
        guard.observe((11, 22, 0, 2), 8.2, False, 0.016)
        self.assertTrue(guard.observe((11, 22, 0, 2), 4.5, True, 0.016).suppress)
        recovered = guard.observe((12, 24, 0, 2), 8.0, False, 0.016)
        self.assertFalse(recovered.suppress)
        self.assertTrue(recovered.released)

    def test_native_focus_distance_identifies_new_area_readiness(self):
        camera = SharedCameraController()
        self.assertAlmostEqual(
            camera.native_focus_distance_to_party(
                (5.0, 1.3, 0.0), (0, 0, 0), (10, 0, 0)
            ),
            0.0,
        )
        self.assertGreater(
            camera.native_focus_distance_to_party(
                (250.0, 40.0, 220.0), (0, 0, 0), (10, 0, 0)
            ),
            300.0,
        )

    def test_native_focus_distance_rejects_invalid_samples(self):
        camera = SharedCameraController()
        self.assertIsNone(
            camera.native_focus_distance_to_party(
                (math.nan, 1.0, 2.0), (0, 0, 0), (10, 0, 0)
            )
        )

    def test_first_frame_is_exact_midpoint_with_native_height(self):
        camera = SharedCameraController()
        frame = camera.update((0, 2, 0), (10, 4, 6), 1 / 60)
        self.assertEqual(frame.focus, (5.0, 5.15, 3.0))

    def test_stair_focus_stays_above_the_upper_actor(self):
        camera = SharedCameraController()
        frame = camera.update((0, 3, 0), (0, 10, 0), 1 / 60)
        self.assertGreater(frame.focus[1], 10.0)

    def test_small_group_jitter_remains_inside_comfort_deadzone(self):
        camera = SharedCameraController()
        initial = camera.update((0, 0, 0), (2, 0, 0), 1 / 60).focus
        for _ in range(30):
            frame = camera.update((0.01, 0, 0), (2.01, 0, 0), 1 / 60)
        self.assertAlmostEqual(frame.focus[0], initial[0], places=5)

    def test_same_present_is_idempotent_even_if_python_reads_new_positions(self):
        camera = SharedCameraController()
        tick = 5_000_000
        first = camera.update(
            (0, 0, 0),
            (8, 0, 0),
            1 / 60,
            present_tick_us=tick,
        )
        duplicate = camera.update(
            (20, 0, 0),
            (28, 0, 0),
            0.010,
            present_tick_us=tick,
        )

        self.assertIs(duplicate, first)
        self.assertFalse(camera.frame_advanced)
        self.assertEqual(camera.frame_delta_seconds, 0.0)

        advanced = camera.update(
            (20, 0, 0),
            (28, 0, 0),
            0.010,
            present_tick_us=tick + 16_667,
        )
        self.assertIsNot(advanced, first)
        self.assertTrue(camera.frame_advanced)
        self.assertAlmostEqual(camera.frame_delta_seconds, 0.016667, places=6)

    def test_present_delta_replaces_variable_python_poll_delta(self):
        fast_poll = SharedCameraController()
        slow_poll = SharedCameraController()
        first_tick = 8_000_000
        for camera in (fast_poll, slow_poll):
            camera.update(
                (0, 0, 0),
                (6, 0, 0),
                1 / 60,
                present_tick_us=first_tick,
            )

        fast = fast_poll.update(
            (1, 0, 0),
            (7, 0, 0),
            0.001,
            present_tick_us=first_tick + 20_000,
        )
        slow = slow_poll.update(
            (1, 0, 0),
            (7, 0, 0),
            0.049,
            present_tick_us=first_tick + 20_000,
        )

        self.assertEqual(fast, slow)
        self.assertAlmostEqual(fast_poll.frame_delta_seconds, 0.020, places=6)
        self.assertAlmostEqual(slow_poll.frame_delta_seconds, 0.020, places=6)

    def test_shared_focus_converges_to_equal_horizontal_midpoint(self):
        camera = SharedCameraController()
        frame = None
        for _ in range(240):
            frame = camera.update((0, 0, 0), (10, 0, 0), 1 / 60)
        self.assertIsNotNone(frame)
        self.assertAlmostEqual(frame.focus[0], 5.0, delta=0.031)

    def test_player_order_never_changes_shared_framing(self):
        camera_ab = SharedCameraController()
        camera_ba = SharedCameraController()
        path_a = ((0, 0, 0), (0.1, 0.2, 0.0), (0.3, 0.4, 0.2))
        path_b = ((8, 2, 4), (8.2, 2.1, 4.1), (8.5, 2.3, 4.4))
        frame_ab = frame_ba = None
        for p1, p2 in zip(path_a, path_b):
            frame_ab = camera_ab.update(p1, p2, 1 / 60)
            frame_ba = camera_ba.update(p2, p1, 1 / 60)
        self.assertIsNotNone(frame_ab)
        self.assertIsNotNone(frame_ba)
        for actual, swapped in zip(frame_ab.focus, frame_ba.focus):
            self.assertAlmostEqual(actual, swapped, places=6)
        self.assertAlmostEqual(
            frame_ab.extra_distance, frame_ba.extra_distance, places=6
        )

    def test_horizontal_separation_drives_zoom_but_large_height_is_clamped(self):
        camera = SharedCameraController()
        horizontal = camera.update((0, 0, 0), (14, 0, 0), 1 / 60)
        self.assertGreater(horizontal.extra_distance, 0.0)
        camera.reset()
        vertical = camera.update((0, 0, 0), (0, 100, 0), 1 / 60)
        self.assertLess(vertical.effective_separation, 2.0)
        self.assertEqual(vertical.extra_distance, 0.0)

    def test_internal_collision_corridor_covers_midpoint_prop(self):
        camera = SharedCameraController(
            CameraTuning(internal_collision_corridor_radius=2.0)
        )
        exclusion = camera.internal_collision_exclusion_group(
            ((-6, 0, 0), (6, 0, 0)),
            (0, 1.3, 0),
            (0, 0, 1),
        )
        self.assertTrue(exclusion.active)
        self.assertEqual(exclusion.start_distance, 0.0)
        self.assertAlmostEqual(exclusion.end_distance, 2.0)

    def test_internal_collision_corridor_extends_toward_camera_side_actor(self):
        camera = SharedCameraController(
            CameraTuning(internal_collision_corridor_radius=1.5)
        )
        exclusion = camera.internal_collision_exclusion_group(
            ((0, 0, -5), (0, 0, 5)),
            (0, 1.3, 0),
            (0, -0.2, 1),
        )
        self.assertEqual(exclusion.start_distance, 0.0)
        self.assertAlmostEqual(exclusion.end_distance, 6.5)

    def test_internal_collision_filter_stops_before_external_wall_region(self):
        camera = SharedCameraController(
            CameraTuning(internal_collision_corridor_radius=1.5)
        )
        exclusion = camera.internal_collision_exclusion_group(
            ((-4, 0, 0), (4, 0, 0)),
            (0, 1.3, 0),
            (0, 0, 10),
        )
        self.assertLess(exclusion.end_distance, 2.0)
        # A wall six units behind the party remains in the native recast.
        self.assertGreater(6.0, exclusion.end_distance)

    def test_internal_collision_corridor_is_player_order_independent(self):
        camera = SharedCameraController()
        forward = camera.internal_collision_exclusion_group(
            ((-3, 0, -2), (5, 0, 4)), (1, 1.3, 1), (1, 0, 2)
        )
        swapped = camera.internal_collision_exclusion_group(
            ((5, 0, 4), (-3, 0, -2)), (1, 1.3, 1), (1, 0, 2)
        )
        self.assertAlmostEqual(forward.start_distance, swapped.start_distance)
        self.assertAlmostEqual(forward.end_distance, swapped.end_distance)

    def test_internal_collision_corridor_rejects_non_intersecting_ray(self):
        camera = SharedCameraController(
            CameraTuning(internal_collision_corridor_radius=1.0)
        )
        exclusion = camera.internal_collision_exclusion_group(
            ((-1, 0, 0), (1, 0, 0)),
            (10, 1.3, 10),
            (0, 0, 1),
        )
        self.assertFalse(exclusion.active)

    def test_zoom_out_is_faster_than_zoom_in(self):
        tuning = CameraTuning(zoom_out_seconds=0.2, zoom_in_seconds=0.8)
        camera = SharedCameraController(tuning)
        for _ in range(30):
            far = camera.update((0, 0, 0), (20, 0, 0), 1 / 60)
        peak = far.extra_distance
        near = camera.update((0, 0, 0), (0, 0, 0), 1 / 60)
        self.assertGreater(near.extra_distance, peak * 0.95)

    def test_collision_pressure_steps_back_and_recovers_slowly(self):
        """Hitting scenery must stop the camera, not send it all the way in.

        The native solver collapses its own boom in a single frame, so the
        collapsed figure says nothing about what was fitting a frame earlier.
        Learning from it threw the whole extension away, and the camera walked
        back in and probed out again about once every two seconds.
        """

        camera = SharedCameraController()
        camera.extra_distance = 10.0
        camera.collision_safe_extra_distance(
            actual_boom=10.2,
            native_boom=0.2,
            delta_seconds=1 / 60,
        )
        stepped = camera.collision_safe_extra_distance(
            actual_boom=1.2,
            native_boom=0.2,
            delta_seconds=1 / 60,
        )
        self.assertGreater(stepped, 8.0)
        self.assertLess(stepped, 10.0)
        self.assertTrue(camera.collision_active)
        for _ in range(20):
            held = camera.collision_safe_extra_distance(
                actual_boom=20.0,
                native_boom=10.0,
                delta_seconds=1 / 60,
            )
        # A cap below the native boom suppresses only co-op extension.  It no
        # longer counteracts FF12 by publishing a negative distance.
        self.assertEqual(held, 0.0)
        for _ in range(30):
            probing = camera.collision_safe_extra_distance(
                actual_boom=20.0,
                native_boom=10.0,
                delta_seconds=1 / 60,
            )
        self.assertGreater(probing, 0.0)
        self.assertLess(probing, camera.extra_distance)
        for _ in range(180):
            recovered = camera.collision_safe_extra_distance(
                actual_boom=20.0,
                native_boom=10.0,
                delta_seconds=1 / 60,
            )
        self.assertAlmostEqual(recovered, camera.extra_distance, places=2)

    def test_small_normal_boom_lag_does_not_latch_collision(self):
        camera = SharedCameraController()
        camera.extra_distance = 2.0
        result = camera.collision_safe_extra_distance(
            actual_boom=12.85,
            native_boom=11.63,
            delta_seconds=1 / 60,
        )
        self.assertFalse(camera.collision_active)
        self.assertAlmostEqual(result, 2.0)

    def test_one_moving_player_produces_midpoint_lead(self):
        camera = SharedCameraController()
        camera.update((0, 0, 0), (2, 0, 0), 1 / 60)
        frame = camera.update((0, 0, 0), (2.1, 0, 0), 1 / 60)
        self.assertGreater(frame.lead[0], 0.0)

    def test_native_boom_rebound_never_forces_below_native(self):
        camera = SharedCameraController()
        camera.extra_distance = 10.0
        camera.collision_safe_extra_distance(
            actual_boom=1.2,
            native_boom=0.2,
            delta_seconds=1 / 60,
        )
        correction = camera.collision_safe_extra_distance(
            actual_boom=1.2,
            native_boom=11.0,
            delta_seconds=1 / 60,
        )
        self.assertEqual(correction, 0.0)
        self.assertEqual(11.0 + correction, 11.0)

    def test_zero_coop_zoom_never_creates_negative_feedback(self):
        camera = SharedCameraController()
        camera.extra_distance = 0.0
        correction = camera.collision_safe_extra_distance(
            actual_boom=1.8,
            native_boom=7.0,
            delta_seconds=1 / 60,
        )
        self.assertEqual(correction, 0.0)
        self.assertFalse(camera.collision_active)
        self.assertEqual(camera.collision_boom_cap, 7.0)

    def test_native_wall_flag_freezes_instead_of_zeroing_extension(self):
        camera = SharedCameraController()
        camera.extra_distance = 4.0
        camera.collision_safe_extra_distance(10.0, 6.0, 1 / 60)
        camera.extra_distance = 8.0
        solution = camera.solve_obstruction(
            actual_boom=10.0,
            native_boom=6.0,
            delta_seconds=1 / 60,
            allow_auto_orbit=False,
            # The native flag remains authoritative even while manual camera
            # input has disabled the inferred feedback limiter.
            allow_collision_limiter=False,
            native_back_on_wall=True,
        )
        self.assertEqual(solution.extra_distance, 4.0)
        self.assertIsNone(camera.collision_boom_cap)
        self.assertTrue(camera.collision_active)
        self.assertTrue(camera.native_wall_latched)
        self.assertEqual(6.0 + solution.extra_distance, 10.0)

    def test_native_wall_release_recovers_without_negative_zoom(self):
        tuning = CameraTuning(
            native_wall_hold_seconds=0.02,
            native_wall_recover_units_per_second=12.0,
        )
        camera = SharedCameraController(tuning)
        camera.extra_distance = 4.0
        camera.collision_safe_extra_distance(10.0, 6.0, 0.01)
        camera.extra_distance = 8.0
        camera.collision_safe_extra_distance(
            10.0,
            6.0,
            0.01,
            native_back_on_wall=True,
        )
        recovered = 4.0
        for _ in range(20):
            recovered = camera.collision_safe_extra_distance(
                6.0 + recovered,
                6.0,
                0.01,
                native_back_on_wall=False,
                wall_release_requested=True,
            )
        self.assertGreater(recovered, 4.0)
        self.assertLessEqual(recovered, camera.extra_distance)

    def test_native_wall_latch_survives_one_clear_flag_frame(self):
        camera = SharedCameraController()
        camera.extra_distance = 4.0
        camera.collision_safe_extra_distance(10.0, 6.0, 0.01)
        camera.extra_distance = 8.0
        contact = camera.solve_obstruction(
            10.0,
            6.0,
            0.01,
            allow_auto_orbit=False,
            allow_collision_limiter=False,
            native_back_on_wall=True,
        )
        first_clear_frame = camera.solve_obstruction(
            10.0,
            6.0,
            0.01,
            allow_auto_orbit=False,
            allow_collision_limiter=False,
            native_back_on_wall=False,
        )
        self.assertEqual(contact.extra_distance, 4.0)
        self.assertEqual(first_clear_frame.extra_distance, 4.0)
        self.assertTrue(camera.native_wall_latched)

    def test_stationary_wall_latch_never_probes_by_timer(self):
        camera = SharedCameraController()
        camera.extra_distance = 4.0
        camera.collision_safe_extra_distance(10.0, 6.0, 0.01)
        camera.extra_distance = 8.0
        frozen = camera.collision_safe_extra_distance(
            10.0,
            6.0,
            0.01,
            native_back_on_wall=True,
        )
        for _ in range(1000):
            output = camera.collision_safe_extra_distance(
                10.0,
                6.0,
                0.01,
                native_back_on_wall=False,
                wall_release_requested=False,
            )
            self.assertEqual(output, frozen)
        self.assertTrue(camera.native_wall_latched)

    def test_sustained_obstruction_starts_bounded_orbit_search(self):
        tuning = CameraTuning(
            obstruction_enter_seconds=0.03,
            orbit_probe_seconds=0.02,
        )
        camera = SharedCameraController(tuning)
        camera.extra_distance = 8.0
        solution = None
        for _ in range(20):
            solution = camera.solve_obstruction(1.2, 6.0, 0.01)
        self.assertIsNotNone(solution)
        self.assertTrue(solution.active)
        self.assertNotEqual(solution.yaw_offset, 0.0)
        self.assertLessEqual(abs(solution.yaw_offset), tuning.orbit_max_radians)
        self.assertLessEqual(abs(solution.pitch_offset), tuning.pitch_max_radians)

    def test_clear_view_returns_orbit_toward_neutral(self):
        tuning = CameraTuning(
            obstruction_enter_seconds=0.02,
            obstruction_exit_seconds=0.03,
            orbit_probe_seconds=0.01,
        )
        camera = SharedCameraController(tuning)
        camera.extra_distance = 8.0
        for _ in range(30):
            blocked = camera.solve_obstruction(1.0, 6.0, 0.01)
        peak = abs(blocked.yaw_offset)
        for _ in range(100):
            clear = camera.solve_obstruction(14.0, 6.0, 0.01)
        self.assertFalse(clear.active)
        self.assertLess(abs(clear.yaw_offset), peak)

    def test_native_boom_collapse_is_detected_after_game_overwrites_request(self):
        camera = SharedCameraController()
        camera.solve_obstruction(8.8, 8.8, 1 / 60)
        collapsed = camera.solve_obstruction(2.6, 2.6, 1 / 60)
        self.assertTrue(collapsed.active)
        self.assertTrue(camera.native_collapse_latched)
        self.assertGreater(collapsed.blocked_ratio, 0.60)
        self.assertNotEqual(camera.orbit_target, 0.0)

    def test_gradual_native_zoom_does_not_look_like_collision(self):
        camera = SharedCameraController()
        solution = camera.solve_obstruction(9.0, 9.0, 1 / 60)
        for step in range(1, 61):
            native = 9.0 - step * 0.03
            solution = camera.solve_obstruction(native, native, 1 / 60)
        self.assertFalse(solution.active)
        self.assertFalse(camera.native_collapse_latched)

    def test_vertical_focus_descends_faster_than_horizontal_centre(self):
        camera = SharedCameraController()
        camera.update((0, 8, 0), (2, 8, 0), 1 / 60)
        frame = camera.update((1, 4, 0), (3, 4, 0), 1 / 60)
        horizontal_progress = frame.focus[0] - 1.0
        vertical_progress = 9.3 - frame.focus[1]
        self.assertGreater(vertical_progress, horizontal_progress)

    def test_stairs_suppress_predictive_lead_and_track_midpoint_tightly(self):
        camera = SharedCameraController()
        camera.update((0.0, 5.0, 0.0), (2.0, 5.0, 0.0), 1.0 / 60.0)
        # Establish downward actor velocity while also moving horizontally.
        frame = camera.update((0.2, 4.8, 0.0), (2.2, 4.8, 0.0), 1.0 / 60.0)
        self.assertLess(abs(frame.lead[0]), 0.08)
        self.assertLess(abs(frame.focus[0] - 1.2), 0.25)

    def test_orbit_is_released_immediately_while_user_owns_camera(self):
        camera = SharedCameraController()
        camera.extra_distance = 5.0
        camera.solve_obstruction(9.0, 9.0, 1.0 / 60.0)
        camera.solve_obstruction(2.5, 2.5, 1.0 / 60.0)
        self.assertTrue(camera.obstruction_active)
        released = camera.solve_obstruction(
            2.5,
            2.5,
            1.0 / 60.0,
            allow_auto_orbit=False,
        )
        self.assertFalse(released.active)
        self.assertEqual(released.yaw_offset, 0.0)
        self.assertEqual(released.pitch_offset, 0.0)
        self.assertGreaterEqual(released.extra_distance, 0.0)
        self.assertEqual(camera.collision_zoom_correction, 0.0)
        self.assertIsNone(camera.collision_boom_cap)

    def test_boom_limiter_remains_available_without_auto_orbit(self):
        camera = SharedCameraController()
        camera.extra_distance = 6.0
        camera.solve_obstruction(
            10.0,
            6.0,
            1.0 / 60.0,
            allow_auto_orbit=False,
            allow_collision_limiter=True,
        )
        limited = camera.solve_obstruction(
            2.0,
            6.0,
            1.0 / 60.0,
            allow_auto_orbit=False,
            allow_collision_limiter=True,
        )
        self.assertFalse(limited.active)
        self.assertEqual(limited.yaw_offset, 0.0)
        self.assertLess(limited.extra_distance, camera.extra_distance)
        self.assertTrue(camera.collision_active)

    def test_lead_requires_both_players_to_move_together(self):
        camera = SharedCameraController()
        camera.update((0, 0, 0), (2, 0, 0), 1 / 60)
        together = camera.update((0.1, 0, 0), (2.1, 0, 0), 1 / 60)
        self.assertGreater(together.lead[0], 0.0)
        camera.reset()
        camera.update((0, 0, 0), (2, 0, 0), 1 / 60)
        opposed = camera.update((0.1, 0, 0), (1.9, 0, 0), 1 / 60)
        self.assertEqual(opposed.lead, (0.0, 0.0, 0.0))

    def test_100hz_polling_stays_smooth_over_60hz_actor_commits(self):
        camera = SharedCameraController()
        samples = []
        for tick in range(500):
            time_seconds = tick * 0.01
            present_index = math.floor(time_seconds * 60.0)
            committed = present_index / 60.0
            x = committed * 6.0
            present_tick_us = round(committed * 1_000_000.0) + 1
            frame = camera.update(
                (x, 0, 0),
                (x + 3, 0, 0),
                0.01,
                present_tick_us=present_tick_us,
            )
            if camera.frame_advanced:
                samples.append((present_tick_us, frame.focus[0]))
        velocities = [
            (samples[index][1] - samples[index - 1][1])
            / ((samples[index][0] - samples[index - 1][0]) / 1_000_000.0)
            for index in range(1, len(samples))
        ]
        # Duplicate 100 Hz polls no longer advance camera state. On the actual
        # 60 Hz Present clock, a steady 6 u/s group remains free of the old
        # velocity sawtooth caused by mixing producer and renderer cadence.
        stable = velocities[60:]
        self.assertLess(max(abs(value - 6.0) for value in stable), 0.5)

    def test_frame_exports_spring_velocity_and_reset_epoch_to_native_bridge(self):
        camera = SharedCameraController()
        first = camera.update((0, 0, 0), (2, 0, 0), 1 / 60)
        moving = camera.update((0.1, 0, 0), (2.1, 0, 0), 1 / 60)
        initial_epoch = moving.reset_epoch

        self.assertEqual(first.reset_epoch, initial_epoch)
        self.assertEqual(moving.focus_velocity, camera.focus_velocity)
        self.assertTrue(all(math.isfinite(value) for value in moving.focus_velocity))

        camera.reset()
        reset_frame = camera.update((8, 2, 1), (10, 2, 1), 1 / 60)
        self.assertNotEqual(reset_frame.reset_epoch, initial_epoch)
        self.assertEqual(reset_frame.focus_velocity, (0.0, 0.0, 0.0))

    def test_uneven_ground_does_not_toggle_camera_motion_mode(self):
        camera = SharedCameraController()
        samples = []
        horizontal = 0.0
        height = 0.0
        for tick in range(400):
            horizontal += 0.06
            # Mirror a quantized slope: actor Y advances on every other
            # controller tick, so sampled vertical speed alternates between
            # zero and a value above the terrain-transition threshold.
            if tick % 2 == 0:
                height += 0.006
            frame = camera.update(
                (horizontal, height, 0.0),
                (horizontal + 3.0, height, 0.0),
                0.01,
            )
            samples.append(
                (
                    frame.focus[0],
                    frame.focus[1],
                    camera.terrain_transition_blend,
                )
            )

        stable = samples[100:]
        blends = [sample[2] for sample in stable]
        self.assertLess(max(blends) - min(blends), 0.02)
        horizontal_velocity = [
            (samples[index][0] - samples[index - 1][0]) / 0.01
            for index in range(101, len(samples))
        ]
        vertical_velocity = [
            (samples[index][1] - samples[index - 1][1]) / 0.01
            for index in range(101, len(samples))
        ]
        self.assertLess(
            max(horizontal_velocity) - min(horizontal_velocity),
            0.02,
        )
        self.assertLess(
            max(vertical_velocity) - min(vertical_velocity),
            0.01,
        )


class CollisionBackoffTests(unittest.TestCase):
    """One step back per collision, not per frame.

    Pressure lasts many frames.  Stepping on each of them walked the cap to the
    floor in about half a second, and the camera stopped leaving the leader at
    all -- indistinguishable from having no co-op camera.
    """

    def test_sustained_pressure_costs_a_single_step(self):
        camera = SharedCameraController()
        camera.extra_distance = 10.0
        camera.collision_safe_extra_distance(
            actual_boom=10.2, native_boom=0.2, delta_seconds=1 / 60
        )
        for _ in range(30):
            held = camera.collision_safe_extra_distance(
                actual_boom=1.2, native_boom=0.2, delta_seconds=1 / 60
            )
        self.assertGreater(held, 8.0)

    def test_each_fresh_collision_costs_another_step(self):
        camera = SharedCameraController()
        camera.extra_distance = 10.0
        camera.collision_safe_extra_distance(
            actual_boom=10.2, native_boom=0.2, delta_seconds=1 / 60
        )
        primeiro = camera.collision_safe_extra_distance(
            actual_boom=1.2, native_boom=0.2, delta_seconds=1 / 60
        )
        # Let it settle out of the collision, then collide again.
        for _ in range(240):
            camera.collision_safe_extra_distance(
                actual_boom=20.0, native_boom=10.0, delta_seconds=1 / 60
            )
        camera.collision_safe_extra_distance(
            actual_boom=10.2, native_boom=0.2, delta_seconds=1 / 60
        )
        segundo = camera.collision_safe_extra_distance(
            actual_boom=1.2, native_boom=0.2, delta_seconds=1 / 60
        )
        self.assertLessEqual(segundo, primeiro)


class VelocityPresentClockTests(unittest.TestCase):
    """A steady walk must read as a steady speed.

    Measured in game: the party positions were perfectly smooth while the focus
    we publish carried roughly a pixel and a half of shiver, and the native eye
    inherited it.  The speed estimate was the source -- it divided by how long
    our own loop took to notice the move, which is not how long the move took.
    """

    SPEED = 6.0

    def speed(self, observation_age, present_interval):
        camera = SharedCameraController()
        distance = self.SPEED * present_interval
        _position, velocity, _age = camera._update_actor_velocity_sample(
            (0.0, 0.0, distance),
            (0.0, 0.0, 0.0),
            observation_age,
            (0.0, 0.0, 0.0),
            sample_seconds=present_interval,
        )
        return velocity[2]

    def test_present_time_ignores_how_late_the_python_poll_observed_it(self):
        early = self.speed(0.014, 1.0 / 60.0)
        late = self.speed(0.020, 1.0 / 60.0)
        self.assertAlmostEqual(early, late)
        self.assertAlmostEqual(early, self.SPEED)

    def test_same_world_speed_is_exact_at_every_present_rate(self):
        for rate in (30.0, 59.94, 60.0, 100.0, 144.0, 240.0):
            with self.subTest(rate=rate):
                interval = 1.0 / rate
                self.assertAlmostEqual(
                    self.speed(0.01, interval),
                    self.SPEED,
                    places=6,
                )

    def test_group_sampler_uses_committed_present_timestamps(self):
        for rate in (30.0, 59.94, 100.0, 144.0, 240.0):
            camera = SharedCameraController()
            interval_us = round(1_000_000.0 / rate)
            tick_us = 1_000_000
            x = 0.0
            camera.update(
                (x, 0.0, 0.0),
                (x + 3.0, 0.0, 0.0),
                0.01,
                present_tick_us=tick_us,
            )
            for _ in range(20):
                tick_us += interval_us
                x += self.SPEED * interval_us / 1_000_000.0
                camera.update(
                    (x, 0.0, 0.0),
                    (x + 3.0, 0.0, 0.0),
                    0.01,
                    present_tick_us=tick_us,
                )
            with self.subTest(rate=rate):
                self.assertAlmostEqual(
                    camera.sampled_velocities[0][0],
                    self.SPEED,
                    places=5,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
