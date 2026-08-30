import sys
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import coop_runtime as runtime  # noqa: E402
import follow_handoff as handoff  # noqa: E402


def token(role, member, actor, generation=1, epoch=1):
    return handoff.OwnerToken(
        role,
        member,
        actor,
        actor + 0x1000,
        generation,
        epoch,
        ((generation & 0xFFFF) << 16) | (member & 0xFFFF),
    )


def observation(owner, sample=1, **changes):
    values = dict(
        token=owner,
        sample=sample,
        available=True,
        alive=True,
        materialized=True,
        layout_coherent=True,
        leader_member_id=0,
        cutscene=False,
        action_state=1,
        target=0,
        action=handoff.NATIVE_IDLE_ACTION,
        descriptor_action=handoff.FOLLOW_ACTION,
        follows_native_list=True,
        status_blocked=False,
    )
    values.update(changes)
    return handoff.FollowerObservation(**values)


class FollowHandoffPolicyTests(unittest.TestCase):
    def test_old_is_disarmed_and_cleared_before_new_commit(self):
        old = token("p2", 5, 0x10000)
        new = token("p2", 1, 0x20000)
        state = handoff.HandoffState((("p2", old),), ())

        state, effects = handoff.reconcile_owners(
            state,
            {"p2": new},
            acquire_eligible={"p2": True},
            observations={old.incarnation_key: observation(old)},
            now=10.0,
            world_epoch=1,
        )

        self.assertEqual(
            [effect.kind for effect in effects],
            [
                handoff.EffectKind.DISARM_OLD,
                handoff.EffectKind.CLEAR_OLD_METADATA,
                handoff.EffectKind.REQUEST_NATIVE_FOLLOW,
                handoff.EffectKind.ACQUIRE_NEW,
            ],
        )
        self.assertEqual(state.owner_map()["p2"], new)
        self.assertEqual(state.repairs[0].attempts, 1)

    def test_two_role_swap_releases_every_old_role_before_any_acquire(self):
        old_p2 = token("p2", 5, 0x10000)
        old_p3 = token("p3", 1, 0x20000)
        # Re-role the same incarnations without changing their generation.
        new_p2 = handoff.OwnerToken(
            "p2",
            old_p3.member_id,
            old_p3.actor,
            old_p3.phys,
            old_p3.actor_generation,
            old_p3.world_epoch,
        )
        new_p3 = handoff.OwnerToken(
            "p3",
            old_p2.member_id,
            old_p2.actor,
            old_p2.phys,
            old_p2.actor_generation,
            old_p2.world_epoch,
        )
        state = handoff.HandoffState((("p2", old_p2), ("p3", old_p3)), ())

        state, effects = handoff.reconcile_owners(
            state,
            {"p2": new_p2, "p3": new_p3},
            acquire_eligible={"p2": True, "p3": True},
            observations={},
            now=0.0,
            world_epoch=1,
        )

        kinds = [item.kind for item in effects]
        first_acquire = kinds.index(handoff.EffectKind.ACQUIRE_NEW)
        self.assertTrue(
            all(
                kind
                in {
                    handoff.EffectKind.DISARM_OLD,
                    handoff.EffectKind.CLEAR_OLD_METADATA,
                }
                for kind in kinds[:first_acquire]
            )
        )
        self.assertNotIn(handoff.EffectKind.REQUEST_NATIVE_FOLLOW, kinds)
        self.assertEqual(state.repairs, ())

    def test_rejected_direct_swap_acquire_restores_repair_across_mapping_gap(self):
        old_p2 = token("p2", 5, 0x10000)
        old_p3 = token("p3", 1, 0x20000)
        new_p2 = handoff.OwnerToken(
            "p2",
            old_p3.member_id,
            old_p3.actor,
            old_p3.phys,
            old_p3.actor_generation,
            old_p3.world_epoch,
        )
        new_p3 = handoff.OwnerToken(
            "p3",
            old_p2.member_id,
            old_p2.actor,
            old_p2.phys,
            old_p2.actor_generation,
            old_p2.world_epoch,
        )
        state = handoff.HandoffState((("p2", old_p2), ("p3", old_p3)), ())

        state, effects = handoff.reconcile_owners(
            state,
            {"p2": new_p2, "p3": new_p3},
            acquire_eligible={"p2": True, "p3": True},
            observations={},
            now=0.0,
            world_epoch=1,
        )
        failed = next(
            effect
            for effect in effects
            if effect.kind == handoff.EffectKind.ACQUIRE_NEW and effect.token == new_p2
        )
        self.assertEqual(failed.reason, "transfer")

        state = handoff.reject_acquisition(
            state,
            new_p2,
            restore_repair=True,
            now=0.01,
        )
        state, gap_effects = handoff.reconcile_owners(
            state,
            {"p2": None, "p3": new_p3},
            acquire_eligible={"p2": False, "p3": True},
            observations={},
            now=0.02,
            world_epoch=1,
        )
        state, retry_effects = handoff.advance_repairs(
            state,
            {},
            now=0.02,
            world_epoch=1,
        )

        self.assertEqual(gap_effects, ())
        self.assertEqual(retry_effects, ())
        self.assertNotIn("p2", state.owner_map())
        self.assertEqual(state.owner_map()["p3"], new_p3)
        self.assertEqual(tuple(item.token for item in state.repairs), (new_p2,))

    def test_boundary_rejection_removes_only_the_matching_acquisition(self):
        owner = token("p2", 5, 0x10000)
        other = token("p3", 1, 0x20000)
        state = handoff.HandoffState((("p2", owner), ("p3", other)), ())

        state = handoff.reject_acquisition(state, owner)

        self.assertNotIn("p2", state.owner_map())
        self.assertEqual(state.owner_map()["p3"], other)

    def test_reacquire_cancels_a_pending_repair_for_same_incarnation(self):
        owner = token("p2", 5, 0x10000)
        pending = handoff.FollowRepair(
            owner,
            created_at=0.0,
            deadline=1.0,
            next_attempt_at=0.0,
        )
        state = handoff.HandoffState((), (pending,))

        state, effects = handoff.reconcile_owners(
            state,
            {"p2": owner},
            acquire_eligible={"p2": True},
            observations={owner.incarnation_key: observation(owner)},
            now=0.1,
            world_epoch=1,
        )

        self.assertEqual(state.repairs, ())
        self.assertEqual(
            [effect.kind for effect in effects],
            [handoff.EffectKind.ACQUIRE_NEW],
        )

    def test_current_owner_also_cancels_any_stale_repair_for_its_incarnation(self):
        owner = token("p2", 5, 0x10000)
        pending = handoff.FollowRepair(owner, 0.0, 1.0)
        state = handoff.HandoffState((("p2", owner),), (pending,))

        state, effects = handoff.reconcile_owners(
            state,
            {"p2": owner},
            acquire_eligible={"p2": True},
            observations={owner.incarnation_key: observation(owner)},
            now=0.1,
            world_epoch=1,
        )

        self.assertEqual(state.owner_map(), {"p2": owner})
        self.assertEqual(state.repairs, ())
        self.assertEqual(effects, ())

    def test_follow_confirmation_must_be_from_a_later_sample(self):
        old = token("p2", 5, 0x10000)
        state = handoff.HandoffState((("p2", old),), ())
        state, _effects = handoff.reconcile_owners(
            state,
            {"p2": None},
            acquire_eligible={"p2": False},
            observations={old.incarnation_key: observation(old, sample=4)},
            now=0.0,
            world_epoch=1,
        )
        confirmed_same_sample = observation(
            old,
            sample=4,
            action=handoff.FOLLOW_ACTION,
        )

        state, effects = handoff.advance_repairs(
            state,
            {old.incarnation_key: confirmed_same_sample},
            now=0.01,
            world_epoch=1,
        )
        self.assertEqual(effects, ())
        self.assertEqual(len(state.repairs), 1)

        confirmed_later = observation(
            old,
            sample=5,
            action=handoff.FOLLOW_ACTION,
        )
        state, effects = handoff.advance_repairs(
            state,
            {old.incarnation_key: confirmed_later},
            now=0.02,
            world_epoch=1,
        )
        self.assertEqual(effects[0].kind, handoff.EffectKind.CONFIRM_NATIVE_FOLLOW)
        self.assertEqual(state.repairs, ())

    def test_actor_already_in_native_follow_confirms_without_a_mod_request(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(owner, 0.0, 1.0)
        state = handoff.HandoffState((), (repair,))

        state, effects = handoff.advance_repairs(
            state,
            {
                owner.incarnation_key: observation(
                    owner,
                    sample=7,
                    action=handoff.FOLLOW_ACTION,
                )
            },
            now=0.2,
            world_epoch=1,
        )

        self.assertEqual(state.repairs, ())
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].kind, handoff.EffectKind.CONFIRM_NATIVE_FOLLOW)
        self.assertEqual(effects[0].reason, "already_native")

    def test_retry_is_four_attempts_and_one_second_bounded(self):
        old = token("p2", 5, 0x10000)
        state = handoff.HandoffState((("p2", old),), ())
        state, effects = handoff.reconcile_owners(
            state,
            {"p2": None},
            acquire_eligible={"p2": False},
            observations={old.incarnation_key: observation(old, sample=1)},
            now=0.0,
            world_epoch=1,
        )
        requests = sum(
            item.kind == handoff.EffectKind.REQUEST_NATIVE_FOLLOW for item in effects
        )
        for index, now in enumerate((0.1, 0.2, 0.3, 0.4), start=2):
            state, effects = handoff.advance_repairs(
                state,
                {old.incarnation_key: observation(old, sample=index)},
                now=now,
                world_epoch=1,
            )
            requests += sum(
                item.kind == handoff.EffectKind.REQUEST_NATIVE_FOLLOW
                for item in effects
            )

        # Keep the fourth request pending for one later native observation so
        # it can still be confirmed; the following due tick enforces the cap.
        state, effects = handoff.advance_repairs(
            state,
            {old.incarnation_key: observation(old, sample=6)},
            now=0.5,
            world_epoch=1,
        )

        self.assertEqual(requests, 4)
        self.assertEqual(state.repairs, ())
        self.assertEqual(effects[-1].kind, handoff.EffectKind.CANCEL_NATIVE_FOLLOW)

    def test_fourth_request_keeps_its_full_ack_window(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(
            owner,
            created_at=0.0,
            deadline=1.0,
            attempts=handoff.FOLLOW_REPAIR_MAX_ATTEMPTS,
            next_attempt_at=0.4,
            requested_sample=4,
        )

        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (repair,)),
            {owner.incarnation_key: observation(owner, sample=5)},
            now=0.31,
            world_epoch=1,
        )

        self.assertEqual(state.repairs, (repair,))
        self.assertEqual(effects, ())

    def test_fourth_request_can_be_acknowledged_before_its_window_ends(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(
            owner,
            created_at=0.0,
            deadline=1.0,
            attempts=handoff.FOLLOW_REPAIR_MAX_ATTEMPTS,
            next_attempt_at=0.4,
            requested_sample=4,
        )

        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (repair,)),
            {
                owner.incarnation_key: observation(
                    owner,
                    sample=5,
                    action=handoff.FOLLOW_ACTION,
                )
            },
            now=0.31,
            world_epoch=1,
        )

        self.assertEqual(state.repairs, ())
        self.assertEqual(effects[0].kind, handoff.EffectKind.CONFIRM_NATIVE_FOLLOW)

    def test_reserve_cutscene_and_epoch_cancel(self):
        cases = (
            dict(materialized=False),
            dict(layout_coherent=False),
            dict(cutscene=True),
        )
        for changes in cases:
            with self.subTest(changes=changes):
                old = token("p2", 5, 0x10000)
                repair = handoff.FollowRepair(old, 0.0, 1.0)
                state = handoff.HandoffState((), (repair,))
                state, effects = handoff.advance_repairs(
                    state,
                    {old.incarnation_key: observation(old, **changes)},
                    now=0.1,
                    world_epoch=1,
                )
                self.assertEqual(state.repairs, ())
                self.assertEqual(
                    effects[0].kind,
                    handoff.EffectKind.CANCEL_NATIVE_FOLLOW,
                )

        old = token("p2", 5, 0x10000)
        dead = observation(old, alive=False, status_blocked=True)
        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (handoff.FollowRepair(old, 0.0, 1.0),)),
            {old.incarnation_key: dead},
            now=1000.0,
            world_epoch=1,
        )
        self.assertEqual(effects, ())
        self.assertEqual(state.repairs[0].attempts, 0)
        self.assertTrue(state.repairs[0].passive_deferral_observed)

        old = token("p2", 5, 0x10000)
        state = handoff.HandoffState((), (handoff.FollowRepair(old, 0.0, 1.0),))
        state, effects = handoff.advance_repairs(state, {}, now=0.1, world_epoch=2)
        self.assertEqual(state.repairs, ())
        self.assertEqual(effects[0].reason, "world_epoch")

        reincarnated = token("p2", 5, 0x10000, generation=2)
        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (handoff.FollowRepair(old, 0.0, 1.0),)),
            {old.incarnation_key: observation(reincarnated)},
            now=0.1,
            world_epoch=1,
        )
        self.assertEqual(state.repairs, ())
        self.assertEqual(effects[0].reason, "generation")

    def test_current_leader_waits_passively_then_follows_after_leader_change(self):
        owner = token("p2", 5, 0x10000)
        current_leader = observation(
            owner,
            leader_member_id=owner.member_id,
            action=0x4999,
            follows_native_list=False,
        )
        state, effects = handoff.advance_repairs(
            handoff.HandoffState(
                (),
                (handoff.FollowRepair(owner, 0.0, 1.0),),
            ),
            {owner.incarnation_key: current_leader},
            now=1000.0,
            world_epoch=1,
        )

        self.assertEqual(effects, ())
        self.assertEqual(state.repairs[0].attempts, 0)
        self.assertTrue(state.repairs[0].passive_deferral_observed)

        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: observation(owner, sample=2)},
            now=1000.1,
            world_epoch=1,
        )
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (handoff.EffectKind.REQUEST_NATIVE_FOLLOW,),
        )

    def test_protected_action_waits_then_requests_and_confirms_follow(self):
        old = token("p2", 5, 0x10000)
        protected = observation(
            old,
            sample=1,
            action_state=2,
            target=0x1234,
            action=0x4999,
            descriptor_action=0x4888,
            follows_native_list=False,
        )
        state = handoff.HandoffState((("p2", old),), ())

        state, effects = handoff.reconcile_owners(
            state,
            {"p2": None},
            acquire_eligible={"p2": False},
            observations={old.incarnation_key: protected},
            now=0.0,
            world_epoch=1,
        )

        self.assertEqual(
            [effect.kind for effect in effects],
            [
                handoff.EffectKind.DISARM_OLD,
                handoff.EffectKind.CLEAR_OLD_METADATA,
            ],
        )
        self.assertEqual(len(state.repairs), 1)
        self.assertEqual(state.repairs[0].attempts, 0)

        state, effects = handoff.advance_repairs(
            state,
            {old.incarnation_key: observation(old, sample=2, target=0x1234)},
            now=0.1,
            world_epoch=1,
        )
        self.assertEqual(effects, ())
        self.assertEqual(len(state.repairs), 1)
        self.assertEqual(state.repairs[0].attempts, 0)

        class NoProcessIO:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected process I/O: {name}")

        runtime.apply_follower_handoff_effects(
            NoProcessIO(),
            effects,
            {old.incarnation_key: protected},
        )

        safe = observation(old, sample=3)
        state, effects = handoff.advance_repairs(
            state,
            {old.incarnation_key: safe},
            now=0.2,
            world_epoch=1,
        )
        self.assertEqual(
            [effect.kind for effect in effects],
            [handoff.EffectKind.REQUEST_NATIVE_FOLLOW],
        )
        self.assertEqual(state.repairs[0].attempts, 1)
        with mock.patch.object(
            runtime,
            "resume_sanitized_follower_action",
            return_value=True,
        ) as resume:
            runtime.apply_follower_handoff_effects(
                object(),
                effects,
                {old.incarnation_key: safe},
            )
        resume.assert_called_once_with(
            mock.ANY,
            old.actor,
            role=old.role,
            handle=old.native_handle,
            expected_phys=old.phys,
        )

        followed = observation(
            old,
            sample=4,
            action=handoff.FOLLOW_ACTION,
        )
        state, effects = handoff.advance_repairs(
            state,
            {old.incarnation_key: followed},
            now=0.21,
            world_epoch=1,
        )
        self.assertEqual(state.repairs, ())
        self.assertEqual(
            [effect.kind for effect in effects],
            [handoff.EffectKind.CONFIRM_NATIVE_FOLLOW],
        )

    def test_each_valid_action_divergence_waits_without_spending_an_attempt(self):
        cases = (
            dict(available=False),
            dict(action_state=2),
            dict(target=0x1234),
            dict(action=0x4999),
            dict(descriptor_action=0x4888),
            dict(follows_native_list=False),
        )
        for changes in cases:
            with self.subTest(changes=changes):
                owner = token("p2", 5, 0x10000)
                repair = handoff.FollowRepair(owner, 0.0, 1.0)
                state, effects = handoff.advance_repairs(
                    handoff.HandoffState((), (repair,)),
                    {owner.incarnation_key: observation(owner, **changes)},
                    now=0.1,
                    world_epoch=1,
                )

                self.assertEqual(state.repairs[0].attempts, 0)
                self.assertEqual(effects, ())

    def test_protected_action_expires_only_at_the_bounded_deadline(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(owner, 0.0, 1.0)
        protected = observation(owner, action=0x4999, target=0x1234)

        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (repair,)),
            {owner.incarnation_key: protected},
            now=0.999,
            world_epoch=1,
        )
        self.assertEqual(state.repairs, (repair,))
        self.assertEqual(effects, ())

        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: protected},
            now=1.0,
            world_epoch=1,
        )
        self.assertEqual(state.repairs, ())
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].kind, handoff.EffectKind.CANCEL_NATIVE_FOLLOW)
        self.assertEqual(effects[0].reason, "bounded")

    def test_unknown_alive_or_leader_state_waits_without_an_unsafe_request(self):
        for changes in (dict(alive=None), dict(leader_member_id=None)):
            with self.subTest(changes=changes):
                owner = token("p2", 5, 0x10000)
                state = handoff.HandoffState(
                    (),
                    (handoff.FollowRepair(owner, 0.0, 1.0),),
                )
                state, effects = handoff.advance_repairs(
                    state,
                    {owner.incarnation_key: observation(owner, **changes)},
                    now=0.1,
                    world_epoch=1,
                )

                self.assertEqual(len(state.repairs), 1)
                self.assertEqual(effects, ())

    def test_transient_read_gap_cannot_consume_the_only_follow_repair(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(owner, 0.0, 1.0)
        unavailable = observation(owner, available=False, alive=None)

        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (repair,)),
            {owner.incarnation_key: unavailable},
            now=5.0,
            world_epoch=1,
        )

        self.assertEqual(effects, ())
        self.assertEqual(len(state.repairs), 1)
        self.assertEqual(state.repairs[0].deadline, 6.0)

        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: observation(owner, sample=2)},
            now=5.1,
            world_epoch=1,
        )
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (handoff.EffectKind.REQUEST_NATIVE_FOLLOW,),
        )
        self.assertEqual(state.repairs[0].attempts, 1)

    def test_command_blocking_status_keeps_only_passive_exact_generation_intent(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(owner, 0.0, 1.0)
        blocked = observation(
            owner,
            status_blocked=True,
        )

        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (repair,)),
            {owner.incarnation_key: blocked},
            now=60.0,
            world_epoch=1,
        )

        self.assertEqual(effects, ())
        self.assertEqual(state.repairs[0].attempts, 0)
        self.assertEqual(state.repairs[0].deadline, 61.0)
        self.assertFalse(blocked.safe_to_request)

        changed = token("p2", 5, 0x10000, generation=2)
        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: observation(changed)},
            now=60.1,
            world_epoch=1,
        )
        self.assertEqual(state.repairs, ())
        self.assertEqual(effects[0].reason, "generation")

    def test_status_blocked_follow_action_is_not_a_false_confirmation(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(owner, 0.0, 1.0)
        blocked = observation(
            owner,
            sample=7,
            action=handoff.FOLLOW_ACTION,
            status_blocked=True,
        )

        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (repair,)),
            {owner.incarnation_key: blocked},
            now=100.0,
            world_epoch=1,
        )

        self.assertFalse(blocked.confirmed)
        self.assertEqual(effects, ())
        self.assertEqual(state.repairs[0].attempts, 0)

        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: observation(owner, sample=8)},
            now=100.1,
            world_epoch=1,
        )
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (handoff.EffectKind.REQUEST_NATIVE_FOLLOW,),
        )

    def test_ko_can_outlive_arbitrary_deadlines_then_resume_exact_follow(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(owner, 0.0, 1.0)
        dead = observation(
            owner,
            sample=1,
            alive=False,
            status_blocked=True,
            action_state=4,
            action=0x4999,
            follows_native_list=False,
        )

        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (repair,)),
            {owner.incarnation_key: dead},
            now=1_000_000.0,
            world_epoch=1,
        )
        self.assertEqual(effects, ())
        self.assertEqual(state.repairs[0].attempts, 0)

        revived_but_settling = observation(
            owner,
            sample=2,
            action_state=4,
            action=0x4999,
            follows_native_list=False,
        )
        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: revived_but_settling},
            now=2_000_000.0,
            world_epoch=1,
        )
        self.assertEqual(effects, ())
        self.assertEqual(state.repairs[0].attempts, 0)

        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: observation(owner, sample=3)},
            now=2_000_000.1,
            world_epoch=1,
        )
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (handoff.EffectKind.REQUEST_NATIVE_FOLLOW,),
        )
        self.assertEqual(state.repairs[0].attempts, 1)

    def test_status_interruption_starts_a_new_bounded_attempt_window(self):
        owner = token("p2", 5, 0x10000)
        exhausted = handoff.FollowRepair(
            owner,
            created_at=0.0,
            deadline=1.0,
            attempts=handoff.FOLLOW_REPAIR_MAX_ATTEMPTS,
            next_attempt_at=0.4,
            requested_sample=4,
        )
        blocked = observation(owner, sample=5, status_blocked=True)

        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (exhausted,)),
            {owner.incarnation_key: blocked},
            now=100.0,
            world_epoch=1,
        )
        self.assertEqual(effects, ())
        self.assertEqual(
            state.repairs[0].attempts,
            handoff.FOLLOW_REPAIR_MAX_ATTEMPTS,
        )

        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: observation(owner, sample=6)},
            now=100.1,
            world_epoch=1,
        )
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (handoff.EffectKind.REQUEST_NATIVE_FOLLOW,),
        )
        self.assertEqual(state.repairs[0].attempts, 1)

    def test_missing_native_handle_never_consumes_a_request_attempt(self):
        owner = handoff.OwnerToken("p2", 5, 0x10000, 0x11000, 1, 1, 0)
        repair = handoff.FollowRepair(owner, 0.0, 1.0)

        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (repair,)),
            {owner.incarnation_key: observation(owner)},
            now=0.5,
            world_epoch=1,
        )

        self.assertEqual(effects, ())
        self.assertEqual(state.repairs[0].attempts, 0)
        self.assertFalse(observation(owner).safe_to_request)

    def test_status_clear_gets_a_full_action_settle_window_at_old_deadline(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(owner, 0.0, 1.0)
        blocked = observation(
            owner,
            action=0x4999,
            target=0x1234,
            status_blocked=True,
        )
        state, effects = handoff.advance_repairs(
            handoff.HandoffState((), (repair,)),
            {owner.incarnation_key: blocked},
            now=0.99,
            world_epoch=1,
        )
        self.assertEqual(effects, ())
        self.assertTrue(state.repairs[0].passive_deferral_observed)

        clearing = observation(
            owner,
            action=0x4999,
            target=0x1234,
            status_blocked=False,
        )
        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: clearing},
            now=1.0,
            world_epoch=1,
        )
        self.assertEqual(effects, ())
        self.assertEqual(state.repairs[0].deadline, 2.0)
        self.assertTrue(state.repairs[0].passive_deferral_observed)

        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: clearing},
            now=100.0,
            world_epoch=1,
        )
        self.assertEqual(effects, ())
        self.assertEqual(state.repairs[0].attempts, 0)
        self.assertTrue(state.repairs[0].passive_deferral_observed)

        state, effects = handoff.advance_repairs(
            state,
            {owner.incarnation_key: observation(owner, sample=2)},
            now=100.1,
            world_epoch=1,
        )
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (handoff.EffectKind.REQUEST_NATIVE_FOLLOW,),
        )

    def test_ineligible_role_transfer_keeps_old_incarnation_repairable(self):
        old = token("p2", 5, 0x10000)
        transferred = handoff.OwnerToken(
            "p3",
            old.member_id,
            old.actor,
            old.phys,
            old.actor_generation,
            old.world_epoch,
        )
        state, effects = handoff.reconcile_owners(
            handoff.HandoffState((("p2", old),), ()),
            {"p2": None, "p3": transferred},
            acquire_eligible={"p2": False, "p3": False},
            observations={
                old.incarnation_key: observation(
                    old,
                    available=False,
                    alive=None,
                )
            },
            now=0.0,
            world_epoch=1,
        )

        self.assertEqual(state.owner_map(), {})
        self.assertEqual(tuple(item.token for item in state.repairs), (old,))
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (
                handoff.EffectKind.DISARM_OLD,
                handoff.EffectKind.CLEAR_OLD_METADATA,
            ),
        )

        state, effects = handoff.advance_repairs(
            state,
            {old.incarnation_key: observation(old, sample=2)},
            now=0.1,
            world_epoch=1,
        )
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (handoff.EffectKind.REQUEST_NATIVE_FOLLOW,),
        )

    def test_shutdown_advances_a_pending_repair_even_without_an_owner(self):
        owner = token("p2", 5, 0x10000)
        repair = handoff.FollowRepair(owner, 0.0, 1.0)
        state = handoff.HandoffState((), (repair,))
        process = object()
        observed = observation(owner, sample=9)

        with (
            mock.patch.object(
                runtime,
                "observe_follower_handoff",
                return_value=observed,
            ) as observe,
            mock.patch.object(
                runtime,
                "resume_sanitized_follower_action",
                return_value=True,
            ) as resume,
            mock.patch.object(
                runtime,
                "apply_follower_handoff_effects",
                wraps=runtime.apply_follower_handoff_effects,
            ) as apply_effects,
        ):
            final_state = runtime.drain_follower_handoff_for_shutdown(
                process,
                state,
                object(),
                {},
                world_epoch=1,
                native_sample=8,
                now=0.2,
            )

        observe.assert_called_once_with(process, owner, mock.ANY, {}, 9)
        resume.assert_called_once_with(
            process,
            owner.actor,
            role=owner.role,
            handle=owner.native_handle,
            expected_phys=owner.phys,
        )
        effects = apply_effects.call_args.args[1]
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (
                handoff.EffectKind.REQUEST_NATIVE_FOLLOW,
                handoff.EffectKind.CANCEL_NATIVE_FOLLOW,
            ),
        )
        self.assertEqual(effects[-1].reason, "shutdown")
        self.assertEqual(final_state.owner_map(), {})
        self.assertEqual(final_state.repairs, ())


