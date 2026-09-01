import itertools
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import session_state as state


class FakeProcess:
    def __init__(self):
        self.memory = {}

    def put(self, address, data):
        for index, value in enumerate(data):
            self.memory[address + index] = value

    def put_u32(self, address, value):
        self.put(address, struct.pack("<I", value))

    def put_u64(self, address, value):
        self.put(address, struct.pack("<Q", value))

    def read(self, address, size):
        try:
            return bytes(self.memory[address + index] for index in range(size))
        except KeyError as error:
            raise OSError(address) from error

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


class SessionTrackerTests(unittest.TestCase):
    def test_invalid_virtual_file_replace_keeps_last_complete_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.json"
            expected = state.SessionIdentity(5, 3, 2)
            state.save_identity(expected, path)
            tracker = state.SessionTracker(path, poll_seconds=0.05)
            self.assertEqual(tracker.identity, expected)

            path.write_text('{"players":', encoding="utf-8")
            retained = tracker.reload(force=True)

        self.assertEqual(retained, expected)

    def test_missing_file_preserves_explicit_startup_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            expected = state.SessionIdentity(5, 4, 3)
            tracker = state.SessionTracker(
                Path(folder) / "missing.json",
                poll_seconds=0.05,
                initial_identity=expected,
            )

        self.assertEqual(tracker.identity, expected)


class ActiveMainPartyMemberTests(unittest.TestCase):
    def test_esper_names_do_not_fall_back_to_party_ids(self):
        self.assertEqual(state.PARTY_NAMES[27], "Belias")
        self.assertEqual(state.PARTY_NAMES[31], "Cuchulainn")
        self.assertEqual(state.PARTY_NAMES[39], "Zodiark")

    def test_guest_does_not_consume_a_playable_slot(self):
        layout = (5, 3, state.EMPTY_PARTY_ID, 11) + (state.EMPTY_PARTY_ID,) * 5
        self.assertEqual(state.active_main_party_members(layout), frozenset((5, 3)))

    def test_summon_owner_role_is_the_assigned_summoner_not_p1(self):
        identity = state.SessionIdentity(0, 5, 1)
        layout = (5, state.EMPTY_PARTY_ID, state.EMPTY_PARTY_ID, 27) + (
            state.EMPTY_PARTY_ID,
        ) * 5

        self.assertEqual(state.active_esper_party_member_id(layout, layout), 27)
        self.assertEqual(
            state.active_summon_owner_party_member_id(identity, layout, layout, 5),
            5,
        )
        self.assertEqual(
            state.active_summon_owner_role(identity, layout, layout, 5),
            "p2",
        )

    def test_torn_esper_layout_never_claims_summon_ownership(self):
        identity = state.SessionIdentity(0, 5, 1)
        current = (5, state.EMPTY_PARTY_ID, state.EMPTY_PARTY_ID, 27) + (
            state.EMPTY_PARTY_ID,
        ) * 5
        ui = (5, state.EMPTY_PARTY_ID, state.EMPTY_PARTY_ID, 28) + (
            state.EMPTY_PARTY_ID,
        ) * 5

        self.assertIsNone(state.active_summon_owner_role(identity, current, ui, 5))


def ready_process(
    *,
    leader=0,
    control=1,
    menu=0,
    menu_flag=None,
    town=False,
    modal=False,
    chocobo=False,
):
    process = FakeProcess()
    battle = 0x500000
    location = 0x600000
    control_root = 0x700000
    location_table = 0x800000
    ui_manager = 0x900000
    ui_object = 0x910000
    process.put_u64(state.BATTLE_INFO_GLOBAL, battle)
    process.put_u64(state.LOCATION_SAVE_GLOBAL, location)
    process.put_u64(state.USER_CONTROL_GLOBAL, control_root)
    process.put_u64(state.LOCATION_INFO_GLOBAL, location_table)
    process.put_u64(state.UI_MANAGER_GLOBAL, ui_manager)
    process.put_u32(state.NATIVE_MENU_STATE, menu)
    process.put_u32(
        state.NATIVE_COMMAND_FLAG_GLOBAL,
        int(bool(menu)) if menu_flag is None else int(menu_flag),
    )
    process.put_u32(battle + state.BATTLE_LEADER_OFFSET, leader)
    process.put_u32(
        battle + state.BATTLE_MODE_FLAGS_OFFSET,
        state.BATTLE_CHOCOBO_MODE_BIT if chocobo else 0,
    )
    layout = struct.pack("<9H", 0, 5, *([state.EMPTY_PARTY_ID] * 7))
    for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
        process.put(
            battle
            + state.BATTLE_PARTY_LAYOUT_OFFSET
            + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
            layout,
        )
    process.put_u32(location + state.LOCATION_CURRENT_OFFSET, 227)
    process.put_u32(location + state.LOCATION_UI_OFFSET, 227)
    process.put(location + state.LOCATION_FLAGS_OFFSET, bytes((int(town),)))
    process.put(control_root + state.USER_CONTROL_ENABLED_OFFSET, bytes((control,)))
    process.put_u32(location_table + 4, 300)
    process.put(location_table + 0x0A + 227 * 3, b"\x00")
    process.put_u32(
        ui_manager + state.NATIVE_MODAL_MANAGER_FLAGS_OFFSET,
        0x40 if modal else 0,
    )
    process.put_u64(ui_manager + state.NATIVE_MODAL_MANAGER_OBJECT_OFFSET, ui_object)
    process.put(
        ui_object + state.NATIVE_MODAL_OBJECT_FLAGS_OFFSET,
        struct.pack("<H", 0x08 if modal else 0),
    )
    return process


