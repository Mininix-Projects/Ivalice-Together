"""Regression cover for the party-size-agnostic shared camera.

The two-player camera was tuned and validated live, so the first requirement
of the generalisation is that two players produce byte-identical output.  The
remaining cases describe what a third player is allowed to change.
"""

import math
import unittest

from shared_camera import SharedCameraController


def walk(steps: int):
    """A deterministic two-player trajectory: converge, separate, climb."""

    for step in range(steps):
        t = step * 0.1
        yield (
            (math.sin(t) * 3.0, 1.0 + math.sin(t * 0.5) * 0.4, math.cos(t) * 2.0),
            (4.0 + math.cos(t) * 2.5, 1.0, -1.0 + math.sin(t * 0.7) * 3.0),
        )


class TwoPlayerEquivalenceTests(unittest.TestCase):
    def test_update_group_matches_the_tuned_two_player_path(self):
        legacy = SharedCameraController()
        group = SharedCameraController()
        for p1, p2 in walk(120):
            expected = legacy.update(p1, p2, 1.0 / 60.0)
            actual = group.update_group((p1, p2), 1.0 / 60.0)
            self.assertEqual(expected.focus, actual.focus)
            self.assertEqual(expected.extra_distance, actual.extra_distance)
            self.assertEqual(expected.effective_separation, actual.effective_separation)
            self.assertEqual(expected.lead, actual.lead)

    def test_focus_target_keeps_the_upper_actor_bias(self):
        camera = SharedCameraController()
        p1 = (0.0, 0.0, 0.0)
        p2 = (4.0, 3.0, 2.0)
        centre = camera._shared_focus_target(p1, p2)
        average_y = 1.5
        expected_y = (
            average_y
            + 1.5 * camera.tuning.vertical_upper_bias
            + camera.tuning.target_height
        )
        self.assertAlmostEqual(centre[0], 2.0)
        self.assertAlmostEqual(centre[2], 1.0)
        self.assertAlmostEqual(centre[1], expected_y)

    def test_distance_still_accepts_two_positions(self):
        camera = SharedCameraController()
        p1 = (0.0, 0.0, 0.0)
        p2 = (2.0, 0.0, 0.0)
        target = camera._shared_focus_target(p1, p2)
        distance = camera.native_focus_distance_to_party(target, p1, p2)
        self.assertIsNotNone(distance)
        self.assertAlmostEqual(distance, 0.0)


class ThirdPlayerTests(unittest.TestCase):
    def test_centre_is_the_group_centroid(self):
        camera = SharedCameraController()
        positions = [(0.0, 0.0, 0.0), (6.0, 0.0, 0.0), (0.0, 0.0, 6.0)]
        centre = camera._shared_focus_target(*positions)
        self.assertAlmostEqual(centre[0], 2.0)
        self.assertAlmostEqual(centre[2], 2.0)

    def test_separation_is_the_widest_pair(self):
        camera = SharedCameraController()
        positions = ((0.0, 0.0, 0.0), (3.0, 0.0, 0.0), (11.0, 0.0, 0.0))
        frame = camera.update_group(positions, 1.0 / 60.0)
        self.assertAlmostEqual(frame.effective_separation, 11.0, places=5)

    def test_a_third_player_between_the_others_does_not_widen_framing(self):
        pair = SharedCameraController()
        trio = SharedCameraController()
        p1 = (0.0, 0.0, 0.0)
        p2 = (10.0, 0.0, 0.0)
        middle = (5.0, 0.0, 0.0)
        for _ in range(90):
            pair_frame = pair.update(p1, p2, 1.0 / 60.0)
            trio_frame = trio.update_group((p1, middle, p2), 1.0 / 60.0)
        self.assertAlmostEqual(
            pair_frame.extra_distance, trio_frame.extra_distance, places=6
        )

    def test_an_outlying_third_player_widens_framing(self):
        pair = SharedCameraController()
        trio = SharedCameraController()
        p1 = (0.0, 0.0, 0.0)
        p2 = (4.0, 0.0, 0.0)
        far = (20.0, 0.0, 0.0)
        for _ in range(90):
            pair_frame = pair.update(p1, p2, 1.0 / 60.0)
            trio_frame = trio.update_group((p1, p2, far), 1.0 / 60.0)
        self.assertGreater(trio_frame.extra_distance, pair_frame.extra_distance)

    def test_velocity_sampling_covers_every_member(self):
        camera = SharedCameraController()
        for step in range(30):
            offset = step * 0.2
            camera.update_group(
                (
                    (0.0, 0.0, 0.0),
                    (2.0, 0.0, 0.0),
                    (4.0 + offset, 0.0, 0.0),
                ),
                1.0 / 60.0,
            )
        self.assertEqual(len(camera.sampled_velocities), 3)
        self.assertGreater(abs(camera.sampled_velocities[2][0]), 0.0)
        self.assertTrue(camera.actors_are_moving)


