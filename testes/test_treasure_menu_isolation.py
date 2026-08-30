import unittest

from headless_menu_model import MenuMode
from headless_menu_shell import custom_menu_leaves_free_player


class TreasureMenuIsolationTests(unittest.TestCase):
    def test_one_open_menu_does_not_block_the_other_players(self):
        self.assertTrue(
            custom_menu_leaves_free_player(
                MenuMode.CLOSED,
                MenuMode.CLOSED,
                MenuMode.LIST,
                p2_enabled=True,
                p3_enabled=True,
            )
        )

    def test_player_inside_the_only_open_menu_is_not_considered_free(self):
        self.assertFalse(
            custom_menu_leaves_free_player(
                MenuMode.LIST,
                MenuMode.LIST,
                MenuMode.LIST,
                p2_enabled=True,
                p3_enabled=True,
            )
        )

    def test_disabled_followers_do_not_keep_interactions_open(self):
        self.assertFalse(
            custom_menu_leaves_free_player(
                MenuMode.LIST,
                MenuMode.CLOSED,
                MenuMode.CLOSED,
                p2_enabled=False,
                p3_enabled=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
