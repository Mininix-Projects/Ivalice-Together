import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from headless_menu_model import EntryKind
from native_action_catalog import NativeActionCatalog
from native_menu_catalog_provider import load_catalog_snapshot
from p2_native_catalog_pages import build_p2_pages


class P2NativeCatalogPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = (
            Path(__file__).resolve().parent.parent
            / "artefatos"
            / "native_menu_catalog_penelo_live.json"
        )
        if not path.exists():
            raise unittest.SkipTest("real merged Penelo fixture is unavailable")
        cls.catalog = load_catalog_snapshot(path)
        cls.action_catalog = NativeActionCatalog()

    def test_real_root_and_disabled_categories_match_native_output(self):
        pages = build_p2_pages(
            self.catalog,
            action_catalog=self.action_catalog,
        )
        self.assertEqual(
            [entry.name for entry in pages["root"].entries],
            ["Attack", "Magicks & Technicks", "Gambits", "Items"],
        )
        categories = pages["magicks"].entries
        self.assertEqual(
            [entry.name for entry in categories],
            ["White Magicks", "Black Magicks", "Arcane Magicks", "Technicks"],
        )
        self.assertEqual(
            [entry.enabled for entry in categories],
            [True, False, False, True],
        )

    def test_real_action_pages_use_native_action_ids_and_live_quantities(self):
        quantities = {0x52: 40, 0x53: 3, 0x58: 8, 0x5B: 2, 0x5C: 1, 0x7D: 6}
        pages = build_p2_pages(
            self.catalog,
            action_catalog=self.action_catalog,
            live_quantities=quantities,
        )
        self.assertEqual(
            [e.action.action_id for e in pages["items"].entries],
            [
                0x52,
                0x53,
                0x58,
                0x5B,
                0x5C,
                0x7D,
            ],
        )
        self.assertEqual(
            [e.action.quantity for e in pages["items"].entries],
            [
                40,
                3,
                8,
                2,
                1,
                6,
            ],
        )
        self.assertEqual(pages["magick:white_magicks"].entries[0].name, "Cure")
        self.assertEqual(pages["technicks"].entries[0].name, "First Aid")
        self.assertTrue(all(e.kind is EntryKind.ACTION for e in pages["items"].entries))

    def test_live_inventory_is_authoritative_over_captured_quantities(self):
        pages = build_p2_pages(
            self.catalog,
            action_catalog=self.action_catalog,
            live_quantities={0x52: 1},
        )
        self.assertEqual(
            [entry.action.action_id for entry in pages["items"].entries],
            [0x52],
        )
        self.assertEqual(pages["items"].entries[0].action.quantity, 1)

        empty = build_p2_pages(
            self.catalog,
            action_catalog=self.action_catalog,
            live_quantities={},
        )
        self.assertEqual(len(empty["items"].entries), 1)
        self.assertEqual(empty["items"].entries[0].name, "No actions available")
        self.assertFalse(empty["items"].entries[0].enabled)


if __name__ == "__main__":
    unittest.main()
