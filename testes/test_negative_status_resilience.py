import struct
import unittest

import coop_runtime as runtime
import follow_handoff as handoff


ACTOR = 0x100000
BATTLE_KEEP = 0x200000
PHYS = 0x300000


# Native status word at battle_keep + 0x64. Petrify is the countdown; Stone is
# the terminal state that actually prevents locomotion.
NEGATIVE_STATUS_BITS = {
    "KO": 0,
    "Stone": 1,
    "Petrify": 2,
    "Stop": 3,
    "Sleep": 4,
    "Confuse": 5,
    "Doom": 6,
    "Blind": 7,
    "Poison": 8,
    "Silence": 9,
    "Sap": 10,
    "Oil": 11,
    "Reverse": 12,
    "Disable": 13,
    "Immobilize": 14,
    "Slow": 15,
    "Disease": 16,
    "HP Critical": 29,
    "X-Zone": 31,
}
MOVEMENT_BLOCKING_STATUSES = {
    "KO",
    "Stone",
    "Stop",
    "Sleep",
    "Immobilize",
    "X-Zone",
}


class StatusProcess:
    def __init__(self, *, hp=100, temporary=0, permanent=0):
        self.hp = hp
        self.temporary = temporary
        self.permanent = permanent
        self.fail_status_read = False
        self.fail_permanent_read = False
        self.temporary_samples = []

    def u64(self, address):
        if address == ACTOR + 0x698:
            return BATTLE_KEEP
        if address == BATTLE_KEEP + runtime.TEMPORARY_STATUS_EFFECTS_OFFSET:
            if self.fail_status_read:
                raise OSError("transient status read")
            return self.temporary
        raise OSError(f"unexpected u64 read at {address:#x}")

    def u32(self, address):
        if address == BATTLE_KEEP + 0x48:
            return self.hp
        if address == BATTLE_KEEP + runtime.TEMPORARY_STATUS_EFFECTS_OFFSET:
            if self.fail_status_read:
                raise OSError("transient status read")
            if self.temporary_samples:
                return self.temporary_samples.pop(0) & 0xFFFFFFFF
            return self.temporary & 0xFFFFFFFF
        if address == BATTLE_KEEP + runtime.PERMANENT_STATUS_EFFECTS_OFFSET:
            if self.fail_permanent_read:
                raise OSError("transient permanent-status read")
            return self.permanent & 0xFFFFFFFF
        raise OSError(f"unexpected u32 read at {address:#x}")


def owner(generation=1):
    return handoff.OwnerToken(
        "p2",
        5,
        ACTOR,
        PHYS,
        generation,
        1,
        ((generation & 0xFFFF) << 16) | 5,
    )


