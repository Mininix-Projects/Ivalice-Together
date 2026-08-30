import unittest

from interaction_transaction_model import (
    EXECUTION_ANIMATION_FLAG,
    InteractionLifecycle,
    InteractionPhase,
    InteractionSignals,
    WEAPON_DRAWN_FLAG,
    WEAPON_HANDLING_FLAG,
    follower_interaction_leader_allows,
    interaction_press_allowed,
)


class InteractionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.life = InteractionLifecycle()
        self.life.start(10.0)

    def test_modal_holds_owner_until_control_returns(self):
        decision = self.life.update(
            InteractionSignals(control_enabled=False, modal_active=True), 10.1
        )
        self.assertTrue(decision.hold_owner)
        self.assertEqual(self.life.phase, InteractionPhase.MODAL)
        decision = self.life.update(
            InteractionSignals(control_enabled=True, modal_active=False), 10.2
        )
        self.assertFalse(decision.complete)
        decision = self.life.update(
            InteractionSignals(control_enabled=True, modal_active=False), 10.34
        )
        self.assertTrue(decision.complete)
        self.assertFalse(decision.hold_owner)

    def test_wrong_owner_aborts_before_ghost_modal(self):
        decision = self.life.update(
            InteractionSignals(
                control_enabled=False,
                modal_active=True,
                wrong_owner_has_target=True,
            ),
            10.1,
        )
        self.assertEqual(self.life.phase, InteractionPhase.ABORTING)
        self.assertTrue(decision.hold_owner)
        self.assertTrue(decision.pulse_cancel)
        decision = self.life.update(
            InteractionSignals(control_enabled=True, modal_active=False), 10.5
        )
        self.assertTrue(decision.complete)

    def test_nonmodal_chest_completes_after_actor_and_object_retire(self):
        self.life.update(
            InteractionSignals(owner_has_target=True, owner_busy=True), 10.1
        )
        decision = self.life.update(
            InteractionSignals(private_alive=False, object_alive=False), 10.3
        )
        self.assertFalse(decision.complete)
        decision = self.life.update(
            InteractionSignals(private_alive=False, object_alive=False), 10.44
        )
        self.assertTrue(decision.complete)

    def test_nonmodal_chest_can_retire_between_actor_samples(self):
        # Some instant pickups publish ownership and consume the private
        # descriptor between two 120-Hz helper ticks. Acceptance plus native
        # retirement is authoritative even if no busy animation was sampled.
        self.life.update(
            InteractionSignals(owner_has_target=True, owner_busy=False), 10.1
        )
        decision = self.life.update(
            InteractionSignals(
                control_enabled=True,
                owner_busy=False,
                private_alive=False,
                object_alive=True,
            ),
            10.2,
        )
        self.assertFalse(decision.complete)
        decision = self.life.update(
            InteractionSignals(
                control_enabled=True,
                owner_busy=False,
                private_alive=False,
                object_alive=True,
            ),
            10.34,
        )
        self.assertTrue(decision.complete)

    def test_dispatch_timeout_aborts_without_leaving_owner_held(self):
        decision = self.life.update(InteractionSignals(), 11.51)
        self.assertTrue(decision.complete)
        self.assertFalse(decision.hold_owner)

    def test_loading_releases_stale_generation_immediately(self):
        decision = self.life.update(InteractionSignals(loading=True), 10.1)
        self.assertTrue(decision.complete)
        self.assertFalse(decision.hold_owner)


class InteractionPressPreflightTests(unittest.TestCase):
    def test_fully_idle_unarmed_actor_accepts_press(self):
        self.assertTrue(interaction_press_allowed(0, 1, 0x4021))
        self.assertTrue(interaction_press_allowed(0, 0, 0xFFFF))

    def test_stable_weapon_drawn_press_starts_native_holster_use(self):
        self.assertTrue(interaction_press_allowed(WEAPON_DRAWN_FLAG, 1, 0x4021))

    def test_active_weapon_handling_press_is_discarded(self):
        self.assertFalse(interaction_press_allowed(WEAPON_HANDLING_FLAG, 1, 0x4021))

    def test_holster_or_execution_press_is_discarded(self):
        self.assertFalse(interaction_press_allowed(EXECUTION_ANIMATION_FLAG, 1, 0x4021))
        self.assertFalse(interaction_press_allowed(0, 2, 0x4021))
        self.assertFalse(interaction_press_allowed(0, 1, 0x0096))


class FollowerLeaderAnimationGateTests(unittest.TestCase):
    def test_blocks_weapon_handling_and_hit_reaction(self):
        self.assertFalse(follower_interaction_leader_allows(WEAPON_HANDLING_FLAG))
        self.assertFalse(follower_interaction_leader_allows(EXECUTION_ANIMATION_FLAG))
        self.assertFalse(
            follower_interaction_leader_allows(
                WEAPON_HANDLING_FLAG | EXECUTION_ANIMATION_FLAG
            )
        )

    def test_allows_drawn_stance_and_unrelated_flags(self):
        self.assertTrue(follower_interaction_leader_allows(0))
        self.assertTrue(follower_interaction_leader_allows(WEAPON_DRAWN_FLAG))
        self.assertTrue(follower_interaction_leader_allows(1 << 37))

    def test_busy_action_fields_are_intentionally_not_inputs(self):
        # Attack/magick/item state can remain queued or recovering long after
        # the visible execution bit clears.  With flags clear, every such
        # state/action combination is outside this gate by construction.
        for _state, _action in ((4, 0x96), (7, 0x5B), (10, 0x75)):
            with self.subTest(state=_state, action=_action):
                self.assertTrue(follower_interaction_leader_allows(0))

    def test_temporal_gate_reopens_immediately_after_each_animation(self):
        samples = (
            0,
            WEAPON_DRAWN_FLAG,
            WEAPON_HANDLING_FLAG,
            0,
            EXECUTION_ANIMATION_FLAG,
            0,
        )
        self.assertEqual(
            [follower_interaction_leader_allows(flags) for flags in samples],
            [True, True, False, True, False, True],
        )

    def test_idle_leader_never_degrades_into_a_one_frame_window(self):
        self.assertTrue(all(follower_interaction_leader_allows(0) for _ in range(240)))


if __name__ == "__main__":
    unittest.main()