class GenerationTests(unittest.TestCase):
    def test_stone_blocks_movement_without_killing_or_reincarnating_owner(self):
        actor = 0x1000
        battle_keep = 0x5000

        class StatusProcess:
            status = 1 << 1

            def u64(self, address):
                return {actor + 0x698: battle_keep}[address]

            def u32(self, address):
                if address == battle_keep + 0x48:
                    return 100
                if address == battle_keep + 0x64:
                    return self.status
                if address == battle_keep + 0x3C:
                    return 0
                raise KeyError(address)

        process = StatusProcess()
        self.assertIs(runtime.actor_is_alive(process, actor), True)
        self.assertIs(runtime.actor_accepts_manual_movement(process, actor), False)

        tracker = handoff.ActorGenerationTracker()
        petrified = tracker.observe(
            "p2",
            5,
            actor,
            actor + 0x1000,
            world_epoch=1,
            alive=runtime.actor_is_alive(process, actor),
            structurally_present=True,
        )
        process.status = 0
        recovered = tracker.observe(
            "p2",
            5,
            actor,
            actor + 0x1000,
            world_epoch=1,
            alive=runtime.actor_is_alive(process, actor),
            structurally_present=True,
        )

        self.assertEqual(recovered.actor_generation, petrified.actor_generation)

    def test_transient_scan_gap_does_not_advance_generation(self):
        tracker = handoff.ActorGenerationTracker()
        first = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )
        self.assertIsNone(
            tracker.observe(
                "p2",
                5,
                0,
                0,
                world_epoch=1,
                alive=None,
                structurally_present=True,
            )
        )
        recovered = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )
        self.assertEqual(recovered.actor_generation, first.actor_generation)

    def test_transient_zero_handle_sample_preserves_generation_and_handle(self):
        tracker = handoff.ActorGenerationTracker()
        first = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            native_handle=0x00100019,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )

        unknown = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            native_handle=0,
            world_epoch=1,
            alive=None,
            structurally_present=True,
        )

        self.assertEqual(unknown.actor_generation, first.actor_generation)
        self.assertEqual(unknown.native_handle, first.native_handle)

    def test_nonzero_handle_generation_change_advances_actor_generation(self):
        tracker = handoff.ActorGenerationTracker()
        first = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            native_handle=0x00100019,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )
        changed = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            native_handle=0x00200019,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )

        self.assertGreater(changed.actor_generation, first.actor_generation)
        self.assertEqual(changed.native_handle, 0x00200019)

    def test_reserve_pointer_handle_and_epoch_changes_advance_generation(self):
        tracker = handoff.ActorGenerationTracker()
        first = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )
        tracker.observe(
            "p2",
            5,
            0,
            0,
            world_epoch=1,
            alive=None,
            structurally_present=False,
        )
        reserve_return = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )
        dead = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            world_epoch=1,
            alive=False,
            structurally_present=True,
        )
        revived = tracker.observe(
            "p2",
            5,
            0x1000,
            0x2000,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )
        moved = tracker.observe(
            "p2",
            5,
            0x3000,
            0x4000,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )
        new_world = tracker.observe(
            "p2",
            5,
            0x3000,
            0x4000,
            world_epoch=2,
            alive=True,
            structurally_present=True,
        )

        self.assertGreater(reserve_return.actor_generation, first.actor_generation)
        self.assertEqual(dead.actor_generation, reserve_return.actor_generation)
        self.assertEqual(revived.actor_generation, dead.actor_generation)
        self.assertGreater(moved.actor_generation, revived.actor_generation)
        self.assertGreater(new_world.actor_generation, moved.actor_generation)

    def test_world_epoch_changes_after_invalid_boundary_even_same_location(self):
        tracker = handoff.WorldEpochTracker()
        self.assertEqual(tracker.observe((10,), valid=True), 1)
        self.assertEqual(tracker.observe(None, valid=False), 1)
        self.assertEqual(tracker.observe((10,), valid=True), 2)


