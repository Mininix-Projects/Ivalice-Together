import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import coop_launcher
import launcher_redesign


class LauncherRedesignTests(unittest.TestCase):
    def test_private_runtime_binary_does_not_open_the_configurator(self):
        self.assertTrue(
            launcher_redesign.is_internal_runtime_binary(
                Path("D:/MO2/mods/Ivalice Together/x64/IvaliceTogetherRuntime/"
                     "IvaliceTogetherRuntime.exe"),
                frozen=True,
            )
        )
        self.assertFalse(
            launcher_redesign.is_internal_runtime_binary(
                Path("D:/MO2/mods/Ivalice Together/x64/"
                     "IvaliceTogetherConfig.exe"),
                frozen=True,
            )
        )

    def test_high_resolution_text_uses_a_native_size_font(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            app.preview = False
            app.window = coop_launcher.pygame.Surface((2880, 1620))
            app.text_commands.clear()
            app.draw_text(
                app.canvas,
                app.fonts.body,
                "High-resolution text",
                (400, 300),
            )
            self.assertEqual(len(app.text_commands), 1)
            native = app.fonts.scaled(app.fonts.body, 2.0)
            self.assertGreater(native.get_height(), app.fonts.body.get_height())
        finally:
            app.close()

    def test_downscaled_text_is_rasterized_at_the_output_size(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            app.preview = False
            app.window = coop_launcher.pygame.Surface((1280, 720))
            app.text_commands.clear()
            app.draw_text(
                app.canvas,
                app.fonts.body,
                "Downscaled text",
                (400, 300),
            )
            self.assertEqual(len(app.text_commands), 1)
            native = app.fonts.scaled(app.fonts.body, 1280 / 1440)
            self.assertLess(native.get_height(), app.fonts.body.get_height())
        finally:
            app.close()

    def test_minimum_window_size_is_enforced(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            app.preview = False
            app.window = coop_launcher.pygame.Surface((800, 500))
            resized = coop_launcher.pygame.Surface(
                coop_launcher.MINIMUM_WINDOW_SIZE
            )
            with (
                mock.patch.object(
                    coop_launcher.pygame.display,
                    "set_mode",
                    return_value=resized,
                ) as set_mode,
                mock.patch.object(coop_launcher.pygame.display, "set_caption"),
                mock.patch.object(app, "_set_native_window_icons"),
            ):
                self.assertTrue(app.enforce_minimum_window_size())
            set_mode.assert_called_once_with(
                coop_launcher.MINIMUM_WINDOW_SIZE,
                coop_launcher.pygame.RESIZABLE | coop_launcher.pygame.DOUBLEBUF,
            )
        finally:
            app.close()

    def test_joystick_refresh_ignores_unsupported_raw_device_enumeration(self):
        app = coop_launcher.LauncherApp.__new__(coop_launcher.LauncherApp)
        app.sdl_states = ()
        with (
            mock.patch.object(
                coop_launcher.pygame.joystick,
                "get_count",
                side_effect=AssertionError("raw joystick enumeration is unsupported"),
            ),
            mock.patch.object(
                coop_launcher.pygame.joystick,
                "Joystick",
                side_effect=AssertionError("raw joystick must not be opened"),
            ),
        ):
            app.refresh_joysticks()
        self.assertEqual(app.joysticks, [])
        self.assertEqual(app.joystick_names, ())

    def test_official_brand_logo_is_loaded_and_fitted(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            self.assertLessEqual(app.brand_logo.get_width(), 205)
            self.assertLessEqual(app.brand_logo.get_height(), 64)
            self.assertGreater(app.brand_logo.get_width(), 190)
            self.assertTrue(app.brand_logo.get_flags() & coop_launcher.pygame.SRCALPHA)
        finally:
            app.close()

    def test_official_spec_uses_redesign_and_only_explicit_brand_asset(self):
        root = Path(__file__).resolve().parents[1] / "source"
        spec = (root / "FF12CoopLauncher.spec").read_text(encoding="utf-8")
        self.assertIn('[str(here / "launcher_redesign.py")]', spec)
        self.assertIn('"ivalice_together_logo.png"', spec)
        self.assertNotIn("unrelated_prototype_payload", spec)

    def test_behavioral_methods_are_inherited_unchanged(self):
        for name in (
            "handle_key",
            "handle_mouse",
            "navigate",
            "poll_pad",
            "run",
            "update_launcher",
        ):
            self.assertIs(
                getattr(launcher_redesign.RedesignLauncherApp, name),
                getattr(coop_launcher.LauncherApp, name),
            )
        self.assertFalse(hasattr(coop_launcher.LauncherApp, "start_coop"))

    def test_every_page_preserves_interactive_geometry(self):
        original = coop_launcher.LauncherApp(preview=True)
        redesign = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            for page in ("home", "colors", "controls", "settings"):
                with self.subTest(page=page):
                    original.page = redesign.page = page
                    original.selected_key = redesign.selected_key = f"nav:{page}"
                    original.scene = redesign.scene = None
                    original.scene_dirty = redesign.scene_dirty = True
                    original.render(now=1.0)
                    redesign.render(now=1.0)
                    original_items = {
                        item.key: tuple(item.rect) for item in original.items
                    }
                    redesign_items = {
                        item.key: tuple(item.rect) for item in redesign.items
                    }
                    self.assertEqual(redesign_items.keys(), original_items.keys())
                    self.assertEqual(
                        {
                            key: rect
                            for key, rect in redesign_items.items()
                            if not key.startswith("nav:")
                        },
                        {
                            key: rect
                            for key, rect in original_items.items()
                            if not key.startswith("nav:")
                        },
                    )
        finally:
            original.close()
            redesign.close()

    def test_every_page_renders_in_both_languages(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            for language in ("en", "pt"):
                app.config["language"] = language
                for page in ("home", "colors", "controls", "settings"):
                    with self.subTest(language=language, page=page):
                        app.page = page
                        app.selected_key = f"nav:{page}"
                        app.scene = None
                        app.scene_dirty = True
                        app.render(now=1.0)
                        self.assertEqual(
                            app.canvas.get_size(), coop_launcher.LOGICAL_SIZE
                        )
        finally:
            app.close()

    def test_damage_meter_copy_stays_clear_of_its_controls(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            first_control_x = coop_launcher.CONTENT_X + coop_launcher.CONTENT_W - 438
            text_width = first_control_x - (coop_launcher.CONTENT_X + 20) - 14
            for language in ("en", "pt"):
                with self.subTest(language=language):
                    app.config["language"] = language
                    copy = app.localize("Por personagem até Zerar.")
                    self.assertLessEqual(app.fonts.body_small.size(copy)[0], text_width)
        finally:
            app.close()

    def test_english_footer_has_no_portuguese_copy(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            app.config["language"] = "en"
            footer = app.localize(
                "MOUSE: selecionar e configurar · BOTÃO DIREITO: cancelar ou limpar"
            )
            self.assertEqual(
                footer,
                "MOUSE: select and configure · RIGHT-CLICK: cancel or clear",
            )
        finally:
            app.close()

    def test_standard_english_pages_do_not_render_portuguese_ui_copy(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        rendered: list[str] = []
        original_draw_text = app.draw_text

        def capture_text(
            surface, font, text, position, color=coop_launcher.TEXT, **kwargs
        ):
            rendered.append(app.localize(text))
            return original_draw_text(surface, font, text, position, color, **kwargs)

        app.draw_text = capture_text
        try:
            app.config["language"] = "en"
            for page in ("home", "colors", "controls", "settings"):
                app.page = page
                app.selected_key = f"nav:{page}"
                app.scene = None
                app.scene_dirty = True
                app.render(now=1.0)

            visible_copy = "\n".join(rendered).casefold()
            for portuguese in (
                "aguardando jogo",
                "começar com todos",
                "configuração",
                "interações",
                "controles de cada jogador",
                "cores da party",
                "jogador 1",
                "jogo fechado",
                "minimizar quando",
                "por personagem",
                "salvar e aplicar",
                "sem controle",
                "sons do menu",
            ):
                with self.subTest(portuguese=portuguese):
                    self.assertNotIn(portuguese, visible_copy)
        finally:
            app.close()

    def test_page_change_keeps_cursor_on_the_navigation_label(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            app.set_page("colors")
            self.assertEqual(app.selected_key, "nav:colors")

            app.selected_key = "colors:mode:character"
            app.set_page("colors")
            self.assertEqual(app.selected_key, "nav:colors")
        finally:
            app.close()

    def test_navigation_prewarm_preserves_visible_state(self):
        app = launcher_redesign.RedesignLauncherApp(preview=True)
        try:
            app.page = "home"
            app.selected_key = "nav:home"
            app.render(now=1.0)
            app.prepare_navigation_visuals(now=1.0)

            self.assertEqual(app.page, "home")
            self.assertEqual(app.selected_key, "nav:home")
            self.assertFalse(app.scene_dirty)
            self.assertIn("nav:home", {item.key for item in app.items})

            cached_sizes = {
                key[1] for key in app.panel_cache if key and key[0] == "redesign_panel"
            }
            self.assertGreaterEqual(len(cached_sizes), 8)
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
