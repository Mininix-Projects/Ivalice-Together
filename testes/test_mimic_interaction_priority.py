from __future__ import annotations

import unittest

from headless_menu_shell import interaction_matches_live_foe


class MimicInteractionPriorityTests(unittest.TestCase):
    def test_normal_treasure_remains_a_world_interaction(self):
        interaction = (2, 0x00200041, 0x1234, 3)
        foes = [("Wolf", 0x00200017, 0x700000)]
        self.assertFalse(interaction_matches_live_foe(interaction, foes))

    def test_transformed_mimic_owns_combat_over_stale_treasure_prompt(self):
        interaction = (2, 0x00400041, 0x1234, 3)
        foes = [("Battery Mimic", 0x00400041, 0x700000)]
        self.assertTrue(interaction_matches_live_foe(interaction, foes))

    def test_actor_generation_must_match_exactly(self):
        interaction = (2, 0x00300041, 0x1234, 3)
        foes = [("Respawned Mimic", 0x00400041, 0x700000)]
        self.assertFalse(interaction_matches_live_foe(interaction, foes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
