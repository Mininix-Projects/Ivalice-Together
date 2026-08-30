from __future__ import annotations

import unittest

from headless_menu_model import (
    ActionDefinition,
    EntryKind,
    MenuEntry,
    MenuMode,
    P2MenuModel,
    PageDefinition,
    TargetGroup,
)
from p2_catalog_atomic import commit_pages_atomically


def action(action_id: int, name: str, *, enabled: bool = True) -> ActionDefinition:
    return ActionDefinition(
        action_id,
        name,
        name + " help",
        (TargetGroup.PARTY,),
        TargetGroup.PARTY,
        "test",
        enabled=enabled,
    )


def action_entry(definition: ActionDefinition) -> MenuEntry:
    return MenuEntry(
        definition.name,
        definition.description,
        EntryKind.ACTION,
        action=definition,
        enabled=definition.enabled,
    )


class AtomicCatalogTests(unittest.TestCase):
    def test_preserves_selected_action_across_reorder(self) -> None:
        a = action(1, "A")
        b = action(2, "B")
        model = P2MenuModel(
            7,
            {"root": PageDefinition("root", "", (action_entry(a), action_entry(b)))},
        )
        model.open()
        model.navigate(1)

        commit_pages_atomically(
            model,
            {"root": PageDefinition("root", "", (action_entry(b), action_entry(a)))},
        )
        self.assertEqual(model.selected_entry.action.action_id, 2)
        self.assertEqual(model.index, 0)

    def test_truncates_submenu_removed_by_new_capabilities(self) -> None:
        child = PageDefinition("mist", "Mist", (action_entry(action(3, "Mist")),))
        old = {
            "root": PageDefinition(
                "root", "", (MenuEntry("Mist", "", EntryKind.PAGE, page_key="mist"),)
            ),
            "mist": child,
        }
        model = P2MenuModel(7, old)
        model.open()
        model.confirm()

        commit_pages_atomically(model, {"root": PageDefinition("root", "", ())})
        self.assertEqual(model.mode, MenuMode.LIST)
        self.assertEqual([state.key for state in model.stack], ["root"])

    def test_refreshes_pending_action_without_leaving_target_mode(self) -> None:
        old_action = action(9, "Potion")
        model = P2MenuModel(
            7, {"root": PageDefinition("root", "", (action_entry(old_action),))}
        )
        model.open()
        model.confirm()
        refreshed = ActionDefinition(
            **{**old_action.__dict__, "quantity": 4, "description": "new help"}
        )

        result = commit_pages_atomically(
            model,
            {"root": PageDefinition("root", "", (action_entry(refreshed),))},
        )
        self.assertTrue(result.target_preserved)
        self.assertEqual(model.mode, MenuMode.TARGET)
        self.assertEqual(model.pending_action.quantity, 4)
        self.assertEqual(model.pending_action.description, "new help")

    def test_cancels_target_if_action_disappears(self) -> None:
        definition = action(9, "Potion")
        model = P2MenuModel(
            7, {"root": PageDefinition("root", "", (action_entry(definition),))}
        )
        model.open()
        model.confirm()

        result = commit_pages_atomically(
            model, {"root": PageDefinition("root", "", ())}
        )
        self.assertTrue(result.target_cancelled)
        self.assertEqual(model.mode, MenuMode.LIST)
        self.assertIsNone(model.pending_action)

    def test_cancels_target_if_native_disables_the_action(self) -> None:
        definition = action(9, "Potion")
        model = P2MenuModel(
            7, {"root": PageDefinition("root", "", (action_entry(definition),))}
        )
        model.open()
        model.confirm()
        disabled = action(9, "Potion", enabled=False)

        result = commit_pages_atomically(
            model, {"root": PageDefinition("root", "", (action_entry(disabled),))}
        )
        self.assertTrue(result.target_cancelled)
        self.assertEqual(model.mode, MenuMode.LIST)
        self.assertIsNone(model.pending_action)

    def test_invalid_tree_does_not_mutate_model(self) -> None:
        original = {"root": PageDefinition("root", "", ())}
        model = P2MenuModel(7, original)
        invalid = {
            "root": PageDefinition(
                "root", "", (MenuEntry("Missing", "", EntryKind.PAGE, page_key="x"),)
            )
        }

        with self.assertRaises(ValueError):
            commit_pages_atomically(model, invalid)
        self.assertIs(model.pages, original)


if __name__ == "__main__":
    unittest.main()
