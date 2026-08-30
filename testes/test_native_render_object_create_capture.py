import struct
import unittest

import native_render_object_create_capture as capture


class NativeRenderObjectCreateCaptureTests(unittest.TestCase):
    def test_hook_replays_exact_prologue_and_returns_after_it(self):
        code_address = 0x5D000000
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

    def test_excluded_object_branches_to_original_prologue(self):
        code_address = 0x5D000000
        buffer_address = 0x5D001000
        excluded = 0x123456789ABCDEF0
        code = capture.build_hook(
            code_address,
            buffer_address,
            excluded_objects=(excluded,),
        )
        marker = b"\x48\xb8" + struct.pack("<Q", excluded) + b"\x48\x39\xc2\x0f\x84"
        branch = code.index(marker) + len(marker)
        displacement = struct.unpack_from("<i", code, branch)[0]
        original_at = code.rfind(capture.ORIGINAL)
        target = branch + 4 + displacement
        self.assertEqual(
            code[target:original_at],
            b"\x41\x5b\x41\x5a\x58\x9d",
        )

    def test_private_buffer_stays_below_code_page(self):
        end = (
            capture.BUFFER_OFFSET
            + capture.HEADER_SIZE
            + capture.CAPACITY * capture.RECORD_SIZE
        )
        self.assertLessEqual(end, capture.CODE_OFFSET)

    def test_record_layout_contains_every_requested_field(self):
        destinations = [destination for _source, destination in capture.OBJECT_FIELDS]
        self.assertEqual(len(destinations), len(set(destinations)))
        self.assertTrue(all(value + 8 <= capture.RECORD_SIZE for value in destinations))


if __name__ == "__main__":
    unittest.main()
