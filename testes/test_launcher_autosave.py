import os
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import coop_launcher
import launcher_core
import launcher_redesign


class LauncherAutosaveTests(unittest.TestCase):
    def test_completed_binding_capture_is_saved_immediately(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.editor = mock.Mock(dirty=True)

        result = app.persist_control_capture(
            launcher_core.CaptureResult("bound", "Comando alterado.")
        )

        app.editor.save.assert_called_once_with()
        self.assertEqual(result.kind, "bound")
        self.assertEqual(
            result.message,
            "Comando alterado. Aplicado ao co-op.",
        )

    def test_control_save_failure_rolls_back_to_disk(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.editor = mock.Mock(dirty=True)
        app.editor.save.side_effect = OSError("read only")

        result = app.persist_control_capture(
            launcher_core.CaptureResult("bound", "Comando alterado.")
        )

        app.editor.reload.assert_called_once_with()
        self.assertEqual(result.kind, "invalid")

    def test_color_selection_is_saved_before_the_picker_closes(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.player_theme_draft = {"p1": "sapphire", "p2": "gold", "p3": "pink"}
        app.character_theme_draft = {5: "gold"}
        app.theme_picker_target = "p1"
        app.theme_picker_return_key = "colors:player:p1"
        app.selected_key = "colors:theme:sapphire"
        app.theme_dirty = False
        app.save_themes = mock.Mock(return_value=True)
        app.close_theme_picker = mock.Mock()
        app.audio = mock.Mock()

        app.select_theme("ruby")

        app.save_themes.assert_called_once_with()
        app.close_theme_picker.assert_called_once_with(sound=False)
        self.assertEqual(app.player_theme_draft["p1"], "ruby")

    def test_manual_save_actions_are_not_exposed(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            for page, forbidden in (
                ("colors", "colors:save"),
                ("controls", "controls:save"),
            ):
                app.page = page
                app.selected_key = f"nav:{page}"
                app.scene = None
                app.scene_dirty = True
                app.render(now=1.0)
                self.assertNotIn(forbidden, {item.key for item in app.items})
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
