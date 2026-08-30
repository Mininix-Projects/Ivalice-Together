import unittest

from headless_menu_shell import NATIVE_SOUND_BY_EVENT
from headless_menu_core import NativeUISoundPlayer, build_sound_pump


class NativeUISoundIdsTests(unittest.TestCase):
    def test_target_group_sound_is_accepted_by_native_pump(self):
        sound_id = NATIVE_SOUND_BY_EVENT["target_group"]
        self.assertEqual(sound_id, 0x0A)
        self.assertIn(sound_id, NativeUISoundPlayer.VALID_IDS)

    def test_treasure_reward_sound_is_accepted_by_native_pump(self):
        self.assertIn(0x27, NativeUISoundPlayer.VALID_IDS)

    def test_treasure_mailbox_has_priority_over_menu_sounds(self):
        code = build_sound_pump(0x6100EC00, 0x6100ED00)
        self.assertIn(bytes.fromhex("41 87 4B 04"), code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
