import struct
import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

import coop_runtime as coop


class FakeProcess:
    def __init__(self):
        self.memory = {}

    def put(self, address, data):
        for index, value in enumerate(data):
            self.memory[address + index] = value

    def read(self, address, size):
        try:
            return bytes(self.memory[address + index] for index in range(size))
        except KeyError as error:
            raise OSError(address) from error

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


class CoopLifecycleTests(unittest.TestCase):
    def test_missing_actor_manager_returns_empty_party_slot_mapping(self):
        process = FakeProcess()
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", 0))
        resolved = coop.resolve_active_party_actor_slots(process, [])
        self.assertEqual(resolved, {})

    def test_lifecycle_policy_keeps_session_active_during_ko(self):
        self.assertEqual(
            coop.coop_lifecycle_flags(True, True, True),
            (True, True, True),
        )
        self.assertEqual(
            coop.coop_lifecycle_flags(True, False, True),
            (True, True, False),
        )
        self.assertEqual(
            coop.coop_lifecycle_flags(True, True, False),
            (True, False, False),
        )
        self.assertEqual(
            coop.coop_lifecycle_flags(True, None, True),
            (True, True, False),
        )
        self.assertEqual(
            coop.coop_lifecycle_flags(False, True, True),
            (False, False, False),
        )

    def test_p3_keeps_movement_and_camera_when_p2_is_ko(self):
        self.assertEqual(
            coop.coop_lifecycle_flags(True, True, False, True),
            (True, True, True),
        )
        self.assertEqual(
            coop.coop_lifecycle_flags(True, True, False, False),
            (True, False, False),
        )

    def test_two_surviving_followers_keep_camera_when_p1_is_ko(self):
        self.assertEqual(
            coop.coop_lifecycle_flags(True, False, True, True),
            (True, True, True),
        )
        # With only one survivor there is no pair left to frame.
        self.assertEqual(
            coop.coop_lifecycle_flags(True, False, True, False),
            (True, True, False),
        )

    def test_each_follower_owns_only_a_live_nonleader_role(self):
        self.assertTrue(coop.follower_role_owns_movement(True, True, 0, 5))
        self.assertFalse(coop.follower_role_owns_movement(True, True, 5, 5))
        self.assertFalse(coop.follower_role_owns_movement(True, False, 0, 5))
        self.assertFalse(
            coop.follower_role_owns_movement(True, True, 0, 5, identity_current=False)
        )

    def test_manual_off_wins_over_an_unknown_actor_mapping(self):
        owner = SimpleNamespace(member_id=5, world_epoch=7)
        common = dict(
            mapping_unknown=True,
            coop_active=True,
            current_owner=owner,
            role_member_id=5,
            native_leader_member_id=0,
            world_epoch=7,
        )

        self.assertTrue(
            coop.retain_owner_during_mapping_gap(
                manual_control=True,
                **common,
            )
        )
        self.assertFalse(
            coop.retain_owner_during_mapping_gap(
                manual_control=False,
                **common,
            )
        )

    def test_coop_off_wins_over_an_unknown_actor_mapping(self):
        owner = SimpleNamespace(member_id=5, world_epoch=7)

        self.assertFalse(
            coop.retain_owner_during_mapping_gap(
                mapping_unknown=True,
                coop_active=False,
                manual_control=True,
                current_owner=owner,
                role_member_id=5,
                native_leader_member_id=0,
                world_epoch=7,
            )
        )

    def test_unknown_status_sample_retains_only_the_exact_current_owner(self):
        owner = SimpleNamespace(member_id=5, world_epoch=7)
        common = dict(
            coop_active=True,
            manual_control=True,
            current_owner=owner,
            candidate_owner=owner,
            role_member_id=5,
            native_leader_member_id=0,
            world_epoch=7,
        )

        self.assertTrue(
            coop.retain_owner_during_liveness_gap(
                actor_alive=None,
                **common,
            )
        )
        for alive in (False, True):
            with self.subTest(alive=alive):
                self.assertFalse(
                    coop.retain_owner_during_liveness_gap(
                        actor_alive=alive,
                        **common,
                    )
                )
        self.assertFalse(
            coop.retain_owner_during_liveness_gap(
                actor_alive=None,
                **(
                    common
                    | {
                        "candidate_owner": SimpleNamespace(
                            member_id=1,
                            world_epoch=7,
                        )
                    }
                ),
            )
        )

    def test_manual_off_wins_over_unknown_status_liveness(self):
        owner = SimpleNamespace(member_id=5, world_epoch=7)

        self.assertFalse(
            coop.retain_owner_during_liveness_gap(
                actor_alive=None,
                coop_active=True,
                manual_control=False,
                current_owner=owner,
                candidate_owner=owner,
                role_member_id=5,
                native_leader_member_id=0,
                world_epoch=7,
            )
        )

    def test_ko_promoted_follower_can_keep_exclusive_movement_owner(self):
        self.assertTrue(
            coop.follower_role_owns_movement(
                True,
                True,
                5,
                5,
                allow_native_leader=True,
            )
        )
        # The opt-in is deliberately absent during an area handoff and every
        # ordinary manual P1 leader change.
        self.assertFalse(coop.follower_role_owns_movement(True, True, 5, 5))

    def test_ko_leader_controller_preserves_then_restores_original_p1(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ),
            mock.patch.object(coop, "publish_native_leader_handoff_active") as publish,
            mock.patch.object(
                coop, "request_native_leader_refresh", return_value=17
            ) as request,
        ):
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    2,
                    False,
                    p1_present=True,
                    now=1.0,
                ),
                "p2",
            )
            self.assertTrue(controller.active)
            publish.assert_not_called()
            request.assert_not_called()

            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    2,
                    True,
                    p1_present=True,
                    now=2.0,
                ),
                "p2",
            )
            request.assert_called_once_with(process, 4)

            self.assertIsNone(
                controller.tick(
                    process,
                    identity,
                    4,
                    True,
                    p1_present=True,
                    now=2.1,
                )
            )
            self.assertFalse(controller.active)
            self.assertEqual(
                publish.call_args_list,
                [mock.call(process, False)],
            )

    def test_ko_leader_controller_requests_p2_before_native_delay(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ) as claim,
            mock.patch.object(coop, "publish_native_leader_handoff_active") as publish,
            mock.patch.object(
                coop, "request_native_leader_refresh", return_value=31
            ) as request,
        ):
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    4,
                    False,
                    p1_present=True,
                    p2_available=True,
                    p3_available=True,
                    now=1.0,
                ),
                "p2",
            )
            claim.assert_called_once_with(process)
            publish.assert_not_called()
            request.assert_called_once_with(process, 2)

            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    4,
                    False,
                    p1_present=True,
                    p2_available=True,
                    p3_available=True,
                    now=1.1,
                ),
                "p2",
            )
            request.assert_called_once()

            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    2,
                    False,
                    p1_present=True,
                    p2_available=True,
                    p3_available=True,
                    now=1.2,
                ),
                "p2",
            )

    def test_ko_leader_controller_falls_back_to_live_p3(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ),
            mock.patch.object(coop, "publish_native_leader_handoff_active"),
            mock.patch.object(
                coop, "request_native_leader_refresh", return_value=32
            ) as request,
        ):
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    4,
                    False,
                    p1_present=True,
                    p2_available=False,
                    p3_available=True,
                    now=1.0,
                ),
                "p3",
            )
            request.assert_called_once_with(process, 1)

    def test_hard_blocked_p2_is_ineligible_and_healthy_p3_is_selected(self):
        actor_p2 = 0x2000
        actor_p3 = 0x3000
        keep_p2 = 0x4000
        keep_p3 = 0x5000

        class Process:
            def u64(self, address):
                return {
                    actor_p2 + 0x698: keep_p2,
                    actor_p3 + 0x698: keep_p3,
                }.get(address, 0)

            def u32(self, address):
                return {
                    keep_p2 + 0x48: 100,
                    keep_p3 + 0x48: 100,
                    keep_p2 + coop.TEMPORARY_STATUS_EFFECTS_OFFSET: (
                        coop.STONE_STATUS_MASK
                    ),
                    keep_p3 + coop.TEMPORARY_STATUS_EFFECTS_OFFSET: 0,
                    keep_p2 + coop.PERMANENT_STATUS_EFFECTS_OFFSET: 0,
                    keep_p3 + coop.PERMANENT_STATUS_EFFECTS_OFFSET: 0,
                }.get(address, 0)

        process = Process()
        self.assertFalse(coop.actor_available_for_ko_leader(process, actor_p2))
        self.assertTrue(coop.actor_available_for_ko_leader(process, actor_p3))

        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ),
            mock.patch.object(coop, "publish_native_leader_handoff_active"),
            mock.patch.object(
                coop, "request_native_leader_refresh", return_value=52
            ) as request,
        ):
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    4,
                    False,
                    p1_present=True,
                    p2_available=coop.actor_available_for_ko_leader(process, actor_p2),
                    p3_available=coop.actor_available_for_ko_leader(process, actor_p3),
                    now=1.0,
                ),
                "p3",
            )
        request.assert_called_once_with(process, 1)

    def test_every_effective_p1_action_blocker_hands_off_and_restores_leadership(
        self,
    ):
        actor_p1 = 0x2000
        actor_p2 = 0x3000
        keep_p1 = 0x4000
        keep_p2 = 0x5000
        status_masks = (
            coop.KO_STATUS_MASK,
            coop.STONE_STATUS_MASK,
            coop.STOP_STATUS_MASK,
            coop.SLEEP_STATUS_MASK,
            coop.CONFUSE_STATUS_MASK,
            coop.DISABLE_STATUS_MASK,
            coop.X_ZONE_STATUS_MASK,
        )

        for status_mask in status_masks:
            for status_offset in (
                coop.TEMPORARY_STATUS_EFFECTS_OFFSET,
                coop.PERMANENT_STATUS_EFFECTS_OFFSET,
            ):
                with self.subTest(
                    status_mask=status_mask,
                    status_offset=status_offset,
                ):
                    process = FakeProcess()
                    process.put(actor_p1 + 0x698, struct.pack("<Q", keep_p1))
                    process.put(actor_p2 + 0x698, struct.pack("<Q", keep_p2))
                    for battle_keep in (keep_p1, keep_p2):
                        process.put(battle_keep + 0x48, struct.pack("<I", 100))
                        process.put(
                            battle_keep + coop.TEMPORARY_STATUS_EFFECTS_OFFSET,
                            struct.pack("<Q", 0),
                        )
                        process.put(
                            battle_keep + coop.PERMANENT_STATUS_EFFECTS_OFFSET,
                            struct.pack("<I", 0),
                        )
                    status_size = (
                        8
                        if status_offset == coop.TEMPORARY_STATUS_EFFECTS_OFFSET
                        else 4
                    )
                    process.put(
                        keep_p1 + status_offset,
                        int(status_mask).to_bytes(status_size, "little"),
                    )

                    # Lifecycle liveness remains HP/KO-only, while native
                    # leader eligibility follows the full native action gate.
                    self.assertIs(
                        coop.actor_is_alive(process, actor_p1),
                        status_mask != coop.KO_STATUS_MASK,
                    )
                    p1_available = coop.actor_available_for_native_leader(
                        process, actor_p1
                    )
                    p2_available = coop.actor_available_for_ko_leader(
                        process, actor_p2
                    )
                    self.assertIs(p1_available, False)
                    self.assertIs(p2_available, True)

                    controller = coop.NativeKoLeaderOwnershipController()
                    identity = coop.session_state.SessionIdentity(4, 2, 1)
                    with (
                        mock.patch.object(
                            coop,
                            "claim_native_leader_transient_handoff",
                            return_value=True,
                        ),
                        mock.patch.object(
                            coop, "publish_native_leader_handoff_active"
                        ) as publish,
                        mock.patch.object(
                            coop,
                            "request_native_leader_refresh",
                            side_effect=(70, 71),
                        ) as request,
                    ):
                        self.assertEqual(
                            controller.tick(
                                process,
                                identity,
                                4,
                                p1_available,
                                p1_present=True,
                                p2_available=p2_available,
                                now=1.0,
                            ),
                            "p2",
                        )
                        self.assertEqual(
                            request.call_args_list,
                            [mock.call(process, 2)],
                        )

                        process.put(
                            keep_p1 + status_offset,
                            bytes(status_size),
                        )
                        p1_recovered = coop.actor_available_for_native_leader(
                            process, actor_p1
                        )
                        self.assertIs(p1_recovered, True)
                        self.assertEqual(
                            controller.tick(
                                process,
                                identity,
                                2,
                                p1_recovered,
                                p1_present=True,
                                p2_available=p2_available,
                                now=2.0,
                            ),
                            "p2",
                        )
                        self.assertEqual(
                            request.call_args_list,
                            [mock.call(process, 2), mock.call(process, 4)],
                        )
                        self.assertIsNone(
                            controller.tick(
                                process,
                                identity,
                                4,
                                p1_recovered,
                                p1_present=True,
                                p2_available=p2_available,
                                now=2.1,
                            )
                        )
                        self.assertFalse(controller.active)
                        self.assertEqual(
                            publish.call_args_list,
                            [mock.call(process, False)],
                        )

    def test_berserk_and_immobilize_keep_configured_p1_leader_available(self):
        actor = 0x2000
        battle_keep = 0x4000
        for status_mask in (
            coop.BERSERK_STATUS_MASK,
            coop.IMMOBILIZE_STATUS_MASK,
        ):
            for status_offset in (
                coop.TEMPORARY_STATUS_EFFECTS_OFFSET,
                coop.PERMANENT_STATUS_EFFECTS_OFFSET,
            ):
                with self.subTest(
                    status_mask=status_mask,
                    status_offset=status_offset,
                ):
                    process = FakeProcess()
                    process.put(actor + 0x698, struct.pack("<Q", battle_keep))
                    process.put(battle_keep + 0x48, struct.pack("<I", 100))
                    process.put(
                        battle_keep + coop.TEMPORARY_STATUS_EFFECTS_OFFSET,
                        struct.pack("<Q", 0),
                    )
                    process.put(
                        battle_keep + coop.PERMANENT_STATUS_EFFECTS_OFFSET,
                        struct.pack("<I", 0),
                    )
                    status_size = (
                        8
                        if status_offset == coop.TEMPORARY_STATUS_EFFECTS_OFFSET
                        else 4
                    )
                    process.put(
                        battle_keep + status_offset,
                        int(status_mask).to_bytes(status_size, "little"),
                    )

                    self.assertIs(
                        coop.actor_available_for_native_leader(process, actor),
                        True,
                    )

    def test_unknown_p1_availability_retains_an_existing_leader_handoff_latch(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ) as claim,
            mock.patch.object(
                coop, "publish_native_leader_handoff_active"
            ) as publish,
            mock.patch.object(
                coop, "request_native_leader_refresh", return_value=80
            ) as request,
        ):
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    4,
                    False,
                    p1_present=True,
                    p2_available=True,
                    now=1.0,
                ),
                "p2",
            )
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    2,
                    None,
                    p1_present=True,
                    p2_available=True,
                    now=1.1,
                ),
                "p2",
            )

        self.assertTrue(controller.active)
        claim.assert_called_once_with(process)
        publish.assert_not_called()
        request.assert_called_once_with(process, 2)

    def test_main_samples_configured_p1_with_status_aware_availability(self):
        source = inspect.getsource(coop.main)
        self.assertIn(
            "configured_p1_leader_available = actor_available_for_native_leader(",
            source,
        )
        self.assertIn(
            "configured_p1_leader_available,\n"
            "                    p1_present=configured_p1_layout_coherent,",
            source,
        )
        self.assertNotIn("configured_p1_alive = actor_is_alive", source)

    def test_torn_ko_replacement_sample_stays_unknown(self):
        actor = 0x2000
        battle_keep = 0x4000

        class HpChangesDuringEverySample:
            def __init__(self):
                self.hp_reads = 0

            def u64(self, address):
                return battle_keep if address == actor + 0x698 else 0

            def u32(self, address):
                if address == battle_keep + 0x48:
                    self.hp_reads += 1
                    return 100 if self.hp_reads % 2 else 0
                return 0

        self.assertIsNone(
            coop.actor_available_for_ko_leader(
                HpChangesDuringEverySample(),
                actor,
            )
        )
        self.assertIsNone(
            coop.actor_available_for_native_leader(
                HpChangesDuringEverySample(),
                actor,
            )
        )

        class BattleObjectChangesDuringEverySample:
            def __init__(self):
                self.identity_reads = 0

            def u64(self, address):
                if address != actor + 0x698:
                    return 0
                self.identity_reads += 1
                return 0x4000 if self.identity_reads % 2 else 0x5000

            def u32(self, address):
                if address in (0x4000 + 0x48, 0x5000 + 0x48):
                    return 100
                return 0

        self.assertIsNone(
            coop.actor_available_for_ko_leader(
                BattleObjectChangesDuringEverySample(),
                actor,
            )
        )
        self.assertIsNone(
            coop.actor_available_for_native_leader(
                BattleObjectChangesDuringEverySample(),
                actor,
            )
        )

    def test_all_hard_blocked_replacements_wait_without_mailbox_request(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ),
            mock.patch.object(coop, "publish_native_leader_handoff_active") as publish,
            mock.patch.object(coop, "request_native_leader_refresh") as request,
        ):
            self.assertIsNone(
                controller.tick(
                    process,
                    identity,
                    4,
                    False,
                    p1_present=True,
                    p2_available=False,
                    p3_available=False,
                    now=1.0,
                )
            )
        publish.assert_not_called()
        request.assert_not_called()
        self.assertFalse(controller.active)

    def test_ko_leader_controller_fails_over_when_promoted_p2_dies(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ),
            mock.patch.object(coop, "publish_native_leader_handoff_active"),
            mock.patch.object(
                coop, "request_native_leader_refresh", side_effect=(41, 42)
            ) as request,
        ):
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    4,
                    False,
                    p1_present=True,
                    p2_available=True,
                    p3_available=True,
                    now=1.0,
                ),
                "p2",
            )
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    2,
                    False,
                    p1_present=True,
                    p2_available=False,
                    p3_available=True,
                    now=1.1,
                ),
                "p3",
            )
            self.assertEqual(
                request.call_args_list,
                [mock.call(process, 2), mock.call(process, 1)],
            )

    def test_stale_dead_p2_leader_promotes_live_p3_on_first_sample(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ),
            mock.patch.object(coop, "publish_native_leader_handoff_active"),
            mock.patch.object(
                coop, "request_native_leader_refresh", return_value=43
            ) as request,
        ):
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    2,
                    False,
                    p1_present=True,
                    p2_available=False,
                    p3_available=True,
                    now=1.0,
                ),
                "p3",
            )
            request.assert_called_once_with(process, 1)

    def test_unknown_promoted_status_does_not_trigger_false_failover(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ),
            mock.patch.object(coop, "publish_native_leader_handoff_active"),
            mock.patch.object(
                coop, "request_native_leader_refresh", return_value=51
            ) as request,
        ):
            controller.tick(
                process,
                identity,
                4,
                False,
                p1_present=True,
                p2_available=True,
                p3_available=True,
                now=1.0,
            )
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    2,
                    False,
                    p1_present=True,
                    p2_available=None,
                    p3_available=True,
                    now=1.1,
                ),
                "p2",
            )
            request.assert_called_once_with(process, 2)

    def test_revive_cancels_a_promotion_that_has_not_applied_yet(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ),
            mock.patch.object(coop, "publish_native_leader_handoff_active") as publish,
            mock.patch.object(
                coop, "request_native_leader_refresh", return_value=33
            ) as request,
        ):
            controller.tick(
                process,
                identity,
                4,
                False,
                p1_present=True,
                p2_available=True,
                now=1.0,
            )
            self.assertIsNone(
                controller.tick(
                    process,
                    identity,
                    4,
                    True,
                    p1_present=True,
                    p2_available=True,
                    now=1.1,
                )
            )
            self.assertFalse(controller.active)
            request.assert_called_once_with(process, 2)
            self.assertEqual(
                publish.call_args_list,
                [mock.call(process, False)],
            )

    def test_torn_initial_ko_sample_does_not_invent_a_takeover(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        with mock.patch.object(coop, "publish_native_leader_handoff_active") as publish:
            self.assertIsNone(
                controller.tick(
                    object(),
                    identity,
                    2,
                    None,
                    p1_present=True,
                    now=1.0,
                )
            )
            self.assertFalse(controller.active)
            publish.assert_not_called()

    def test_stable_follower_leader_survives_unknown_x_zone_actor_gap(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ) as claim,
            mock.patch.object(coop, "publish_native_leader_handoff_active") as publish,
            mock.patch.object(coop, "request_native_leader_refresh") as request,
        ):
            self.assertIsNone(
                controller.tick(
                    process,
                    identity,
                    2,
                    None,
                    p1_present=True,
                    p2_available=True,
                    p3_available=True,
                    now=1.0,
                )
            )
            self.assertIsNone(
                controller.tick(
                    process,
                    identity,
                    2,
                    None,
                    p1_present=True,
                    p2_available=True,
                    p3_available=True,
                    now=(
                        1.0
                        + coop.KO_LEADER_UNKNOWN_PROMOTION_SETTLE_SECONDS
                        - 0.01
                    ),
                )
            )
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    2,
                    None,
                    p1_present=True,
                    p2_available=True,
                    p3_available=True,
                    now=(
                        1.0
                        + coop.KO_LEADER_UNKNOWN_PROMOTION_SETTLE_SECONDS
                        + 0.01
                    ),
                ),
                "p2",
            )

        self.assertTrue(controller.active)
        claim.assert_called_once_with(process)
        publish.assert_not_called()
        request.assert_not_called()

    def test_ko_guard_survives_a_guest_leader_until_main_p1_revives(self):
        controller = coop.NativeKoLeaderOwnershipController()
        identity = coop.session_state.SessionIdentity(4, 2, 1)
        process = object()
        with (
            mock.patch.object(
                coop, "claim_native_leader_transient_handoff", return_value=True
            ),
            mock.patch.object(coop, "publish_native_leader_handoff_active") as publish,
            mock.patch.object(
                coop, "request_native_leader_refresh", return_value=23
            ) as request,
        ):
            self.assertEqual(
                controller.tick(
                    process,
                    identity,
                    2,
                    False,
                    p1_present=True,
                    now=1.0,
                ),
                "p2",
            )
            self.assertIsNone(
                controller.tick(
                    process,
                    identity,
                    13,
                    False,
                    p1_present=True,
                    now=1.1,
                    native_leader_is_known_guest=True,
                )
            )
            self.assertTrue(controller.active)
            request.assert_not_called()

            self.assertIsNone(
                controller.tick(
                    process,
                    identity,
                    13,
                    True,
                    p1_present=True,
                    now=2.0,
                    native_leader_is_known_guest=True,
                )
            )
            request.assert_called_once_with(process, 4)
            self.assertTrue(controller.active)

            self.assertIsNone(
                controller.tick(
                    process,
                    identity,
                    4,
                    True,
                    p1_present=True,
                    now=2.1,
                )
            )
            self.assertFalse(controller.active)
            self.assertEqual(
                publish.call_args_list,
                [mock.call(process, False)],
            )

    def test_p3_ownership_is_independent_of_p2_ko(self):
        self.assertFalse(coop.follower_role_owns_movement(True, False, 0, 5))
        self.assertTrue(coop.follower_role_owns_movement(True, True, 0, 1))

    def test_p3_keeps_master_cave_alive_with_no_p2_actor(self):
        p2_owned = coop.follower_role_owns_movement(
            True, None, 0, 5, actor_present=False
        )
        p3_owned = coop.follower_role_owns_movement(True, True, 0, 1)
        self.assertFalse(p2_owned)
        self.assertTrue(p3_owned)
        self.assertTrue(coop.follower_movement_master_enabled(True, p2_owned, p3_owned))

    def test_torn_p3_status_read_retains_ownership_without_input(self):
        self.assertEqual(
            coop.coop_lifecycle_flags(
                True,
                True,
                False,
                None,
                p2_present=True,
                p3_present=True,
            ),
            (True, True, True),
        )
        self.assertTrue(
            coop.follower_role_owns_movement(
                True,
                None,
                0,
                1,
                actor_present=True,
            )
        )

    def test_identity_change_never_adopts_a_cached_actor_for_missing_role(self):
        old_identity = (0, 5, 1)
        new_identity = (1, 5, 0)
        self.assertNotEqual(old_identity, new_identity)
        self.assertEqual(coop.fresh_role_actor_mapping(None), (0, 0))
        self.assertEqual(
            coop.fresh_role_actor_mapping((7, 0x1234, 0x5678)),
            (0x1234, 0x5678),
        )

    def test_p1_p2_swap_does_not_invalidate_unchanged_p3_cache(self):
        old = coop.session_state.SessionIdentity(0, 5, 1)
        new = coop.session_state.SessionIdentity(5, 0, 1)
        self.assertTrue(coop.role_member_changed(old, new, "p1"))
        self.assertTrue(coop.role_member_changed(old, new, "p2"))
        self.assertFalse(coop.role_member_changed(old, new, "p3"))
        self.assertTrue(
            coop.role_cache_survives_scan_gap(
                member_changed=coop.role_member_changed(old, new, "p3"),
                member_id=1,
                current_layout=(5, 0, 1, 0xFFFF),
                ui_layout=(5, 0, 1, 0xFFFF),
                actor_validity=None,
            )
        )

    def test_changed_or_departed_role_never_keeps_cached_actor(self):
        for changed, layout, validity in (
            (True, (5, 0, 1, 0xFFFF), None),
            (False, (5, 0, 0xFFFF, 0xFFFF), None),
            (False, (5, 0, 1, 0xFFFF), False),
        ):
            with self.subTest(changed=changed, layout=layout, validity=validity):
                self.assertFalse(
                    coop.role_cache_survives_scan_gap(
                        member_changed=changed,
                        member_id=1,
                        current_layout=layout,
                        ui_layout=layout,
                        actor_validity=validity,
                    )
                )

    @staticmethod
    def _movement_snapshot(**overrides):
        values = dict(
            movement_allowed=False,
            identity=coop.session_state.SessionIdentity(0, 5, 1),
            current_layout=(0, 1, 0xFFFF, 0xFFFF),
            ui_layout=(0, 1, 0xFFFF, 0xFFFF),
            p1_gameplay_ready=True,
            user_control_enabled=True,
            native_menu_open=False,
            native_modal_active=False,
            loading=False,
            cutscene=False,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_p3_session_gate_does_not_require_p2_to_be_materialized(self):
        snapshot = self._movement_snapshot()
        self.assertTrue(
            coop.follower_session_movement_allowed(
                snapshot,
                p2_present=False,
                p3_present=True,
            )
        )

        lifecycle = coop.coop_lifecycle_flags(
            True,
            True,
            None,
            True,
            p2_present=False,
            p3_present=True,
        )
        p2_owned = coop.follower_role_owns_movement(
            lifecycle[1], None, 0, 5, actor_present=False
        )
        p3_owned = coop.follower_role_owns_movement(
            lifecycle[1], True, 0, 1, actor_present=True
        )
        self.assertFalse(p2_owned)
        self.assertTrue(p3_owned)
        self.assertTrue(
            coop.follower_movement_master_enabled(lifecycle[1], p2_owned, p3_owned)
        )

    def test_layout_coherence_is_enforced_independently_per_follower(self):
        current = (0, 5, 1, 0xFFFF)
        cases = (
            ((0, 0xFFFF, 1, 0xFFFF), False, True),
            ((0, 5, 0xFFFF, 0xFFFF), True, False),
        )
        for ui, expected_p2, expected_p3 in cases:
            with self.subTest(ui=ui):
                snapshot = self._movement_snapshot(
                    current_layout=current,
                    ui_layout=ui,
                )
                base = coop.follower_session_movement_allowed(
                    snapshot,
                    p2_present=True,
                    p3_present=True,
                )
                self.assertTrue(base)
                p2_coherent = coop.role_member_layout_coherent(5, current, ui)
                p3_coherent = coop.role_member_layout_coherent(1, current, ui)
                self.assertEqual(p2_coherent, expected_p2)
                self.assertEqual(p3_coherent, expected_p3)
                self.assertEqual(
                    coop.follower_role_owns_movement(
                        base,
                        True,
                        0,
                        5,
                        layout_coherent=p2_coherent,
                    ),
                    expected_p2,
                )
                self.assertEqual(
                    coop.follower_role_owns_movement(
                        base,
                        True,
                        0,
                        1,
                        layout_coherent=p3_coherent,
                    ),
                    expected_p3,
                )

    def test_cache_never_survives_a_role_specific_ui_layout_gap(self):
        current = (0, 5, 1, 0xFFFF)
        for member_id, ui in (
            (5, (0, 0xFFFF, 1, 0xFFFF)),
            (1, (0, 5, 0xFFFF, 0xFFFF)),
        ):
            with self.subTest(member_id=member_id, ui=ui):
                self.assertFalse(
                    coop.role_cache_survives_scan_gap(
                        member_changed=False,
                        member_id=member_id,
                        current_layout=current,
                        ui_layout=ui,
                        actor_validity=None,
                    )
                )

    def test_independent_follower_gate_keeps_native_safety_boundaries(self):
        for change in (
            {"native_menu_open": True},
            {"native_modal_active": True},
            {"user_control_enabled": False, "cutscene": True},
            {"loading": True, "p1_gameplay_ready": False},
        ):
            with self.subTest(change=change):
                self.assertFalse(
                    coop.follower_session_movement_allowed(
                        self._movement_snapshot(**change),
                        p2_present=False,
                        p3_present=True,
                    )
                )

    def test_sleep_stop_and_immobilize_block_manual_locomotion(self):
        process = FakeProcess()
        actor = 0x70000
        keep = 0x71000
        process.put(actor + 0x698, struct.pack("<Q", keep))
        process.put(keep + 0x3C, struct.pack("<I", 0))
        for status_bit in (3, 4, 14):
            process.put(keep + 0x64, struct.pack("<Q", 1 << status_bit))
            self.assertFalse(coop.actor_accepts_manual_movement(process, actor))
        process.put(keep + 0x64, struct.pack("<Q", 0))
        self.assertTrue(coop.actor_accepts_manual_movement(process, actor))

    def test_disable_does_not_duplicate_immobilize_policy(self):
        process = FakeProcess()
        actor = 0x72000
        keep = 0x73000
        process.put(actor + 0x698, struct.pack("<Q", keep))
        process.put(keep + 0x3C, struct.pack("<I", 0))
        process.put(keep + 0x64, struct.pack("<Q", 1 << 13))
        self.assertTrue(coop.actor_accepts_manual_movement(process, actor))

    def test_torn_actor_auxiliary_reads_degrade_to_zero_command(self):
        process = FakeProcess()
        actor = 0x73500
        self.assertFalse(coop.actor_fighting_stance(process, actor))
        self.assertFalse(coop.actor_execution_animation_active(process, actor, True))
        self.assertIsNone(
            coop.actor_execution_target_heading(
                process,
                actor,
                (1.0, 2.0, 3.0),
                {},
            )
        )

    def test_actor_manager_gap_is_unknown_not_party_departure(self):
        process = FakeProcess()
        actor = 0x74000
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", 0))
        self.assertIsNone(coop.party_actor_validity(process, actor))

    def test_stable_manager_without_actor_is_real_departure(self):
        process = FakeProcess()
        actor = 0x75000
        manager = 0x76000
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
        process.put(manager, struct.pack("<I", 1))
        process.put(manager + 8, struct.pack("<Q", 0))
        self.assertIs(coop.party_actor_validity(process, actor), False)

    def test_incomplete_manager_scan_is_not_proof_of_departure(self):
        process = FakeProcess()
        actor = 0x75500
        manager = 0x76500
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
        process.put(manager, struct.pack("<I", 2))
        # Slot zero is deliberately unreadable. Slot one is coherently empty;
        # the unread slot could have been this actor during a BWEI rebuild.
        process.put(manager + 8 + 8, struct.pack("<Q", 0))
        self.assertIsNone(coop.party_actor_validity(process, actor))

    def test_main_uses_per_role_status_and_leader_gates(self):
        source = inspect.getsource(coop.main)
        self.assertIn("if p2_input_allowed:", source)
        self.assertIn("p3_input_allowed,", source)
        p2_publish = source.index(
            "if p2_command_owned and p2_committed_owner is not None:"
        )
        self.assertIn("write_command(", source[p2_publish:])
        self.assertNotIn("p2_actor if p2_movement_owned else 0", source)
        p3_publish = source.index(
            "if p3_command_owned and p3_committed_owner is not None:"
        )
        self.assertIn(
            "write_third_player_command(",
            source[p3_publish:],
        )
        self.assertIn("if movement_active or shared_camera_active:", source)
        self.assertNotIn(
            "(movement_active or shared_camera_active) and p2_actor",
            source,
        )
        p3_ownership = source.index("p3_movement_owned = follower_role_owns_movement")
        reconcile = source.index("follow_handoff.reconcile_owners(", p3_ownership)
        apply_effects = source.index("apply_follower_handoff_effects(", reconcile)
        committed = source.index(
            "committed_owners = handoff_state.owner_map()", apply_effects
        )
        follower_master = source.index("follower_movement_master_enabled(", committed)
        shared_master = source.index("movement_active = bool(", follower_master)
        master_publish = source.index("publish_follower_movement_master(", p3_publish)
        self.assertLess(p3_ownership, reconcile)
        self.assertLess(reconcile, apply_effects)
        self.assertLess(apply_effects, committed)
        self.assertLess(committed, follower_master)
        self.assertLess(follower_master, shared_master)
        self.assertLess(p3_publish, master_publish)
        self.assertIn("member_id=p2_committed_owner.member_id", source[p2_publish:])
        self.assertIn("world_epoch=p3_committed_owner.world_epoch", source[p3_publish:])
        self.assertIn("p1_berserk_movement_owned", source[shared_master:])

    def test_role_swap_resolves_current_identity_before_handoff_reconciliation(self):
        source = inspect.getsource(coop.main)
        live_map = source.index(
            "handoff_role_actors = session_state.map_party_slot_actors("
        )
        p2_identity = source.index(
            "p2_member_id = session_snapshot.identity.p2_party_member_id",
            live_map,
        )
        p3_identity = source.index(
            "p3_member_id = session_snapshot.identity.p3_party_member_id",
            p2_identity,
        )
        desired = source.index("desired_owners = {", p3_identity)
        reconcile = source.index("follow_handoff.reconcile_owners(", desired)

        self.assertLess(live_map, p2_identity)
        self.assertLess(p2_identity, p3_identity)
        self.assertLess(p3_identity, desired)
        self.assertLess(desired, reconcile)
        # A config role swap must not wait for the slower cached actor resolver:
        # both replacement tokens are therefore visible to one transaction.
        handoff_window = source[live_map:reconcile]
        desired_window = source[desired:reconcile]
        for role in ("p2", "p3"):
            retained = desired_window.index(f"{role}_gap_owner")
            retain_gate = desired_window.index(
                f"if retain_{role}_mapping_gap", retained
            )
            replacement = desired_window.index(f"else {role}_token", retain_gate)
            desired_gate = desired_window.index(f"if desired_{role}", replacement)
            no_owner = desired_window.index("else None", desired_gate)
            self.assertLess(retained, retain_gate)
            self.assertLess(retain_gate, replacement)
            self.assertLess(replacement, desired_gate)
            self.assertLess(desired_gate, no_owner)
        self.assertNotIn("resolved_identity", handoff_window)

    def test_manual_role_toggle_disarms_its_mailbox_in_the_same_branch(self):
        source = inspect.getsource(coop.main)
        p2_toggle = source.index("if active and p2_control_toggle:")
        p3_toggle = source.index("if active and p3_control_toggle:", p2_toggle)
        after_toggles = source.index("p2_manual_control =", p3_toggle)
        self.assertIn(
            "disarm_second_player_command(process, world_epoch=world_epoch)",
            source[p2_toggle:p3_toggle],
        )
        self.assertIn(
            "disarm_third_player_command(process, world_epoch=world_epoch)",
            source[p3_toggle:after_toggles],
        )

    def test_f8_off_updates_the_follower_master_publication_cache_immediately(self):
        source = inspect.getsource(coop.main)
        f8_toggle = source.index(
            "if startup_safe_foreground_edge(f8, previous_f8, foreground):"
        )
        off_branch = source.index("elif not active:", f8_toggle)
        branch_end = source.index('print("P2 WASD"', off_branch)
        branch = source[off_branch:branch_end]
        publish = branch.index("publish_follower_movement_master(process, False)")
        cache = branch.index("published_follower_movement_active = False")

        self.assertLess(publish, cache)

    def test_f8_edge_is_seeded_and_tracks_raw_background_state(self):
        self.assertFalse(coop.startup_safe_foreground_edge(True, None, True))
        self.assertFalse(coop.startup_safe_foreground_edge(True, True, True))
        self.assertFalse(coop.startup_safe_foreground_edge(True, False, False))
        self.assertTrue(coop.startup_safe_foreground_edge(True, False, True))

        source = inspect.getsource(coop.main)
        self.assertIn("previous_f8: bool | None = None", source)
        self.assertIn("f8 = key_down(VK_F8)", source)
        self.assertNotIn("f8 = foreground and key_down(VK_F8)", source)

    def test_custom_wait_issuer_navigation_keeps_movement_shadow(self):
        process = FakeProcess()
        process.put(
            coop.COMMAND_BLOCK + coop.WAIT_MENU_APPLIED_OFFSET,
            b"\x01",
        )
        process.put(
            coop.NATIVE_COMMAND_FLAG_GLOBAL,
            struct.pack("<I", 1),
        )
        # Native left/right may publish another slot/actor token.  That must
        # not hand P2/P3 back to AI, even though exact close ownership is gone.
        process.put(
            coop.NATIVE_COMMAND_ACTOR_GLOBAL,
            struct.pack("<I", 0x00200013),
        )
        self.assertTrue(coop.custom_wait_shadow_preserves_movement(process))
        process.put(
            coop.NATIVE_COMMAND_FLAG_GLOBAL,
            struct.pack("<I", 0),
        )
        self.assertFalse(coop.custom_wait_shadow_preserves_movement(process))

    def test_custom_menu_keeps_p2_moving_in_active_mode(self):
        self.assertEqual(
            coop.coop_movement_control_flags(True, False, True, True),
            (True, True),
        )

    def test_custom_menu_holds_p2_still_in_wait_mode(self):
        self.assertEqual(
            coop.coop_movement_control_flags(True, False, True, False),
            (True, False),
        )
        self.assertEqual(
            coop.coop_movement_control_flags(True, False, False, True),
            (False, False),
        )
        self.assertEqual(
            coop.coop_movement_control_flags(True, True, False, False),
            (True, True),
        )
        self.assertEqual(
            coop.coop_movement_control_flags(False, True, True, True),
            (False, False),
        )

    def test_p2_interaction_holds_camera_before_modal_flag(self):
        self.assertTrue(coop.camera_session_gate(True, False, True, False, 2))
        self.assertFalse(coop.camera_session_gate(True, False, True, True, 2))
        self.assertFalse(coop.camera_session_gate(False, True, True, False, 2))

    def test_camera_reacquire_waits_for_three_native_ready_samples(self):
        requested = False
        pending = False
        samples = 0
        active, pending, samples = coop.advance_camera_reacquire_gate(
            True, requested, pending, samples, False
        )
        self.assertEqual((active, pending, samples), (False, True, 0))
        requested = True
        for expected in (1, 2):
            active, pending, samples = coop.advance_camera_reacquire_gate(
                True, requested, pending, samples, True
            )
            self.assertEqual((active, pending, samples), (False, True, expected))
        active, pending, samples = coop.advance_camera_reacquire_gate(
            True, requested, pending, samples, True
        )
        self.assertEqual((active, pending, samples), (True, False, 3))

    def test_camera_reacquire_does_not_reopen_after_shared_ownership(self):
        self.assertEqual(
            coop.advance_camera_reacquire_gate(True, True, False, 3, False),
            (True, False, 3),
        )
        self.assertEqual(
            coop.advance_camera_reacquire_gate(False, True, False, 3, False),
            (False, False, 0),
        )

    def test_camera_reacquire_rejects_zero_position_actor_sentinels(self):
        self.assertFalse(
            coop.camera_party_positions_ready((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        )
        self.assertFalse(
            coop.camera_party_positions_ready((210.0, 42.0, 239.0), (0.0, 0.0, 0.0))
        )
        self.assertTrue(
            coop.camera_party_positions_ready(
                (209.2, 42.3, 239.7), (211.4, 42.9, 239.1)
            )
        )

    def test_camera_reacquire_rejects_nonfinite_actor_positions(self):
        self.assertFalse(
            coop.camera_party_positions_ready((float("nan"), 1.0, 2.0), (3.0, 4.0, 5.0))
        )

    def test_actor_alive_uses_hp_and_native_ko_bit(self):
        process = FakeProcess()
        actor = 0x2000
        keep = 0x3000
        process.put(actor + 0x698, struct.pack("<Q", keep))
        process.put(keep + 0x48, struct.pack("<I", 120))
        process.put(keep + 0x3C, struct.pack("<I", 0))
        process.put(keep + 0x64, struct.pack("<Q", 0))
        self.assertIs(coop.actor_is_alive(process, actor), True)
        process.put(keep + 0x64, struct.pack("<Q", 1))
        self.assertIs(coop.actor_is_alive(process, actor), False)
        process.put(keep + 0x64, struct.pack("<Q", 0))
        process.put(keep + 0x48, struct.pack("<I", 0))
        self.assertIs(coop.actor_is_alive(process, actor), False)

    def test_party_resolution_uses_party_slot_not_active_flag(self):
        process = FakeProcess()
        manager = 0x4000
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
        process.put(manager, struct.pack("<I", 8))

        entries = []
        for actor_id, party_slot, active in ((5, 1, False), (2, 0, True)):
            bwei = 0x5000 + actor_id * 0x100
            actor = 0x8000 + actor_id * 0x1000
            phys = 0xA000 + actor_id * 0x1000
            keep = 0xC000 + actor_id * 0x1000
            root = 0xE000 + actor_id * 0x1000
            process.put(manager + 8 + actor_id * 8, struct.pack("<Q", bwei))
            process.put(bwei + 0x30, struct.pack("<Q", actor))
            process.put(bwei + 0x12, bytes((party_slot,)))
            process.put(
                actor + 0x08, struct.pack("<I", coop.PARTY_HANDLE_CLASS + actor_id)
            )
            process.put(
                actor + 0x04,
                struct.pack("<I", coop.ACTIVE_PARTY_ACTOR_FLAG if active else 0),
            )
            process.put(actor + 0x10, struct.pack("<Q", root))
            process.put(root + 0xB8, struct.pack("<Q", phys))
            process.put(actor + 0x698, struct.pack("<Q", keep))
            entries.append((actor_id, actor, phys, (0.0, 0.0, 0.0)))

        resolved = list(
            coop.resolve_active_party_actor_slots(process, entries).values()
        )
        self.assertEqual([entry[0] for entry in resolved], [2, 5])
        self.assertIs(coop.party_actor_validity(process, entries[0][1]), True)

    def test_party_resolution_keeps_native_ko_handle_class(self):
        process = FakeProcess()
        manager = 0x14000
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
        process.put(manager, struct.pack("<I", 2))
        entries = []
        for actor_id, party_slot, handle_class in (
            (0, 0, coop.PARTY_HANDLE_CLASS),
            (1, 1, coop.KO_PARTY_HANDLE_CLASS),
        ):
            bwei = 0x15000 + actor_id * 0x100
            actor = 0x16000 + actor_id * 0x1000
            root = 0x18000 + actor_id * 0x1000
            phys = 0x1A000 + actor_id * 0x1000
            keep = 0x1C000 + actor_id * 0x1000
            process.put(manager + 8 + actor_id * 8, struct.pack("<Q", bwei))
            process.put(bwei + 0x30, struct.pack("<Q", actor))
            process.put(bwei + 0x12, bytes((party_slot,)))
            process.put(actor + 0x08, struct.pack("<I", handle_class | actor_id))
            process.put(actor + 0x10, struct.pack("<Q", root))
            process.put(root + 0xB8, struct.pack("<Q", phys))
            process.put(actor + 0x698, struct.pack("<Q", keep))
            entries.append((actor_id, actor, phys, (0.0, 0.0, 0.0)))
        resolved = list(
            coop.resolve_active_party_actor_slots(process, entries).values()
        )
        self.assertEqual([entry[0] for entry in resolved], [0, 1])
        self.assertIs(coop.party_actor_validity(process, entries[1][1]), True)

    def test_party_resolution_keeps_native_leader_handle_class_and_slots(self):
        process = FakeProcess()
        manager = 0x24000
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
        process.put(manager, struct.pack("<I", 2))
        entries = []
        for actor_id, party_slot, handle_class in (
            (0, 2, coop.KO_PARTY_HANDLE_CLASS),
            (1, 0, coop.LEADER_PARTY_HANDLE_CLASS),
        ):
            bwei = 0x25000 + actor_id * 0x100
            actor = 0x26000 + actor_id * 0x1000
            root = 0x28000 + actor_id * 0x1000
            phys = 0x2A000 + actor_id * 0x1000
            keep = 0x2C000 + actor_id * 0x1000
            process.put(manager + 8 + actor_id * 8, struct.pack("<Q", bwei))
            process.put(bwei + 0x30, struct.pack("<Q", actor))
            process.put(bwei + 0x12, bytes((party_slot,)))
            process.put(actor + 0x08, struct.pack("<I", handle_class | actor_id))
            process.put(actor + 0x10, struct.pack("<Q", root))
            process.put(root + 0xB8, struct.pack("<Q", phys))
            process.put(actor + 0x698, struct.pack("<Q", keep))
            entries.append((actor_id, actor, phys, (0.0, 0.0, 0.0)))
        resolved = coop.resolve_active_party_actor_slots(process, entries)
        self.assertEqual(list(resolved), [0, 2])
        self.assertEqual(resolved[0][0], 1)
        self.assertEqual(resolved[2][0], 0)
        self.assertIs(coop.party_actor_validity(process, entries[1][1]), True)

    def test_party_resolution_ignores_handle_generation_after_full_rebuild(self):
        process = FakeProcess()
        manager = 0x34000
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
        process.put(manager, struct.pack("<I", 2))
        entries = []
        for actor_id, party_slot, handle_class in (
            (0, 0, 0x00400000),
            (1, 1, 0x00700000),
        ):
            bwei = 0x35000 + actor_id * 0x100
            actor = 0x36000 + actor_id * 0x1000
            root = 0x38000 + actor_id * 0x1000
            phys = 0x3A000 + actor_id * 0x1000
            keep = 0x3C000 + actor_id * 0x1000
            process.put(manager + 8 + actor_id * 8, struct.pack("<Q", bwei))
            process.put(bwei + 0x30, struct.pack("<Q", actor))
            process.put(bwei + 0x12, bytes((party_slot,)))
            process.put(actor + 0x08, struct.pack("<I", handle_class | actor_id))
            process.put(actor + 0x10, struct.pack("<Q", root))
            process.put(root + 0xB8, struct.pack("<Q", phys))
            process.put(actor + 0x698, struct.pack("<Q", keep))
            entries.append((actor_id, actor, phys, (0.0, 0.0, 0.0)))
        resolved = coop.resolve_active_party_actor_slots(process, entries)
        self.assertEqual(list(resolved), [0, 1])
        self.assertIs(coop.party_actor_validity(process, entries[0][1]), True)
        self.assertIs(coop.party_actor_validity(process, entries[1][1]), True)

    def test_party_resolution_rejects_stale_actor_manager_handle_index(self):
        process = FakeProcess()
        manager = 0x44000
        bwei = 0x45000
        actor = 0x46000
        keep = 0x47000
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
        process.put(manager, struct.pack("<I", 1))
        process.put(manager + 8, struct.pack("<Q", bwei))
        process.put(bwei + 0x30, struct.pack("<Q", actor))
        process.put(bwei + 0x12, b"\x01")
        # The BWEI is manager entry zero, but the actor belongs to generation
        # index one. This is exactly the transient alias seen during rebuilds.
        process.put(actor + 0x08, struct.pack("<I", 0x00100001))
        process.put(actor + 0x698, struct.pack("<Q", keep))
        entry = (0, actor, 0x48000, (0.0, 0.0, 0.0))
        self.assertEqual(
            coop.resolve_active_party_actor_slots(process, [entry]),
            {},
        )
        self.assertIs(coop.party_actor_validity(process, actor), False)

    def test_party_resolution_omits_ambiguous_native_party_slot(self):
        process = FakeProcess()
        manager = 0x54000
        process.put(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", manager))
        process.put(manager, struct.pack("<I", 2))
        entries = []
        for actor_id in (0, 1):
            bwei = 0x55000 + actor_id * 0x100
            actor = 0x56000 + actor_id * 0x1000
            keep = 0x58000 + actor_id * 0x1000
            process.put(
                manager + 8 + actor_id * 8,
                struct.pack("<Q", bwei),
            )
            process.put(bwei + 0x30, struct.pack("<Q", actor))
            process.put(bwei + 0x12, b"\x01")
            process.put(
                actor + 0x08,
                struct.pack("<I", coop.PARTY_HANDLE_CLASS | actor_id),
            )
            process.put(actor + 0x698, struct.pack("<Q", keep))
            entries.append(
                (actor_id, actor, 0x5A000 + actor_id * 0x1000, (0.0, 0.0, 0.0))
            )
        self.assertEqual(
            coop.resolve_active_party_actor_slots(process, entries),
            {},
        )


if __name__ == "__main__":
    unittest.main()