class FormationExclusionTests(unittest.TestCase):
    def test_a_wall_past_the_formation_is_left_to_the_game(self):
        camera = SharedCameraController()
        party = ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (4.0, 0.0, 0.0))
        exclusion = camera.internal_collision_exclusion_group(
            party, (0.0, 0.0, -10.0), (0.0, 0.0, 1.0)
        )
        self.assertTrue(exclusion.active)
        # The excluded stretch must end at the party, never run on past it.
        self.assertLess(
            exclusion.end_distance,
            10.0 + camera.tuning.internal_collision_corridor_radius + 1.0,
        )

    def test_a_degenerate_party_is_rejected_safely(self):
        camera = SharedCameraController()
        exclusion = camera.internal_collision_exclusion_group(
            ((0.0, 0.0, 0.0),), (0.0, 0.0, -5.0), (0.0, 0.0, 1.0)
        )
        self.assertFalse(exclusion.active)


if __name__ == "__main__":
    unittest.main()


def rescue_camera():
    """A controller with the (default-off) focus rescue switched on.

    The rescue stays disabled in the shipped tuning; these tests describe what
    it does when a future caller opts back into it.
    """

    from dataclasses import replace

    from shared_camera import CameraTuning

    return SharedCameraController(replace(CameraTuning(), focus_rescue_max_pull=0.5))


def seed_party(camera, party):
    """Teach the controller where the party is.

    The rescue is gated on formation width, so a unit test that only feeds the
    burial signal would be describing a party the controller has never seen.
    """

    camera.previous_positions[: len(party)] = list(party)
    return party


