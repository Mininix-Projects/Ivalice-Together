from __future__ import annotations

import ctypes as ct
import hashlib
import os
import json
import sys
import traceback
from pathlib import Path
from typing import Sequence

import runtime_paths


RUNTIME_SWITCH = "--ff12-coop-runtime"
_OWNED_STREAMS: list[object] = []
IMAGE_FILE_MACHINE_AMD64 = 0x8664


class _BestEffortStream:
    """Keep diagnostics from becoming a worker-liveness dependency."""

    def __init__(self, stream: object) -> None:
        self._stream = stream

    def write(self, material: str) -> int:
        try:
            result = self._stream.write(material)
            return len(material) if result is None else int(result)
        except Exception:
            # Logging is observational only.  This also covers a damaged or
            # third-party stream returning an invalid write count.
            return len(material)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    user32 = ct.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ct.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def pe_machine(path: str | Path) -> int:
    target = Path(path)
    with target.open("rb") as stream:
        dos_header = stream.read(64)
        if len(dos_header) != 64 or dos_header[:2] != b"MZ":
            raise RuntimeError(f"invalid PE DOS header: {target}")
        pe_offset = int.from_bytes(dos_header[0x3C:0x40], "little")
        stream.seek(pe_offset)
        header = stream.read(6)
    if len(header) != 6 or header[:4] != b"PE\0\0":
        raise RuntimeError(f"invalid PE signature: {target}")
    return int.from_bytes(header[4:6], "little")


def exit_code(result: object) -> int:
    return 0 if result is None else int(result)


def is_runtime_invocation(arguments: Sequence[str]) -> bool:
    return bool(arguments) and arguments[0] == RUNTIME_SWITCH


def worker_command(
    mode: str,
    source_script: str | Path,
    *arguments: str,
    executable: str | Path | None = None,
    frozen: bool | None = None,
) -> list[str]:
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    program = Path(sys.executable) if executable is None else Path(executable)
    if is_frozen:
        return [str(program), RUNTIME_SWITCH, str(mode), *map(str, arguments)]
    return [
        str(program),
        "-u",
        str(Path(source_script)),
        *map(str, arguments),
    ]


def _prepare_environment() -> Path:
    enable_windows_dpi_awareness()
    game_root = runtime_paths.resolve_game_root(__file__)
    os.environ.setdefault(runtime_paths.GAME_ROOT_ENV, str(game_root))
    data_directory = runtime_paths.resolve_config_directory(__file__)
    os.environ.setdefault(runtime_paths.DATA_DIRECTORY_ENV, str(data_directory))
    data_directory.mkdir(parents=True, exist_ok=True)
    runtime_directory = runtime_paths.resolve_runtime_directory(__file__)
    os.environ.setdefault(runtime_paths.RUNTIME_DIRECTORY_ENV, str(runtime_directory))
    try:
        runtime_directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Runtime status and logs are disposable observability.  Their storage
        # must not veto otherwise valid controller and native-memory startup.
        pass
    return data_directory


def _repair_windowed_streams(data_directory: Path) -> None:
    for attribute, variable, fallback in (
        ("stdout", "FF12_COOP_STDOUT_LOG", "runtime_worker.out.log"),
        ("stderr", "FF12_COOP_STDERR_LOG", "runtime_worker.err.log"),
    ):
        stream = getattr(sys, attribute)
        configured_path = os.environ.get(variable)
        if stream is not None:
            # Helper processes inherit real file handles from the supervisor.
            # Wrap only explicitly redirected runtime streams, leaving the
            # configurator UI and test/interactive consoles untouched.
            if configured_path and not isinstance(stream, _BestEffortStream):
                setattr(sys, attribute, _BestEffortStream(stream))
            continue
        try:
            path = Path(configured_path or data_directory / fallback)
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open("a", encoding="utf-8", buffering=1)
        except (OSError, RuntimeError, TypeError, ValueError):
            try:
                stream = Path(os.devnull).open("a", encoding="utf-8", buffering=1)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
        _OWNED_STREAMS.append(stream)
        setattr(sys, attribute, _BestEffortStream(stream))


