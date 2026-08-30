import unittest

import coop_runtime as movement


class FakeProcess:
    def __init__(self):
        self.writes = {}
        self.patches = {}

    def read(self, address, size):
        if address == movement.HUD_TEXT_COLOR_HOOK_SITE:
            return movement.HUD_TEXT_COLOR_ORIGINAL
        raise AssertionError(f"unexpected read {address:#x}")

    def write(self, address, data):
        self.writes[address] = bytes(data)

    def patch_code(self, address, data):
        self.patches[address] = bytes(data)


class PartyHudTextColorTests(unittest.TestCase):
    def test_canonical_runtime_restores_retired_text_identity_hook(self):
        process = FakeProcess()
        movement.restore_p2_hud_text_color(process)
        self.assertEqual(process.patches, {})

        patch = (
            b"\xe9"
            + movement.rel32(
                movement.HUD_TEXT_COLOR_HOOK_SITE,
                5,
                movement.HUD_TEXT_COLOR_CAVE,
            )
            + b"\x90" * (len(movement.HUD_TEXT_COLOR_ORIGINAL) - 5)
        )
        process.read = lambda address, size: patch
        movement.restore_p2_hud_text_color(process)
        self.assertEqual(
            process.patches[movement.HUD_TEXT_COLOR_HOOK_SITE],
            movement.HUD_TEXT_COLOR_ORIGINAL,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
