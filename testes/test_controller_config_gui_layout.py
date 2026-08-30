import inspect
import unittest

import controller_config_gui as studio


class ControllerConfigGuiLayoutTests(unittest.TestCase):
    def test_default_window_is_compact(self):
        source = inspect.getsource(studio.Configurator.__init__)
        self.assertIn('geometry("960x640")', source)
        self.assertIn("minsize(860, 560)", source)

    def test_controls_use_per_player_emulator_diagrams(self):
        page = inspect.getsource(studio.Configurator._build_controls_page)
        tab = inspect.getsource(studio.Configurator._make_player_tab_diagram)
        diagram = inspect.getsource(studio.PlayerBindingDiagram)
        self.assertIn("_make_player_tab_diagram", page)
        self.assertNotIn("GamepadInputMonitor", page)
        self.assertNotIn("ModernControllerCanvas", page)
        self.assertIn("PlayerBindingDiagram", tab)
        self.assertIn("create_polygon", diagram)
        self.assertIn("create_line", diagram)
        self.assertIn("controls.MAX_BINDINGS_PER_ACTION", diagram)
        self.assertNotIn("range(3)", diagram)
        self.assertNotIn("Allow one controller to own multiple players", page)
        self.assertIn(
            '_make_player_tab_diagram("p3", "PLAYER 3")',
            page,
        )

    def test_controller_assignment_handles_all_three_owners(self):
        source = inspect.getsource(studio.Configurator.assign_controller)
        self.assertIn('for role in ("p1", "p2", "p3")', source)
        self.assertIn("for role in previous_owners:", source)

    def test_every_player_action_has_one_diagram_callout(self):
        for player, actions in studio.controls.PLAYER_ACTIONS.items():
            configured = [action for action, _label in actions]
            diagram = [
                action
                for action, _label, _side, _control in studio.BINDING_DIAGRAM_LAYOUT[
                    player
                ]
            ]
            self.assertCountEqual(diagram, configured)
            self.assertEqual(len(diagram), len(set(diagram)))

    def test_compact_fields_accept_keyboard_and_gamepad_bindings(self):
        self.assertEqual(studio.controls.MAX_BINDINGS_PER_ACTION, 4)
        self.assertEqual(
            studio.compact_binding_label(studio.controls.key_binding(ord("W"))),
            "W",
        )
        self.assertEqual(
            studio.compact_binding_label(
                studio.controls.pad_binding(1, "LS_UP"), assigned_index=1
            ),
            "P2 LS-UP",
        )

    def test_configurator_preserves_the_shared_four_binding_limit(self):
        setter = inspect.getsource(studio.Configurator._set_binding)
        diagram = inspect.getsource(studio.PlayerBindingDiagram._build_callouts)
        self.assertIn("controls.MAX_BINDINGS_PER_ACTION", setter)
        self.assertIn("controls.MAX_BINDINGS_PER_ACTION", diagram)
        self.assertNotIn("unique[:3]", setter)

    def test_character_cards_are_grouped_into_party_and_guests(self):
        source = inspect.getsource(studio.Configurator._build_themes_page)
        card = inspect.getsource(studio.Configurator._make_theme_card)
        self.assertIn("MAIN PARTY", source)
        self.assertIn("GUESTS", source)
        self.assertIn("MAIN_CHARACTER_PROFILES", source)
        self.assertIn("GUEST_CHARACTER_PROFILES", source)
        self.assertIn("width=56", card)
        self.assertIn("height=68", card)
        self.assertIn("height=8", card)
        self.assertNotIn("AVAILABLE PRESETS", source)


if __name__ == "__main__":
    unittest.main()