def _configure_autostart_environment(
    role_arguments: Sequence[str],
    runtime_directory: Path,
) -> int:
    if len(role_arguments) != 1:
        raise ValueError("autostart requires exactly one game PID")
    try:
        game_pid = int(str(role_arguments[0]), 10)
    except ValueError as error:
        raise ValueError("autostart requires a decimal game PID") from error
    if game_pid <= 0:
        raise ValueError("autostart requires a positive game PID")

    os.environ[runtime_paths.GAME_PID_ENV] = str(game_pid)
    log_path = runtime_directory / "runtime_logs_auto" / "autostart.log"
    os.environ["FF12_COOP_STDOUT_LOG"] = str(log_path)
    os.environ["FF12_COOP_STDERR_LOG"] = str(log_path)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # One stable append-only filename preserves the previous failure when
        # the native sentinel relaunches after an abrupt Python process exit.
        log_path.touch(exist_ok=True)
    except (OSError, RuntimeError, ValueError):
        # The stream repair path falls back to the null device.  Diagnostics
        # are useful but can never be a prerequisite for co-op startup.
        pass
    return game_pid


def _run_autostart(game_pid: int, data_directory: Path) -> int:
    import game_process
    import launcher_core

    game_root = runtime_paths.resolve_game_root(__file__)
    expected_game = (game_root / "x64" / launcher_core.GAME_EXECUTABLE).resolve(
        strict=False
    )
    game_process.validate_process_id(game_pid, expected_game)

    if launcher_core.runtime_mutex_active(game_pid):
        return 0

    launcher_core.ensure_runtime_storage(data_directory)

    import start_headless_coop

    try:
        supervisor = getattr(
            start_headless_coop,
            "run_persistent_supervisor",
            None,
        ) or start_headless_coop.main
        return exit_code(supervisor())
    except start_headless_coop.GameExitedBeforeStartup:
        # Steam may replace the directly opened bootstrap PID with the real
        # game process.  The replacement loads this autostart module itself,
        # so the obsolete worker must end quietly instead of reporting a false
        # runtime failure to the user.
        return 0


