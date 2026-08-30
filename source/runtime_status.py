from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import atomic_files
import runtime_paths


STATUS_VERSION = 1


@dataclass(frozen=True)
class RuntimeStatus:
    game_pid: int
    state: str
    message: str


def status_path(game_pid: int, data_directory: Path | None = None) -> Path:
    if int(game_pid) <= 0:
        raise ValueError("game PID must be positive")
    root = (
        runtime_paths.resolve_runtime_directory(__file__)
        if data_directory is None
        else Path(data_directory)
    )
    return root / "runtime_status" / f"{int(game_pid)}.json"


def write_status(
    game_pid: int,
    state: str,
    message: str,
    *,
    data_directory: Path | None = None,
) -> RuntimeStatus:
    if state not in ("preparing", "ready"):
        raise ValueError(f"unsupported runtime state: {state}")
    status = RuntimeStatus(int(game_pid), state, str(message))
    target = status_path(status.game_pid, data_directory)
    payload = {
        "version": STATUS_VERSION,
        "game_pid": status.game_pid,
        "state": status.state,
        "message": status.message,
    }
    atomic_files.write_text(
        target,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return status


def read_status(
    game_pid: int,
    *,
    data_directory: Path | None = None,
) -> RuntimeStatus | None:
    try:
        payload = json.loads(
            status_path(game_pid, data_directory).read_text(encoding="utf-8")
        )
        if int(payload.get("version", 0)) != STATUS_VERSION:
            return None
        status = RuntimeStatus(
            int(payload["game_pid"]),
            str(payload["state"]),
            str(payload.get("message", "")),
        )
        if status.game_pid != int(game_pid) or status.state not in (
            "preparing",
            "ready",
        ):
            return None
        return status
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def clear_status(
    game_pid: int,
    *,
    data_directory: Path | None = None,
) -> None:
    try:
        status_path(game_pid, data_directory).unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "RuntimeStatus",
    "clear_status",
    "read_status",
    "status_path",
    "write_status",
]
