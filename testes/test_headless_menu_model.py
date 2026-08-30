"""Offline regression tests for :mod:`headless_menu_model`."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ferramentas"))

from headless_menu_model import (
    ATTACK_ACTION_ID,
    EntryKind,
    MenuEntry,
    MenuMode,
    P2MenuModel,
    PageDefinition,
    TargetCandidate,
    TargetGroup,
    make_demo_catalog,
)
from native_queue_analysis import (
    NativeQueueSnapshot,
    QueuePhase,
    SubmissionDisposition,
    predict_native_disposition,
)


ISSUER = 0x100008
VAAN = 0x100007
FOE = 0x200027
NEUTRAL = 0x200030


def snapshot(state: int, current: int = 0, requested: int = 0):
    return NativeQueueSnapshot(
        actor_flags_000=0,
        state_6b4=state,
        descriptor_708=0,
        target_710=VAAN if current else 0,
        current_action_714=current,
        charge_current_768=200 if state == 4 else 0,
        charge_required_76c=17040 if state == 4 else 0,
        requested_action_ba0=requested,
        request_flags_ba4=(1 << 14) if requested else 0,
        requested_target_bb8=VAAN if requested else 0,
    )


class MenuModelTests(unittest.TestCase):
    def setUp(self):
        self.model = P2MenuModel(ISSUER, make_demo_catalog())
        self.model.set_candidates(
            (
                TargetCandidate(FOE, "Battery Mimic B", TargetGroup.FOES),
                TargetCandidate(NEUTRAL, "Alraune", TargetGroup.NEUTRALS),
                TargetCandidate(VAAN, "Vaan", TargetGroup.PARTY),
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
            )
        )

    def test_every_fresh_open_starts_at_attack(self):
        self.model.open()
        self.model.navigate(3)
        self.assertNotEqual(self.model.index, 0)
        self.model.close()
        self.model.open()
        self.assertEqual(self.model.index, 0)
        self.assertEqual(self.model.selected_entry.name, "Attack")

    def test_page_navigation_moves_by_visible_rows_and_never_wraps(self):
        entries = tuple(
            MenuEntry(str(index), "", EntryKind.ACTION) for index in range(12)
        )
        model = P2MenuModel(
            ISSUER,
            {"root": PageDefinition("root", "Root", entries)},
        )
        model.open()

        self.assertEqual(model.navigate_page(1, 5).value, 5)
        self.assertEqual(model.navigate_page(1, 5).value, 10)
        self.assertEqual(model.navigate_page(1, 5).value, 11)
        self.assertEqual(model.navigate_page(1, 5).kind, "ignored")
        self.assertEqual(model.navigate_page(-1, 5).value, 6)

    def test_page_back_restores_parent_selection(self):
        self.model.open()
        self.model.navigate(1)
        self.assertEqual(self.model.selected_entry.name, "Magicks & Technicks")
        self.model.confirm()
        self.assertEqual(self.model.page.key, "magicks")
        self.model.back()
        self.assertEqual(self.model.page.key, "root")
        self.assertEqual(self.model.selected_entry.name, "Magicks & Technicks")

    def test_demo_ids_match_native_action_bank(self):
        self.model.open()
        self.model.navigate(3)  # Items
        self.model.confirm()
        self.assertEqual(self.model.page.key, "items")
        self.assertEqual(self.model.page.entries[0].action.action_id, 0x52)
        self.assertEqual(self.model.page.entries[1].action.action_id, 0x5B)

    def test_attack_target_and_submit(self):
        self.model.open()
        opened = self.model.confirm()
        self.assertEqual(opened.kind, "target_opened")
        self.assertEqual(self.model.mode, MenuMode.TARGET)
        sent = self.model.confirm()
        self.assertEqual(sent.kind, "submit")
        self.assertEqual(
            (
                sent.request.issuer,
                sent.request.target,
                sent.request.action_id,
                sent.request.flag,
            ),
            (ISSUER, FOE, ATTACK_ACTION_ID, 0),
        )
        self.assertEqual(self.model.mode, MenuMode.CLOSED)

    def test_party_target_group_is_independent(self):
        self.model.open()
        self.model.confirm()
        self.model.switch_target_group(TargetGroup.PARTY)
        self.model.navigate(1)
        sent = self.model.confirm()
        self.assertEqual(sent.request.target, ISSUER)

    def test_offensive_cycle_visits_neutral_before_party(self):
        self.model.open()
        self.model.confirm()
        self.assertEqual(self.model.target_group, TargetGroup.FOES)
        self.assertEqual(
            self.model.cycle_target_group().value,
            TargetGroup.NEUTRALS,
        )
        self.assertEqual(
            self.model.cycle_target_group().value,
            TargetGroup.PARTY,
        )

    def test_cycle_skips_neutral_family_when_none_exists(self):
        self.model.set_candidates(
            candidate
            for group in (TargetGroup.FOES, TargetGroup.PARTY)
            for candidate in self.model.valid_candidates(group)
        )
        self.model.open()
        self.model.confirm()
        self.assertEqual(
            self.model.cycle_target_group().value,
            TargetGroup.PARTY,
        )

    def test_live_refresh_preserves_target_order_and_selected_handle(self):
        second_foe = FOE + 1
        self.model.open()
        self.model.confirm()
        self.model.set_candidates(
            (
                TargetCandidate(FOE, "Foe A", TargetGroup.FOES, 8.0),
                TargetCandidate(second_foe, "Foe B", TargetGroup.FOES, 9.0),
            )
        )
        self.model.navigate(1)
        self.assertEqual(
            self.model.valid_candidates()[self.model.target_index].handle, second_foe
        )
        self.model.set_candidates(
            (
                TargetCandidate(second_foe, "Foe B", TargetGroup.FOES, 7.0),
                TargetCandidate(FOE, "Foe A", TargetGroup.FOES, 10.0),
            )
        )
        self.assertEqual(
            [candidate.handle for candidate in self.model.valid_candidates()],
            [FOE, second_foe],
        )
        self.assertEqual(
            self.model.valid_candidates()[self.model.target_index].handle, second_foe
        )

    def test_live_refresh_removes_missing_targets_and_appends_new_ones(self):
        second_foe = FOE + 1
        third_foe = FOE + 2
        self.model.open()
        self.model.confirm()
        self.model.set_candidates(
            (
                TargetCandidate(FOE, "Foe A", TargetGroup.FOES),
                TargetCandidate(second_foe, "Foe B", TargetGroup.FOES),
            )
        )
        self.model.set_candidates(
            (
                TargetCandidate(third_foe, "Foe C", TargetGroup.FOES),
                TargetCandidate(second_foe, "Foe B", TargetGroup.FOES),
            )
        )
        self.assertEqual(
            [candidate.handle for candidate in self.model.valid_candidates()],
            [second_foe, third_foe],
        )

    def test_fresh_action_uses_provider_order_instead_of_last_target(self):
        self.model.open()
        self.model.confirm()
        self.model.switch_target_group(TargetGroup.PARTY)
        self.model.navigate(1)
        self.assertEqual(
            self.model.valid_candidates()[self.model.target_index].handle,
            ISSUER,
        )

    def test_fresh_party_action_selects_issuer_even_if_provider_order_is_stale(self):
        self.model.open()
        self.model.confirm()
        self.model.switch_target_group(TargetGroup.PARTY)
        self.model.set_candidates(
            (
                TargetCandidate(VAAN, "Vaan", TargetGroup.PARTY),
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
            )
        )
        self.assertEqual(
            self.model.valid_candidates()[self.model.target_index].handle,
            ISSUER,
        )

    def test_switching_from_refreshed_foes_selects_the_issuer_not_hud_row_zero(self):
        self.model.open()
        self.model.confirm()
        # The frontend refreshes the initial foe list before the player changes
        # target family. This was the missing state in the older regression.
        self.model.set_candidates(
            (
                TargetCandidate(FOE, "Battery Mimic B", TargetGroup.FOES),
                TargetCandidate(VAAN, "Vaan", TargetGroup.PARTY),
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
            )
        )
        self.model.switch_target_group(TargetGroup.PARTY)
        self.model.set_candidates(
            (
                TargetCandidate(VAAN, "Vaan", TargetGroup.PARTY),
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
            )
        )

        selected = self.model.valid_candidates()[self.model.target_index]
        self.assertEqual(selected.handle, ISSUER)
        self.assertTrue(self.model.party_target_locked_to_issuer)
        self.assertEqual(self.model.confirm().request.target, ISSUER)

    def test_self_target_survives_a_transient_empty_party_sample(self):
        self.model.open()
        self.model.confirm()
        self.model.switch_target_group(TargetGroup.PARTY)
        self.model.set_candidates(
            (
                TargetCandidate(VAAN, "Vaan", TargetGroup.PARTY),
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
            )
        )
        self.model.set_candidates(())
        self.assertEqual(self.model.confirm().kind, "invalid_target")

        self.model.set_candidates(
            (
                TargetCandidate(VAAN, "Vaan", TargetGroup.PARTY),
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
            )
        )
        selected = self.model.valid_candidates()[self.model.target_index]
        self.assertEqual(selected.handle, ISSUER)
        self.assertEqual(self.model.confirm().request.target, ISSUER)

    def test_party_member_identity_survives_handle_regeneration(self):
        regenerated_vaan = VAAN + 0x10000
        self.model.open()
        self.model.confirm()
        self.model.switch_target_group(TargetGroup.PARTY)
        self.model.set_candidates(
            (
                TargetCandidate(
                    ISSUER,
                    "Penelo",
                    TargetGroup.PARTY,
                    party_member_id=5,
                ),
                TargetCandidate(
                    VAAN,
                    "Vaan",
                    TargetGroup.PARTY,
                    party_member_id=0,
                ),
            )
        )
        self.model.navigate(1)
        self.model.set_candidates(
            (
                TargetCandidate(
                    regenerated_vaan,
                    "Vaan",
                    TargetGroup.PARTY,
                    party_member_id=0,
                ),
                TargetCandidate(
                    ISSUER,
                    "Penelo",
                    TargetGroup.PARTY,
                    party_member_id=5,
                ),
            )
        )
        selected = self.model.valid_candidates()[self.model.target_index]
        self.assertEqual(selected.handle, regenerated_vaan)

    def test_fresh_self_target_uses_member_identity_after_issuer_handle_changes(self):
        old_issuer = ISSUER
        current_issuer = ISSUER + 0x10000
        self.model.issuer_party_member_id = 5
        self.model.open()
        self.model.navigate(3)  # Items
        self.model.confirm()
        self.model.confirm()  # Potion -> fresh party target page
        self.assertEqual(self.model.issuer, old_issuer)

        self.model.set_candidates(
            (
                TargetCandidate(
                    VAAN,
                    "Balthier",
                    TargetGroup.PARTY,
                    party_member_id=3,
                ),
                TargetCandidate(
                    current_issuer,
                    "Penelo",
                    TargetGroup.PARTY,
                    party_member_id=5,
                ),
            )
        )

        selected = self.model.valid_candidates()[self.model.target_index]
        self.assertEqual(selected.handle, current_issuer)
        self.assertTrue(self.model.party_target_locked_to_issuer)
        self.assertEqual(self.model.confirm().request.target, current_issuer)

    def test_fresh_party_action_uses_first_legal_target_when_issuer_is_illegal(self):
        dead_ally = VAAN + 1
        self.model.open()
        self.model.confirm()
        self.model.switch_target_group(TargetGroup.PARTY)
        self.model.set_candidates(
            (
                TargetCandidate(
                    ISSUER,
                    "Penelo",
                    TargetGroup.PARTY,
                    selectable=False,
                ),
                TargetCandidate(
                    dead_ally,
                    "Ashe",
                    TargetGroup.PARTY,
                    selectable=True,
                ),
            )
        )
        self.assertEqual(
            self.model.valid_candidates()[self.model.target_index].handle,
            dead_ally,
        )
        self.model.back()
        self.model.close()

        # A new action must not inherit the prior target handle.  Party
        # providers put the issuer first, which is the native quick-Potion
        # expectation.
        self.model.open()
        self.model.confirm()
        self.model.switch_target_group(TargetGroup.PARTY)
        self.model.set_candidates(
            (
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
                TargetCandidate(VAAN, "Vaan", TargetGroup.PARTY),
            )
        )
        self.assertEqual(
            self.model.valid_candidates()[self.model.target_index].handle,
            ISSUER,
        )

    def test_menu_lifecycle_does_not_touch_queue_snapshot(self):
        action = snapshot(10, 0x52, 0x52)
        before = action
        self.model.open()
        self.model.close()
        self.model.open()
        self.assertEqual(action, before)
        self.assertEqual(action.phase, QueuePhase.EXECUTING)

    def test_native_queue_dispositions(self):
        self.assertEqual(
            predict_native_disposition(snapshot(1)), SubmissionDisposition.START
        )
        self.assertEqual(
            predict_native_disposition(snapshot(4, 0x96)),
            SubmissionDisposition.REPLACE_CHARGING,
        )
        self.assertEqual(
            predict_native_disposition(snapshot(7, 0x52, 0x52)),
            SubmissionDisposition.QUEUE_AFTER_CURRENT,
        )
        self.assertEqual(
            predict_native_disposition(snapshot(10, 0x52, 0x52)),
            SubmissionDisposition.QUEUE_AFTER_CURRENT,
        )

    def test_request_pulse_comes_from_actor_flags_not_stable_ba4(self):
        idle = snapshot(1, 0x4021, 0x5B)
        self.assertEqual(idle.phase, QueuePhase.IDLE)
        self.assertFalse(idle.manual_request_pulse)
        pulsed = NativeQueueSnapshot(
            **{
                **idle.__dict__,
                "actor_flags_000": idle.actor_flags_000 | (1 << 14) | (1 << 32),
            }
        )
        self.assertTrue(pulsed.manual_request_pulse)

    def test_back_from_target_does_not_submit_or_close_root(self):
        self.model.open()
        self.model.confirm()
        event = self.model.back()
        self.assertEqual(event.kind, "target_closed")
        self.assertEqual(self.model.mode, MenuMode.LIST)
        self.assertEqual(self.model.selected_entry.name, "Attack")

    def test_back_from_synthetic_target_without_stack_closes_fail_safe(self):
        self.model.open()
        self.model.confirm()
        self.model.stack.clear()

        event = self.model.back()

        self.assertEqual(event.kind, "closed")
        self.assertEqual(self.model.mode, MenuMode.CLOSED)
        self.assertEqual(self.model.stack, [])
        self.assertIsNone(self.model.pending_action)

    def test_navigation_without_a_real_destination_is_silent(self):
        self.model.open()
        self.model.confirm()
        self.model.set_candidates(())
        self.assertEqual(self.model.navigate(1).kind, "ignored")

        self.model.back()
        one_row = self.model.pages["root"].__class__(
            "one", "One", (self.model.pages["root"].entries[0],)
        )
        self.model.pages = {**self.model.pages, "one": one_row}
        self.model.stack[-1].key = "one"
        self.assertEqual(self.model.navigate(1).kind, "ignored")


if __name__ == "__main__":
    unittest.main(verbosity=2)
