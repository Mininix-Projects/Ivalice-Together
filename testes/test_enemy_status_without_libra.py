"""Enemy afflictions appear on the card with or without Libra."""

import unittest

import headless_world_target_overlay as world_overlay
from headless_menu_shell import live_target_panel_height


def status_bit(name: str) -> int:
    for bit, label, _icon, _good in world_overlay.STATUS_EFFECTS:
        if label == name:
            return 1 << bit
    raise AssertionError(f"status {name!r} ausente da tabela")


AFFLICTIONS = (
    status_bit("Blind")
    | status_bit("Poison")
    | status_bit("Silence")
    | status_bit("Slow")
)


def target(*, reveal: bool, status: int = 0, weak: int = 0):
    return world_overlay.LiveTargetInfo(
        name="Wolf",
        actor=1,
        handle=1,
        position=(0.0, 0.0, 0.0),
        level=12,
        current_hp=430,
        max_hp=980,
        reveal_stats=reveal,
        weak_mask=weak,
        status_mask=status,
        viewer_has_libra=reveal,
    )


def card_height(info) -> int:
    return world_overlay.render_target_info(info, player=1, theme_name="gold").height


class EnemyStatusWithoutLibraTests(unittest.TestCase):
    def test_afflictions_add_their_band_without_libra(self):
        rows = len(world_overlay.status_effect_lines(AFFLICTIONS))
        self.assertGreater(rows, 0)
        self.assertEqual(
            card_height(target(reveal=False, status=AFFLICTIONS)),
            world_overlay.panel_height(False, rows),
        )

    def test_the_band_is_the_same_with_and_without_libra(self):
        self.assertEqual(
            card_height(target(reveal=False, status=AFFLICTIONS)),
            card_height(target(reveal=True, status=AFFLICTIONS)),
        )

    def test_a_clean_enemy_still_has_no_band(self):
        self.assertEqual(
            card_height(target(reveal=False, status=0)),
            world_overlay.panel_height(False, 0),
        )

    def test_libra_still_buys_level_hp_and_weakness(self):
        # Libra still owns the weakness row. Only that row changes the height
        # between states when the target has a known weakness.
        weak = 0x02
        self.assertLess(
            card_height(target(reveal=False, weak=weak)),
            card_height(target(reveal=True, weak=weak)),
        )

    def test_layout_height_now_agrees_with_what_is_drawn(self):
        # live_target_panel_height always counted status rows. The old gate
        # reserved space for a strip that was never drawn.
        for reveal in (False, True):
            with self.subTest(reveal=reveal):
                info = target(reveal=reveal, status=AFFLICTIONS)
                self.assertEqual(live_target_panel_height(info), card_height(info))


if __name__ == "__main__":
    unittest.main()