class HandoffEffectBoundaryTests(unittest.TestCase):
    class Process:
        def __init__(self, owner, current_phys, pair=(0.75, 1.0)):
            self.owner = owner
            self.actor = owner.actor
            self.root = owner.actor + 0x100000
            self.current_phys = current_phys
            self.pair = pair
            self.heading_reads = []
            self.pointer_reads = []

        def u32(self, address):
            if address == self.actor + 0x08:
                return self.owner.native_handle
            raise KeyError(address)

        def u64(self, address):
            self.pointer_reads.append(address)
            if address == self.actor + 0x10:
                return self.root
            if address == self.root + 0xB8:
                return self.current_phys
            raise KeyError(address)

        def read(self, address, size):
            self.heading_reads.append((address, size))
            return struct.pack("<2f", *self.pair)

    def test_acquire_revalidates_actor_phys_and_rejects_pointer_reuse(self):
        owner = token("p2", 5, 0x10000)
        process = self.Process(owner, owner.phys + 0x100)

        headings, rejected = runtime.apply_follower_handoff_effects(
            process,
            (handoff.HandoffEffect(handoff.EffectKind.ACQUIRE_NEW, owner),),
            {},
        )

        self.assertEqual(
            process.pointer_reads,
            [owner.actor + 0x10, process.root + 0xB8],
        )
        self.assertEqual(headings, {})
        self.assertEqual(rejected, (owner,))
        self.assertEqual(process.heading_reads, [])

    def test_acquire_rejects_a_reused_handle_before_pointer_or_heading_read(self):
        owner = token("p2", 5, 0x18000)
        process = self.Process(owner, owner.phys)

        def changed_handle(address):
            if address == owner.actor + 0x08:
                return owner.native_handle + 0x10000
            raise KeyError(address)

        process.u32 = changed_handle
        headings, rejected = runtime.apply_follower_handoff_effects(
            process,
            (handoff.HandoffEffect(handoff.EffectKind.ACQUIRE_NEW, owner),),
            {},
        )

        self.assertEqual(headings, {})
        self.assertEqual(rejected, (owner,))
        self.assertEqual(process.pointer_reads, [])
        self.assertEqual(process.heading_reads, [])

    def test_acquire_without_a_provable_native_handle_is_rejected(self):
        owner = handoff.OwnerToken("p3", 1, 0x19000, 0x1A000, 1, 1, 0)
        process = self.Process(owner, owner.phys)

        headings, rejected = runtime.apply_follower_handoff_effects(
            process,
            (handoff.HandoffEffect(handoff.EffectKind.ACQUIRE_NEW, owner),),
            {},
        )

        self.assertEqual(headings, {})
        self.assertEqual(rejected, (owner,))
        self.assertEqual(process.pointer_reads, [])

    def test_request_requires_the_same_complete_observation_token(self):
        owner = token("p2", 5, 0x1B000)
        reused = token("p2", 5, 0x1B000, generation=2)
        effect = handoff.HandoffEffect(
            handoff.EffectKind.REQUEST_NATIVE_FOLLOW,
            owner,
        )

        with mock.patch.object(
            runtime,
            "resume_sanitized_follower_action",
        ) as resume:
            runtime.apply_follower_handoff_effects(
                object(),
                (effect,),
                {owner.incarnation_key: observation(reused)},
            )

        resume.assert_not_called()

    def test_stale_world_epoch_cannot_request_or_acquire(self):
        owner = token("p2", 5, 0x1B800, epoch=1)
        process = self.Process(owner, owner.phys)
        effects = (
            handoff.HandoffEffect(
                handoff.EffectKind.REQUEST_NATIVE_FOLLOW,
                owner,
            ),
            handoff.HandoffEffect(handoff.EffectKind.ACQUIRE_NEW, owner),
        )

        with mock.patch.object(
            runtime,
            "resume_sanitized_follower_action",
        ) as resume:
            headings, rejected = runtime.apply_follower_handoff_effects(
                process,
                effects,
                {owner.incarnation_key: observation(owner)},
                current_world_epoch=2,
            )

        resume.assert_not_called()
        self.assertEqual(headings, {})
        self.assertEqual(rejected, (owner,))
        self.assertEqual(process.pointer_reads, [])
        self.assertEqual(process.heading_reads, [])

    def test_request_cleanup_transport_error_cannot_escape_the_frame(self):
        owner = token("p2", 5, 0x1C000)
        effect = handoff.HandoffEffect(
            handoff.EffectKind.REQUEST_NATIVE_FOLLOW,
            owner,
        )
        with mock.patch.object(
            runtime,
            "resume_sanitized_follower_action",
            side_effect=RuntimeError("ack cleanup raced teardown"),
        ):
            headings, rejected = runtime.apply_follower_handoff_effects(
                object(),
                (effect,),
                {owner.incarnation_key: observation(owner)},
            )

        self.assertEqual(headings, {})
        self.assertEqual(rejected, ())

    def test_acquire_seeds_only_after_actor_phys_revalidation(self):
        owner = token("p3", 1, 0x20000)
        process = self.Process(owner, owner.phys, pair=(-0.5, -0.25))

        headings, rejected = runtime.apply_follower_handoff_effects(
            process,
            (handoff.HandoffEffect(handoff.EffectKind.ACQUIRE_NEW, owner),),
            {},
        )

        self.assertEqual(headings, {"p3": (-0.5, -0.25)})
        self.assertEqual(rejected, ())
        self.assertEqual(process.heading_reads, [(owner.phys + 0xA4, 8)])
        self.assertEqual(
            process.pointer_reads,
            [owner.actor + 0x10, process.root + 0xB8] * 2,
        )

    def test_acquire_is_not_committed_without_its_own_heading_seed(self):
        owner = token("p2", 5, 0x30000)

        class UnreadableHeading(self.Process):
            def read(self, address, size):
                self.heading_reads.append((address, size))
                raise OSError(299, "generation changed")

        process = UnreadableHeading(owner, owner.phys)
        headings, rejected = runtime.apply_follower_handoff_effects(
            process,
            (handoff.HandoffEffect(handoff.EffectKind.ACQUIRE_NEW, owner),),
            {},
        )

        self.assertEqual(headings, {})
        self.assertEqual(rejected, (owner,))

    def test_acquire_rechecks_actor_phys_after_heading_seed(self):
        owner = token("p3", 1, 0x40000)

        class PointerRace(self.Process):
            def __init__(self):
                super().__init__(owner, owner.phys)
                self.phys_reads = 0

            def u64(self, address):
                self.pointer_reads.append(address)
                if address == owner.actor + 0x10:
                    return self.root
                if address == self.root + 0xB8:
                    self.phys_reads += 1
                    return owner.phys if self.phys_reads == 1 else owner.phys + 0x100
                raise KeyError(address)

        process = PointerRace()
        headings, rejected = runtime.apply_follower_handoff_effects(
            process,
            (handoff.HandoffEffect(handoff.EffectKind.ACQUIRE_NEW, owner),),
            {},
        )

        self.assertEqual(process.phys_reads, 2)
        self.assertEqual(headings, {})
        self.assertEqual(rejected, (owner,))

    def test_follow_request_publishes_identity_without_actor_dereference(self):
        owner = handoff.OwnerToken(
            "p2",
            5,
            0x50000,
            0x51000,
            1,
            1,
            0x00100031,
        )
        runtime.set_dual_interaction_base(0x51000000)

        class PublishOnly:
            def __init__(self):
                self.writes = []

            def read(self, address, size):
                if owner.actor <= address < owner.actor + 0x1000:
                    raise AssertionError("Python dereferenced actor state")
                return b"\x00" * size

            def write(self, address, data):
                self.writes.append((address, bytes(data)))

        process = PublishOnly()
        self.assertFalse(
            runtime.resume_sanitized_follower_action(
                process,
                owner.actor,
                role=owner.role,
                handle=owner.native_handle,
                expected_phys=owner.phys,
            )
        )
        self.assertEqual(
            process.writes[-1],
            (
                runtime.P2_FOLLOWER_ACTION_MAILBOX,
                struct.pack("<I", 2),
            ),
        )
        self.assertFalse(
            any(owner.actor <= address < owner.actor + 0x1000 for address, _ in process.writes)
        )

    def test_confirm_and_cancel_are_explicit_diagnostic_no_ops(self):
        owner = token("p2", 5, 0x60000)

        class NoProcessIO:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected process I/O: {name}")

        headings, rejected = runtime.apply_follower_handoff_effects(
            NoProcessIO(),
            (
                handoff.HandoffEffect(handoff.EffectKind.CONFIRM_NATIVE_FOLLOW, owner),
                handoff.HandoffEffect(handoff.EffectKind.CANCEL_NATIVE_FOLLOW, owner),
            ),
            {},
        )

        self.assertEqual(headings, {})
        self.assertEqual(rejected, ())


