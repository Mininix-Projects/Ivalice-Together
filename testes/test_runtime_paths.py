import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import controller_bindings
import menu_themes
import runtime_paths


IMPORTED_DATA_DIRECTORY = os.environ.get(runtime_paths.DATA_DIRECTORY_ENV)


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        # Auto-discovery scenarios must not inherit worker/bootstrap overrides
        # from the invoking shell or from an earlier test in the same process.
        # Empty override values have the same production meaning as "unset"
        # while patch.dict restores the exact previous environment afterwards.
        self._path_environment = mock.patch.dict(
            "os.environ",
            {
                runtime_paths.GAME_ROOT_ENV: "",
                runtime_paths.DATA_DIRECTORY_ENV: "",
                runtime_paths.RUNTIME_DIRECTORY_ENV: "",
            },
            clear=False,
        )
        self._path_environment.start()
        self.addCleanup(self._path_environment.stop)

    def test_source_modules_share_the_live_coop_directory(self):
        expected = (
            Path(IMPORTED_DATA_DIRECTORY).expanduser().resolve()
            if IMPORTED_DATA_DIRECTORY
            else Path(menu_themes.__file__).resolve().parent
        )
        self.assertEqual(menu_themes.CONFIG_PATH.parent, expected)
        self.assertEqual(controller_bindings.CONFIG_PATH.parent, expected)

    def test_nested_frozen_studio_finds_live_coop_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            coop = Path(directory) / "coop"
            executable = (
                coop / "build_coop_studio" / "dist_any_name" / "FF12CoopStudio.exe"
            )
            executable.parent.mkdir(parents=True)
            for marker in runtime_paths.COOP_DIRECTORY_MARKERS:
                (coop / marker).write_text("", encoding="utf-8")
            resolved = runtime_paths.resolve_config_directory(
                __file__,
                executable=executable,
                frozen=True,
                working_directory=executable.parent,
            )
            self.assertEqual(resolved, coop.resolve())

    def test_standalone_frozen_studio_falls_back_beside_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "FF12CoopStudio.exe"
            resolved = runtime_paths.resolve_config_directory(
                __file__,
                executable=executable,
                frozen=True,
                working_directory=executable.parent,
            )
            self.assertEqual(resolved, executable.resolve().parent)

    def test_frozen_launcher_in_game_root_finds_coop_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coop = root / "coop"
            coop.mkdir()
            executable = root / "IvaliceTogetherConfig.exe"
            for marker in runtime_paths.COOP_DIRECTORY_MARKERS:
                (coop / marker).write_text("", encoding="utf-8")
            resolved = runtime_paths.resolve_config_directory(
                __file__,
                executable=executable,
                frozen=True,
                working_directory=executable.parent,
            )
            self.assertEqual(resolved, coop.resolve())

    def test_frozen_mo2_launcher_inside_x64_uses_game_root_without_vbf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "x64").mkdir()
            (root / "x64" / "FFXII_TZA.exe").write_bytes(b"game")
            executable = root / "x64" / "IvaliceTogetherConfig.exe"

            self.assertEqual(
                runtime_paths.resolve_game_root(
                    __file__,
                    executable=executable,
                    frozen=True,
                    working_directory=root / "x64",
                ),
                root.resolve(),
            )
            self.assertEqual(
                runtime_paths.resolve_config_directory(
                    __file__,
                    executable=executable,
                    frozen=True,
                    working_directory=root / "x64",
                ),
                root.resolve() / "x64" / "coop",
            )

    def test_mo2_backing_x64_launcher_keeps_config_in_the_mod(self):
        with tempfile.TemporaryDirectory() as directory:
            mod = Path(directory) / "MO2" / "mods" / "Ivalice Together"
            executable = mod / "x64" / "IvaliceTogetherConfig.exe"
            executable.parent.mkdir(parents=True)

            self.assertEqual(
                runtime_paths.resolve_config_directory(
                    __file__,
                    executable=executable,
                    frozen=True,
                    working_directory=executable.parent,
                ),
                mod.resolve() / "x64" / "coop",
            )

    def test_one_file_launcher_needs_no_loose_runtime_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "IvaliceTogetherConfig.exe"
            resolved = runtime_paths.resolve_config_directory(
                __file__,
                executable=executable,
                frozen=True,
                working_directory=executable.parent,
            )
            self.assertEqual(resolved, executable.resolve().parent / "coop")

    def test_mo2_backing_launcher_uses_game_working_directory_without_vbf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "x64").mkdir(parents=True)
            (game / "x64" / "FFXII_TZA.exe").write_bytes(b"game")
            executable = (
                root
                / "Mod Organizer"
                / "mods"
                / "Ivalice Together"
                / "IvaliceTogetherConfig.exe"
            )
            executable.parent.mkdir(parents=True)

            self.assertEqual(
                runtime_paths.resolve_game_root(
                    __file__,
                    executable=executable,
                    frozen=True,
                    working_directory=game,
                ),
                game.resolve(),
            )
            self.assertEqual(
                runtime_paths.resolve_config_directory(
                    __file__,
                    executable=executable,
                    frozen=True,
                    working_directory=game,
                ),
                executable.resolve().parent / "coop",
            )

    def test_renamed_launcher_without_vbf_still_uses_coop_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "x64").mkdir()
            (root / "x64" / "FFXII_TZA.exe").write_bytes(b"game")
            executable = root / "Renamed Private Build.exe"
            resolved = runtime_paths.resolve_config_directory(
                __file__,
                executable=executable,
                frozen=True,
                working_directory=root,
            )
            self.assertEqual(resolved, root.resolve() / "coop")

    def test_worker_environment_is_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "game"
            data = root / "coop"
            with mock.patch.dict(
                "os.environ",
                {
                    runtime_paths.GAME_ROOT_ENV: str(root),
                    runtime_paths.DATA_DIRECTORY_ENV: str(data),
                },
            ):
                self.assertEqual(
                    runtime_paths.resolve_game_root(__file__), root.resolve()
                )
                self.assertEqual(
                    runtime_paths.resolve_config_directory(__file__),
                    data.resolve(),
                )


if __name__ == "__main__":
    unittest.main()
