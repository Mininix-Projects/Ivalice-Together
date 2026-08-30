import struct
import unittest

import native_sprite_draw_capture as capture


class NativeSpriteDrawCaptureTests(unittest.TestCase):
    def test_hook_replays_exact_quad_prologue(self):
        hook, return_address, original = capture.SITES["quad"]
        code_address = 0x5D00E000
        code = capture.build_hook(
            code_address,
            0x5D001000,
            original,
            return_address,
        )
        original_at = code.rfind(original)
        self.assertGreater(original_at, 0)
        jump_at = original_at + len(original)
        displacement = struct.unpack_from("<i", code, jump_at + 1)[0]
        self.assertEqual(code_address + jump_at + 5 + displacement, return_address)

    def test_excluded_caller_branches_to_register_restore(self):
        _hook, return_address, original = capture.SITES["quad"]
        code = capture.build_hook(
            0x5D00E000,
            0x5D001000,
            original,
            return_address,
            excluded_callers=(0x2D8D37,),
        )
        marker = (
            b"\x48\xb8" + struct.pack("<Q", 0x2D8D37) + b"\x48\x39\x44\x24\x20\x0f\x84"
        )
        displacement_at = code.index(marker) + len(marker)
        displacement = struct.unpack_from("<i", code, displacement_at)[0]
        target = displacement_at + 4 + displacement
        original_at = code.rfind(original)
        self.assertEqual(code[target:original_at], b"\x41\x5b\x41\x5a\x58\x9d")

    def test_private_buffer_stays_below_code(self):
        end = (
            capture.BUFFER_OFFSET
            + capture.HEADER_SIZE
            + capture.CAPACITY * capture.RECORD_SIZE
        )
        self.assertLessEqual(end, capture.CODE_OFFSET)


if __name__ == "__main__":
    unittest.main()