def dispatch(arguments: Sequence[str]) -> int:
    if not is_runtime_invocation(arguments) or len(arguments) < 2:
        raise ValueError("missing internal co-op runtime mode")
    mode = str(arguments[1])
    role_arguments = [str(value) for value in arguments[2:]]
    data_directory = _prepare_environment()
    runtime_directory = runtime_paths.resolve_runtime_directory(__file__)
    autostart_pid = None
    if mode == "autostart":
        autostart_pid = _configure_autostart_environment(
            role_arguments,
            runtime_directory,
        )
    _repair_windowed_streams(runtime_directory)
    sys.argv = [f"{mode}.py", *role_arguments]

    if mode == "selftest":
        # Load every component without attaching to the game and audit the build files.
        import battle_pack_runtime_catalog
        import controller_bindings
        import coop_roster
        import coop_runtime
        import headless_menu_shell
        import headless_menu_renderer
        import headless_world_target_overlay
        import helper_lease
        import inject_dll
        import launcher_core
        import menu_themes
        import native_action_catalog
        import native_prompt_glyphs
        import native_ui_assets
        import penelo_story_guard
        import session_state
        import start_headless_coop
        import story_party_patch

        required = {
            "renderer_dll": start_headless_coop.RENDER_DLL,
            "cave_bootstrap_dll": start_headless_coop.CAVE_BOOTSTRAP_DLL,
            "camera_filter": Path(coop_runtime.__file__)
            .resolve()
            .with_name("native_camera_collision_filter_v2.bin"),
            "battle_pack": battle_pack_runtime_catalog.DEFAULT_BATTLE_PACK,
            "action_pack": native_action_catalog.DEFAULT_PACK,
            "action_help": native_action_catalog.DEFAULT_HELP_ACTION,
            "menu_help": native_action_catalog.DEFAULT_HELP_MENU,
            "font_data": native_ui_assets.FONT_DAT,
            "font_kerning": native_ui_assets.FONT_KER,
            "font_atlas": native_ui_assets.FONT_ATLAS,
            "window_atlas": native_ui_assets.NATIVE_WINDOW_ATLAS,
            "cursor_atlas": headless_menu_renderer.CURSOR_ATLAS_PATH,
            "portrait": headless_menu_renderer.PENELO_PORTRAIT_PATH,
            "info_atlas": headless_menu_renderer.INFO_ATLAS_PATH,
            "interaction_atlas": headless_menu_shell.P2_INTERACTION_ATLAS_PATH,
            "interaction_pad_atlas": native_prompt_glyphs.PAD_ATLAS_PATH,
            "interaction_keyboard_atlas": native_prompt_glyphs.KEY_ATLAS_PATH,
            "launcher_background": Path(__file__).resolve().parent
            / "launcher_assets"
            / "background.png",
            "launcher_move_sound": Path(__file__).resolve().parent
            / "launcher_assets"
            / "move.wav",
        }
        expected_helper_roles = {
            "headless_menu",
            "movement",
            "story_party",
            "penelo_guard",
        }
        if set(helper_lease.ROLE_MUTEX_PREFIXES) != expected_helper_roles:
            raise RuntimeError("embedded helper lease role contract is incomplete")
        helper_lease_names = {
            role: helper_lease.mutex_name(1, role)
            for role in sorted(expected_helper_roles)
        }
        if len(set(helper_lease_names.values())) != len(expected_helper_roles):
            raise RuntimeError("embedded helper lease names are not unique")
        for filename in (
            "background.png",
            "cancel.wav",
            "confirm.wav",
            "cursol_4_p.png",
            "error.wav",
            "icon.ico",
            "ivalice_together_logo.png",
            "move.wav",
            "open.wav",
        ):
            required[f"launcher_asset_{filename}"] = launcher_core.bundled_asset(
                filename
            )
        for (
            member_id,
            path,
        ) in headless_menu_renderer.PORTRAIT_PATH_BY_MEMBER_ID.items():
            required[f"portrait_{member_id}"] = path
        for name, path in (
            ("pause_on_off", headless_menu_renderer.PAUSE_ON_OFF_PATH),
            ("leader_command_flag", headless_menu_renderer.LEADER_COMMAND_FLAG_PATH),
        ):
            required[name] = path
        for _bit, name in headless_world_target_overlay.WEAK_ELEMENTS:
            required[f"target_trait_{name}"] = (
                headless_world_target_overlay.TRAIT_SPRITE_DIR / f"{name}.png"
            )
        for (
            _bit,
            _name,
            icon,
            _beneficial,
        ) in headless_world_target_overlay.STATUS_EFFECTS:
            if icon:
                required[f"target_status_{icon}"] = (
                    headless_world_target_overlay.TRAIT_SPRITE_DIR
                    / "status"
                    / f"{icon}.png"
                )
        for filename in (
            "border_bl.png",
            "border_bottom.png",
            "border_br.png",
            "border_left.png",
            "border_right.png",
            "border_tl.png",
            "border_top.png",
            "border_tr.png",
            "panel.png",
        ):
            required[f"menu_skin_{filename}"] = (
                native_ui_assets.SKIN_MENU_ROOT / filename
            )
        for index, relative in enumerate(launcher_core.GAME_OVERRIDE_PATHS, start=1):
            required[f"game_override_{index}"] = (
                launcher_core.game_override_bundle_root() / relative
            )
        missing = [name for name, path in required.items() if not Path(path).is_file()]
        if missing:
            raise FileNotFoundError(
                "embedded co-op resources missing: " + ", ".join(missing)
            )
        for name in ("renderer_dll", "cave_bootstrap_dll"):
            machine = pe_machine(required[name])
            if machine != IMAGE_FILE_MACHINE_AMD64:
                raise RuntimeError(
                    f"embedded DLL is not x64: {name} (machine=0x{machine:04X})"
                )

        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame
        from PIL import Image

        pygame.init()
        controller_diagnostics: dict[str, object] = {}
        try:
            pygame.display.set_mode((1, 1))
            background = pygame.image.load(
                str(required["launcher_background"])
            ).convert()
            pygame.transform.smoothscale(background, (64, 36))
            pygame.font.Font(None, 14).render("Ivalice Together", True, (255, 255, 255))
            pygame.mixer.Sound(str(required["launcher_move_sound"]))
            for _name, path in required.items():
                suffix = Path(path).suffix.casefold()
                if suffix in {".png", ".ico"}:
                    with Image.open(path) as image:
                        image.verify()
                elif suffix == ".wav":
                    pygame.mixer.Sound(str(path))
            if not controller_bindings.sdl_backend_available():
                raise RuntimeError("embedded SDL controller backend is unavailable")
            xinput_states = controller_bindings.read_xinput_states()
            sdl_states = controller_bindings.read_sdl_controller_states()
            controller_diagnostics = {
                "xinput_slots": [
                    index
                    for index, state in enumerate(xinput_states)
                    if state is not None
                ],
                "sdl_controllers": [dict(state.assignment) for state in sdl_states],
            }
        finally:
            pygame.quit()

        # A first run has no persistent configuration or binaries yet. The
        # self-test covers missing and interrupted files.
        config_directory = data_directory / "runtime_selftest" / "configuração limpa"
        config_directory.mkdir(parents=True, exist_ok=True)
        config_checks = (
            (
                "launcher",
                launcher_core.load_launcher_config,
                launcher_core.DEFAULT_LAUNCHER_CONFIG,
            ),
            (
                "controllers",
                controller_bindings.load_config,
                controller_bindings.default_config(),
            ),
            ("roster", coop_roster.load, coop_roster.default_roster()),
            ("session", session_state.load_config, session_state.default_config()),
            ("themes", menu_themes.load_selection, menu_themes.default_selection()),
        )
        checked_configs: list[str] = []
        for name, loader, expected in config_checks:
            path = config_directory / f"{name}.json"
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            if loader(path) != expected:
                raise RuntimeError(f"first-run defaults failed: {name}")
            path.write_text('{"interrompido":', encoding="utf-8")
            if loader(path) != expected:
                raise RuntimeError(f"truncated config fallback failed: {name}")
            checked_configs.append(name)

        executable = Path(sys.executable).resolve()
        for mode in (
            "autostart",
            "bootstrap",
            "story_party",
            "penelo_guard",
            "movement",
            "inject_dll",
            "headless_menu",
        ):
            command = worker_command(
                mode,
                f"{mode}.py",
                executable=executable,
                frozen=True,
            )
            if command[:3] != [str(executable), RUNTIME_SWITCH, mode]:
                raise RuntimeError(f"frozen worker command failed: {mode}")
        renderer_copy = start_headless_coop.materialize_render_dll(
            start_headless_coop.RENDER_DLL,
            data_directory / "runtime_selftest",
        )
        if not renderer_copy.is_file() or "_MEI" in str(renderer_copy.parent):
            raise RuntimeError("renderer was not materialized persistently")
        bootstrap_copy = start_headless_coop.materialize_runtime_dll(
            start_headless_coop.CAVE_BOOTSTRAP_DLL,
            data_directory / "runtime_selftest",
        )
        if not bootstrap_copy.is_file() or "_MEI" in str(bootstrap_copy.parent):
            raise RuntimeError("rel32 bootstrap was not materialized persistently")
        bootstrap_hash = hashlib.sha256(bootstrap_copy.read_bytes()).hexdigest()
        if bootstrap_hash.casefold() != launcher_core.AUTOSTART_MODULE_SHA256:
            raise RuntimeError(
                "embedded autostart module does not match the launcher profile"
            )
        camera_copy = start_headless_coop.materialize_runtime_dll(
            required["camera_filter"],
            data_directory / "runtime_selftest",
        )
        if not camera_copy.is_file() or "_MEI" in str(camera_copy.parent):
            raise RuntimeError("camera filter was not materialized persistently")

        report = data_directory / "runtime_selftest.json"
        report.write_text(
            json.dumps(
                {
                    "result": "ok",
                    "frozen": bool(getattr(sys, "frozen", False)),
                    "executable": str(Path(sys.executable).resolve()),
                    "data_directory": str(data_directory.resolve()),
                    "resource_count": len(required),
                    "first_run_configs": checked_configs,
                    "worker_commands": "ok",
                    "helper_leases": sorted(helper_lease_names),
                    "controller_backends": controller_diagnostics,
                    "persistent_renderer": str(renderer_copy),
                    "persistent_cave_bootstrap": str(bootstrap_copy),
                    "camera_filter": str(camera_copy),
                    "embedded_autostart_sha256": bootstrap_hash.upper(),
                    "resources": {name: str(path) for name, path in required.items()},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            f"FF12_COOP_SELFTEST_OK resources={len(required)} report={report}",
            flush=True,
        )
        return 0

    if mode == "autostart":
        if autostart_pid is None:
            raise RuntimeError("autostart PID was not configured")
        return _run_autostart(autostart_pid, data_directory)
    if mode == "bootstrap":
        import start_headless_coop

        supervisor = getattr(
            start_headless_coop,
            "run_persistent_supervisor",
            None,
        ) or start_headless_coop.main
        return exit_code(supervisor())
    if mode == "story_party":
        import story_party_patch

        return exit_code(story_party_patch.main())
    if mode == "penelo_guard":
        import penelo_story_guard

        return exit_code(penelo_story_guard.main())
    if mode == "movement":
        import coop_runtime

        return exit_code(coop_runtime.main())
    if mode == "inject_dll":
        if len(role_arguments) != 1:
            raise ValueError("inject_dll requires one DLL path")
        import inject_dll

        inject_dll.inject(role_arguments[0])
        return 0
    if mode == "headless_menu":
        import headless_menu_shell

        return exit_code(headless_menu_shell.CoopMenuRuntime().run())
    raise ValueError(f"unknown internal co-op runtime mode: {mode}")


def safe_dispatch(arguments: Sequence[str]) -> int:
    try:
        return dispatch(arguments)
    except BaseException:
        reported = False
        try:
            if sys.stderr is not None:
                traceback.print_exc(file=sys.stderr)
                reported = True
        except BaseException:
            pass
        if not reported:
            try:
                _write_emergency_traceback(traceback.format_exc())
            except BaseException:
                # Even a damaged emergency-log path cannot replace the
                # original, deterministic worker failure code with a crash.
                pass
        return 1


def _write_emergency_traceback(formatted: str) -> Path | None:
    """Persist a startup failure that happened before windowed streams existed."""

    candidates: list[Path] = []
    for variable, suffix in (
        (runtime_paths.RUNTIME_DIRECTORY_ENV, ()),
        ("LOCALAPPDATA", ("Ivalice Together", "Runtime")),
        ("TEMP", ("Ivalice Together",)),
    ):
        configured = os.environ.get(variable)
        if not configured:
            continue
        try:
            candidate = Path(configured).expanduser()
            candidates.append(candidate.joinpath(*suffix))
        except (OSError, RuntimeError, TypeError, ValueError):
            continue

    checked: set[Path] = set()
    for directory in candidates:
        try:
            target_directory = directory.resolve(strict=False)
            if target_directory in checked:
                continue
            checked.add(target_directory)
            target_directory.mkdir(parents=True, exist_ok=True)
            target = target_directory / "runtime_bootstrap_error.log"
            with target.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(formatted)
                if not formatted.endswith("\n"):
                    stream.write("\n")
            return target
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return None


__all__ = [
    "RUNTIME_SWITCH",
    "dispatch",
    "exit_code",
    "is_runtime_invocation",
    "safe_dispatch",
    "worker_command",
]
