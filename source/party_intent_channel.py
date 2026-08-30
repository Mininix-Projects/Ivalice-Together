from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
import uuid
from typing import Iterable

import atomic_files
import runtime_paths


CHANNEL_VERSION = 1
RUNTIME_DIRECTORY_NAME = "runtime_logs_headless"
MAIN_MEMBER_IDS = frozenset(range(6))
MIN_STABLE_POLLS = 2
MIN_STABLE_SECONDS = 0.20
PUBLISH_ATTEMPTS = 3
PUBLISH_RETRY_SECONDS = 0.02


def channel_path(game_pid: int) -> Path:
    if int(game_pid) <= 0:
        raise ValueError("game PID must be positive")
    return (
        runtime_paths.resolve_runtime_directory(__file__)
        / RUNTIME_DIRECTORY_NAME
        / str(int(game_pid))
        / "party_active_intent.json"
    )


def normalize_active_members(values: Iterable[int]) -> tuple[int, ...]:
    raw_members = tuple(values)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw_members):
        raise ValueError("active party intent members must be exact integers")
    members = tuple(raw_members)
    if not 1 <= len(members) <= 3:
        raise ValueError("active party intent must contain one to three members")
    if len(set(members)) != len(members):
        raise ValueError("active party intent contains duplicate members")
    if any(member not in MAIN_MEMBER_IDS for member in members):
        raise ValueError("active party intent contains a guest/story member")
    return members


@dataclass(frozen=True)
class PartyIntentRecord:
    game_pid: int
    writer: str
    epoch: int
    published_unix_ns: int
    battle_info: int
    active_members: tuple[int, ...]
    source: str

    @property
    def token(self) -> tuple[str, int]:
        return self.writer, self.epoch

    def payload(self) -> dict[str, object]:
        return {
            "version": CHANNEL_VERSION,
            "game_pid": self.game_pid,
            "writer": self.writer,
            "epoch": self.epoch,
            "published_unix_ns": self.published_unix_ns,
            "battle_info": self.battle_info,
            "active_members": list(self.active_members),
            "source": self.source,
        }