class HandoffObservationTests(unittest.TestCase):
    class Process:
        def __init__(self, owner):
            self.owner = owner
            self.descriptor = 0x90000
            self.root = owner.actor + 0x100000

        def u64(self, address):
            values = {
                self.owner.actor + 0x10: self.root,
                self.root + 0xB8: self.owner.phys,
                self.owner.actor + 0x708: self.descriptor,
                self.owner.actor + 0x6E8: runtime.FOLLOWER_LIST_PRIMARY,
                self.owner.actor + 0x6F0: 0,
            }
            return values[address]

        def u32(self, address):
            if address == self.owner.actor + 0x08:
                return self.owner.native_handle
            if address == self.owner.actor + 0x710:
                return 0
            raise KeyError(address)

        def read(self, address, size):
            values = {
                (self.owner.actor + 0x6B4, 1): b"\x01",
                (self.owner.actor + 0x714, 2): struct.pack(
                    "<H", handoff.NATIVE_IDLE_ACTION
                ),
                (self.descriptor, 2): struct.pack("<H", handoff.FOLLOW_ACTION),
            }
            return values[(address, size)]

    def test_party_slot_keys_are_mapped_to_member_identity_before_observation(self):
        owner = token("p2", 5, 0x10000)
        empty = 0xFFFF
        snapshot = SimpleNamespace(
            current_layout=(0, 5, empty, empty) + (empty,) * 5,
            ui_layout=(0, 5, empty, empty) + (empty,) * 5,
            leader_party_member_id=0,
            cutscene=False,
            loading=False,
        )
        party_actor_slots = {
            # The native resolver is keyed by party slot, not member id.
            1: (17, owner.actor, owner.phys, (1.0, 2.0, 3.0)),
        }

        with (
            mock.patch.object(runtime, "actor_is_alive", return_value=True),
            mock.patch.object(
                runtime,
                "actor_follow_request_blocked",
                return_value=False,
            ),
        ):
            observed = runtime.observe_follower_handoff(
                self.Process(owner),
                owner,
                snapshot,
                party_actor_slots,
                sample=3,
            )

        self.assertTrue(observed.available)
        self.assertTrue(observed.materialized)
        self.assertTrue(observed.safe_to_request)

    def test_missing_handle_is_unknown_not_a_false_native_confirmation(self):
        owner = handoff.OwnerToken("p2", 5, 0x10000, 0x11000, 1, 1, 0)
        empty = 0xFFFF
        snapshot = SimpleNamespace(
            current_layout=(0, 5, empty, empty) + (empty,) * 5,
            ui_layout=(0, 5, empty, empty) + (empty,) * 5,
            leader_party_member_id=0,
            cutscene=False,
            loading=False,
        )
        party_actor_slots = {
            1: (17, owner.actor, owner.phys, (1.0, 2.0, 3.0)),
        }

        with (
            mock.patch.object(runtime, "actor_is_alive", return_value=True),
            mock.patch.object(
                runtime,
                "actor_follow_request_blocked",
                return_value=False,
            ),
        ):
            observed = runtime.observe_follower_handoff(
                self.Process(owner),
                owner,
                snapshot,
                party_actor_slots,
                sample=3,
            )

        self.assertFalse(observed.available)
        self.assertFalse(observed.confirmed)
        self.assertFalse(observed.safe_to_request)

    def test_observation_rejects_a_reused_native_handle_before_follow(self):
        owner = handoff.OwnerToken(
            "p2",
            5,
            0x10000,
            0x11000,
            1,
            1,
            native_handle=0x00100019,
        )
        empty = 0xFFFF
        snapshot = SimpleNamespace(
            current_layout=(0, 5, empty, empty) + (empty,) * 5,
            ui_layout=(0, 5, empty, empty) + (empty,) * 5,
            leader_party_member_id=0,
            cutscene=False,
            loading=False,
        )
        party_actor_slots = {
            1: (17, owner.actor, owner.phys, (1.0, 2.0, 3.0)),
        }
        process = self.Process(owner)
        original_u32 = process.u32

        def changed_handle(address):
            if address == owner.actor + 0x08:
                return 0x00200019
            return original_u32(address)

        process.u32 = changed_handle
        with (
            mock.patch.object(runtime, "actor_is_alive", return_value=True),
            mock.patch.object(
                runtime,
                "actor_follow_request_blocked",
                return_value=False,
            ),
        ):
            observed = runtime.observe_follower_handoff(
                process,
                owner,
                snapshot,
                party_actor_slots,
                sample=4,
            )

        self.assertFalse(observed.available)
        self.assertFalse(observed.materialized)
        state, effects = handoff.advance_repairs(
            handoff.HandoffState(
                (),
                (handoff.FollowRepair(owner, 0.0, 1.0),),
            ),
            {owner.incarnation_key: observed},
            now=0.1,
            world_epoch=1,
        )
        self.assertEqual(state.repairs, ())
        self.assertEqual(effects[0].reason, "ineligible")


if __name__ == "__main__":
    unittest.main()
