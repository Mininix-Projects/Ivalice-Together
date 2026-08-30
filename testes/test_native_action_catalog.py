from __future__ import annotations

import unittest

from native_action_catalog import NativeActionCatalog


class NativeActionCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = NativeActionCatalog()

    def test_live_proven_actions(self):
        self.assertEqual(self.catalog.record(0x52).name, "Potion")
        self.assertIn("small amount of HP", self.catalog.record(0x52).description)
        self.assertEqual(self.catalog.record(0x5B).name, "Antidote")
        self.assertIn("Poison", self.catalog.record(0x5B).description)
        self.assertEqual(self.catalog.record(0x96).name, "Attack")
        self.assertEqual(
            self.catalog.record(0x96).description, "Attack with weapon in hand."
        )

    def test_magick_technick_mist_and_esper_ranges(self):
        self.assertEqual(self.catalog.record(0x00).name, "Cure")
        self.assertIn("small amount of HP", self.catalog.record(0x00).description)
        self.assertEqual(self.catalog.record(0x9E).name, "First Aid")
        self.assertIn("HP Critical", self.catalog.record(0x9E).description)
        self.assertEqual(self.catalog.record(0xD0).name, "Red Spiral")
        self.assertIn("Rank", self.catalog.record(0xD0).description)
        self.assertEqual(self.catalog.record(0x106).name, "Belias")
        self.assertEqual(self.catalog.record(0x106).description, "Summon Belias.")

    def test_motes_inherit_native_magick_help(self):
        reverse = self.catalog.record(0x7C)
        self.assertEqual(reverse.name, "Reverse Mote")
        self.assertTrue(reverse.description)
        self.assertEqual(reverse.description_confidence, "derived-native-name")

    def test_actor_unlock_provider_controls_visible_pages(self):
        pages = self.catalog.build_menu_pages(
            {
                "White Magicks": (0x00,),
                "Items": (0x52, 0x5B),
                "Mist": (0xD0,),
            }
        )
        root_names = [entry.name for entry in pages["root"].entries]
        self.assertEqual(
            root_names, ["Attack", "Magicks & Technicks", "Mist", "Gambits", "Items"]
        )
        self.assertEqual(pages["items"].entries[1].name, "Antidote")


if __name__ == "__main__":
    unittest.main(verbosity=2)