def observation(token, sample, **changes):
    values = dict(
        token=token,
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


class NegativeStatusPolicyTests(unittest.TestCase):
    def test_native_status_map_has_the_exact_locomotion_subset(self):
        expected = sum(
            1 << NEGATIVE_STATUS_BITS[name] for name in MOVEMENT_BLOCKING_STATUSES
        )
        self.assertEqual(runtime.MOVEMENT_BLOCKING_STATUS_MASK, expected)
        self.assertEqual(runtime.CONFUSE_STATUS_MASK, 1 << 5)
        self.assertEqual(runtime.DISABLE_STATUS_MASK, 1 << 13)
        self.assertFalse(runtime.MOVEMENT_BLOCKING_STATUS_MASK & runtime.CONFUSE_STATUS_MASK)
        self.assertFalse(runtime.MOVEMENT_BLOCKING_STATUS_MASK & runtime.DISABLE_STATUS_MASK)
        self.assertFalse(runtime.MOVEMENT_BLOCKING_STATUS_MASK & runtime.BERSERK_STATUS_MASK)

    def test_every_negative_status_is_nonfatal_and_clears_reversibly(self):
        process = StatusProcess()
        cases = dict(NEGATIVE_STATUS_BITS)
        cases["Berserk"] = 27

        for name, bit in cases.items():
            with self.subTest(status=name):
                process.temporary = 1 << bit
                alive = runtime.actor_is_alive(process, ACTOR)
                accepts_movement = runtime.actor_accepts_manual_movement(
                    process, ACTOR
                )

                self.assertIs(alive, name != "KO")
                self.assertIs(
                    accepts_movement,
                    name not in MOVEMENT_BLOCKING_STATUSES,
                )
                self.assertIs(
                    runtime.actor_has_berserk(process, ACTOR),
                    name == "Berserk",
                )
                self.assertEqual(
                    runtime.follower_role_owns_movement(True, alive, 0, 5),
                    name != "KO",
                )

                logical, movement, camera = runtime.coop_lifecycle_flags(
                    True,
                    True,
                    alive,
                    True,
                    p2_present=True,
                    p3_present=True,
                )
                self.assertTrue(logical)
                self.assertTrue(movement)
                self.assertTrue(camera)

                process.temporary = 0
                self.assertIs(runtime.actor_is_alive(process, ACTOR), True)
                self.assertIs(
                    runtime.actor_accepts_manual_movement(process, ACTOR), True
                )

    def test_non_ko_statuses_preserve_generation_when_control_resumes(self):
        process = StatusProcess()
        cases = dict(NEGATIVE_STATUS_BITS)
        cases.pop("KO")
        cases["Berserk"] = 27

        for name, bit in cases.items():
            with self.subTest(status=name):
                tracker = handoff.ActorGenerationTracker()
                process.temporary = 1 << bit
                affected = tracker.observe(
                    "p2",
                    5,
                    ACTOR,
                    PHYS,
                    world_epoch=1,
                    alive=runtime.actor_is_alive(process, ACTOR),
                    structurally_present=True,
                )
                process.temporary = 0
                recovered = tracker.observe(
                    "p2",
                    5,
                    ACTOR,
                    PHYS,
                    world_epoch=1,
                    alive=runtime.actor_is_alive(process, ACTOR),
                    structurally_present=True,
                )

                self.assertEqual(
                    recovered.actor_generation,
                    affected.actor_generation,
                )

    def test_each_permanent_only_blocker_is_observed_by_native_policy(self):
        process = StatusProcess()
        for name in MOVEMENT_BLOCKING_STATUSES:
            with self.subTest(status=name):
                process.temporary = 0
                process.permanent = 1 << NEGATIVE_STATUS_BITS[name]

                self.assertIs(
                    runtime.actor_is_alive(process, ACTOR),
                    name != "KO",
                )
                self.assertIs(
                    runtime.actor_accepts_manual_movement(process, ACTOR),
                    False,
                )

                process.permanent = 0
                self.assertIs(runtime.actor_is_alive(process, ACTOR), True)
                self.assertIs(
                    runtime.actor_accepts_manual_movement(process, ACTOR), True
                )

    def test_each_native_command_blocker_defers_follow_until_status_clears(self):
        process = StatusProcess()
        blocking = {
            "KO",
            "Stone",
            "Stop",
            "Sleep",
            "Confuse",
            "Disable",
            "X-Zone",
            "Berserk",
        }
        cases = dict(NEGATIVE_STATUS_BITS)
        cases["Berserk"] = 27
        token = owner()

        for name in blocking:
            for storage in ("temporary", "permanent"):
                with self.subTest(status=name, storage=storage):
                    process.temporary = 0
                    process.permanent = 0
                    setattr(process, storage, 1 << cases[name])
                    self.assertIs(
                        runtime.actor_follow_request_blocked(process, ACTOR),
                        True,
                    )
                    repair = handoff.FollowRepair(token, 0.0, 1.0)
                    affected = observation(
                        token,
                        1,
                        alive=name != "KO",
                        status_blocked=True,
                        action_state=4,
                        target=0x200005,
                        action=0x4999,
                        follows_native_list=False,
                    )
                    state, effects = handoff.advance_repairs(
                        handoff.HandoffState((), (repair,)),
                        {token.incarnation_key: affected},
                        now=1_000_000.0,
                        world_epoch=1,
                    )
                    self.assertEqual(effects, ())
                    self.assertEqual(state.repairs[0].attempts, 0)
                    self.assertEqual(state.repairs[0].deadline, 1_000_001.0)

                    setattr(process, storage, 0)
                    self.assertIs(
                        runtime.actor_follow_request_blocked(process, ACTOR),
                        False,
                    )
                    settling = observation(
                        token,
                        2,
                        action_state=4,
                        target=0x200005,
                        action=0x4999,
                        follows_native_list=False,
                    )
                    state, effects = handoff.advance_repairs(
                        state,
                        {token.incarnation_key: settling},
                        now=2_000_000.0,
                        world_epoch=1,
                    )
                    self.assertEqual(effects, ())
                    self.assertEqual(state.repairs[0].attempts, 0)

                    state, effects = handoff.advance_repairs(
                        state,
                        {token.incarnation_key: observation(token, 3)},
                        now=2_000_000.1,
                        world_epoch=1,
                    )
                    self.assertEqual(
                        tuple(effect.kind for effect in effects),
                        (handoff.EffectKind.REQUEST_NATIVE_FOLLOW,),
                    )
                    self.assertEqual(state.repairs[0].attempts, 1)

    def test_status_snapshot_fails_safe_for_either_half_and_for_mutation(self):
        process = StatusProcess()
        tracker = handoff.ActorGenerationTracker()
        first = tracker.observe(
            "p2",
            5,
            ACTOR,
            PHYS,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )

        for failing_attribute in ("fail_status_read", "fail_permanent_read"):
            with self.subTest(failure=failing_attribute):
                setattr(process, failing_attribute, True)
                self.assertIsNone(runtime.actor_is_alive(process, ACTOR))
                self.assertIsNone(
                    runtime.actor_accepts_manual_movement(process, ACTOR)
                )
                self.assertIsNone(runtime.actor_has_berserk(process, ACTOR))
                unknown = tracker.observe(
                    "p2",
                    5,
                    ACTOR,
                    PHYS,
                    world_epoch=1,
                    alive=None,
                    structurally_present=True,
                )
                self.assertEqual(unknown.actor_generation, first.actor_generation)
                setattr(process, failing_attribute, False)

        process.temporary_samples = [0, 1, 0, 1, 0, 1]
        self.assertIsNone(runtime.battle_status_effects(process, BATTLE_KEEP))

    def test_status_snapshot_retries_one_transport_fault_within_its_fixed_budget(self):
        class OneFault(StatusProcess):
            remaining_faults = 1

            def u32(self, address):
                if self.remaining_faults:
                    self.remaining_faults -= 1
                    raise RuntimeError("actor rematerialized during sample")
                return super().u32(address)

        process = OneFault(
            temporary=runtime.STONE_STATUS_MASK,
            permanent=runtime.BERSERK_STATUS_MASK,
        )

        self.assertEqual(
            runtime.battle_status_effects(process, BATTLE_KEEP),
            runtime.STONE_STATUS_MASK | runtime.BERSERK_STATUS_MASK,
        )

    def test_every_actor_sample_helper_contains_volatile_transport_errors(self):
        error_factories = (
            lambda: OSError("gone"),
            lambda: RuntimeError("detached"),
            lambda: struct.error("short"),
            lambda: TypeError("bad pointer"),
            lambda: ValueError("bad value"),
            lambda: OverflowError("bad address"),
            lambda: AttributeError("transport unavailable"),
            lambda: IndexError("short read"),
            lambda: KeyError("missing sample"),
        )

        for make_error in error_factories:
            class ExplodingProcess:
                def u64(self, _address):
                    raise make_error()

                def u32(self, _address):
                    raise make_error()

                def read(self, _address, _size):
                    raise make_error()

            process = ExplodingProcess()
            with self.subTest(error=type(make_error()).__name__):
                self.assertIsNone(runtime.battle_status_effects(process, BATTLE_KEEP))
                self.assertIsNone(runtime.actor_is_alive(process, ACTOR))
                self.assertIsNone(runtime.actor_accepts_manual_movement(process, ACTOR))
                self.assertIsNone(runtime.actor_has_berserk(process, ACTOR))
                self.assertIsNone(runtime.actor_follow_request_blocked(process, ACTOR))
                self.assertEqual(runtime.actor_physics_pointer(process, ACTOR), 0)
                self.assertEqual(runtime.actor_u32_or_zero(process, ACTOR, 8), 0)
                self.assertEqual(runtime.actor_u64_or_zero(process, ACTOR, 8), 0)
                self.assertFalse(runtime.actor_fighting_stance(process, ACTOR))
                self.assertFalse(
                    runtime.actor_execution_animation_active(process, ACTOR, True)
                )
                self.assertIsNone(
                    runtime.actor_execution_target_heading(
                        process,
                        ACTOR,
                        (0.0, 0.0, 0.0),
                        {},
                    )
                )

    def test_ko_revival_preserves_exact_identity_and_reacquires_control(self):
        process = StatusProcess()
        tracker = handoff.ActorGenerationTracker()
        first = tracker.observe(
            "p2",
            5,
            ACTOR,
            PHYS,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )

        process.temporary = runtime.KO_STATUS_MASK
        dead = tracker.observe(
            "p2",
            5,
            ACTOR,
            PHYS,
            world_epoch=1,
            alive=runtime.actor_is_alive(process, ACTOR),
            structurally_present=True,
        )
        self.assertEqual(dead.actor_generation, first.actor_generation)
        self.assertFalse(runtime.follower_role_owns_movement(True, False, 0, 5))

        process.temporary = 0
        revived = tracker.observe(
            "p2",
            5,
            ACTOR,
            PHYS,
            world_epoch=1,
            alive=runtime.actor_is_alive(process, ACTOR),
            structurally_present=True,
        )
        self.assertEqual(revived.actor_generation, dead.actor_generation)
        state, effects = handoff.reconcile_owners(
            handoff.HandoffState(),
            {"p2": revived},
            acquire_eligible={"p2": True},
            observations={},
            now=0.0,
            world_epoch=1,
        )
        self.assertEqual(state.owner_map(), {"p2": revived})
        self.assertEqual(
            tuple(effect.kind for effect in effects),
            (handoff.EffectKind.ACQUIRE_NEW,),
        )

    def test_transient_status_read_retains_lifecycle_and_generation(self):
        process = StatusProcess()
        tracker = handoff.ActorGenerationTracker()
        first = tracker.observe(
            "p2",
            5,
            ACTOR,
            PHYS,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )

        process.fail_status_read = True
        self.assertIsNone(runtime.actor_is_alive(process, ACTOR))
        self.assertIsNone(runtime.actor_accepts_manual_movement(process, ACTOR))
        self.assertTrue(runtime.follower_role_owns_movement(True, None, 0, 5))
        self.assertEqual(
            runtime.coop_lifecycle_flags(
                True,
                True,
                None,
                None,
                p2_present=True,
                p3_present=False,
            ),
            (True, True, True),
        )
        unknown = tracker.observe(
            "p2",
            5,
            ACTOR,
            PHYS,
            world_epoch=1,
            alive=None,
            structurally_present=True,
        )
        self.assertTrue(
            runtime.retain_owner_during_liveness_gap(
                actor_alive=None,
                coop_active=True,
                manual_control=True,
                current_owner=first,
                candidate_owner=unknown,
                role_member_id=5,
                native_leader_member_id=0,
                world_epoch=1,
            )
        )

        process.fail_status_read = False
        recovered = tracker.observe(
            "p2",
            5,
            ACTOR,
            PHYS,
            world_epoch=1,
            alive=True,
            structurally_present=True,
        )
        self.assertEqual(unknown.actor_generation, first.actor_generation)
        self.assertEqual(recovered.actor_generation, first.actor_generation)

    def test_status_action_waits_then_native_follow_resumes_after_clear(self):
        token = owner()
        for name in tuple(NEGATIVE_STATUS_BITS) + ("Berserk",):
            if name == "KO":
                continue
            with self.subTest(status=name):
                repair = handoff.FollowRepair(token, 0.0, 1.0)
                blocked = observation(
                    token,
                    1,
                    action_state=4,
                    target=0x200005,
                    action=0x4999,
                    follows_native_list=False,
                )
                state, effects = handoff.advance_repairs(
                    handoff.HandoffState((), (repair,)),
                    {token.incarnation_key: blocked},
                    now=0.25,
                    world_epoch=1,
                )
                self.assertEqual(effects, ())
                self.assertEqual(len(state.repairs), 1)

                ready = observation(token, 2)
                state, effects = handoff.advance_repairs(
                    state,
                    {token.incarnation_key: ready},
                    now=0.50,
                    world_epoch=1,
                )
                self.assertEqual(
                    tuple(effect.kind for effect in effects),
                    (handoff.EffectKind.REQUEST_NATIVE_FOLLOW,),
                )
                confirmed = observation(
                    token,
                    3,
                    action=handoff.FOLLOW_ACTION,
                )
                state, effects = handoff.advance_repairs(
                    state,
                    {token.incarnation_key: confirmed},
                    now=0.60,
                    world_epoch=1,
                )
                self.assertEqual(state.repairs, ())
                self.assertEqual(
                    tuple(effect.kind for effect in effects),
                    (handoff.EffectKind.CONFIRM_NATIVE_FOLLOW,),
                )


if __name__ == "__main__":
    unittest.main()
