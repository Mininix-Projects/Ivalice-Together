import re
import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "fontes_nativos"
    / "native_camera_collision_filter_v2.S"
).read_text(encoding="utf-8")


class NativeCameraCollisionAssemblyTests(unittest.TestCase):
    def test_wrapper_preserves_the_windows_x64_call_frame(self):
        self.assertIn("sub rsp, 0x88", SOURCE)
        self.assertIn("add rsp, 0x88", SOURCE)
        for instruction in (
            "mov qword ptr [rsp + 0x30], rcx",
            "mov qword ptr [rsp + 0x38], rdx",
            "mov qword ptr [rsp + 0x40], r8",
            "mov qword ptr [rsp + 0x48], r9",
            "mov rax, qword ptr [rsp + 0xB0]",
            "mov qword ptr [rsp + 0x20], rax",
            "mov rax, qword ptr [rsp + 0xB8]",
            "mov qword ptr [rsp + 0x28], rax",
        ):
            self.assertIn(instruction, SOURCE)

        # RSP is 8 mod 16 on entry. Subtracting 0x88 realigns it for both
        # native calls while preserving the 32-byte shadow space and args 5/6.
        self.assertEqual(0x88 % 16, 8)

    def test_every_unordered_float_comparison_fails_closed(self):
        comparisons = list(re.finditer(r"^\s*ucomiss\b.*$", SOURCE, re.MULTILINE))
        self.assertEqual(len(comparisons), 6)
        for comparison in comparisons:
            tail = SOURCE[comparison.end() :].lstrip().splitlines()
            self.assertTrue(tail, "ucomiss has no branch")
            self.assertEqual(tail[0].strip(), "jp .external_hit")

    def test_template_patch_markers_remain_exact_and_non_overlapping(self):
        self.assertEqual(SOURCE.count("0x1111111122222222"), 1)
        self.assertEqual(SOURCE.count("0x3333333344444444"), 4)
        self.assertEqual(SOURCE.count("0x5555555566666666"), 2)
        self.assertIn("cmp dword ptr [rsp + 0x54], 24", SOURCE)
        self.assertIn(".align 4", SOURCE)

    def test_wrapper_uses_only_volatile_general_and_simd_registers(self):
        # The wrapper may freely use RAX/R10/R11 and XMM0..5. Any use of a
        # nonvolatile Windows-x64 register would require an explicit save pair.
        forbidden = re.compile(
            r"\b(rbx|rbp|rsi|rdi|r12|r13|r14|r15|xmm(?:[6-9]|1[0-5]))\b",
            re.IGNORECASE,
        )
        self.assertIsNone(forbidden.search(SOURCE))


if __name__ == "__main__":
    unittest.main()