class FocusRescueTests(unittest.TestCase):
    """The synthetic focus must escape geometry the actors are never in."""

    BURIED = {"actual_boom": 12.0, "native_boom": 1.0, "native_back_on_wall": True}
    CLEAR = {"actual_boom": 12.0, "native_boom": 12.0, "native_back_on_wall": False}

    def test_idle_camera_is_untouched(self):
        camera = rescue_camera()
        party = ((0.0, 0.0, 0.0), (8.0, 0.0, 0.0))
        plain = camera._shared_focus_target(*party)
        self.assertEqual(camera._rescued_focus(plain, party), plain)
        self.assertEqual(camera.focus_rescue_blend, 0.0)

    def test_native_wall_signal_engages_and_releases(self):
        camera = rescue_camera()
        seed_party(camera, ((0.0, 0.0, 0.0), (12.0, 0.0, 0.0)))
        for _ in range(30):
            camera.note_focus_burial(delta_seconds=1.0 / 60.0, **self.BURIED)
        engaged = camera.focus_rescue_blend
        self.assertGreater(engaged, 0.8)
        # Release is deliberately delayed by the hold: a sustained burst backs
        # the hold off toward its cap, so three seconds of calm must NOT be
        # enough to let the focus fall back onto the obstacle.
        for _ in range(180):
            camera.note_focus_burial(delta_seconds=1.0 / 60.0, **self.CLEAR)
        self.assertGreater(camera.focus_rescue_blend, 0.5)
        for _ in range(60 * 20):
            camera.note_focus_burial(delta_seconds=1.0 / 60.0, **self.CLEAR)
        self.assertEqual(camera.focus_rescue_blend, 0.0)

    def test_collapsed_boom_alone_engages(self):
        camera = rescue_camera()
        seed_party(camera, ((0.0, 0.0, 0.0), (12.0, 0.0, 0.0)))
        for _ in range(30):
            camera.note_focus_burial(
                actual_boom=12.0,
                native_boom=2.0,
                native_back_on_wall=False,
                delta_seconds=1.0 / 60.0,
            )
        self.assertGreater(camera.focus_rescue_blend, 0.8)

    def test_focus_moves_toward_the_nearest_actor_only(self):
        camera = rescue_camera()
        party = seed_party(camera, ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)))
        for _ in range(60):
            camera.note_focus_burial(delta_seconds=1.0 / 60.0, **self.BURIED)
        plain = camera._shared_focus_target(*party)
        rescued = camera._rescued_focus(plain, party)
        # Y is deliberately preserved; only the horizontal plane escapes.
        self.assertEqual(rescued[1], plain[1])
        self.assertGreater(abs(rescued[0] - plain[0]), 1.0)
        # It may approach an actor but must never overshoot past one.
        self.assertGreaterEqual(rescued[0], 0.0)
        self.assertLessEqual(rescued[0], 10.0)

    def test_pull_is_capped_by_tuning(self):
        camera = rescue_camera()
        party = seed_party(camera, ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)))
        for _ in range(600):
            camera.note_focus_burial(delta_seconds=1.0 / 60.0, **self.BURIED)
        plain = camera._shared_focus_target(*party)
        rescued = camera._rescued_focus(plain, party)
        travelled = abs(rescued[0] - plain[0]) / abs(plain[0] - 0.0)
        self.assertLessEqual(travelled, camera.tuning.focus_rescue_max_pull + 1e-6)

    def test_players_standing_together_are_never_rescued(self):
        camera = rescue_camera()
        party = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
        for _ in range(60):
            camera.note_focus_burial(delta_seconds=1.0 / 60.0, **self.BURIED)
        plain = camera._shared_focus_target(*party)
        self.assertEqual(camera._rescued_focus(plain, party), plain)

    def test_disabling_the_tuning_restores_the_old_behaviour(self):
        from dataclasses import replace

        camera = rescue_camera()
        camera.tuning = replace(camera.tuning, focus_rescue_max_pull=0.0)
        party = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0))
        for _ in range(60):
            camera.note_focus_burial(delta_seconds=1.0 / 60.0, **self.BURIED)
        plain = camera._shared_focus_target(*party)
        self.assertEqual(camera._rescued_focus(plain, party), plain)

    def test_three_players_rescue_toward_the_closest_member(self):
        camera = rescue_camera()
        party = seed_party(camera, ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (5.5, 0.0, 1.0)))
        for _ in range(60):
            camera.note_focus_burial(delta_seconds=1.0 / 60.0, **self.BURIED)
        plain = camera._shared_focus_target(*party)
        rescued = camera._rescued_focus(plain, party)
        self.assertGreater(camera.focus_rescue_offset, 0.0)
        self.assertLess(
            math.hypot(rescued[0] - 5.5, rescued[2] - 1.0),
            math.hypot(plain[0] - 5.5, plain[2] - 1.0),
        )


