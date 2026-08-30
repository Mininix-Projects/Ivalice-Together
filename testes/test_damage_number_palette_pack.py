import importlib.util
import colorsys
import os
import sys
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
COOP = REPOSITORY / "source"
TOOLS = REPOSITORY / "ferramentas"
sys.path.insert(0, str(COOP))


def find_game_archive() -> Path:
    configured = os.environ.get("FF12_GAME_ROOT")
    if configured:
        return Path(configured) / "FFXII_TZA.vbf"
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "FFXII_TZA.vbf"
        if candidate.is_file():
            return candidate
    return Path("__missing_FFXII_TZA.vbf")


GAME_ARCHIVE = find_game_archive()

import launcher_core  # noqa: E402
import menu_themes  # noqa: E402


def load_builder():
    path = TOOLS / "build_damage_number_palette_pack.py"
    if not path.is_file():
        raise unittest.SkipTest("local game-asset builder is unavailable")
    spec = importlib.util.spec_from_file_location("damage_palette_builder", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = load_builder()
OVERRIDE_RELATIVE = builder.section_override_path(Path(), "us")
OVERRIDE = COOP / "game_overrides" / OVERRIDE_RELATIVE


@unittest.skipUnless(OVERRIDE.is_file(), "game-derived palette pack is unavailable")
class DamageNumberPalettePackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = OVERRIDE.read_bytes()
        cls.picture = builder.parse_picture(cls.pack, 0)

    def test_every_language_has_a_section_only_override(self):
        for locale in builder.LOCALES:
            relative = builder.section_override_path(Path(), locale)
            with self.subTest(locale=locale):
                self.assertTrue((COOP / "game_overrides" / relative).is_file())
                self.assertIn(relative, launcher_core.GAME_OVERRIDE_PATHS)
        self.assertFalse(
            (COOP / "game_overrides" / Path(builder.ARCHIVE_ENTRY)).exists()
        )

    def test_pack_appends_exactly_seven_palettes(self):
        self.assertEqual(self.picture.palette_count, 115)
        self.assertEqual(
            [theme.index for theme in builder.THEMES],
            list(range(108, 115)),
        )

    def test_every_custom_palette_preserves_the_approved_alpha_curve(self):
        source_start = (
            self.picture.clut_offset + builder.SOURCE_PALETTE * builder.PALETTE_SIZE
        )
        source = self.pack[source_start : source_start + builder.PALETTE_SIZE]
        source_alpha = source[3::4]
        self.assertGreater(len(set(source_alpha)), 90)
        for theme in builder.THEMES:
            start = self.picture.clut_offset + theme.index * builder.PALETTE_SIZE
            palette = self.pack[start : start + builder.PALETTE_SIZE]
            self.assertEqual(palette[3::4], source_alpha, theme.name)
            self.assertEqual(
                palette,
                builder.build_theme_palette(source, theme),
                theme.name,
            )

    def test_only_emerald_uses_the_dark_damage_curve(self):
        source_start = (
            self.picture.clut_offset + builder.SOURCE_PALETTE * builder.PALETTE_SIZE
        )
        source = self.pack[source_start : source_start + builder.PALETTE_SIZE]
        for theme in builder.THEMES:
            legacy = builder.build_palette(source, theme.hue_degrees)
            themed = builder.build_theme_palette(source, theme)
            if theme.name == "emerald":
                self.assertNotEqual(themed, legacy)
                self.assertEqual(theme.saturation_floor, 0.62)
                self.assertEqual(theme.value_scale, 0.78)
                self.assertEqual(theme.value_gamma, 2.10)
                self.assertEqual(theme.neutral_core_transition, (0.30, 0.65))
            else:
                self.assertEqual(themed, legacy, theme.name)

    def test_emerald_keeps_a_white_core_but_darkens_the_green_edge(self):
        emerald = next(theme for theme in builder.THEMES if theme.name == "emerald")
        core = builder.rotate_hue(
            255,
            255,
            255,
            emerald.hue_degrees,
            saturation_floor=emerald.saturation_floor,
            value_scale=emerald.value_scale,
            value_gamma=emerald.value_gamma,
            neutral_core_transition=emerald.neutral_core_transition,
        )
        self.assertEqual(core, (255, 255, 255))

        outline = builder.rotate_hue(
            207,
            55,
            48,
            emerald.hue_degrees,
            saturation_floor=emerald.saturation_floor,
            value_scale=emerald.value_scale,
            value_gamma=emerald.value_gamma,
            neutral_core_transition=emerald.neutral_core_transition,
        )
        red, green, blue = outline
        self.assertGreater(green, red * 2)
        self.assertGreater(green, blue * 1.7)
        self.assertLess(green, 150)

    def test_runtime_theme_indexes_match_the_appended_palettes(self):
        expected = {theme.name: theme.index for theme in builder.THEMES}
        self.assertEqual(menu_themes.DAMAGE_NUMBER_CLUT_BY_THEME, expected)
        for name in expected:
            self.assertEqual(
                menu_themes.damage_number_color(name),
                (127, 127, 127),
            )

    def test_hue_rotation_preserves_the_approved_saturation_and_value_ramps(self):
        # Representative authored colours from palette 5's digit region.
        core = builder.approved_pink(255, 177, 196)
        outline = builder.approved_pink(207, 55, 48)
        gold_core = builder.rotate_hue(255, 177, 196, 52.0)
        gold_outline = builder.rotate_hue(207, 55, 48, 52.0)

        pink_core_hsv = colorsys.rgb_to_hsv(*(channel / 255.0 for channel in core))
        pink_outline_hsv = colorsys.rgb_to_hsv(
            *(channel / 255.0 for channel in outline)
        )
        gold_core_hsv = colorsys.rgb_to_hsv(*(channel / 255.0 for channel in gold_core))
        gold_outline_hsv = colorsys.rgb_to_hsv(
            *(channel / 255.0 for channel in gold_outline)
        )

        for pink, gold in (
            (pink_core_hsv, gold_core_hsv),
            (pink_outline_hsv, gold_outline_hsv),
        ):
            self.assertAlmostEqual(gold[0], 52.0 / 360.0, delta=1.0 / 255.0)
            self.assertAlmostEqual(gold[1], pink[1], delta=1.0 / 255.0)
            self.assertAlmostEqual(gold[2], pink[2], delta=1.0 / 255.0)

    def test_generated_override_rebuilds_identically_from_the_vbf(self):
        if not GAME_ARCHIVE.is_file():
            self.skipTest("set FF12_GAME_ROOT to enable archive comparison")
        base = builder.read_archive_entry(
            GAME_ARCHIVE,
            builder.ARCHIVE_ENTRY,
        )
        rebuilt, _hashes = builder.build_pack(base)
        self.assertEqual(builder.battle_section(rebuilt), self.pack)


if __name__ == "__main__":
    unittest.main()
