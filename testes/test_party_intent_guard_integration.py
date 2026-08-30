from __future__ import annotations

from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import headless_menu_shell as menu
import party_intent_channel
import penelo_story_guard as guard


class LayoutProcess:
    def __init__(self, base: int, default, native_menu):
        self.base = base
        self.layouts = {
            menu.BATTLE_DEFAULT_LAYOUT: tuple(default),
            menu.BATTLE_MENU_LAYOUT: tuple(native_menu),
        }

    def read(self, address: int, size: int) -> bytes:
        layout_id = (
            address - self.base - guard.PARTY_LAYOUT_OFFSET
        ) // guard.PARTY_LAYOUT_STRIDE
        return struct.pack("<9H", *self.layouts[layout_id])


def snapshot(base: int, current, ui, **overrides):
    values = dict(
        battle_info=base,
        current_layout=tuple(current),
        ui_layout=tuple(ui),
        native_menu_open=False,
        native_modal_active=False,
        loading=False,
        cutscene=False,
        user_control_enabled=True,
        current_location=10,
        ui_location=10,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class ExplicitPartyIntentIntegrationTests(unittest.TestCase):
    def test_headless_requires_all_four_layouts_and_field_generation(self):
        base = 0x500000
        active = (0, 5, 1, 11, 2, 3, 4, guard.EMPTY, guard.EMPTY)
        process = LayoutProcess(base, active, active)
        state = snapshot(base, active, active)
        self.assertEqual(
            menu.coherent_manual_party_members(process, state),
            (0, 5, 1),
        )
        process.layouts[menu.BATTLE_MENU_LAYOUT] = (
            0,
            5,
            3,
            11,
            1,
            2,
            4,
            guard.EMPTY,
            guard.EMPTY,
        )
        self.assertIsNone(menu.coherent_manual_party_members(process, state))
        process.layouts[menu.BATTLE_MENU_LAYOUT] = active
        self.assertIsNone(
            menu.coherent_manual_party_members(
                process,
                snapshot(base, active, active, user_control_enabled=False),
            )
        )

    def test_active_restore_uses_minimal_swaps_and_preserves_guest_reserves(self):
        original = (0, 4, 5, 11, 1, 2, 3, guard.EMPTY, guard.EMPTY)
        result = guard.apply_active_main_members(original, (0, 5, 1))
        self.assertEqual(result, (0, 5, 1, 11, 4, 2, 3, guard.EMPTY, guard.EMPTY))
        self.assertEqual(result[3], original[3])
        self.assertEqual(result[5:], original[5:])

    def test_two_member_intent_moves_only_the_extra_actor_to_empty_reserve(self):
        original = (0, 5, 1, 11, 4, 2, 3, guard.EMPTY, guard.EMPTY)
        result = guard.apply_active_main_members(original, (0, 5))
        self.assertEqual(result[:4], (0, 5, guard.EMPTY, 11))
        self.assertEqual(result[4:7], original[4:7])
        self.assertEqual(result[7], 1)

    def test_same_generation_torn_manual_event_retries_then_wins(self):
        base = 0x500000
        coherent = (0, 5, 1, 11, 2, 3, 4, guard.EMPTY, guard.EMPTY)
        torn = (0, 5, 3, 11, 1, 2, 4, guard.EMPTY, guard.EMPTY)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "party.json"
            publisher = party_intent_channel.PartyIntentPublisher(123, path)
            record = publisher.publish((0, 5, 1), battle_info=base, source="p2")
            instance = guard.PeneloStoryGuard.__new__(guard.PeneloStoryGuard)
            instance.intent_path = path
            instance.game_pid = 123
            instance.last_party_intent_token = None
            instance.accepted_active_members = (0, 5, 3)
            instance.pending_story_restore = {5}
            instance.story_removal_watches = {5: 9.0}
            instance.story_removal_counters = (0,) * 16
            instance.last_active_anomaly = None
            instance.bad_signature = object()
            instance.bad_since = 4.0
            counters = (1,) + (0,) * 15

            self.assertFalse(
                instance.consume_manual_party_intent(
                    base, (torn,) * guard.PARTY_LAYOUT_COUNT, counters
                )
            )
            self.assertIsNone(instance.last_party_intent_token)
            self.assertTrue(
                instance.consume_manual_party_intent(
                    base, (coherent,) * guard.PARTY_LAYOUT_COUNT, counters
                )
            )
            self.assertEqual(instance.last_party_intent_token, record.token)
            self.assertEqual(instance.accepted_active_members, (0, 5, 1))
            self.assertEqual(instance.pending_story_restore, set())
            self.assertEqual(instance.story_removal_watches, {})
            self.assertEqual(instance.story_removal_counters, counters)

    def test_same_members_in_a_different_order_do_not_adopt_old_intent(self):
        base = 0x500000
        reordered = (5, 0, 1, 11, 2, 3, 4, guard.EMPTY, guard.EMPTY)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "party.json"
            party_intent_channel.PartyIntentPublisher(123, path).publish(
                (0, 5, 1), battle_info=base, source="p1"
            )
            instance = guard.PeneloStoryGuard.__new__(guard.PeneloStoryGuard)
            instance.intent_path = path
            instance.game_pid = 123
            instance.last_party_intent_token = None
            instance.accepted_active_members = (5, 0, 1)
            instance.pending_story_restore = set()
            instance.story_removal_watches = {}
            instance.story_removal_counters = (0,) * 16
            instance.last_active_anomaly = None
            instance.bad_signature = None
            instance.bad_since = 0.0

            self.assertFalse(
                instance.consume_manual_party_intent(
                    base,
                    (reordered,) * guard.PARTY_LAYOUT_COUNT,
                    (0,) * 16,
                )
            )
            self.assertIsNone(instance.last_party_intent_token)

    def test_mailbox_write_failure_never_terminates_the_live_menu(self):
        shell = menu.CoopMenuRuntime.__new__(menu.CoopMenuRuntime)
        shell.process = object()
        shell.party_edit_capture = SimpleNamespace(
            observe=mock.Mock(return_value=((0, 5, 1), 0x500000, "p3"))
        )
        shell.party_intent_publisher = SimpleNamespace(
            publish=mock.Mock(side_effect=OSError("locked"))
        )
        state = SimpleNamespace(native_menu_open=False, battle_info=0x500000)
        with mock.patch.object(
            menu, "coherent_manual_party_members", return_value=(0, 5, 1)
        ):
            shell.maintain_party_edit_capture(state, now=1.0)

    def test_only_a_journaled_accepted_protected_loss_arms_restore(self):
        active_without_ashe = (
            0,
            5,
            4,
            11,
            1,
            2,
            3,
            guard.EMPTY,
            guard.EMPTY,
        )
        instance = guard.PeneloStoryGuard.__new__(guard.PeneloStoryGuard)
        instance.accepted_active_members = (0, 5, 1)
        instance.story_removal_counters = (0,) * 16
        instance.pending_story_restore = set()
        instance.story_removal_watches = {}
        counters = list(instance.story_removal_counters)
        counters[guard.ASHE_ID] = 1
        counters[guard.BALTHIER_ID] = 1
        instance.observe_story_removals(
            (active_without_ashe,) * guard.PARTY_LAYOUT_COUNT,
            tuple(counters),
            now=1.0,
        )
        self.assertEqual(instance.pending_story_restore, {guard.ASHE_ID})
        self.assertNotIn(guard.BALTHIER_ID, instance.story_removal_watches)

    def test_fresh_vaan_fallback_can_activate_missing_authored_members(self):
        base = 0x500000
        source = (0, guard.EMPTY, guard.EMPTY, guard.EMPTY) + (guard.EMPTY,) * 5
        layouts = [source] * guard.PARTY_LAYOUT_COUNT

        class Process:
            def __init__(self):
                self.writes = []

            def write(self, address, data):
                layout_id = (
                    address - base - guard.PARTY_LAYOUT_OFFSET
                ) // guard.PARTY_LAYOUT_STRIDE
                layouts[layout_id] = guard.unpack_layout(data)
                self.writes.append(layout_id)

        class Caller:
            def call(self, _function, *_args):
                return 0

        instance = guard.PeneloStoryGuard.__new__(guard.PeneloStoryGuard)
        instance.process = Process()
        instance.caller = Caller()
        instance.accepted_active_members = None
        instance.layout = lambda _base, layout_id: layouts[layout_id]
        instance.battle_info = lambda: base
        instance.controlled_party_is_vaan = lambda _base: True
        instance.town_materialization_is_validly_collapsed = (
            lambda *_args, **_kwargs: False
        )
        instance.bad_signature = None
        instance.bad_since = 0.0
        instance.cooldown_until = 0.0
        flags = tuple(0 for _member in guard.PROTECTED_MEMBER_IDS)
        original_add = instance.caller.call

        # Model addPartyMember making each authored member available before
        # the normalizer places the unlocked roster in reserve.
        def native_call(function, *args):
            if function == guard.ADD_PARTY_MEMBER:
                member = args[0]
                for layout_id in (guard.DEFAULT_LAYOUT, guard.MENU_LAYOUT):
                    layout = list(layouts[layout_id])
                    layout[4 + list(guard.PROTECTED_MEMBER_IDS).index(member)] = member
                    layouts[layout_id] = tuple(layout)
            return original_add(function, *args)

        instance.caller.call = native_call
        with unittest.mock.patch.object(guard.time, "sleep"):
            instance.repair(base, (flags, tuple(layouts)))
        for layout_id in (guard.DEFAULT_LAYOUT, guard.MENU_LAYOUT):
            layout = layouts[layout_id]
            self.assertEqual(
                layout[: guard.COOP_ACTIVE_SLOTS],
                (guard.VAAN_ID, guard.EMPTY, guard.EMPTY),
            )
            # Slot three is FF12's dedicated guest/story seat.  Any authored
            # companions stay in the five reserve slots so a checked
            # "Start with all characters" still begins with Vaan solo and
            # leaves the actual formation to FF12's native Party menu.
            self.assertEqual(layout[3], guard.EMPTY)
            self.assertEqual(
                {member_id for member_id in layout[4:] if member_id != guard.EMPTY},
                set(guard.PROTECTED_MEMBER_IDS),
            )


if __name__ == "__main__":
    unittest.main()
