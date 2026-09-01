from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import launcher_core
import session_state


BATTLE_MENU_SOUND_CONFIG_POLL_SECONDS = 0.25
INTERACTION_POST_LOAD_GRACE_SECONDS = 0.25


class BattleMenuSoundSettingsTracker:
    def __init__(
        self,
        path: Path | None = None,
        poll_seconds: float = BATTLE_MENU_SOUND_CONFIG_POLL_SECONDS,
    ) -> None:
        self.path = launcher_core.launcher_config_path() if path is None else path
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.settings = {"p1": True, "p2": True, "p3": True}
        self._stamp: tuple[int, int] | None = None
        self._next_poll = 0.0
        self.reload(force=True)

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def reload(
        self,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> dict[str, bool]:
        now = time.monotonic() if now is None else float(now)
        if not force and now < self._next_poll:
            return self.settings
        self._next_poll = now + self.poll_seconds
        stamp = self._file_stamp()
        if not force and stamp == self._stamp:
            return self.settings
        self._stamp = stamp
        configured = launcher_core.load_launcher_config(self.path)["battle_menu_sounds"]
        self.settings = {
            player: bool(configured[player]) for player in ("p1", "p2", "p3")
        }
        return self.settings


@dataclass
class PendingCombatIndicator:
    source: int
    target: int
    origin: str
    node: int
    next_try: float
    attempt_started: float
    deadline: float
    attempts: int


@dataclass(frozen=True, slots=True)
class PlayerMenuInput:
    up: bool = False
    down: bool = False
    # ``None`` keeps direct/test callers compatible with a fresh press.  The
    # production sampler always publishes True for the initial edge and False
    # for delayed repeats while the direction remains held.
    up_edge: bool | None = None
    down_edge: bool | None = None
    left: bool = False
    right: bool = False
    page_up: bool = False
    page_down: bool = False
    confirm: bool = False
    cancel: bool = False
    confirm_edge: bool = False
    cancel_edge: bool = False
    target_group: bool = False
    target_group_direction: int = 0
    character_up: bool = False
    character_down: bool = False
    character_hotkey: bool = False
    character_direction_ready: bool = False
    native_party_menu: bool = False
    native_previous: bool = False
    native_next: bool = False


@dataclass(frozen=True, slots=True)
class WorldInteractions:
    p1: tuple[int, int, int, int] | None
    p2: tuple[int, int, int, int] | None
    p3: tuple[int, int, int, int] | None
    p1_ready: bool
    p2_present: bool
    p3_present: bool


class InteractionTransitionGate:
    """Keep interaction markers out of loading and post-load frames."""

    def __init__(self, grace_seconds: float = INTERACTION_POST_LOAD_GRACE_SECONDS):
        self.grace_seconds = float(grace_seconds)
        self.location: int | None = None
        self.resume_at = 0.0

    def allows(self, snapshot: session_state.SessionSnapshot, now: float) -> bool:
        location = snapshot.current_location
        ready = bool(
            snapshot.gameplay_ready
            and not snapshot.loading
            and location is not None
            and location != 0xFFFFFFFF
            and location == snapshot.ui_location
        )
        if not ready:
            self.location = None
            self.resume_at = 0.0
            return False
        if self.location != location:
            self.location = location
            self.resume_at = now + self.grace_seconds
            return False
        return now >= self.resume_at
