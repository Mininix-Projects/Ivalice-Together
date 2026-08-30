"""A remedy opens on the ally who carries what it cures.

Reported in play: an Antidote used from the co-op menu landed on the character
issuing it instead of the poisoned one.  The party target page deliberately
starts on its issuer: right for a Potion, wrong for anything that treats a
specific ailment.
"""

from __future__ import annotations

import unittest

from headless_menu_model import (
    ActionDefinition,
    MenuMode,
    P2MenuModel,
    PageDefinition,
    TargetCandidate,
    TargetGroup,
    remedy_status_mask,
)

POISON = 1 << 8
BLIND = 1 << 7
SILENCE = 1 << 9
SLEEP = 1 << 4
CONFUSE = 1 << 5
STOP = 1 << 3
SLOW = 1 << 15
DISEASE = 1 << 16

ISSUER = 0x100001
POISONED = 0x100002
BLINDED = 0x100003


def action(name: str) -> ActionDefinition:
    return ActionDefinition(
        action_id=0x5B,
        name=name,
        description=name,
        target_groups=(TargetGroup.PARTY,),
        default_group=TargetGroup.PARTY,
        category="items",
    )


def model_on_target_page(pending: ActionDefinition, party) -> P2MenuModel:
    model = P2MenuModel(
        issuer=ISSUER,
        pages={"root": PageDefinition(key="root", title="root", entries=())},
    )
    model.mode = MenuMode.TARGET
    model.pending_action = pending
    model.target_group = TargetGroup.PARTY
    model.preserve_candidate_on_refresh = False
    model.set_candidates(party)
    return model


def party(*rows) -> tuple[TargetCandidate, ...]:
    return tuple(
        TargetCandidate(handle, name, TargetGroup.PARTY, status_mask=mask)
        for handle, name, mask in rows
    )


class RemedyTableTests(unittest.TestCase):
    def test_the_table_is_keyed_case_insensitively(self):
        self.assertEqual(remedy_status_mask("Antidote"), POISON)
        self.assertEqual(remedy_status_mask("  antidote "), POISON)

    def test_a_plain_restorative_is_not_a_remedy(self):
        self.assertEqual(remedy_status_mask("Potion"), 0)
        self.assertEqual(remedy_status_mask("Hi-Potion"), 0)

    def test_each_remedy_names_its_own_ailment(self):
        self.assertEqual(remedy_status_mask("Eye Drops"), BLIND)
        self.assertEqual(remedy_status_mask("Echo Herbs"), SILENCE)
        self.assertTrue(remedy_status_mask("Remedy") & POISON)
        self.assertTrue(remedy_status_mask("Remedy") & BLIND)
        self.assertEqual(remedy_status_mask("Prince's Kiss"), SLEEP)
        self.assertEqual(remedy_status_mask("Chronos Tear"), STOP | SLOW)
        self.assertEqual(remedy_status_mask("Nu Khai Sand"), CONFUSE)
        self.assertEqual(remedy_status_mask("Serum"), DISEASE)


class RemedyTargetPriorityTests(unittest.TestCase):
    def test_an_antidote_opens_on_the_poisoned_ally(self):
        model = model_on_target_page(
            action("Antidote"),
            party(
                (ISSUER, "Vaan", 0),
                (POISONED, "Penelo", POISON),
            ),
        )
        self.assertEqual(model.valid_candidates()[model.target_index].handle, POISONED)

    def test_a_potion_still_opens_on_its_issuer(self):
        model = model_on_target_page(
            action("Potion"),
            party(
                (POISONED, "Penelo", POISON),
                (ISSUER, "Vaan", 0),
            ),
        )
        self.assertEqual(model.valid_candidates()[model.target_index].handle, ISSUER)

    def test_princes_kiss_opens_on_the_sleeping_ally(self):
        sleeping = 0x100004
        model = model_on_target_page(
            action("Prince's Kiss"),
            party(
                (ISSUER, "Vaan", 0),
                (sleeping, "Penelo", SLEEP),
            ),
        )
        self.assertEqual(model.valid_candidates()[model.target_index].handle, sleeping)

    def test_with_nobody_afflicted_the_issuer_keeps_the_cursor(self):
        model = model_on_target_page(
            action("Antidote"),
            party(
                (BLINDED, "Ashe", BLIND),
                (ISSUER, "Vaan", 0),
            ),
        )
        self.assertEqual(model.valid_candidates()[model.target_index].handle, ISSUER)

    def test_a_remedy_matches_any_of_the_ailments_it_treats(self):
        model = model_on_target_page(
            action("Remedy"),
            party(
                (ISSUER, "Vaan", 0),
                (BLINDED, "Ashe", BLIND),
            ),
        )
        self.assertEqual(model.valid_candidates()[model.target_index].handle, BLINDED)

    def test_the_first_afflicted_ally_wins_when_several_qualify(self):
        model = model_on_target_page(
            action("Antidote"),
            party(
                (ISSUER, "Vaan", 0),
                (POISONED, "Penelo", POISON),
                (BLINDED, "Ashe", POISON | BLIND),
            ),
        )
        self.assertEqual(model.valid_candidates()[model.target_index].handle, POISONED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