def read_party_intent(
    path: Path,
    *,
    game_pid: int | None = None,
) -> PartyIntentRecord | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        exact_integer_fields = (
            payload.get("version"),
            payload.get("game_pid"),
            payload.get("epoch"),
            payload.get("published_unix_ns"),
            payload.get("battle_info"),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in exact_integer_fields
        ):
            return None
        if payload["version"] != CHANNEL_VERSION:
            return None
        pid = payload["game_pid"]
        if pid <= 0 or (game_pid is not None and pid != int(game_pid)):
            return None
        writer = str(payload["writer"])
        epoch = payload["epoch"]
        published = payload["published_unix_ns"]
        battle_info = payload["battle_info"]
        source = str(payload.get("source", ""))
        if not writer or epoch <= 0 or published <= 0 or battle_info <= 0:
            return None
        members = normalize_active_members(payload["active_members"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return PartyIntentRecord(
        game_pid=pid,
        writer=writer,
        epoch=epoch,
        published_unix_ns=published,
        battle_info=battle_info,
        active_members=members,
        source=source,
    )


class PartyIntentPublisher:
    def __init__(self, game_pid: int, path: Path | None = None) -> None:
        if int(game_pid) <= 0:
            raise ValueError("game PID must be positive")
        self.game_pid = int(game_pid)
        self.path = channel_path(self.game_pid) if path is None else Path(path)
        self.writer = uuid.uuid4().hex
        self.epoch = 0

    def publish(
        self,
        active_members: Iterable[int],
        *,
        battle_info: int,
        source: str,
    ) -> PartyIntentRecord:
        members = normalize_active_members(active_members)
        if (
            isinstance(battle_info, bool)
            or not isinstance(battle_info, int)
            or battle_info <= 0
        ):
            raise ValueError("battle-info generation must be nonzero")
        next_epoch = self.epoch + 1
        # Reserve the token before the first filesystem mutation.  A failed
        # replace can be ambiguous (the destination may already contain the
        # record), so this writer must never reuse the epoch for other content.
        self.epoch = next_epoch
        record = PartyIntentRecord(
            game_pid=self.game_pid,
            writer=self.writer,
            epoch=next_epoch,
            published_unix_ns=time.time_ns(),
            battle_info=battle_info,
            active_members=members,
            source=str(source),
        )
        payload = json.dumps(record.payload(), indent=2, ensure_ascii=False) + "\n"
        for attempt in range(PUBLISH_ATTEMPTS):
            try:
                atomic_files.write_text(
                    self.path,
                    payload,
                    encoding="utf-8",
                )
                break
            except OSError:
                # Some filesystems can commit the replace and then fail while
                # cleaning up the temporary.  Exact readback turns that
                # ambiguous result into a committed transaction without ever
                # reusing its writer/epoch token for different content.
                if read_party_intent(self.path, game_pid=self.game_pid) == record:
                    break
                if attempt + 1 == PUBLISH_ATTEMPTS:
                    raise
                time.sleep(PUBLISH_RETRY_SECONDS)
        return record


class ExplicitPartyEditCapture:
    def __init__(
        self,
        *,
        min_stable_polls: int = MIN_STABLE_POLLS,
        min_stable_seconds: float = MIN_STABLE_SECONDS,
    ) -> None:
        if min_stable_polls < 2:
            raise ValueError("at least two stable polls are required")
        if min_stable_seconds < 0.0:
            raise ValueError("stable duration cannot be negative")
        self.min_stable_polls = int(min_stable_polls)
        self.min_stable_seconds = float(min_stable_seconds)
        self.armed = False
        self.source = ""
        self.battle_info = 0
        self._signature: tuple[int, tuple[int, ...]] | None = None
        self._stable_polls = 0
        self._stable_since = 0.0

    def begin(self, source: str, *, battle_info: int) -> None:
        if (
            isinstance(battle_info, bool)
            or not isinstance(battle_info, int)
            or battle_info <= 0
        ):
            raise ValueError("battle-info generation must be nonzero")
        self.armed = True
        self.source = str(source)
        self.battle_info = battle_info
        self._signature = None
        self._stable_polls = 0
        self._stable_since = 0.0

    def cancel(self) -> None:
        self.armed = False
        self.source = ""
        self.battle_info = 0
        self._signature = None
        self._stable_polls = 0
        self._stable_since = 0.0

    def observe(
        self,
        *,
        native_frontend_open: bool,
        field_stable: bool,
        battle_info: int,
        active_members: Iterable[int] | None,
        now: float,
    ) -> tuple[tuple[int, ...], int, str] | None:
        if not self.armed:
            return None
        battle_info_known = bool(
            not isinstance(battle_info, bool)
            and isinstance(battle_info, int)
            and battle_info > 0
        )
        if battle_info_known and battle_info != self.battle_info:
            self.cancel()
            return None
        if native_frontend_open or not field_stable or not battle_info_known:
            self._signature = None
            self._stable_polls = 0
            self._stable_since = 0.0
            return None
        try:
            members = normalize_active_members(active_members or ())
        except (TypeError, ValueError):
            self._signature = None
            self._stable_polls = 0
            self._stable_since = 0.0
            return None
        signature = (battle_info, members)
        if signature != self._signature:
            self._signature = signature
            self._stable_polls = 1
            self._stable_since = float(now)
            return None
        self._stable_polls += 1
        if (
            self._stable_polls < self.min_stable_polls
            or float(now) - self._stable_since < self.min_stable_seconds
        ):
            return None
        result = members, battle_info, self.source
        self.cancel()
        return result


__all__ = [
    "ExplicitPartyEditCapture",
    "PartyIntentPublisher",
    "PartyIntentRecord",
    "channel_path",
    "normalize_active_members",
    "read_party_intent",
]
