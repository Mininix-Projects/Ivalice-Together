import struct
import unittest

import native_damage_glyph_capture as capture


class NativeDamageGlyphCaptureTests(unittest.TestCase):
    def test_hook_replays_exact_prologue_and_returns_after_it(self):
        code_address = 0x5D00E000
        buffer_address = 0x5D001000
        code = capture.build_hook(code_address, buffer_address)
        original_at = code.rfind(capture.ORIGINAL)
        self.assertGreater(original_at, 0)
        jump_at = original_at + len(capture.ORIGINAL)
        self.assertEqual(code[jump_at], 0xE9)
        displacement = struct.unpack_from("<i", code, jump_at + 1)[0]
        self.assertEqual(
            code_address + jump_at + 5 + displacement,
            capture.RETURN,
        )

    def test_private_buffer_stays_below_code(self):
        end = (
            capture.BUFFER_OFFSET
            + capture.HEADER_SIZE
            + capture.CAPACITY * capture.RECORD_SIZE
        )
        self.assertLessEqual(end, capture.CODE_OFFSET)

    def test_entry_snapshot_fits_record(self):
        self.assertLessEqual(
            capture.ENTRY_RECORD_OFFSET + capture.ENTRY_SIZE,
            capture.RECORD_SIZE,
        )

    def test_native_layout_offsets_are_inside_snapshot(self):
        for offset in (0x0A, 0x18, 0x20, 0x39):
            self.assertLess(offset, capture.ENTRY_SIZE)


if __name__ == "__main__":
    unittest.main()
