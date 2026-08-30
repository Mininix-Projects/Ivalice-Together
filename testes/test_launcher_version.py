"""Check the internal version and prevent hard-coded UI version strings."""

import re
import unittest
from pathlib import Path

import launcher_core as core

HERE = Path(__file__).resolve().parents[1] / "source"


class LauncherVersionTests(unittest.TestCase):
    def test_the_resource_matches_the_constant(self):
        raw = (HERE / "launcher_version.txt").read_text(encoding="utf-8")
        shown = re.search(r"StringStruct\('FileVersion', '([^']+)'\)", raw)
        self.assertIsNotNone(shown, "launcher_version.txt sem FileVersion")
        self.assertEqual(shown.group(1), core.LAUNCHER_VERSION)

    def test_the_numeric_tuple_matches_too(self):
        raw = (HERE / "launcher_version.txt").read_text(encoding="utf-8")
        parts = tuple(int(p) for p in core.LAUNCHER_VERSION.split("."))
        numeric = (*parts, 0, 0, 0)[:3]
        expected = f"({numeric[0]}, {numeric[1]}, {numeric[2]}, 0)"
        self.assertIn(f"filevers={expected}", raw)
        self.assertIn(f"prodvers={expected}", raw)

    def test_the_window_does_not_hardcode_a_version(self):
        source = (HERE / "coop_launcher.py").read_text(encoding="utf-8")
        self.assertIn('pygame.display.set_caption("Ivalice Together")', source)
        self.assertIsNone(
            re.search(r'"VERSÃO \d+\.\d+', source),
            "a tela voltou a escrever a versao a mao",
        )


if __name__ == "__main__":
    unittest.main()
