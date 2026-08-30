from __future__ import annotations

import sys
import os
from pathlib import Path


COOP_DIRECTORY_MARKERS = (
    "headless_menu_shell.py",
    "session_config.json",
)

GAME_ROOT_ENV = "FF12_COOP_GAME_ROOT"
DATA_DIRECTORY_ENV = "FF12_COOP_DATA_DIR"
GAME_PID_ENV = "FF12_COOP_GAME_PID"
CAVE_BASE_ENV = "FF12_COOP_CAVE_BASE"
DUAL_CAVE_BASE_ENV = "FF12_COOP_DUAL_CAVE_BASE"
DAMAGE_CAVE_BASE_ENV = "FF12_COOP_DAMAGE_CAVE_BASE"
RUNTIME_DIRECTORY_ENV = "FF12_COOP_RUNTIME_DIR"
PUBLIC_CONFIGURATOR_STEM = "ivalicetogetherconfig"


def resolve_game_root(
    module_file: str | Path,
    *,
    executable: str | Path | None = None,
    frozen: bool | None = None,
    working_directory: str | Path | None = None,
) -> Path:
    configured = os.environ.get(GAME_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if is_frozen:
        executable_path = (
            Path(sys.executable) if executable is None else Path(executable)
        )
        executable_directory = executable_path.resolve().parent
        current_directory = (
            Path.cwd()
            if working_directory is None
            else Path(working_directory).resolve()
        )
        candidates = (
            executable_directory,
            *executable_directory.parents,
            current_directory,
            *current_directory.parents,
        )
        checked: set[Path] = set()
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate in checked:
                continue
            checked.add(candidate)
            if (candidate / "x64" / "FFXII_TZA.exe").is_file():
                return candidate
        return executable_directory
    return Path(module_file).resolve().parent.parent


def resolve_config_directory(
    module_file: str | Path,
    *,
    executable: str | Path | None = None,
    frozen: bool | None = None,
    working_directory: str | Path | None = None,
) -> Path:
    configured = os.environ.get(DATA_DIRECTORY_ENV)
    if configured:
        return Path(configured).expanduser().resolve()

    source_directory = Path(module_file).resolve().parent
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if not is_frozen:
        return source_directory

    executable_path = Path(sys.executable) if executable is None else Path(executable)
    runtime_directory = executable_path.resolve().parent
    for candidate in (runtime_directory, *runtime_directory.parents):
        if all((candidate / marker).is_file() for marker in COOP_DIRECTORY_MARKERS):
            return candidate

        coop_child = candidate / "coop"
        if all((coop_child / marker).is_file() for marker in COOP_DIRECTORY_MARKERS):
            return coop_child

    if executable_path.stem.casefold() == PUBLIC_CONFIGURATOR_STEM:
        return runtime_directory / "coop"

    resolved_game = resolve_game_root(
        module_file,
        executable=executable_path,
        frozen=True,
        working_directory=working_directory,
    )
    if (resolved_game / "x64" / "FFXII_TZA.exe").is_file():
        return resolved_game / "coop"

    if (runtime_directory / "x64" / "FFXII_TZA.exe").is_file():
        return runtime_directory / "coop"
    return runtime_directory


def resolve_runtime_directory(module_file: str | Path) -> Path:
    """Resolve disposable runtime data outside mod-manager virtual output."""

    configured = os.environ.get(RUNTIME_DIRECTORY_ENV)
    if configured:
        return Path(configured).expanduser().resolve()

    if bool(getattr(sys, "frozen", False)) or os.environ.get(GAME_ROOT_ENV):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return (
                Path(local_app_data).expanduser().resolve()
                / "Ivalice Together"
                / "Runtime"
            )

    return resolve_config_directory(module_file) / "runtime"


def config_path(module_file: str | Path, filename: str) -> Path:
    return resolve_config_directory(module_file) / filename


__all__ = [
    "COOP_DIRECTORY_MARKERS",
    "CAVE_BASE_ENV",
    "DAMAGE_CAVE_BASE_ENV",
    "DATA_DIRECTORY_ENV",
    "GAME_PID_ENV",
    "DUAL_CAVE_BASE_ENV",
    "GAME_ROOT_ENV",
    "RUNTIME_DIRECTORY_ENV",
    "config_path",
    "resolve_config_directory",
    "resolve_game_root",
    "resolve_runtime_directory",
]