class SessionStateTests(unittest.TestCase):
    def test_p2_summon_is_a_ready_world_owned_only_by_p2(self):
        process = ready_process(leader=5)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        summon_layout = struct.pack(
            "<9H",
            5,
            state.EMPTY_PARTY_ID,
            state.EMPTY_PARTY_ID,
            27,
            *([state.EMPTY_PARTY_ID] * 5),
        )
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                summon_layout,
            )

        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5, 1),
            materialized_party_count=2,
        )

        self.assertFalse(snapshot.loading)
        self.assertTrue(snapshot.gameplay_ready)
        self.assertFalse(snapshot.p1_gameplay_ready)
        self.assertFalse(snapshot.p1_menu_allowed)
        self.assertTrue(snapshot.p2_menu_allowed)
        self.assertTrue(snapshot.movement_allowed)
        self.assertIn("active summon owned by P2", snapshot.reason)
        self.assertIsNone(state.replacement_identity_for_live_party(snapshot))

    def test_role_reconciliation_is_exhaustive_for_native_slot_classes(self):
        empty = state.EMPTY_PARTY_ID
        checked = 0
        for configured_ids in itertools.permutations(range(6), 3):
            configured = state.SessionIdentity(*configured_ids)
            for live_count in (1, 2, 3):
                for live in itertools.permutations(range(6), live_count):
                    for guest in (None, 11):
                        slots = (
                            tuple(live)
                            + (empty,) * (3 - live_count)
                            + ((guest,) if guest is not None else (empty,))
                            + (empty,) * 5
                        )
                        leaders = tuple(live) + ((guest,) if guest is not None else ())
                        for leader in leaders:
                            snapshot = SimpleNamespace(
                                identity=configured,
                                battle_info=1,
                                user_control_enabled=True,
                                native_menu_open=False,
                                native_modal_active=False,
                                current_location=7,
                                ui_location=7,
                                current_layout=slots,
                                ui_layout=slots,
                                leader_party_member_id=leader,
                            )
                            replacement = state.replacement_identity_for_live_party(
                                snapshot,
                            )
                            if leader not in state.MAIN_PARTY_MEMBER_IDS:
                                self.assertIsNone(replacement)
                                continue

                            effective = replacement or configured
                            roles = (
                                effective.p1_party_member_id,
                                effective.p2_party_member_id,
                                effective.p3_party_member_id,
                            )
                            self.assertEqual(effective.p1_party_member_id, leader)
                            self.assertTrue(
                                all(
                                    member_id in state.MAIN_PARTY_MEMBER_IDS
                                    for member_id in roles
                                    if member_id is not None
                                )
                            )
                            assigned_roles = {
                                member_id for member_id in roles if member_id is not None
                            }
                            self.assertEqual(assigned_roles, set(live))
                            if (
                                configured.p2_party_member_id in live
                                and leader != configured.p2_party_member_id
                            ):
                                self.assertEqual(
                                    effective.p2_party_member_id,
                                    configured.p2_party_member_id,
                                )
                            if (
                                configured.p3_party_member_id in live
                                and leader != configured.p3_party_member_id
                            ):
                                self.assertEqual(
                                    effective.p3_party_member_id,
                                    configured.p3_party_member_id,
                                )
                            checked += 1
        self.assertEqual(checked, 102240)

    def test_single_configured_role_cannot_collide_with_other_default(self):
        config = state.normalize_config({"players": {"p1": {"party_member_id": 5}}})
        identity = state.SessionIdentity.from_config(config)
        self.assertNotEqual(identity.p1_party_member_id, identity.p2_party_member_id)

    def test_guest_or_aliased_roles_are_never_persisted_from_config(self):
        config = state.normalize_config(
            {
                "players": {
                    "p1": {"party_member_id": 11},
                    "p2": {"party_member_id": 5},
                    "p3": {"party_member_id": 5},
                }
            }
        )
        identity = state.SessionIdentity.from_config(config)
        self.assertEqual(identity, state.SessionIdentity(0, 5, None, 3))

    def test_field_allows_both_menus_and_camera(self):
        snapshot = state.read_snapshot(
            ready_process(), state.SessionIdentity(), materialized_party_count=2
        )
        self.assertTrue(snapshot.p1_menu_allowed)
        self.assertTrue(snapshot.p2_menu_allowed)
        self.assertTrue(snapshot.camera_allowed)

    def test_world_signature_requires_coherent_roots_and_location(self):
        process = ready_process()
        snapshot = state.read_snapshot(
            process, state.SessionIdentity(), materialized_party_count=2
        )
        self.assertEqual(
            snapshot.coherent_world_signature,
            (snapshot.battle_info, 227),
        )

        location = process.u64(state.LOCATION_SAVE_GLOBAL)
        process.put_u32(location + state.LOCATION_UI_OFFSET, 228)
        torn = state.read_snapshot(
            process, state.SessionIdentity(), materialized_party_count=2
        )
        self.assertIsNone(torn.coherent_world_signature)

    def test_chocobo_mode_releases_native_buttons_and_blocks_battle_menus(self):
        snapshot = state.read_snapshot(
            ready_process(chocobo=True),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertTrue(snapshot.chocobo_mode_active)
        self.assertEqual(snapshot.reason, "chocobo")
        self.assertFalse(snapshot.p1_menu_allowed)
        self.assertFalse(snapshot.p2_menu_allowed)
        self.assertFalse(snapshot.bridge_ownership_allowed)
        self.assertTrue(snapshot.movement_allowed)
        self.assertTrue(snapshot.camera_allowed)

    def test_other_battle_mode_bits_are_not_mistaken_for_chocobo(self):
        process = ready_process()
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        process.put_u32(battle + state.BATTLE_MODE_FLAGS_OFFSET, 0x08)
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertFalse(snapshot.chocobo_mode_active)
        self.assertTrue(snapshot.p1_menu_allowed)

    def test_solo_leader_keeps_f6_and_p1_world_input_available(self):
        process = ready_process(leader=0)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        solo = struct.pack("<9H", 0, *([state.EMPTY_PARTY_ID] * 8))
        for layout_id in (
            state.BATTLE_CURRENT_LAYOUT,
            state.BATTLE_UI_LAYOUT,
        ):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                solo,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5),
            materialized_party_count=1,
        )
        self.assertFalse(snapshot.loading)
        self.assertTrue(snapshot.p1_gameplay_ready)
        self.assertFalse(snapshot.gameplay_ready)
        self.assertTrue(snapshot.p1_menu_allowed)
        self.assertFalse(snapshot.p2_menu_allowed)
        self.assertTrue(snapshot.bridge_ownership_allowed)
        self.assertFalse(snapshot.movement_allowed)
        self.assertFalse(snapshot.camera_allowed)

    def test_solo_new_leader_reassigns_p1_and_keeps_distinct_p2_placeholder(self):
        identity = state.available_identity(
            state.SessionIdentity(0, 5),
            (5, state.EMPTY_PARTY_ID),
            (5, state.EMPTY_PARTY_ID),
            leader=5,
        )
        self.assertEqual(identity, state.SessionIdentity(5, None))

    def test_nonleader_blocks_only_p1_menu(self):
        snapshot = state.read_snapshot(
            ready_process(leader=5),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertFalse(snapshot.p1_menu_allowed)
        self.assertTrue(snapshot.p2_menu_allowed)
        self.assertTrue(snapshot.p2_is_leader)

    def test_town_blocks_combat_menus_but_keeps_camera(self):
        snapshot = state.read_snapshot(
            ready_process(town=True),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertTrue(snapshot.town)
        self.assertFalse(snapshot.p1_menu_allowed)
        self.assertFalse(snapshot.p2_menu_allowed)
        self.assertTrue(snapshot.camera_allowed)

    def test_control_lock_is_cutscene_and_releases_camera(self):
        snapshot = state.read_snapshot(
            ready_process(control=0),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertTrue(snapshot.cutscene)
        self.assertFalse(snapshot.camera_allowed)
        self.assertFalse(snapshot.p2_menu_allowed)

    def test_native_world_modal_stops_movement_but_retains_camera(self):
        snapshot = state.read_snapshot(
            ready_process(control=0, modal=True),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertTrue(snapshot.native_modal_active)
        self.assertFalse(snapshot.cutscene)
        self.assertFalse(snapshot.movement_allowed)
        self.assertTrue(snapshot.camera_allowed)

    def test_native_world_modal_closes_all_custom_command_menus(self):
        snapshot = state.read_snapshot(
            ready_process(control=1, modal=True),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertTrue(snapshot.native_modal_active)
        self.assertFalse(snapshot.p1_menu_allowed)
        self.assertFalse(snapshot.p2_menu_allowed)

    def test_native_menu_retains_camera_but_releases_movement(self):
        snapshot = state.read_snapshot(
            ready_process(menu=4),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertFalse(snapshot.movement_allowed)
        self.assertTrue(snapshot.camera_allowed)

    def test_field_target_actor_token_does_not_suspend_followers(self):
        snapshot = state.read_snapshot(
            ready_process(menu=5, menu_flag=0),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertFalse(snapshot.native_menu_open)
        self.assertEqual(snapshot.reason, "field")
        self.assertTrue(snapshot.movement_allowed)
        self.assertTrue(snapshot.bridge_ownership_allowed)

    def test_native_fullscreen_menu_releases_input_bridge(self):
        snapshot = state.read_snapshot(
            ready_process(menu=4),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertFalse(snapshot.bridge_ownership_allowed)
        self.assertFalse(snapshot.p1_menu_allowed)

    def test_owned_command_shadow_keeps_menu_input_but_not_movement(self):
        snapshot = state.read_snapshot(
            ready_process(menu=1),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        effective = state.mask_owned_command_shadow(snapshot, owned=True)
        self.assertFalse(effective.native_menu_open)
        self.assertTrue(effective.bridge_ownership_allowed)
        self.assertTrue(effective.p1_menu_allowed)
        self.assertTrue(effective.p2_menu_allowed)
        self.assertFalse(effective.movement_allowed)

    def test_unowned_native_menu_is_never_masked(self):
        snapshot = state.read_snapshot(
            ready_process(menu=4),
            state.SessionIdentity(),
            materialized_party_count=2,
        )
        self.assertIs(state.mask_owned_command_shadow(snapshot, owned=False), snapshot)

    def test_transition_mismatch_is_loading(self):
        process = ready_process()
        location = process.u64(state.LOCATION_SAVE_GLOBAL)
        process.put_u32(location + state.LOCATION_UI_OFFSET, 228)
        snapshot = state.read_snapshot(
            process, state.SessionIdentity(), materialized_party_count=2
        )
        self.assertTrue(snapshot.loading)
        self.assertFalse(snapshot.camera_allowed)

    def test_roles_follow_member_ids_after_party_slot_swap(self):
        process = ready_process(leader=5)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        swapped = struct.pack("<9H", 5, 0, *([state.EMPTY_PARTY_ID] * 7))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                swapped,
            )
        snapshot = state.read_snapshot(
            process, state.SessionIdentity(), materialized_party_count=2
        )
        mapping = state.map_party_slot_actors(
            snapshot, {0: "Penelo actor", 1: "Vaan actor"}
        )
        self.assertEqual(mapping[0], "Vaan actor")
        self.assertEqual(mapping[5], "Penelo actor")

    def test_reserve_ids_do_not_shift_materialized_actor_mapping(self):
        process = ready_process()
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        layout = struct.pack(
            "<9H",
            0,
            5,
            state.EMPTY_PARTY_ID,
            state.EMPTY_PARTY_ID,
            3,
            4,
            state.EMPTY_PARTY_ID,
            state.EMPTY_PARTY_ID,
            state.EMPTY_PARTY_ID,
        )
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                layout,
            )
        snapshot = state.read_snapshot(
            process, state.SessionIdentity(), materialized_party_count=2
        )
        mapping = state.map_party_slot_actors(
            snapshot, {0: "Vaan actor", 1: "Penelo actor"}
        )
        self.assertEqual(mapping, {0: "Vaan actor", 5: "Penelo actor"})

    def test_explicit_party_slots_preserve_a_missing_leader_gap(self):
        process = ready_process(leader=0)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        layout = struct.pack("<9H", 0, 3, 2, *([state.EMPTY_PARTY_ID] * 6))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                layout,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 2),
            materialized_party_count=2,
        )
        mapping = state.map_party_slot_actors(
            snapshot, {1: "Balthier actor", 2: "Fran actor"}
        )
        self.assertEqual(mapping, {3: "Balthier actor", 2: "Fran actor"})
        self.assertEqual(state.party_slot_for_member(snapshot, 2), 2)
        self.assertEqual(state.party_slot_for_member(snapshot, 0), 0)
        self.assertIsNone(state.party_slot_for_member(snapshot, 4))

    def test_available_identity_preserves_roles_still_in_party(self):
        identity = state.available_identity(
            state.SessionIdentity(0, 4),
            (4, 0, 5),
            (0, 5, 4),
            leader=0,
        )
        self.assertEqual(identity, state.SessionIdentity(0, 4))

    def test_ko_promoted_p2_keeps_the_existing_role_owner(self):
        identity = state.SessionIdentity(4, 2, 1)
        self.assertEqual(
            state.ko_promoted_follower_role(identity, 2, False, p1_present=True),
            "p2",
        )
        # One torn death read must not turn the promotion into a deliberate
        # P1 character selection before the next coherent actor sample.
        self.assertEqual(
            state.ko_promoted_follower_role(identity, 2, None, p1_present=True),
            "p2",
        )

    def test_ko_promoted_p3_keeps_the_existing_role_owner(self):
        identity = state.SessionIdentity(4, 2, 1)
        self.assertEqual(
            state.ko_promoted_follower_role(identity, 1, False, p1_present=True),
            "p3",
        )

    def test_live_or_departed_p1_never_creates_a_ko_takeover(self):
        identity = state.SessionIdentity(4, 2, 1)
        self.assertIsNone(
            state.ko_promoted_follower_role(identity, 2, True, p1_present=True)
        )
        self.assertIsNone(
            state.ko_promoted_follower_role(identity, 2, False, p1_present=False)
        )
        self.assertIsNone(
            state.ko_promoted_follower_role(identity, 0, False, p1_present=True)
        )

    def test_available_identity_moves_p1_to_new_live_leader(self):
        identity = state.available_identity(
            state.SessionIdentity(0, 5),
            (0, 5, 4),
            (0, 5, 4),
            leader=4,
        )
        self.assertEqual(identity, state.SessionIdentity(4, 5))

    def test_leader_taking_p2_character_moves_p2_to_former_p1(self):
        identity = state.available_identity(
            state.SessionIdentity(0, 5),
            (0, 5, 4),
            (0, 5, 4),
            leader=5,
        )
        self.assertEqual(identity, state.SessionIdentity(5, 0))

    def test_removed_p2_replaces_only_p2_and_preserves_p3_owner(self):
        identity = state.available_identity(
            state.SessionIdentity(0, 1, 4),
            (0, 4, 5),
            (0, 4, 5),
            leader=0,
        )
        self.assertEqual(identity, state.SessionIdentity(0, 5, 4))

    def test_removed_p3_replaces_only_p3_and_preserves_p2_owner(self):
        identity = state.available_identity(
            state.SessionIdentity(0, 1, 4),
            (0, 1, 5),
            (0, 1, 5),
            leader=0,
        )
        self.assertEqual(identity, state.SessionIdentity(0, 1, 5))

    def test_empty_seats_follow_join_order_without_compacting_p3(self):
        empty = state.EMPTY_PARTY_ID
        identity = state.SessionIdentity(0, None, None, 3)

        identity = state.available_identity(
            identity,
            (0, 5, empty, empty),
            (0, 5, empty, empty),
            leader=0,
        )
        self.assertEqual(identity, state.SessionIdentity(0, 5, None, 3))

        identity = state.available_identity(
            identity,
            (0, 5, 1, empty),
            (0, 5, 1, empty),
            leader=0,
        )
        self.assertEqual(identity, state.SessionIdentity(0, 5, 1, 3))

        identity = state.available_identity(
            identity,
            (0, 1, empty, empty),
            (0, 1, empty, empty),
            leader=0,
        )
        self.assertEqual(identity, state.SessionIdentity(0, None, 1, 3))

        identity = state.available_identity(
            identity,
            (0, empty, empty, empty),
            (0, empty, empty, empty),
            leader=0,
        )
        self.assertEqual(identity, state.SessionIdentity(0, None, None, 3))

        identity = state.available_identity(
            identity,
            (0, 4, empty, empty),
            (0, 4, empty, empty),
            leader=0,
        )
        self.assertEqual(identity, state.SessionIdentity(0, 4, None, 3))

    def test_p3_remains_gameplay_ready_while_p2_seat_is_empty(self):
        process = ready_process(leader=0)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        layout = struct.pack("<9H", 0, 1, *([state.EMPTY_PARTY_ID] * 7))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                layout,
            )

        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, None, 1, 3),
            materialized_party_count=2,
        )

        self.assertTrue(snapshot.gameplay_ready)
        self.assertTrue(snapshot.movement_allowed)
        self.assertFalse(snapshot.p2_menu_allowed)
        self.assertFalse(snapshot.p2_is_leader)

    def test_empty_p2_never_matches_an_unknown_native_leader(self):
        identity = state.SessionIdentity(0, None, 1, 3)
        self.assertIsNone(
            state.ko_promoted_follower_role(
                identity,
                None,
                False,
                p1_present=True,
            )
        )
        layout = (0, 1, state.EMPTY_PARTY_ID, state.EMPTY_PARTY_ID)
        self.assertIsNone(
            state.active_summon_owner_role(identity, layout, layout, None)
        )

    def test_p1_taking_p3_character_preserves_p2_and_swaps_only_p1_p3(self):
        identity = state.available_identity(
            state.SessionIdentity(1, 0, 4),
            (0, 4, 1),
            (0, 4, 1),
            leader=4,
        )
        self.assertEqual(identity, state.SessionIdentity(4, 0, 1))

    def test_snapshot_does_not_alias_p2_as_p1_before_reconciliation(self):
        snapshot = state.read_snapshot(
            ready_process(leader=5),
            state.SessionIdentity(0, 5),
            materialized_party_count=2,
        )
        self.assertEqual(snapshot.identity.p1_party_member_id, 0)
        self.assertEqual(snapshot.p1_owner_party_member_id, 0)
        self.assertFalse(snapshot.p1_is_leader)

    def test_solo_mode_gives_p1_menu_to_the_live_native_leader(self):
        snapshot = state.read_snapshot(
            ready_process(leader=5),
            state.SessionIdentity(0, 5),
            materialized_party_count=2,
        )
        solo = state.with_solo_leader_ownership(snapshot)

        self.assertEqual(solo.identity, state.SessionIdentity(0, 5))
        self.assertEqual(solo.p1_reserved_party_member_id, 0)
        self.assertEqual(solo.p1_owner_party_member_id, 5)
        self.assertTrue(solo.p1_is_leader)
        self.assertTrue(solo.p1_menu_allowed)
        self.assertTrue(solo.p1_gameplay_ready)

    def test_solo_mode_uses_live_leader_when_configured_p1_left_party(self):
        process = ready_process(leader=5)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        live = struct.pack("<9H", 5, 1, *([state.EMPTY_PARTY_ID] * 7))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                live,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5, 1),
            materialized_party_count=2,
        )
        self.assertFalse(snapshot.p1_gameplay_ready)

        solo = state.with_solo_leader_ownership(snapshot)

        self.assertEqual(solo.p1_owner_party_member_id, 5)
        self.assertTrue(solo.p1_gameplay_ready)
        self.assertTrue(solo.bridge_ownership_allowed)
        self.assertTrue(solo.p1_menu_allowed)
        self.assertFalse(solo.loading)
        self.assertFalse(solo.gameplay_ready)
        self.assertFalse(solo.p2_menu_allowed)
        self.assertFalse(solo.movement_allowed)
        self.assertFalse(solo.camera_allowed)
        self.assertEqual(solo.reason, "field")

    def test_solo_mode_ignores_a_torn_native_leader_sample(self):
        snapshot = state.read_snapshot(
            ready_process(leader=5),
            state.SessionIdentity(0, 5),
            materialized_party_count=2,
        )
        torn = state.replace(snapshot, leader_party_member_id=4)

        self.assertIs(state.with_solo_leader_ownership(torn), torn)

    def test_snapshot_exposes_guest_as_transient_p1_without_persisting_it(self):
        process = ready_process(leader=11)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        with_guest = struct.pack("<9H", 0, 5, 1, 11, *([state.EMPTY_PARTY_ID] * 5))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                with_guest,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5, 1),
            materialized_party_count=4,
        )
        self.assertEqual(snapshot.p1_owner_party_member_id, 11)
        self.assertEqual(snapshot.p1_reserved_party_member_id, 0)
        self.assertEqual(snapshot.identity, state.SessionIdentity(0, 5, 1))
        self.assertTrue(snapshot.p1_is_leader)
        self.assertTrue(snapshot.p1_menu_allowed)

    def test_reks_only_prologue_is_a_ready_transient_p1_generation(self):
        process = ready_process(leader=6)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        reks_only = struct.pack(
            "<9H", 6, *([state.EMPTY_PARTY_ID] * 8)
        )
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                reks_only,
            )

        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5, 1),
            materialized_party_count=1,
        )

        self.assertEqual(snapshot.p1_owner_party_member_id, 6)
        self.assertEqual(snapshot.p1_reserved_party_member_id, 0)
        self.assertEqual(snapshot.identity, state.SessionIdentity(0, 5, 1))
        self.assertTrue(snapshot.p1_gameplay_ready)
        self.assertTrue(snapshot.p1_is_leader)
        self.assertTrue(snapshot.p1_menu_allowed)
        self.assertTrue(snapshot.bridge_ownership_allowed)

    def test_esper_can_never_become_transient_p1(self):
        identity = state.SessionIdentity(0, 5, 1)
        layout = (0, 5, 33, *([state.EMPTY_PARTY_ID] * 6))

        self.assertEqual(
            state.p1_controlled_party_member_id(identity, layout, layout, 33),
            0,
        )

    def test_unknown_party_id_can_never_become_transient_p1(self):
        self.assertEqual(
            state.p1_controlled_party_member_id(
                state.SessionIdentity(0, 5, 1),
                (0, 5, 1, 99),
                (0, 5, 1, 99),
                99,
            ),
            0,
        )

    def test_live_party_replacement_transfers_absent_p2_to_substitute(self):
        snapshot = state.read_snapshot(
            ready_process(leader=0),
            state.SessionIdentity(0, 4),
            materialized_party_count=2,
        )
        replacement = state.replacement_identity_for_live_party(snapshot)
        self.assertEqual(replacement, state.SessionIdentity(0, 5))

    def test_cutscene_temporary_party_never_rewrites_player_identity(self):
        process = ready_process(leader=0, control=0)
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 4),
            materialized_party_count=2,
        )
        self.assertFalse(snapshot.user_control_enabled)
        self.assertIsNone(
            state.replacement_identity_for_live_party(
                snapshot,
            )
        )

        control_root = process.u64(state.USER_CONTROL_GLOBAL)
        process.put(
            control_root + state.USER_CONTROL_ENABLED_OFFSET,
            b"\x01",
        )
        resumed = state.read_snapshot(
            process,
            state.SessionIdentity(0, 4),
            materialized_party_count=2,
        )
        self.assertEqual(
            state.replacement_identity_for_live_party(
                resumed,
            ),
            state.SessionIdentity(0, 5),
        )

    def test_live_party_replacement_allows_only_two_materialized_field_actors(self):
        process = ready_process(leader=5)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        layout = struct.pack("<9H", 2, 1, 5, *([state.EMPTY_PARTY_ID] * 6))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                layout,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(5, 3),
            materialized_party_count=2,
        )
        replacement = state.replacement_identity_for_live_party(snapshot)
        self.assertEqual(replacement, state.SessionIdentity(5, 2))

    def test_complete_party_swap_reconciles_before_actors_materialize(self):
        process = ready_process(leader=4)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        layout = struct.pack("<9H", 4, 1, 5, *([state.EMPTY_PARTY_ID] * 6))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                layout,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 3),
            materialized_party_count=0,
        )
        replacement = state.replacement_identity_for_live_party(snapshot)
        self.assertEqual(replacement, state.SessionIdentity(4, 1))

    def test_two_slot_party_edit_keeps_the_active_p3_character_in_p3(self):
        process = ready_process(leader=0)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        layout = struct.pack("<9H", 0, 4, *([state.EMPTY_PARTY_ID] * 7))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                layout,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(5, 1, 4),
            materialized_party_count=2,
        )

        replacement = state.replacement_identity_for_live_party(snapshot)

        self.assertEqual(replacement, state.SessionIdentity(0, None, 4))

    def test_removing_p2_does_not_promote_the_active_p3_character(self):
        process = ready_process(leader=5)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        layout = struct.pack("<9H", 5, 4, *([state.EMPTY_PARTY_ID] * 7))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                layout,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(5, 3, 4),
            materialized_party_count=2,
        )

        self.assertEqual(
            state.replacement_identity_for_live_party(snapshot),
            state.SessionIdentity(5, None, 4),
        )

    def test_replacing_p2_preserves_the_active_p3_character(self):
        process = ready_process(leader=5)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        layout = struct.pack("<9H", 5, 0, 4, *([state.EMPTY_PARTY_ID] * 6))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                layout,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(5, 3, 4),
            materialized_party_count=3,
        )

        self.assertEqual(
            state.replacement_identity_for_live_party(snapshot),
            state.SessionIdentity(5, 0, 4),
        )

    def test_departed_p3_releases_the_third_player_seat(self):
        process = ready_process(leader=0)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        layout = struct.pack("<9H", 0, 5, *([state.EMPTY_PARTY_ID] * 7))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                layout,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5, 1),
            materialized_party_count=2,
        )

        self.assertEqual(
            state.replacement_identity_for_live_party(snapshot),
            state.SessionIdentity(0, 5, None, 3),
        )

    def test_empty_follower_seats_fill_in_active_party_order(self):
        identity = state.SessionIdentity(0, 5, 1)
        identity = state.available_identity(identity, (0,), (0,), leader=0)
        self.assertEqual(identity, state.SessionIdentity(0, None, None, 3))

        # The first active follower owns P2; adding a second follower owns P3.
        identity = state.available_identity(identity, (0, 5), (0, 5), leader=0)
        self.assertEqual(identity, state.SessionIdentity(0, 5, None, 3))
        identity = state.available_identity(identity, (0, 5, 1), (0, 5, 1), leader=0)
        self.assertEqual(identity, state.SessionIdentity(0, 5, 1))

        # Removing P2 does not promote the still-live P3 character.
        identity = state.available_identity(identity, (0, 1), (0, 1), leader=0)
        self.assertEqual(identity, state.SessionIdentity(0, None, 1))

        # Once both followers leave, the next character claims the free P2 seat.
        identity = state.available_identity(identity, (0,), (0,), leader=0)
        self.assertEqual(identity, state.SessionIdentity(0, None, None, 3))
        identity = state.available_identity(identity, (0, 4), (0, 4), leader=0)
        self.assertEqual(identity, state.SessionIdentity(0, 4, None, 3))

    def test_live_party_replacement_follows_new_leader_when_p1_is_absent(self):
        process = ready_process(leader=5)
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(4, 0),
            materialized_party_count=2,
        )
        replacement = state.replacement_identity_for_live_party(snapshot)
        self.assertEqual(replacement, state.SessionIdentity(5, 0))

    def test_owned_command_shadow_reconciles_an_occupied_leader_swap(self):
        process = ready_process(leader=5)
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5, 1),
            materialized_party_count=3,
        )
        snapshot = replace(snapshot, native_menu_open=True)

        self.assertIsNone(state.replacement_identity_for_live_party(snapshot))

        effective = state.mask_owned_command_shadow(snapshot, owned=True)
        self.assertEqual(
            state.replacement_identity_for_live_party(effective),
            state.SessionIdentity(5, 0, None, 3),
        )

    def test_solo_field_reconciles_an_absent_configured_p1_to_leader(self):
        process = ready_process(leader=5)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        solo = struct.pack("<9H", 5, *([state.EMPTY_PARTY_ID] * 8))
        for layout_id in (
            state.BATTLE_CURRENT_LAYOUT,
            state.BATTLE_UI_LAYOUT,
        ):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                solo,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5),
            materialized_party_count=1,
        )
        self.assertEqual(
            state.replacement_identity_for_live_party(snapshot),
            state.SessionIdentity(5, None),
        )

    def test_identity_save_retries_a_transient_windows_replace_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text("{}", encoding="utf-8")
            real_replace = state.atomic_files.replace_file
            attempts = 0

            def flaky_replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("temporary scanner lock")
                return real_replace(source, destination)

            with mock.patch.object(
                state.atomic_files,
                "replace_file",
                side_effect=flaky_replace,
            ):
                state.save_identity(state.SessionIdentity(4, 5), path)
            self.assertEqual(attempts, 3)
            self.assertEqual(
                state.SessionIdentity.from_config(state.load_config(path)),
                state.SessionIdentity(4, 5),
            )

    def test_identity_save_drops_a_temporary_guest_role(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            state.save_identity(state.SessionIdentity(0, 5, 11), path)
            self.assertEqual(
                state.SessionIdentity.from_config(state.load_config(path)),
                state.SessionIdentity(0, 5, None, 3),
            )

    def test_live_party_replacement_rejects_transient_ui_generation(self):
        process = ready_process(leader=0)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        process.put(
            battle
            + state.BATTLE_PARTY_LAYOUT_OFFSET
            + state.BATTLE_UI_LAYOUT * state.BATTLE_PARTY_LAYOUT_STRIDE,
            struct.pack("<9H", 0, 4, *([state.EMPTY_PARTY_ID] * 7)),
        )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 4),
            materialized_party_count=2,
        )
        self.assertIsNone(state.replacement_identity_for_live_party(snapshot))

    def test_live_party_replacement_rejects_duplicate_main_member(self):
        process = ready_process(leader=0)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        duplicated = struct.pack("<9H", 0, 0, 5, 11, *([state.EMPTY_PARTY_ID] * 5))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                duplicated,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 4),
            materialized_party_count=3,
        )
        self.assertIsNone(state.replacement_identity_for_live_party(snapshot))

    def test_live_party_replacement_rejects_duplicate_guest(self):
        process = ready_process(leader=0)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        duplicated = struct.pack("<9H", 0, 5, 11, 11, *([state.EMPTY_PARTY_ID] * 5))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                duplicated,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 4),
            materialized_party_count=3,
        )
        self.assertIsNone(state.replacement_identity_for_live_party(snapshot))

    def test_leader_switch_with_guest_keeps_roles_on_main_members(self):
        process = ready_process(leader=1)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        with_guest = struct.pack("<9H", 0, 5, 1, 11, *([state.EMPTY_PARTY_ID] * 5))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                with_guest,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5, 1),
            materialized_party_count=3,
        )
        replacement = state.replacement_identity_for_live_party(snapshot)
        self.assertEqual(replacement, state.SessionIdentity(1, 5, 0))
        self.assertNotIn(
            11,
            (
                replacement.p1_party_member_id,
                replacement.p2_party_member_id,
                replacement.p3_party_member_id,
            ),
        )

    def test_guest_leader_generation_is_never_persisted(self):
        process = ready_process(leader=11)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        with_guest = struct.pack("<9H", 0, 5, 1, 11, *([state.EMPTY_PARTY_ID] * 5))
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                with_guest,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 4),
            materialized_party_count=3,
        )
        self.assertIsNone(state.replacement_identity_for_live_party(snapshot))

    def test_two_main_members_plus_guest_release_the_absent_p3_role(self):
        process = ready_process(leader=5)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        with_guest = struct.pack(
            "<9H", 0, 5, state.EMPTY_PARTY_ID, 11, *([state.EMPTY_PARTY_ID] * 5)
        )
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                with_guest,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5, 1),
            materialized_party_count=3,
        )
        self.assertEqual(
            state.replacement_identity_for_live_party(snapshot),
            state.SessionIdentity(5, 0, None, 3),
        )

    def test_removed_p1_does_not_keep_an_absent_follower_assignment(self):
        process = ready_process(leader=1)
        battle = process.u64(state.BATTLE_INFO_GLOBAL)
        with_guest = struct.pack(
            "<9H", 1, 5, state.EMPTY_PARTY_ID, 11, *([state.EMPTY_PARTY_ID] * 5)
        )
        for layout_id in (state.BATTLE_CURRENT_LAYOUT, state.BATTLE_UI_LAYOUT):
            process.put(
                battle
                + state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * state.BATTLE_PARTY_LAYOUT_STRIDE,
                with_guest,
            )
        snapshot = state.read_snapshot(
            process,
            state.SessionIdentity(0, 5, 1),
            materialized_party_count=3,
        )
        replacement = state.replacement_identity_for_live_party(snapshot)
        self.assertEqual(replacement, state.SessionIdentity(1, 5, None, 3))
        self.assertEqual(
            len(
                {
                    replacement.p1_party_member_id,
                    replacement.p2_party_member_id,
                }
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
