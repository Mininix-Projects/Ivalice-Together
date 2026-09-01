from __future__ import annotations

import unittest

from headless_frontend_controller import HeadlessFrontendController, InputCommand
from headless_menu_model import (
    EntryKind,
    MenuEntry,
    P2MenuModel,
    PageDefinition,
    TargetCandidate,
    TargetGroup,
    make_demo_catalog,
)


ISSUER = 0x100008
VAAN = 0x100007
FOE = 0x200027
NEUTRAL = 0x200030


class FrontendControllerTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

        def targets(_model):
            return (
                TargetCandidate(FOE, "Battery Mimic B", TargetGroup.FOES),
                TargetCandidate(NEUTRAL, "Alraune", TargetGroup.NEUTRALS),
                TargetCandidate(VAAN, "Vaan", TargetGroup.PARTY),
                TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
            )

        def submit(request):
            self.requests.append(request)
            return 0

        self.model = P2MenuModel(ISSUER, make_demo_catalog())
        self.controller = HeadlessFrontendController(
            self.model, target_provider=targets, submitter=submit
        )

    def test_complete_potion_flow_submits_once(self):
        self.controller.dispatch(InputCommand.TOGGLE)
        for _ in range(3):
            self.controller.dispatch(InputCommand.DOWN)
        self.controller.dispatch(InputCommand.CONFIRM)  # Items
        self.controller.dispatch(InputCommand.CONFIRM)  # Potion -> target
        result = self.controller.dispatch(InputCommand.CONFIRM)  # Penelo
        self.assertEqual(result.native_result, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(
            (
                self.requests[0].issuer,
                self.requests[0].target,
                self.requests[0].action_id,
                self.requests[0].flag,
            ),
            (ISSUER, ISSUER, 0x52, 0),
        )
        self.assertFalse(self.model.is_open)

    def test_open_close_and_navigation_never_submit(self):
        self.controller.dispatch(InputCommand.TOGGLE)
        self.controller.dispatch(InputCommand.DOWN)
        self.controller.dispatch(InputCommand.UP)
        self.controller.dispatch(InputCommand.BACK)
        self.assertEqual(self.requests, [])

    def test_repeat_commands_stop_at_boundaries_and_fresh_commands_wrap(self):
        self.controller.dispatch(InputCommand.TOGGLE)

        self.assertEqual(
            self.controller.dispatch(InputCommand.UP_REPEAT).event.kind,
            "ignored",
        )
        self.assertEqual(self.model.index, 0)
        self.controller.dispatch(InputCommand.UP)
        self.assertEqual(self.model.index, len(self.model.page.entries) - 1)
        self.assertEqual(
            self.controller.dispatch(InputCommand.DOWN_REPEAT).event.kind,
            "ignored",
        )
        self.controller.dispatch(InputCommand.DOWN)
        self.assertEqual(self.model.index, 0)

    def test_page_commands_use_the_presented_row_count(self):
        entries = tuple(
            MenuEntry(str(index), "", EntryKind.ACTION) for index in range(12)
        )
        self.model.pages = {"root": PageDefinition("root", "Root", entries)}
        self.controller.dispatch(InputCommand.TOGGLE)

        self.assertEqual(
            self.controller.dispatch(InputCommand.PAGE_DOWN).event.value,
            self.controller.presenter.visible_row_limit("root"),
        )
        self.assertEqual(
            self.controller.dispatch(InputCommand.PAGE_UP).event.value,
            0,
        )

    def test_page_commands_are_ignored_outside_a_list(self):
        self.assertEqual(
            self.controller.dispatch(InputCommand.PAGE_DOWN).event.kind,
            "ignored",
        )
        self.controller.dispatch(InputCommand.TOGGLE)
        self.controller.dispatch(InputCommand.CONFIRM)  # Attack -> target mode
        self.assertEqual(
            self.controller.dispatch(InputCommand.PAGE_UP).event.kind,
            "ignored",
        )

    def test_target_groups_do_not_share_focus(self):
        self.controller.dispatch(InputCommand.TOGGLE)
        self.controller.dispatch(InputCommand.CONFIRM)  # Attack
        self.controller.dispatch(InputCommand.PARTY)
        result = self.controller.dispatch(InputCommand.CONFIRM)
        self.assertEqual(result.native_result, 0)
        self.assertEqual(self.requests[0].target, ISSUER)

    def test_cycle_command_uses_neutral_middle_family(self):
        self.controller.dispatch(InputCommand.TOGGLE)
        self.controller.dispatch(InputCommand.CONFIRM)
        result = self.controller.dispatch(InputCommand.CYCLE_TARGET_GROUP)
        self.assertEqual(result.event.value, TargetGroup.NEUTRALS)
        result = self.controller.dispatch(InputCommand.CONFIRM)
        self.assertEqual(result.native_result, 0)
        self.assertEqual(self.requests[0].target, NEUTRAL)

    def test_previous_cycle_command_walks_the_groups_backwards(self):
        self.controller.dispatch(InputCommand.TOGGLE)
        self.controller.dispatch(InputCommand.CONFIRM)
        result = self.controller.dispatch(InputCommand.CYCLE_TARGET_GROUP_PREVIOUS)
        self.assertEqual(result.event.value, TargetGroup.PARTY)

    def test_new_item_selection_does_not_reuse_previous_party_target(self):
        self.controller.target_provider = lambda _model: (
            TargetCandidate(FOE, "Battery Mimic B", TargetGroup.FOES),
            TargetCandidate(ISSUER, "Penelo", TargetGroup.PARTY),
            TargetCandidate(VAAN, "Vaan", TargetGroup.PARTY),
        )
        self.controller.dispatch(InputCommand.TOGGLE)
        for _ in range(3):
            self.controller.dispatch(InputCommand.DOWN)
        self.controller.dispatch(InputCommand.CONFIRM)  # Items
        self.controller.dispatch(InputCommand.CONFIRM)  # Potion
        self.controller.dispatch(InputCommand.DOWN)  # choose Vaan
        self.assertEqual(
            self.model.valid_candidates()[self.model.target_index].handle,
            VAAN,
        )
        self.controller.dispatch(InputCommand.BACK)
        self.controller.dispatch(InputCommand.CONFIRM)  # Potion again
        self.assertEqual(
            self.model.valid_candidates()[self.model.target_index].handle,
            ISSUER,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