class FocusRescueOscillationTests(unittest.TestCase):
    """The reported failure: the camera kept probing the same rock forever.

    The burial signal disappears whenever the correction is applied, so a
    controller that releases on absence alone re-buries the focus and pumps.
    These simulate exactly that world and require convergence.
    """

    def simulate(self, seconds: float = 12.0, dt: float = 1.0 / 60.0):
        """World model: the focus is buried whenever it is NOT pulled out."""

        camera = rescue_camera()
        party = seed_party(camera, ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)))
        engaged_history = []
        for _ in range(int(seconds / dt)):
            plain = camera._shared_focus_target(*party)
            rescued = camera._rescued_focus(plain, party)
            pulled_out = math.hypot(rescued[0] - plain[0], rescued[2] - plain[2]) > 1.5
            camera.note_focus_burial(
                actual_boom=12.0,
                native_boom=12.0 if pulled_out else 1.0,
                native_back_on_wall=not pulled_out,
                delta_seconds=dt,
            )
            engaged_history.append(pulled_out)
        return camera, engaged_history

    def test_the_correction_latches_instead_of_pumping(self):
        _camera, history = self.simulate()
        # Count how often it flipped between corrected and uncorrected.
        flips = sum(
            1
            for index in range(1, len(history))
            if history[index] != history[index - 1]
        )
        self.assertLessEqual(
            flips, 6, f"correcao oscilou {flips} vezes; deveria travar"
        )

    def test_it_spends_the_window_holding_the_focus_out(self):
        _camera, history = self.simulate()
        # The hold is bounded on purpose, so the correction re-probes now and
        # then instead of biasing the shot forever after the party moves on.
        # What must not happen is the pumping the player reported: the focus
        # stays out for the large majority of the window.
        engaged = sum(1 for value in history if value)
        self.assertGreater(engaged / len(history), 0.7)

    def test_the_hold_backs_off_up_to_the_cap(self):
        camera, _history = self.simulate()
        self.assertGreater(camera.focus_rescue_hold_length, 2.0)
        self.assertLessEqual(
            camera.focus_rescue_hold_length,
            camera.tuning.focus_rescue_hold_max_seconds,
        )

    def test_it_still_releases_once_the_obstacle_is_really_gone(self):
        camera, _history = self.simulate(seconds=6.0)
        self.assertGreater(camera.focus_rescue_blend, 0.5)
        for _ in range(60 * 20):
            camera.note_focus_burial(
                actual_boom=12.0,
                native_boom=12.0,
                native_back_on_wall=False,
                delta_seconds=1.0 / 60.0,
            )
        self.assertEqual(camera.focus_rescue_blend, 0.0)
        self.assertIsNone(camera.focus_rescue_anchor)

    def test_the_anchor_does_not_swap_between_equidistant_players(self):
        camera = rescue_camera()
        party = seed_party(camera, ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)))
        for _ in range(60):
            camera.note_focus_burial(
                actual_boom=12.0,
                native_boom=1.0,
                native_back_on_wall=True,
                delta_seconds=1.0 / 60.0,
            )
        centre = camera._shared_focus_target(*party)
        camera._rescued_focus(centre, party)
        first = camera.focus_rescue_anchor
        for _ in range(120):
            camera._rescued_focus(centre, party)
        self.assertEqual(first, camera.focus_rescue_anchor)


class FocusRescueFalsePositiveTests(unittest.TestCase):
    """A camera touching a wall must never re-frame the party.

    Reported live: the shot drifted onto P1 at random and left P2 off screen
    while the two were close together.  ``back_on_wall`` is a statement about
    the boom, not about the focus, so it may only be acted on when the party
    is wide enough for its midpoint to be somewhere neither player is.
    """

    def engage(self, camera, positions, frames=120):
        for _ in range(frames):
            camera.update_group(positions, 1.0 / 60.0)
            camera.note_focus_burial(
                actual_boom=12.0,
                native_boom=1.0,
                native_back_on_wall=True,
                delta_seconds=1.0 / 60.0,
            )

    def test_close_party_against_a_wall_is_never_rescued(self):
        camera = rescue_camera()
        party = ((0.0, 0.0, 0.0), (3.0, 0.0, 0.0))
        self.engage(camera, party)
        self.assertEqual(camera.focus_rescue_blend, 0.0)
        plain = camera._shared_focus_target(*party)
        self.assertEqual(camera._rescued_focus(plain, party), plain)

    def test_a_genuinely_spread_party_is_still_rescued(self):
        camera = rescue_camera()
        party = ((0.0, 0.0, 0.0), (14.0, 0.0, 0.0))
        self.engage(camera, party)
        self.assertGreater(camera.focus_rescue_blend, 0.5)
        self.assertGreater(camera.focus_rescue_offset, 0.0)

    def test_the_pull_leaves_both_players_inside_the_framing(self):
        camera = rescue_camera()
        party = ((0.0, 0.0, 0.0), (14.0, 0.0, 0.0))
        self.engage(camera, party)
        plain = camera._shared_focus_target(*party)
        rescued = camera._rescued_focus(plain, party)
        # The focus may not travel more than half-way to a player, otherwise
        # the far one falls out of shot.
        travelled = abs(rescued[0] - plain[0]) / 7.0
        self.assertLessEqual(travelled, 0.5 + 1e-6)
