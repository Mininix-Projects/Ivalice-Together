from __future__ import annotations

import unittest

from headless_menu_model import TargetGroup
from headless_targeting import ActorTargetSnapshot, attack_candidates


class HeadlessTargetingTests(unittest.TestCase):
    def setUp(self):
        self.issuer = ActorTargetSnapshot(
            0x100008, "Penelo", 0.0, 50.0, 0.0, party_member=True
        )

    def test_attack_boundary_is_inclusive_and_horizontal(self):
        actor = ActorTargetSnapshot(
            0x200027, "Battery Mimic B", 24.0, -900.0, 0.0, unit_type=4
        )
        candidates = attack_candidates(self.issuer, (actor,))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].group, TargetGroup.FOES)

    def test_materialized_foe_outside_range_is_not_selectable(self):
        actor = ActorTargetSnapshot(
            0x200027, "Battery Mimic B", 24.001, 0.0, 0.0, unit_type=4
        )
        self.assertEqual(attack_candidates(self.issuer, (actor,)), ())

    def test_enemy_uses_unit_type_not_aggro_flag_or_handle_generation(self):
        generated_handle = ActorTargetSnapshot(
            0x400027, "Respawned foe", 1.0, 0.0, 0.0, unit_type=1
        )
        cockatrice = ActorTargetSnapshot(
            0x200028,
            "Cockatrice",
            2.0,
            0.0,
            0.0,
            actor_flags=0,
            unit_type=1,
        )
        candidates = attack_candidates(self.issuer, (generated_handle, cockatrice))
        self.assertEqual(
            [candidate.handle for candidate in candidates],
            [0x400027, 0x200028],
        )

    def test_noncombat_hidden_and_pre_spawn_units_are_rejected(self):
        ally = ActorTargetSnapshot(0x200030, "Friendly NPC", 1.0, 0.0, 0.0, unit_type=3)
        hidden_foe = ActorTargetSnapshot(
            0x200031,
            "Buried zombie",
            1.0,
            0.0,
            0.0,
            unit_type=1,
            hidden=True,
        )
        inactive_foe = ActorTargetSnapshot(
            0x200032,
            "Pending specter",
            1.0,
            0.0,
            0.0,
            unit_type=1,
            active=False,
        )
        self.assertEqual(
            attack_candidates(self.issuer, (ally, hidden_foe, inactive_foe)),
            (),
        )

    def test_mimic_and_hunt_unit_types_are_attackable(self):
        mimic = ActorTargetSnapshot(0x200033, "Mimic", 1.0, 0.0, 0.0, unit_type=4)
        hunt = ActorTargetSnapshot(0x200034, "Hunt", 2.0, 0.0, 0.0, unit_type=7)
        self.assertEqual(
            [
                candidate.handle
                for candidate in attack_candidates(self.issuer, (hunt, mimic))
            ],
            [0x200033, 0x200034],
        )

    def test_party_candidates_remain_available_as_separate_group(self):
        vaan = ActorTargetSnapshot(0x100007, "Vaan", 100.0, 0.0, 0.0, party_member=True)
        candidates = attack_candidates(self.issuer, (vaan, self.issuer))
        self.assertEqual(
            [candidate.group for candidate in candidates], [TargetGroup.PARTY] * 2
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
