import unittest

from native_action_catalog import NativeActionCatalog
from p1_native_menu_mirror import NativePageSnapshot, P1NativeMirrorPresenter


def snapshot(type_id, entries, *, index=0, variant=0, generation=(1,)):
    return NativePageSnapshot(
        generation=generation,
        type_id=type_id,
        actor_token=0x100005,
        variant=variant,
        subcategory=0,
        context=0,
        count=len(entries),
        index=index,
        pitch=46,
        entries=tuple(bytes.fromhex(entry) for entry in entries),
    )


class P1NativeMenuMirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.presenter = P1NativeMirrorPresenter(NativeActionCatalog())

    def setUp(self):
        self.presenter.reset()

    def test_root_uses_native_order_including_mist(self):
        frame = self.presenter.present(
            snapshot(
                6,
                (
                    "0000000000000000",
                    "1200000000000000",
                    "1300000000000000",
                    "0d00000000000000",
                    "0300000000000000",
                ),
                index=2,
            ),
            gambits_enabled=True,
            delta_seconds=0.016,
        )
        self.assertIsNotNone(frame)
        self.assertEqual(
            frame.view.entries,
            ("Attack", "Magicks & Technicks", "Mist", "Gambits", "Items"),
        )
        self.assertEqual(frame.help_text, "Use Mist abilities.")
        self.assertEqual(frame.view.right_labels[3], "ON")

    def test_category_disabled_flag_and_terminal_magic(self):
        categories = snapshot(
            8,
            (
                "0000040400080000",
                "0100020627080000",
                "0400050627080000",
                "0200070000080000",
            ),
            index=0,
            generation=(2,),
        )
        frame = self.presenter.present(
            categories, gambits_enabled=True, delta_seconds=0.016
        )
        self.assertEqual(
            frame.view.entries,
            ("White Magicks", "Black Magicks", "Arcane Magicks", "Technicks"),
        )
        self.assertEqual(frame.view.enabled, (True, False, False, True))
        action = self.presenter.present(
            snapshot(
                11,
                ("0000080012080000",),
                variant=1,
                generation=(3,),
            ),
            gambits_enabled=True,
            delta_seconds=0.016,
        )
        self.assertEqual(action.view.title, "White Magicks")
        self.assertEqual(action.view.entries, ("Cure",))

    def test_items_decode_action_ids_quantities_and_disabled(self):
        frame = self.presenter.present(
            snapshot(
                11,
                (
                    "5200040014080000",
                    "5b00070015080000",
                ),
                variant=3,
            ),
            gambits_enabled=False,
            delta_seconds=0.016,
        )
        self.assertEqual(frame.view.entries, ("Potion", "Antidote"))
        self.assertEqual(frame.view.right_labels, {0: "4", 1: "7"})
        self.assertEqual(frame.view.enabled, (True, False))

    def test_rejects_unknown_or_inconsistent_page(self):
        bad = NativePageSnapshot((1,), 6, 0x100005, 0, 0, 0, 2, 0, 46, (b"\0" * 8,))
        self.assertIsNone(
            self.presenter.present(bad, gambits_enabled=True, delta_seconds=0.016)
        )
        self.assertIsNone(
            self.presenter.present(
                snapshot(9, ("0000000000000000",)),
                gambits_enabled=True,
                delta_seconds=0.016,
            )
        )


if __name__ == "__main__":
    unittest.main()
