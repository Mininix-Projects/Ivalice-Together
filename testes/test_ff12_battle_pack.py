from __future__ import annotations

import unittest
from pathlib import Path

from ff12_battle_pack import BattleTextPack


PACK = (
    Path(__file__).resolve().parents[1]
    / "source"
    / "extracted_ui_assets/ps2data/image/ff12/test_battle/us/binaryfile"
    / "battle_pack.bin"
)


class BattleTextPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = BattleTextPack.read(PACK)

    def test_group_counts(self):
        self.assertEqual(len(self.pack.groups), 15)
        self.assertEqual(len(self.pack.group("actions")), 545)
        self.assertEqual(len(self.pack.group("actors")), 629)

    def test_live_action_ids_resolve_without_hardcoding(self):
        self.assertEqual(self.pack.action_name(0x52), "Potion")
        self.assertEqual(self.pack.action_name(0x5B), "Antidote")
        self.assertEqual(self.pack.action_name(0x96), "Attack")

    def test_enemy_name_is_in_native_actor_bank(self):
        matches = self.pack.find("Battery Mimic")
        self.assertTrue(matches)
        self.assertEqual(matches[0].group, 8)
        self.assertEqual(self.pack.actor_name(matches[0].index, "B"), "Battery Mimic B")

    def test_core_command_categories(self):
        commands = self.pack.group("command_categories")
        self.assertEqual(
            [entry.clean_text for entry in commands[:4]],
            ["Attack", "Magicks", "Technicks", "Items"],
        )

    def test_extended_glyphs_are_decoded(self):
        self.assertIn(
            "Cúchulainn", [entry.clean_text for entry in self.pack.group("actions")]
        )
        self.assertIn(
            "Yagyu Darkblade & Mesa",
            [entry.clean_text for entry in self.pack.group("abilities")],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
