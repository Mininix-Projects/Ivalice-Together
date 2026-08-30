"""Render an enemy's name in red when its level greatly exceeds the player."""

import unittest

from PIL import ImageChops

import headless_world_target_overlay as world_overlay


GAP = world_overlay.DANGEROUS_LEVEL_GAP


def target(*, level: int, viewer: int, hostile: bool = True, reveal: bool = False):
    return world_overlay.LiveTargetInfo(
        name="Vargnahl",
        actor=1,
        handle=1,
        position=(0.0, 0.0, 0.0),
        level=level,
        current_hp=8400,
        max_hp=12000,
        reveal_stats=reveal,
        viewer_has_libra=reveal,
        viewer_level=viewer,
        hostile=hostile,
    )


def card(info):
    return world_overlay.render_target_info(info, player=1, theme_name="gold")


def reddish_pixels(image) -> int:
    total = 0
    # Flattened pixel data is sufficient because the cards are small and offline.
    for red, green, blue, alpha in image.convert("RGBA").get_flattened_data():
        if alpha > 60 and red > 140 and red > green + 60 and red > blue + 60:
            total += 1
    return total


class DangerousEnemyNameTests(unittest.TestCase):
    def test_threshold_is_exact(self):
        self.assertFalse(
            world_overlay.outclasses_viewer(target(level=20 + GAP - 1, viewer=20))
        )
        self.assertTrue(
            world_overlay.outclasses_viewer(target(level=20 + GAP, viewer=20))
        )

    def test_a_weaker_or_equal_enemy_never_paints(self):
        for level in (5, 20):
            with self.subTest(level=level):
                self.assertFalse(
                    world_overlay.outclasses_viewer(target(level=level, viewer=20))
                )

    def test_allies_never_paint(self):
        self.assertFalse(
            world_overlay.outclasses_viewer(target(level=99, viewer=20, hostile=False))
        )

    def test_unknown_viewer_level_never_paints(self):
        # If the read fails, retaining the native color is safer than painting all text.
        self.assertFalse(world_overlay.outclasses_viewer(target(level=99, viewer=0)))

    def test_libra_does_not_change_the_verdict(self):
        for reveal in (False, True):
            with self.subTest(reveal=reveal):
                self.assertTrue(
                    world_overlay.outclasses_viewer(
                        target(level=45, viewer=20, reveal=reveal)
                    )
                )

    def test_the_card_actually_turns_red_without_libra(self):
        safe = card(target(level=45, viewer=40))
        deadly = card(target(level=45, viewer=20))
        self.assertEqual(reddish_pixels(safe), 0)
        self.assertGreater(reddish_pixels(deadly), 100)

    def test_name_and_level_change_but_hp_does_not(self):
        safe = card(target(level=45, viewer=40)).convert("RGB")
        deadly = card(target(level=45, viewer=20)).convert("RGB")
        self.assertEqual(safe.size, deadly.size)

        # RGBA getbbox observes alpha, which is identical for the same glyphs.
        # Compare RGB so the color change remains visible to the assertion.
        box = ImageChops.difference(safe, deadly).getbbox()
        self.assertIsNotNone(box)
        left, top, _right, bottom = box
        # The change starts on the level/name row and includes the "Lv." label.
        self.assertLess(left, 40)
        # The HP row below it must remain unchanged.
        self.assertLess(bottom, safe.height // 2)
        self.assertGreater(top, 0)

    def test_layout_is_untouched(self):
        self.assertEqual(
            card(target(level=45, viewer=40)).height,
            card(target(level=45, viewer=20)).height,
        )


if __name__ == "__main__":
    unittest.main()
