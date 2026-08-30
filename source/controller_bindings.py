from __future__ import annotations

import ctypes as ct
import hashlib
import json
import math
import os
import struct
import time
from ctypes import wintypes as wt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import atomic_files
import controller_state_transport
import runtime_paths

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")

try:
    import pygame
    import pygame._sdl2.controller as sdl_controller
except (ImportError, OSError):
    pygame = None
    sdl_controller = None


CONFIG_VERSION = 10
PLAYER_IDS = ("p1", "p2", "p3")
DESKTOP_DEVICE = "desktop"
MAX_BINDINGS_PER_ACTION = 4


INITIAL_REPEAT_DELAY = 0.250
REPEAT_INTERVAL = 0.100
AXIS_DEADZONE = 0.24
SDL_PUMP_FAILURE_GRACE_SECONDS = 0.100
XINPUT_STATE_FAILURE_GRACE_SECONDS = 0.100


@dataclass
class ConfirmReopenGuard:
    delay_seconds: float = 0.20
    requires_release: bool = False
    blocked_until: float = 0.0

    def arm(self, now: float) -> None:
        self.requires_release = True
        self.blocked_until = max(self.blocked_until, float(now) + self.delay_seconds)

    def update(self, *, confirm_down: bool, now: float) -> None:
        if self.requires_release and not confirm_down:
            self.requires_release = False

            self.blocked_until = max(
                self.blocked_until, float(now) + self.delay_seconds
            )

    def can_open(self, now: float) -> bool:
        return not self.requires_release and float(now) >= self.blocked_until


@dataclass(frozen=True)
class ConfigNormalizationReport:
    """Describes a persisted controller-config repair worth surfacing in the UI."""

    source_version: int | None
    migrated_desktop: bool
    kept_desktop_owner: str | None
    removed_desktop_owners: tuple[str, ...]

    @property
    def incompatible_future(self) -> bool:
        return bool(
            self.source_version is not None
            and self.source_version > CONFIG_VERSION
        )

    @property
    def needs_persist(self) -> bool:
        if self.incompatible_future:
            return False
        return bool(
            self.source_version != CONFIG_VERSION
            or self.migrated_desktop
            or self.removed_desktop_owners
        )


class UnsupportedControllerConfigVersion(RuntimeError):
    def __init__(self, source_version: int) -> None:
        self.source_version = int(source_version)
        self.supported_version = CONFIG_VERSION
        super().__init__(
            "controller configuration version "
            f"{self.source_version} is newer than supported version "
            f"{self.supported_version}"
        )


CONFIG_PATH = runtime_paths.config_path(__file__, "controller_config.json")


PLAYER_ACTIONS = {
    "p1": (
        ("menu_up", "Menu: Up"),
        ("menu_down", "Menu: Down"),
        ("menu_left", "Menu: Left / previous"),
        ("menu_right", "Menu: Right / next"),
        ("confirm", "Menu: Confirm / open"),
        ("cancel", "Menu: Cancel / close"),
        ("native_party_menu", "Native: Party / Licenses"),
        ("native_pause", "Native: Pause"),
    ),
    "p2": (
        ("toggle_control", "Co-op: Enable / disable Player 2"),
        ("move_up", "Movement: Forward"),
        ("move_down", "Movement: Back"),
        ("move_left", "Movement: Left"),
        ("move_right", "Movement: Right"),
        ("run_toggle", "Movement: Walk / run toggle"),
        ("flee", "Battle: Flee"),
        ("battle_menu", "Battle: Open command menu"),
        ("menu_up", "Menu: Up"),
        ("menu_down", "Menu: Down"),
        ("menu_left", "Menu: Left"),
        ("menu_right", "Menu: Right"),
        ("target_previous", "Menu: Previous target group"),
        ("target_next", "Menu: Next target group"),
        ("quickening", "Quickening: Activate slot"),
        ("confirm", "Menu: Confirm / open / interact"),
        ("cancel", "Menu: Cancel / close"),
        ("native_party_menu", "Native: Party / Licenses"),
        ("native_pause", "Native: Pause"),
    ),
    "p3": (
        ("toggle_control", "Co-op: Enable / disable Player 3"),
        ("move_up", "Movement: Forward"),
        ("move_down", "Movement: Back"),
        ("move_left", "Movement: Left"),
        ("move_right", "Movement: Right"),
        ("run_toggle", "Movement: Walk / run toggle"),
        ("flee", "Battle: Flee"),
        ("battle_menu", "Battle: Open command menu"),
        ("menu_up", "Menu: Up"),
        ("menu_down", "Menu: Down"),
        ("menu_left", "Menu: Left"),
        ("menu_right", "Menu: Right"),
        ("target_previous", "Menu: Previous target group"),
        ("target_next", "Menu: Next target group"),
        ("quickening", "Quickening: Activate slot"),
        ("confirm", "Menu: Confirm / open / interact"),
        ("cancel", "Menu: Cancel / close"),
        ("native_party_menu", "Native: Party / Licenses"),
        ("native_pause", "Native: Pause"),
    ),
}


VK_NAMES = {
    0x01: "Mouse Left",
    0x02: "Mouse Right",
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x10: "Shift",
    0x11: "Ctrl",
    0x12: "Alt",
    0x1B: "Esc",
    0x20: "Space",
    0x21: "Page Up",
    0x22: "Page Down",
    0x23: "End",
    0x24: "Home",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x2D: "Insert",
    0x2E: "Delete",
    0x60: "Num 0",
    0x61: "Num 1",
    0x62: "Num 2",
    0x63: "Num 3",
    0x64: "Num 4",
    0x65: "Num 5",
    0x66: "Num 6",
    0x67: "Num 7",
    0x68: "Num 8",
    0x69: "Num 9",
    0xA0: "Left Shift",
    0xA1: "Right Shift",
    0xA2: "Left Ctrl",
    0xA3: "Right Ctrl",
}
for _code in range(ord("A"), ord("Z") + 1):
    VK_NAMES[_code] = chr(_code)
for _code in range(ord("0"), ord("9") + 1):
    VK_NAMES[_code] = chr(_code)
for _index in range(1, 13):
    VK_NAMES[0x6F + _index] = f"F{_index}"


XINPUT_BUTTONS = {
    "DPAD_UP": 0x0001,
    "DPAD_DOWN": 0x0002,
    "DPAD_LEFT": 0x0004,
    "DPAD_RIGHT": 0x0008,
    "START": 0x0010,
    "BACK": 0x0020,
    "LEFT_THUMB": 0x0040,
    "RIGHT_THUMB": 0x0080,
    "LB": 0x0100,
    "RB": 0x0200,
    "A": 0x1000,
    "B": 0x2000,
    "X": 0x4000,
    "Y": 0x8000,
}

XINPUT_AXIS_CONTROLS = (
    "LS_UP",
    "LS_DOWN",
    "LS_LEFT",
    "LS_RIGHT",
    "RS_UP",
    "RS_DOWN",
    "RS_LEFT",
    "RS_RIGHT",
    "LT",
    "RT",
)

GAMEPAD_CONTROLS = tuple(XINPUT_BUTTONS) + XINPUT_AXIS_CONTROLS


def key_binding(vk: int) -> dict[str, Any]:
    return {"device": "keyboard", "vk": int(vk)}


def pad_binding(index: int, control: str) -> dict[str, Any]:
    return {"device": "xinput", "index": int(index), "control": control}


def sdl_assignment(
    guid: str,
    ordinal: int,
    name: str,
    uid: str = "",
) -> dict[str, Any]:
    assignment = {
        "backend": "sdl",
        "guid": str(guid).strip().casefold(),
        "ordinal": max(0, int(ordinal)),
        "name": " ".join(str(name).split()) or "SDL controller",
    }
    normalized_uid = str(uid).strip().casefold()
    if normalized_uid:
        assignment["uid"] = normalized_uid[:64]
    return assignment


def normalize_device_assignment(value: Any) -> int | dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 3 else None
    if not isinstance(value, dict) or value.get("backend") != "sdl":
        return None
    guid = str(value.get("guid", "")).strip().casefold()
    if not guid:
        return None
    try:
        ordinal = int(value.get("ordinal", 0))
    except (TypeError, ValueError):
        return None
    if ordinal < 0:
        return None
    return sdl_assignment(
        guid,
        ordinal,
        str(value.get("name", "SDL controller")),
        str(value.get("uid", "")),
    )


def device_assignment_key(value: Any) -> tuple[Any, ...] | None:
    normalized = normalize_device_assignment(value)
    if isinstance(normalized, int):
        return ("xinput", normalized)
    if isinstance(normalized, dict):
        if normalized.get("uid"):
            return ("sdl-uid", normalized["uid"])
        return ("sdl", normalized["guid"], normalized["ordinal"])
    return None


def _legacy_sdl_assignment_key(value: Any) -> tuple[Any, ...] | None:
    normalized = normalize_device_assignment(value)
    if not isinstance(normalized, dict):
        return None
    return ("sdl", normalized["guid"], normalized["ordinal"])


def sdl_assignments_match(left: Any, right: Any) -> bool:
    """Compare SDL identities without letting a reconnect steal an identical pad."""

    left = normalize_device_assignment(left)
    right = normalize_device_assignment(right)
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_uid = left.get("uid")
    right_uid = right.get("uid")
    if left_uid and right_uid:
        return left_uid == right_uid
    if left_uid or right_uid:
        # Old configuration files have no stable id. They may be upgraded by
        # matching the old GUID/ordinal once, but a modern assignment must never
        # fall back to an ordinal after its physical device disappears.
        legacy = left if not left_uid else right
        modern = right if not left_uid else left
        return (
            legacy["guid"] == modern["guid"]
            and legacy["ordinal"] == modern["ordinal"]
        )
    return _legacy_sdl_assignment_key(left) == _legacy_sdl_assignment_key(right)


def normalize_device_aliases(value: Any) -> list[dict[str, Any]]:
    """Return deterministic SDL aliases, with one owner for each SDL identity.

    A single physical pad may legitimately expose more than one SDL identity
    for the same XInput slot.  DS4Windows commonly does this when both the
    physical HID device and its virtual output are visible.  Keeping every
    observed SDL identity in the same XInput group prevents that one pad from
    being assigned to two players.
    """
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    claimed_sdl: list[dict[str, Any]] = []
    for candidate in value:
        if not isinstance(candidate, dict):
            continue
        xinput = candidate.get("xinput")
        if isinstance(xinput, bool) or not isinstance(xinput, int):
            continue
        if not 0 <= xinput <= 3:
            continue
        sdl = normalize_device_assignment(candidate.get("sdl"))
        if not isinstance(sdl, dict):
            continue
        if any(sdl_assignments_match(sdl, claimed) for claimed in claimed_sdl):
            continue
        result.append({"xinput": xinput, "sdl": sdl})
        claimed_sdl.append(sdl)
    return result


def register_device_alias(
    config: dict[str, Any],
    xinput: int,
    sdl: Any,
) -> bool:
    """Record that two backend identities represent one physical controller."""
    normalized_sdl = normalize_device_assignment(sdl)
    if (
        isinstance(xinput, bool)
        or not isinstance(xinput, int)
        or not 0 <= xinput <= 3
        or not isinstance(normalized_sdl, dict)
    ):
        raise ValueError("invalid controller alias")
    aliases = normalize_device_aliases(config.get("device_aliases"))
    replacement = {"xinput": xinput, "sdl": normalized_sdl}
    filtered = [
        alias
        for alias in aliases
        if alias["xinput"] != xinput
        and not sdl_assignments_match(alias["sdl"], normalized_sdl)
    ]
    filtered.append(replacement)
    changed = filtered != aliases
    config["device_aliases"] = filtered
    return changed


def register_device_alias_group(
    config: dict[str, Any],
    xinput: int,
    sdl_assignments: Iterable[Any],
) -> bool:
    """Replace one XInput slot's aliases with all simultaneous SDL mirrors."""
    if (
        isinstance(xinput, bool)
        or not isinstance(xinput, int)
        or not 0 <= xinput <= 3
    ):
        raise ValueError("invalid XInput controller alias")
    normalized_group: list[dict[str, Any]] = []
    for candidate in sdl_assignments:
        normalized = normalize_device_assignment(candidate)
        if not isinstance(normalized, dict) or any(
            sdl_assignments_match(normalized, current)
            for current in normalized_group
        ):
            continue
        normalized_group.append(normalized)
    if not normalized_group:
        raise ValueError("controller alias group has no valid SDL identities")

    aliases = normalize_device_aliases(config.get("device_aliases"))
    filtered = [
        alias
        for alias in aliases
        if alias["xinput"] != xinput
        and not any(
            sdl_assignments_match(alias["sdl"], current)
            for current in normalized_group
        )
    ]
    filtered.extend({"xinput": xinput, "sdl": sdl} for sdl in normalized_group)
    normalized_aliases = normalize_device_aliases(filtered)
    changed = normalized_aliases != aliases
    config["device_aliases"] = normalized_aliases
    return changed


def device_identity_keys(
    config: dict[str, Any],
    assignment: Any,
) -> set[tuple[Any, ...]]:
    """Resolve every backend key known to represent an assigned controller."""
    key = device_assignment_key(assignment)
    if key is None:
        return set()
    normalized = normalize_device_assignment(assignment)
    keys = {key}
    for alias in normalize_device_aliases(config.get("device_aliases")):
        xinput_key = ("xinput", alias["xinput"])
        sdl_key = device_assignment_key(alias["sdl"])
        if key == xinput_key and sdl_key is not None:
            keys.add(sdl_key)
        elif isinstance(normalized, dict) and sdl_assignments_match(
            normalized,
            alias["sdl"],
        ):
            keys.add(xinput_key)
    return keys


def device_assignments_overlap(
    config: dict[str, Any],
    left: Any,
    right: Any,
) -> bool:
    normalized_left = normalize_device_assignment(left)
    normalized_right = normalize_device_assignment(right)
    if isinstance(normalized_left, dict) and isinstance(normalized_right, dict):
        if sdl_assignments_match(normalized_left, normalized_right):
            return True
    left_keys = device_identity_keys(config, left)
    return bool(left_keys and left_keys & device_identity_keys(config, right))


def aliased_sdl_assignment(
    config: dict[str, Any],
    xinput: int | None,
) -> dict[str, Any] | None:
    assignments = aliased_sdl_assignments(config, xinput)
    return assignments[0] if assignments else None


def aliased_sdl_assignments(
    config: dict[str, Any],
    xinput: int | None,
) -> tuple[dict[str, Any], ...]:
    if xinput is None:
        return ()
    return tuple(
        dict(alias["sdl"])
        for alias in normalize_device_aliases(config.get("device_aliases"))
        if alias["xinput"] == xinput
    )


def aliased_xinput_slot(
    config: dict[str, Any],
    sdl: Any,
) -> int | None:
    if not isinstance(normalize_device_assignment(sdl), dict):
        return None
    for alias in normalize_device_aliases(config.get("device_aliases")):
        if sdl_assignments_match(alias["sdl"], sdl):
            return int(alias["xinput"])
    return None


def equivalent_sdl_assignments(
    config: dict[str, Any],
    assignment: Any,
) -> tuple[dict[str, Any], ...]:
    """Return every SDL identity proven to belong to the configured pad.

    A direct SDL assignment can be one member of a physical/virtual mirror
    group.  Keeping the direct identity first preserves the configured
    preference, while the remaining aliases let a DS4Windows/Steam Input
    mirror carry the same owner's input when that preferred exposure is
    temporarily absent.
    """

    direct = normalize_device_assignment(assignment)
    if not isinstance(direct, dict):
        return ()
    result = [direct]
    xinput = aliased_xinput_slot(config, direct)
    if xinput is None:
        return tuple(result)
    for candidate in aliased_sdl_assignments(config, xinput):
        if any(sdl_assignments_match(candidate, current) for current in result):
            continue
        result.append(candidate)
    return tuple(result)


FOLLOWER_TOGGLE_KEYS = {
    # F3 belongs to FF12's quadruple-speed shortcut.  The co-op-only toggles
    # deliberately use separate, otherwise unclaimed keys so either role can
    # still be enabled or disabled explicitly after desktop ownership moves.
    "p2": 0x75,  # F6
    "p3": 0x76,  # F7
}


# Keyboard1's default PC layout, expressed in the co-op action vocabulary.
# This immutable-by-convention table is shared by P2 and P3 so their keyboard
# profiles cannot drift again.  Each call to _follower_default_bindings()
# materializes fresh binding dictionaries; no mutable list is shared between
# players or default_config() calls.
FOLLOWER_KEYBOARD_DEFAULTS: dict[str, tuple[int, ...]] = {
    "move_up": (ord("W"),),
    "move_down": (ord("S"),),
    "move_left": (ord("A"),),
    "move_right": (ord("D"),),
    "run_toggle": (0xA0,),  # Left Shift
    "flee": (0xA2,),  # Left Ctrl
    "battle_menu": (ord("F"),),
    "menu_up": (ord("I"), 0x68),  # Numpad 8
    "menu_down": (ord("K"), 0x62),  # Numpad 2
    "menu_left": (ord("J"), 0x64, ord("Q")),  # Numpad 4, alternate Q
    "menu_right": (ord("L"), 0x66, ord("E")),  # Numpad 6, alternate E
    "target_previous": (ord("1"),),
    "target_next": (ord("3"),),
    # F is also Square's Quickening slot.  Battle Menu and Quickening are
    # deliberately separate logical actions even though FF12 assigns both to
    # F; the runtime chooses the one valid in the current presentation.
    "quickening": (ord("F"),),
    "confirm": (0x20, 0x0D, 0x01),  # Space, Enter, mouse left
    "cancel": (ord("C"), 0x08, 0x02),  # C, Backspace, mouse right
    "native_party_menu": (ord("R"),),
    "native_pause": (0x1B,),  # Esc
}


FOLLOWER_GAMEPAD_DEFAULTS: dict[str, str] = {
    "move_up": "LS_UP",
    "move_down": "LS_DOWN",
    "move_left": "LS_LEFT",
    "move_right": "LS_RIGHT",
    "run_toggle": "LEFT_THUMB",
    "flee": "RT",
    "battle_menu": "X",
    "menu_up": "DPAD_UP",
    "menu_down": "DPAD_DOWN",
    "menu_left": "DPAD_LEFT",
    "menu_right": "DPAD_RIGHT",
    "target_previous": "LB",
    "target_next": "RB",
    "confirm": "A",
    "cancel": "B",
    "native_party_menu": "Y",
    "native_pause": "START",
}


def _follower_default_bindings(
    player: str,
    pad_index: int,
) -> dict[str, list[dict[str, Any]]]:
    if player not in FOLLOWER_TOGGLE_KEYS:
        raise ValueError(f"unknown follower player: {player}")
    result: dict[str, list[dict[str, Any]]] = {}
    for action, _label in PLAYER_ACTIONS[player]:
        if action == "toggle_control":
            result[action] = [key_binding(FOLLOWER_TOGGLE_KEYS[player])]
            continue
        values = [
            key_binding(vk)
            for vk in FOLLOWER_KEYBOARD_DEFAULTS.get(action, ())
        ]
        control = FOLLOWER_GAMEPAD_DEFAULTS.get(action)
        if action == "quickening":
            # Native Quickening assigns Square to P2 and Cross to P3.
            control = "X" if player == "p2" else "A"
        if control is not None:
            values.append(pad_binding(pad_index, control))
        result[action] = values
    return result


def default_config() -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "device_aliases": [],
        "devices": {"p1": 0, "p2": None, "p3": None},
        # Keyboard and mouse are one physical source.  Keep ownership separate
        # from gamepad assignment so migration never invents or discards a pad.
        "desktop": {"p1": False, "p2": True, "p3": False},
        "players": {
            "p1": {
                "menu_up": [pad_binding(0, "DPAD_UP")],
                "menu_down": [pad_binding(0, "DPAD_DOWN")],
                "menu_left": [pad_binding(0, "DPAD_LEFT"), pad_binding(0, "LB")],
                "menu_right": [pad_binding(0, "DPAD_RIGHT"), pad_binding(0, "RB")],
                "confirm": [pad_binding(0, "A")],
                "cancel": [pad_binding(0, "B")],
                "native_party_menu": [pad_binding(0, "Y")],
                "native_pause": [pad_binding(0, "START")],
            },
            "p2": _follower_default_bindings("p2", 1),
            "p3": _follower_default_bindings("p3", 2),
        },
    }


def desktop_owner(config: dict[str, Any]) -> str | None:
    desktop = config.get("desktop")
    if not isinstance(desktop, dict):
        return None
    return next(
        (player for player in PLAYER_IDS if desktop.get(player) is True),
        None,
    )


def player_owns_desktop(config: dict[str, Any], player: str) -> bool:
    if player not in PLAYER_IDS:
        raise ValueError(f"unknown player: {player}")
    return desktop_owner(config) == player


def set_desktop_owner(config: dict[str, Any], player: str | None) -> str | None:
    if player is not None and player not in PLAYER_IDS:
        raise ValueError(f"unknown player: {player}")
    previous = desktop_owner(config)
    config["desktop"] = {role: role == player for role in PLAYER_IDS}
    return previous


def _source_config_version(raw: Any) -> int | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("version")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _players_with_legacy_keyboard_bindings(raw: dict[str, Any]) -> tuple[str, ...]:
    players = raw.get("players")
    if not isinstance(players, dict):
        return ()
    owners: list[str] = []
    for player in PLAYER_IDS:
        actions = players.get(player)
        if not isinstance(actions, dict):
            continue
        if any(
            isinstance(binding, dict)
            and binding.get("device") == "keyboard"
            and isinstance(binding.get("vk"), int)
            and not isinstance(binding.get("vk"), bool)
            and 0 < int(binding["vk"]) < 256
            for bindings in actions.values()
            if isinstance(bindings, list)
            for binding in bindings
        ):
            owners.append(player)
    return tuple(owners)


def _normalize_desktop_ownership(
    raw: dict[str, Any],
    *,
    source_version: int | None,
) -> tuple[dict[str, bool], ConfigNormalizationReport]:
    supplied = raw.get("desktop")
    canonical_supplied = bool(
        isinstance(supplied, dict)
        and set(supplied) == set(PLAYER_IDS)
        and all(isinstance(supplied[player], bool) for player in PLAYER_IDS)
    )
    explicit_owners = (
        tuple(player for player in PLAYER_IDS if supplied.get(player) is True)
        if isinstance(supplied, dict)
        else ()
    )

    if isinstance(supplied, dict):
        owner = explicit_owners[0] if explicit_owners else None
        removed = explicit_owners[1:]
    else:
        # Version 8 and older inferred desktop use from bindings.  Pick the
        # first role deterministically (P2 for the established P2/P3 defaults)
        # without deleting the now-inactive maps belonging to the other roles.
        candidates = _players_with_legacy_keyboard_bindings(raw)
        owner = candidates[0] if candidates else None
        removed = ()

    incompatible_future = bool(
        source_version is not None and source_version > CONFIG_VERSION
    )
    migrated = bool(
        not incompatible_future
        and (source_version != CONFIG_VERSION or not canonical_supplied)
    )
    desktop = {player: player == owner for player in PLAYER_IDS}
    report = ConfigNormalizationReport(
        source_version=source_version,
        migrated_desktop=migrated,
        kept_desktop_owner=owner,
        removed_desktop_owners=removed,
    )
    return desktop, report


def normalize_config_with_report(
    raw: Any,
) -> tuple[dict[str, Any], ConfigNormalizationReport]:
    result = default_config()
    if not isinstance(raw, dict):
        report = ConfigNormalizationReport(
            source_version=None,
            migrated_desktop=True,
            kept_desktop_owner=desktop_owner(result),
            removed_desktop_owners=(),
        )
        return result, report
    source_version = _source_config_version(raw)
    result["desktop"], report = _normalize_desktop_ownership(
        raw,
        source_version=source_version,
    )
    if report.incompatible_future:
        # Keep a non-saveable version marker on the normalized runtime view.
        # Runtime consumers can safely use the known fields, while a later
        # load_config() -> save_config() pair cannot erase future fields.
        assert source_version is not None
        result["version"] = source_version
    result["device_aliases"] = normalize_device_aliases(raw.get("device_aliases"))
    supplied_devices = raw.get("devices")
    if isinstance(supplied_devices, dict):
        for player in PLAYER_IDS:
            if player not in supplied_devices:
                continue
            value = normalize_device_assignment(supplied_devices.get(player))
            result["devices"][player] = value
        if "p3" not in supplied_devices and result["devices"]["p3"] in (
            result["devices"]["p1"],
            result["devices"]["p2"],
        ):
            result["devices"]["p3"] = None

    claimed: list[int | dict[str, Any]] = []
    for player in PLAYER_IDS:
        assignment = result["devices"].get(player)
        key = device_assignment_key(assignment)
        if key is None:
            continue
        if any(
            device_assignments_overlap(result, assignment, current)
            for current in claimed
        ):
            # Never invent a different physical slot while repairing a hand-
            # edited or legacy duplicate. An unassigned role is visible in the
            # configurator; silently moving it to a disconnected slot is not.
            result["devices"][player] = None
            key = None
        if key is not None:
            claimed.append(assignment)
    players = raw.get("players")
    if not isinstance(players, dict):
        return result, report
    for player, actions in PLAYER_ACTIONS.items():
        supplied = players.get(player)
        if not isinstance(supplied, dict):
            continue
        supplied = dict(supplied)
        if source_version == 9 and player in FOLLOWER_TOGGLE_KEYS:
            pad_index = 1 if player == "p2" else 2
            legacy_battle_menu = [key_binding(ord("F"))]
            legacy_native_pause = [key_binding(0x1B)]
            if supplied.get("battle_menu") == legacy_battle_menu:
                supplied["battle_menu"] = [
                    *legacy_battle_menu,
                    pad_binding(pad_index, "X"),
                ]
            if supplied.get("native_pause") == legacy_native_pause:
                supplied["native_pause"] = [
                    *legacy_native_pause,
                    pad_binding(pad_index, "START"),
                ]
        legacy_targets = supplied.get("target_toggle")
        if isinstance(legacy_targets, list):
            keyboard = [
                binding
                for binding in legacy_targets
                if isinstance(binding, dict) and binding.get("device") == "keyboard"
            ]
            previous = [
                binding
                for binding in legacy_targets
                if isinstance(binding, dict)
                and binding.get("device") == "xinput"
                and binding.get("control") == "LB"
            ]
            following = [
                binding
                for binding in legacy_targets
                if isinstance(binding, dict)
                and binding.get("device") == "xinput"
                and binding.get("control") != "LB"
            ]
            if len(keyboard) >= 2:
                previous.insert(0, keyboard[0])
                following[0:0] = keyboard[1:]
            else:
                following[0:0] = keyboard
            defaults = result["players"][player]
            if previous and not any(
                binding.get("device") == "xinput" for binding in previous
            ):
                previous.extend(
                    binding
                    for binding in defaults["target_previous"]
                    if binding.get("device") == "xinput"
                )
            if following and not any(
                binding.get("device") == "xinput" for binding in following
            ):
                following.extend(
                    binding
                    for binding in defaults["target_next"]
                    if binding.get("device") == "xinput"
                )
            if "target_previous" not in supplied and previous:
                supplied["target_previous"] = previous
            if "target_next" not in supplied and following:
                supplied["target_next"] = following
        for action, _label in actions:
            if action not in supplied or not isinstance(supplied[action], list):
                continue
            valid = []
            for binding in supplied[action]:
                if not isinstance(binding, dict):
                    continue
                if binding.get("device") == "keyboard":
                    try:
                        vk = int(binding["vk"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if 0 < vk < 256:
                        valid.append(key_binding(vk))
                elif binding.get("device") == "xinput":
                    try:
                        index = int(binding["index"])
                        control = str(binding["control"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if 0 <= index <= 3 and (
                        control in XINPUT_BUTTONS or control in XINPUT_AXIS_CONTROLS
                    ):
                        assigned = result["devices"].get(player)
                        valid.append(
                            pad_binding(
                                assigned if isinstance(assigned, int) else index,
                                control,
                            )
                        )
            result["players"][player][action] = valid[:MAX_BINDINGS_PER_ACTION]
    return result, report


def normalize_config(raw: Any) -> dict[str, Any]:
    return normalize_config_with_report(raw)[0]


def load_config_with_report(
    path: Path = CONFIG_PATH,
) -> tuple[dict[str, Any], ConfigNormalizationReport]:
    try:
        return normalize_config_with_report(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return normalize_config_with_report(None)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return load_config_with_report(path)[0]


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    source_version = _source_config_version(config)
    if source_version is not None and source_version > CONFIG_VERSION:
        raise UnsupportedControllerConfigVersion(source_version)
    normalized = normalize_config(config)
    atomic_files.write_text(
        path,
        json.dumps(normalized, indent=2, ensure_ascii=False) + "\n",
    )


class XINPUT_GAMEPAD(ct.Structure):
    _fields_ = [
        ("wButtons", wt.WORD),
        ("bLeftTrigger", ct.c_ubyte),
        ("bRightTrigger", ct.c_ubyte),
        ("sThumbLX", ct.c_short),
        ("sThumbLY", ct.c_short),
        ("sThumbRX", ct.c_short),
        ("sThumbRY", ct.c_short),
    ]


class XINPUT_STATE(ct.Structure):
    _fields_ = [("dwPacketNumber", wt.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


def _load_xinput():
    for name in ("XINPUT1_4.dll", "XINPUT1_3.dll", "XINPUT9_1_0.dll"):
        try:
            dll = ct.WinDLL(name)
            dll.XInputGetState.argtypes = [wt.DWORD, ct.POINTER(XINPUT_STATE)]
            dll.XInputGetState.restype = wt.DWORD
            return dll
        except OSError:
            continue
    return None


_user32 = ct.WinDLL("user32", use_last_error=True)
_user32.GetAsyncKeyState.argtypes = [ct.c_int]
_user32.GetAsyncKeyState.restype = ct.c_short
_xinput = _load_xinput()


def read_xinput_states() -> list[XINPUT_STATE | None]:
    result: list[XINPUT_STATE | None] = []
    for index in range(4):
        state = XINPUT_STATE()
        try:
            status = (
                _xinput.XInputGetState(index, ct.byref(state))
                if _xinput is not None
                else -1
            )
        except Exception:
            status = -1
        if status == 0:
            result.append(state)
        else:
            result.append(None)
    return result


def _signed_axis(value: int) -> float:
    return max(-1.0, min(1.0, value / (32767.0 if value >= 0 else 32768.0)))


def _radial_stick_axes(x: float, y: float) -> tuple[float, float]:
    """Apply the deadzone to the stick vector without crushing diagonals.

    Applying the deadzone to each axis independently turns a fully deflected
    circular-gate diagonal (roughly 0.707 on each axis) into two ~0.61 axes.
    FF12 then sees an ~0.87 vector and selects its walk plateau instead of run.
    A radial deadzone preserves the direction and maps the vector magnitude as
    a whole, so cardinal and diagonal movement reach the same full deflection.
    """

    x = 0.0 if abs(float(x)) <= AXIS_DEADZONE else float(x)
    y = 0.0 if abs(float(y)) <= AXIS_DEADZONE else float(y)
    magnitude = min(1.0, math.hypot(x, y))
    if magnitude <= AXIS_DEADZONE:
        return 0.0, 0.0
    normalized = min(
        1.0,
        (magnitude - AXIS_DEADZONE) / (1.0 - AXIS_DEADZONE),
    )
    scale = normalized / magnitude
    return x * scale, y * scale


def xinput_control_strength(state: XINPUT_STATE | None, control: str) -> float:
    if state is None:
        return 0.0
    gamepad = state.Gamepad
    if control in XINPUT_BUTTONS:
        return 1.0 if gamepad.wButtons & XINPUT_BUTTONS[control] else 0.0
    if control == "LT":
        return gamepad.bLeftTrigger / 255.0
    if control == "RT":
        return gamepad.bRightTrigger / 255.0
    axis_name, direction = control.split("_", 1)
    if axis_name == "LS":
        x = _signed_axis(gamepad.sThumbLX)
        y = _signed_axis(gamepad.sThumbLY)
    elif axis_name == "RS":
        x = _signed_axis(gamepad.sThumbRX)
        y = _signed_axis(gamepad.sThumbRY)
    else:
        return 0.0
    x, y = _radial_stick_axes(x, y)
    raw = {"UP": y, "DOWN": -y, "LEFT": -x, "RIGHT": x}[direction]
    return max(0.0, min(1.0, raw))


def _xinput_axis_from_strengths(
    strengths: dict[str, float],
    positive: str,
    negative: str,
) -> int:
    value = max(
        -1.0,
        min(
            1.0,
            float(strengths.get(positive, 0.0)) - float(strengths.get(negative, 0.0)),
        ),
    )
    scale = 32767.0 if value >= 0.0 else 32768.0
    return int(round(value * scale))


def pack_xinput_gamepad_from_strengths(strengths: dict[str, float]) -> bytes:
    """Translate every normalized SDL control into XINPUT_GAMEPAD layout."""

    buttons = 0
    for control, mask in XINPUT_BUTTONS.items():
        if float(strengths.get(control, 0.0)) > 0.5:
            buttons |= mask

    def trigger(control: str) -> int:
        value = max(0.0, min(1.0, float(strengths.get(control, 0.0))))
        return int(round(value * 255.0))

    return struct.pack(
        "<HBBhhhh",
        buttons,
        trigger("LT"),
        trigger("RT"),
        _xinput_axis_from_strengths(strengths, "LS_RIGHT", "LS_LEFT"),
        _xinput_axis_from_strengths(strengths, "LS_UP", "LS_DOWN"),
        _xinput_axis_from_strengths(strengths, "RS_RIGHT", "RS_LEFT"),
        _xinput_axis_from_strengths(strengths, "RS_UP", "RS_DOWN"),
    )


@dataclass(frozen=True)
class SDLControllerState:
    assignment: dict[str, Any]
    strengths: dict[str, float]
    xinput_gamepad: bytes = b""
    generation: int = 0


def _sdl_device_metadata(index: int) -> tuple[int | None, str]:
    """Read the SDL instance id and a privacy-safe persistent device id.

    pygame 2.5 does not expose SDL_JoystickPathForIndex. Calling that function
    from the SDL2 module already loaded by pygame avoids opening every device on
    every frame. Only a hash of the OS path is persisted in controller_config.
    """

    if os.name != "nt":
        return None, ""
    try:
        kernel32 = ct.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wt.HMODULE
        kernel32.GetProcAddress.argtypes = [wt.HMODULE, ct.c_char_p]
        kernel32.GetProcAddress.restype = ct.c_void_p
        module = kernel32.GetModuleHandleW("SDL2.dll")
        if not module:
            return None, ""
        instance_address = kernel32.GetProcAddress(
            module, b"SDL_JoystickGetDeviceInstanceID"
        )
        path_address = kernel32.GetProcAddress(module, b"SDL_JoystickPathForIndex")
        instance_id = None
        if instance_address:
            instance_reader = ct.WINFUNCTYPE(ct.c_int, ct.c_int)(instance_address)
            value = int(instance_reader(int(index)))
            if value >= 0:
                instance_id = value
        uid = ""
        if path_address:
            path_reader = ct.WINFUNCTYPE(ct.c_char_p, ct.c_int)(path_address)
            raw_path = path_reader(int(index))
            if raw_path:
                normalized_path = raw_path.decode("utf-8", errors="replace").casefold()
                uid = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:32]
        return instance_id, uid
    except (AttributeError, OSError, TypeError, ValueError):
        return None, ""


class _SDLControllerBackend:
    def __init__(self) -> None:
        self._controllers: dict[int, tuple[Any, dict[str, Any]]] = {}
        self._connection_generation_by_instance: dict[int, int] = {}
        self._next_connection_generation = 0
        self._ordinal_by_identity: dict[tuple[str, str], int] = {}
        self._unavailable = pygame is None or sdl_controller is None
        self._last_coherent_states: tuple[SDLControllerState, ...] = ()
        self._last_coherent_at = 0.0
        self._pump_failure_started_at: float | None = None

    def _close(self) -> None:
        for controller, _assignment in self._controllers.values():
            try:
                controller.quit()
            except Exception:
                pass
        self._controllers.clear()
        self._connection_generation_by_instance.clear()
        self._ordinal_by_identity.clear()
        self._last_coherent_states = ()
        self._last_coherent_at = 0.0
        self._pump_failure_started_at = None

    def _allocate_ordinal(self, guid: str, uid: str) -> int:
        identity = (guid, uid)
        if uid and identity in self._ordinal_by_identity:
            return self._ordinal_by_identity[identity]
        used = {
            assignment["ordinal"]
            for _controller, assignment in self._controllers.values()
            if assignment["guid"] == guid
        }
        reserved = {
            ordinal
            for (reserved_guid, _uid), ordinal in self._ordinal_by_identity.items()
            if reserved_guid == guid
        }
        ordinal = next(
            candidate
            for candidate in range(len(used | reserved) + 1)
            if candidate not in used and candidate not in reserved
        )
        if uid:
            self._ordinal_by_identity[identity] = ordinal
        return ordinal

    def _refresh(self) -> bool:
        if self._unavailable:
            return True
        try:
            if not sdl_controller.get_init():
                sdl_controller.init()
            count = int(sdl_controller.get_count())
            if count < 0:
                raise OSError("SDL controller enumeration failed")
            observed_instances: set[int] = set()
            enumeration_complete = True
            for index in range(count):
                controller = None
                try:
                    if not sdl_controller.is_controller(index):
                        continue
                    metadata_instance_id, uid = _sdl_device_metadata(index)
                    if (
                        metadata_instance_id is not None
                        and metadata_instance_id in self._controllers
                    ):
                        controller, assignment = self._controllers[
                            metadata_instance_id
                        ]
                        if uid and not assignment.get("uid"):
                            upgraded = sdl_assignment(
                                assignment["guid"],
                                assignment["ordinal"],
                                assignment["name"],
                                uid,
                            )
                            self._controllers[metadata_instance_id] = (
                                controller,
                                upgraded,
                            )
                            self._ordinal_by_identity[
                                (upgraded["guid"], upgraded["uid"])
                            ] = upgraded["ordinal"]
                        observed_instances.add(metadata_instance_id)
                        continue

                    controller = sdl_controller.Controller(index)
                    opened_instance = int(controller.id)
                    # The handle's instance id is authoritative.  Enumeration
                    # can reorder between the metadata query and Controller(),
                    # so never key the new handle (or persist a path-derived
                    # uid) under a stale pre-open instance id.
                    if (
                        metadata_instance_id is not None
                        and metadata_instance_id != opened_instance
                    ):
                        uid = ""
                    instance_id = opened_instance
                    if instance_id in self._controllers:
                        controller.quit()
                        observed_instances.add(instance_id)
                        continue
                    joystick = controller.as_joystick()
                    guid = str(joystick.get_guid()).strip().casefold()
                    ordinal = self._allocate_ordinal(guid, uid)
                    assignment = sdl_assignment(
                        guid,
                        ordinal,
                        str(controller.name),
                        uid,
                    )
                    self._controllers[instance_id] = (controller, assignment)
                    self._next_connection_generation = (
                        self._next_connection_generation + 1
                    ) & 0xFFFFFFFF
                    if self._next_connection_generation == 0:
                        self._next_connection_generation = 1
                    self._connection_generation_by_instance[instance_id] = (
                        self._next_connection_generation
                    )
                    observed_instances.add(instance_id)
                except Exception:
                    # Virtual devices can remain in SDL's enumeration for one
                    # notification after disappearing.  One stale entry must
                    # not prevent every healthy controller from being opened.
                    if controller is not None:
                        try:
                            controller.quit()
                        except Exception:
                            pass
                    enumeration_complete = False
                    continue

            for instance_id in tuple(self._controllers):
                controller, _assignment = self._controllers[instance_id]
                if instance_id in observed_instances:
                    try:
                        if controller.attached():
                            continue
                    except Exception:
                        # Enumeration still identified this exact instance.
                        # One uncertain attachment query is not proof that the
                        # controller disappeared; its frame read below remains
                        # the authoritative per-device liveness check.
                        continue
                elif not enumeration_complete:
                    try:
                        if controller.attached():
                            continue
                    except Exception:
                        continue
                else:
                    # A coherent enumeration omission is not sufficient to
                    # retire an exact handle that still reports itself live.
                    # This closes a one-frame SDL hotplug/classification race
                    # without retaining a handle after confirmed detach.
                    try:
                        if controller.attached():
                            continue
                    except Exception:
                        pass
                try:
                    controller.quit()
                except Exception:
                    pass
                self._controllers.pop(instance_id, None)
                self._connection_generation_by_instance.pop(instance_id, None)
            return True
        except Exception:
            # A transient Bluetooth/virtual-device notification must not tear
            # down controllers whose existing handles may still be healthy.
            # The caller may continue sampling them and applies a bounded grace
            # if the global SDL pump is uncertain too.
            return False

    def _global_failure_sample(self) -> tuple[SDLControllerState, ...]:
        now = time.monotonic()
        if self._pump_failure_started_at is None:
            self._pump_failure_started_at = now
        if (
            self._last_coherent_states
            and now - self._pump_failure_started_at
            <= SDL_PUMP_FAILURE_GRACE_SECONDS
        ):
            return self._last_coherent_states
        return ()

    @staticmethod
    def _axis(controller: Any, axis: int) -> float:
        raw = int(controller.get_axis(axis))
        return _signed_axis(raw)

    def _native_strengths(self, controller: Any) -> dict[str, float]:
        button_ids = {
            "DPAD_UP": pygame.CONTROLLER_BUTTON_DPAD_UP,
            "DPAD_DOWN": pygame.CONTROLLER_BUTTON_DPAD_DOWN,
            "DPAD_LEFT": pygame.CONTROLLER_BUTTON_DPAD_LEFT,
            "DPAD_RIGHT": pygame.CONTROLLER_BUTTON_DPAD_RIGHT,
            "START": pygame.CONTROLLER_BUTTON_START,
            "BACK": pygame.CONTROLLER_BUTTON_BACK,
            "LEFT_THUMB": pygame.CONTROLLER_BUTTON_LEFTSTICK,
            "RIGHT_THUMB": pygame.CONTROLLER_BUTTON_RIGHTSTICK,
            "LB": pygame.CONTROLLER_BUTTON_LEFTSHOULDER,
            "RB": pygame.CONTROLLER_BUTTON_RIGHTSHOULDER,
            "A": pygame.CONTROLLER_BUTTON_A,
            "B": pygame.CONTROLLER_BUTTON_B,
            "X": pygame.CONTROLLER_BUTTON_X,
            "Y": pygame.CONTROLLER_BUTTON_Y,
        }
        strengths = {
            control: 1.0 if controller.get_button(button) else 0.0
            for control, button in button_ids.items()
        }
        left_x = self._axis(controller, pygame.CONTROLLER_AXIS_LEFTX)
        left_y = self._axis(controller, pygame.CONTROLLER_AXIS_LEFTY)
        right_x = self._axis(controller, pygame.CONTROLLER_AXIS_RIGHTX)
        right_y = self._axis(controller, pygame.CONTROLLER_AXIS_RIGHTY)
        strengths.update(
            {
                "LS_UP": max(0.0, -left_y),
                "LS_DOWN": max(0.0, left_y),
                "LS_LEFT": max(0.0, -left_x),
                "LS_RIGHT": max(0.0, left_x),
                "RS_UP": max(0.0, -right_y),
                "RS_DOWN": max(0.0, right_y),
                "RS_LEFT": max(0.0, -right_x),
                "RS_RIGHT": max(0.0, right_x),
            }
        )
        for control, axis in (
            ("LT", pygame.CONTROLLER_AXIS_TRIGGERLEFT),
            ("RT", pygame.CONTROLLER_AXIS_TRIGGERRIGHT),
        ):
            raw = max(0, int(controller.get_axis(axis)))
            strengths[control] = min(1.0, raw / 32767.0)
        return strengths

    def _normalized_strengths(self, native: dict[str, float]) -> dict[str, float]:
        result = dict(native)
        for prefix in ("LS", "RS"):
            x = float(native[f"{prefix}_RIGHT"]) - float(native[f"{prefix}_LEFT"])
            y = float(native[f"{prefix}_UP"]) - float(native[f"{prefix}_DOWN"])
            x, y = _radial_stick_axes(x, y)
            result.update(
                {
                    f"{prefix}_UP": max(0.0, y),
                    f"{prefix}_DOWN": max(0.0, -y),
                    f"{prefix}_LEFT": max(0.0, -x),
                    f"{prefix}_RIGHT": max(0.0, x),
                }
            )
        return result

    def read(self) -> tuple[SDLControllerState, ...]:
        refresh_coherent = self._refresh()
        if not self._controllers:
            if not refresh_coherent:
                return self._global_failure_sample()
            # A coherent empty enumeration proves disconnection; never retain
            # movement from a pad SDL has actually removed.
            self._last_coherent_states = ()
            self._last_coherent_at = 0.0
            self._pump_failure_started_at = None
            return ()
        try:
            sdl_controller.update()
        except Exception:
            # One failed global pump is not proof that every healthy controller
            # simultaneously returned to neutral.  Grace starts when failure
            # is first observed (rather than at the previous frame timestamp),
            # then fails closed so a real disconnect cannot leave movement held.
            return self._global_failure_sample()
        self._pump_failure_started_at = None

        result = []
        for instance_id, (controller, assignment) in sorted(
            self._controllers.items()
        ):
            try:
                if not controller.attached():
                    raise RuntimeError("SDL controller detached")
                native = self._native_strengths(controller)
                result.append(
                    SDLControllerState(
                        dict(assignment),
                        self._normalized_strengths(native),
                        pack_xinput_gamepad_from_strengths(native),
                        self._connection_generation_by_instance.get(instance_id, 0),
                    )
                )
            except Exception:
                # One stale Bluetooth or virtual device must not zero every
                # healthy player's input for the frame.  Retire only the
                # controller whose own query failed; _refresh() can reopen it
                # after SDL reports a coherent generation again.
                try:
                    controller.quit()
                except Exception:
                    pass
                self._controllers.pop(instance_id, None)
                self._connection_generation_by_instance.pop(instance_id, None)
        states = tuple(result)
        self._last_coherent_states = states
        self._last_coherent_at = time.monotonic() if states else 0.0
        return states

    def shutdown(self) -> None:
        self._close()
        if self._unavailable:
            return
        try:
            if sdl_controller.get_init():
                sdl_controller.quit()
        except Exception:
            pass


_sdl_backend = _SDLControllerBackend()


def sdl_backend_available() -> bool:
    return pygame is not None and sdl_controller is not None


def read_sdl_controller_states() -> tuple[SDLControllerState, ...]:
    return _sdl_backend.read()


def release_sdl_controllers() -> None:
    _sdl_backend.shutdown()


def sdl_control_strength(state: SDLControllerState | None, control: str) -> float:
    if state is None:
        return 0.0
    return float(state.strengths.get(str(control).upper(), 0.0))


def resolve_sdl_state(
    states: tuple[SDLControllerState, ...],
    assignment: Any,
) -> SDLControllerState | None:
    normalized = normalize_device_assignment(assignment)
    if not isinstance(normalized, dict):
        return None
    if normalized.get("uid"):
        return next(
            (
                state
                for state in states
                if normalize_device_assignment(state.assignment).get("uid")
                == normalized["uid"]
            ),
            None,
        )
    legacy_key = _legacy_sdl_assignment_key(normalized)
    return next(
        (
            state
            for state in states
            if _legacy_sdl_assignment_key(state.assignment) == legacy_key
        ),
        None,
    )


def effective_gamepad_assignment(
    config: dict[str, Any],
    player: str,
    xinput_states: list[XINPUT_STATE | None],
    sdl_states: tuple[SDLControllerState, ...],
) -> int | dict[str, Any] | None:
    """Resolve the live backend for a role without changing its ownership."""

    configured = normalize_device_assignment(config.get("devices", {}).get(player))
    if isinstance(configured, int):
        if configured < len(xinput_states) and xinput_states[configured] is not None:
            return configured
        sdl_aliases = aliased_sdl_assignments(config, configured)
        for sdl in sdl_aliases:
            if resolve_sdl_state(sdl_states, sdl) is not None:
                return sdl
        return None
    if isinstance(configured, dict):
        if resolve_sdl_state(sdl_states, configured) is not None:
            return configured
        xinput = aliased_xinput_slot(config, configured)
        if (
            xinput is not None
            and xinput < len(xinput_states)
            and xinput_states[xinput] is not None
        ):
            return xinput
        for sibling in equivalent_sdl_assignments(config, configured)[1:]:
            if resolve_sdl_state(sdl_states, sibling) is not None:
                return sibling
        return configured
    return None


def gamepad_assignment_connected(
    config: dict[str, Any],
    player: str,
    xinput_states: list[XINPUT_STATE | None],
    sdl_states: tuple[SDLControllerState, ...],
) -> bool:
    effective = effective_gamepad_assignment(
        config,
        player,
        xinput_states,
        sdl_states,
    )
    if isinstance(effective, int):
        return effective < len(xinput_states) and xinput_states[effective] is not None
    if isinstance(effective, dict):
        return resolve_sdl_state(sdl_states, effective) is not None
    return False


def upgrade_sdl_identities(
    config: dict[str, Any],
    states: tuple[SDLControllerState, ...],
) -> bool:
    """Add persistent ids to unambiguous legacy SDL assignments in-place."""

    replacements: dict[tuple[Any, ...], dict[str, Any] | None] = {}
    for state in states:
        assignment = normalize_device_assignment(state.assignment)
        if not isinstance(assignment, dict) or not assignment.get("uid"):
            continue
        legacy_key = _legacy_sdl_assignment_key(assignment)
        assert legacy_key is not None
        if legacy_key not in replacements:
            replacements[legacy_key] = assignment
            continue
        previous = replacements[legacy_key]
        if previous is not None and not sdl_assignments_match(previous, assignment):
            # More than one stable physical identity matches the same legacy
            # GUID/ordinal.  Refuse to choose based on enumeration order; the
            # user must make an explicit assignment instead.
            replacements[legacy_key] = None

    changed = False
    devices = config.get("devices", {})
    if isinstance(devices, dict):
        for player in ("p1", "p2", "p3"):
            current = normalize_device_assignment(devices.get(player))
            if not isinstance(current, dict) or current.get("uid"):
                continue
            replacement = replacements.get(_legacy_sdl_assignment_key(current))
            if isinstance(replacement, dict):
                devices[player] = dict(replacement)
                changed = True

    aliases = normalize_device_aliases(config.get("device_aliases"))
    for alias in aliases:
        current = alias["sdl"]
        if current.get("uid"):
            continue
        replacement = replacements.get(_legacy_sdl_assignment_key(current))
        if isinstance(replacement, dict):
            alias["sdl"] = dict(replacement)
            changed = True
    if changed:
        config["device_aliases"] = normalize_device_aliases(aliases)
    return changed


class BindingPoller:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        initial = self._read_config_snapshot()
        self.config = normalize_config(None) if initial is None else initial
        # A failed first read must not bless the file's current stamp.  Keeping
        # a sentinel forces the ordinary live-reload path to retry, rather than
        # leaving the process on defaults until somebody edits the file again.
        self._mtime_ns: int | None = (
            -1 if initial is None else self._stat_mtime()
        )
        self._next_reload_check = 0.0
        self._enabled = False
        self._keyboard_enabled = False
        self._states: list[XINPUT_STATE | None] = [None] * 4
        self._last_coherent_xinput_states: list[XINPUT_STATE | None] = [None] * 4
        self._xinput_failure_started_at: list[float | None] = [None] * 4
        self._xinput_gap_exposed = [False] * 4
        self._recovered_xinput_slots: set[int] = set()
        self._sdl_states: tuple[SDLControllerState, ...] = ()
        self._controller_broker_role = controller_state_transport.configured_role()
        self._controller_channel = controller_state_transport.ControllerStateChannel(
            len(GAMEPAD_CONTROLS)
        )
        self._controller_channel_available = self._controller_channel.open()
        self._controller_broker_sample_ready = False
        self._controller_broker_writer_id: int | None = None
        self._gamepad_source_signatures: dict[str, tuple[Any, ...]] = {}
        self._gamepad_handoff_players: set[str] = set()
        self._strength_cache: dict[tuple[str, str, bool], float] = {}
        self._event_strength_cache: dict[tuple[str, str, bool], float] = {}
        self._previous: dict[tuple[str, str, str], bool] = {}
        self._next_repeat: dict[tuple[str, str], float] = {}
        self._resumed_keyboard_holds: set[tuple[str, int]] = set()
        self._active_device: dict[str, str] = {
            player: self._default_active_device(player) for player in PLAYER_IDS
        }
        self._source_was_down: dict[tuple[str, str], bool] = {}
        # A helper can be restarted while a key/button is physically held.
        # Treat the first coherent sample exactly like a live configuration
        # handoff: seed edge/repeat history before any consumer observes it.
        # Otherwise a watchdog restart can turn an inherited F6/F7 (or any
        # menu/action binding) into a brand-new command and silently revoke a
        # player's manual-control role.
        self._reload_history_pending = True
        self._frame_started = False

    def _stat_mtime(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _read_config_snapshot(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return normalize_config(raw)

    def desktop_owner(self) -> str | None:
        return desktop_owner(self.config)

    def owns_desktop(self, player: str) -> bool:
        return player_owns_desktop(self.config, player)

    def desktop_input_enabled(self, player: str) -> bool:
        return bool(self._enabled and self._keyboard_enabled and self.owns_desktop(player))

    def _default_active_device(self, player: str) -> str:
        if self.assigned_gamepad(player) is not None:
            return "xinput"
        return "keyboard" if self.owns_desktop(player) else "xinput"

    def _reconcile_active_devices(self) -> None:
        for player in PLAYER_IDS:
            active = self._active_device.get(player)
            if active not in ("keyboard", "xinput") or (
                active == "keyboard" and not self.owns_desktop(player)
            ):
                self._active_device[player] = self._default_active_device(player)

    def _reload_if_changed(self, now: float) -> None:
        if now < self._next_reload_check:
            return
        self._next_reload_check = now + 0.25
        current = self._stat_mtime()
        if current != self._mtime_ns:
            # Atomic replacement and antivirus/indexer locks can create a short
            # interval where stat succeeds but the following read does not.  A
            # generic ``load_config`` fallback would replace the live, known-good
            # ownership table with defaults and permanently revoke P2/P3 until
            # another mtime change.  Runtime reload is transactional: retain the
            # previous snapshot and leave the old stamp in place so the next
            # poll retries the same candidate.
            candidate = self._read_config_snapshot()
            if candidate is None:
                return
            self.config = candidate
            self._mtime_ns = current
            self._strength_cache.clear()
            self._reconcile_active_devices()
            self._reload_history_pending = True

    def begin_frame(
        self,
        enabled: bool = True,
        *,
        keyboard_enabled: bool | None = None,
    ) -> None:
        now = time.monotonic()
        previous_states = self._states
        previous_sdl_states = self._sdl_states
        self._reload_if_changed(now)
        enabled_was = self._enabled
        keyboard_was_enabled = self._keyboard_enabled
        self._enabled = bool(enabled)
        self._keyboard_enabled = bool(
            self._enabled if keyboard_enabled is None else keyboard_enabled
        )
        keyboard_resumed = bool(
            self._frame_started
            and self._keyboard_enabled
            and not keyboard_was_enabled
        )
        gamepad_resumed = bool(
            self._frame_started and self._enabled and not enabled_was
        )
        self._strength_cache.clear()
        self._event_strength_cache.clear()
        self._states = (
            self._read_xinput_states_resilient(now)
            if self._enabled
            else [None] * 4
        )
        has_sdl_assignment = any(
            self.sdl_assignment_for_player(player)
            for player in PLAYER_IDS
        )
        if (
            self._enabled
            and has_sdl_assignment
            and self._controller_broker_role == controller_state_transport.ROLE_READER
        ):
            # A reader is never allowed to open SDL devices itself.  If the
            # named mapping was temporarily unavailable during construction,
            # ControllerStateChannel.read() retries opening it every frame and
            # yields an idle sample until the sole writer is available.  A
            # direct fallback here would let movement and menu helpers poll the
            # same physical HID concurrently and reintroduce disappearing or
            # duplicated input.
            self._sdl_states = self._shared_sdl_states()
        else:
            self._sdl_states = (
                read_sdl_controller_states()
                if self._enabled and has_sdl_assignment
                else ()
            )
        if self._controller_broker_role == controller_state_transport.ROLE_WRITER:
            self._publish_shared_sdl_states()
        self._track_gamepad_source_handoffs()
        if gamepad_resumed:
            self._gamepad_handoff_players.update(PLAYER_IDS)
        if self._recovered_xinput_slots:
            self._gamepad_handoff_players.update(
                player
                for player in PLAYER_IDS
                if (
                    isinstance(
                        assignment := self.effective_gamepad_assignment(player),
                        int,
                    )
                    and assignment in self._recovered_xinput_slots
                )
            )
            self._recovered_xinput_slots.clear()
        self._update_active_devices()
        if self._reload_history_pending:
            self._seed_input_history(now)
            self._reload_history_pending = False
            self._gamepad_handoff_players.clear()
        else:
            if keyboard_resumed:
                self._seed_keyboard_history(
                    now,
                    previous_states,
                    previous_sdl_states,
                )
            elif self._keyboard_enabled:
                self._release_resumed_keyboard_holds()
            for player in self._gamepad_handoff_players:
                self._seed_player_input_history(now, player)
            self._gamepad_handoff_players.clear()
        self._frame_started = True

    def _read_xinput_states_resilient(
        self,
        now: float,
    ) -> list[XINPUT_STATE | None]:
        """Bridge brief XInput query gaps, then expose a bounded disconnect."""

        try:
            raw_states = list(read_xinput_states())
        except Exception:
            raw_states = []
        raw_states = (raw_states + [None] * 4)[:4]
        result: list[XINPUT_STATE | None] = []
        for index, current in enumerate(raw_states):
            previous = self._last_coherent_xinput_states[index]
            if current is not None:
                if self._xinput_gap_exposed[index]:
                    self._recovered_xinput_slots.add(index)
                self._last_coherent_xinput_states[index] = current
                self._xinput_failure_started_at[index] = None
                self._xinput_gap_exposed[index] = False
                result.append(current)
                continue
            if previous is None:
                result.append(None)
                continue
            failure_started = self._xinput_failure_started_at[index]
            if failure_started is None:
                failure_started = float(now)
                self._xinput_failure_started_at[index] = failure_started
            if float(now) - failure_started <= XINPUT_STATE_FAILURE_GRACE_SECONDS:
                result.append(previous)
                continue
            self._xinput_gap_exposed[index] = True
            result.append(None)
        return result

    def _publish_shared_sdl_states(self) -> None:
        rows = []
        connected = []
        generations = []
        for player in PLAYER_IDS:
            direct = self.assigned_sdl(player)
            candidates = (
                equivalent_sdl_assignments(self.config, direct)
                if direct is not None
                else aliased_sdl_assignments(
                    self.config,
                    self.assigned_xinput(player),
                )
            )
            state = next(
                (
                    resolved
                    for assignment in candidates
                    if (resolved := resolve_sdl_state(self._sdl_states, assignment))
                    is not None
                ),
                None,
            )
            connected.append(state is not None)
            generations.append(state.generation if state is not None else 0)
            rows.append(
                tuple(
                    sdl_control_strength(state, control) for control in GAMEPAD_CONTROLS
                )
            )
        self._controller_channel_available = self._controller_channel.publish(
            rows,
            connected=connected,
            generations=generations,
        )

    def _shared_sdl_states(self) -> tuple[SDLControllerState, ...]:
        rows = self._controller_channel.read()
        if rows is None:
            self._controller_channel_available = False
            # Treat the first later coherent broker sample as a startup sample.
            # The writer can disappear while a physical button remains held;
            # without reseeding, its recovery would manufacture a new menu or
            # toggle edge from that inherited hold.
            self._controller_broker_sample_ready = False
            self._controller_broker_writer_id = None
            return ()
        self._controller_channel_available = True
        writer_id = getattr(self._controller_channel, "sample_writer_id", None)
        if isinstance(writer_id, bool) or not isinstance(writer_id, int):
            writer_id = None
        connected_mask = getattr(
            self._controller_channel,
            "sample_connected_mask",
            None,
        )
        if isinstance(connected_mask, bool) or not isinstance(connected_mask, int):
            # Compatibility with an injected legacy/fake channel: rows from an
            # old API represented every configured role as connected.
            connected_mask = (1 << len(PLAYER_IDS)) - 1
        raw_generations = getattr(
            self._controller_channel,
            "sample_generations",
            (),
        )
        if not (
            isinstance(raw_generations, tuple)
            and len(raw_generations) == len(PLAYER_IDS)
            and all(
                not isinstance(value, bool) and isinstance(value, int)
                for value in raw_generations
            )
        ):
            raw_generations = (0,) * len(PLAYER_IDS)
        self._controller_broker_sample_ready = True
        self._controller_broker_writer_id = writer_id
        result = []
        for index, (player, row) in enumerate(zip(PLAYER_IDS, rows)):
            assignment = self.sdl_assignment_for_player(player)
            if assignment is not None and connected_mask & (1 << index):
                result.append(
                    SDLControllerState(
                        assignment,
                        dict(zip(GAMEPAD_CONTROLS, row)),
                        generation=raw_generations[index],
                    )
                )
        return tuple(result)

    def _effective_gamepad_source_signature(self, player: str) -> tuple[Any, ...]:
        assignment = self.effective_gamepad_assignment(player)
        if isinstance(assignment, int):
            return ("xinput", assignment)
        if not isinstance(assignment, dict):
            return ("none",)
        state = resolve_sdl_state(self._sdl_states, assignment)
        identity = device_assignment_key(assignment)
        if state is None:
            return ("sdl-disconnected", identity)
        writer_id = (
            self._controller_broker_writer_id
            if self._controller_broker_role
            == controller_state_transport.ROLE_READER
            else None
        )
        return ("sdl", identity, writer_id, state.generation)

    def _track_gamepad_source_handoffs(self) -> None:
        current = {
            player: self._effective_gamepad_source_signature(player)
            for player in PLAYER_IDS
        }
        if self._frame_started:
            self._gamepad_handoff_players.update(
                player
                for player in PLAYER_IDS
                if current[player] != self._gamepad_source_signatures.get(player)
                and self._source_handoff_requires_seed(
                    self._gamepad_source_signatures.get(player),
                    current[player],
                )
            )
        self._gamepad_source_signatures = current

    @staticmethod
    def _source_handoff_requires_seed(
        previous: tuple[Any, ...] | None,
        current: tuple[Any, ...],
    ) -> bool:
        """Absorb controls inherited across every effective source change.

        Button changes never alter this signature.  A different signature is
        therefore a backend/slot/connection-generation handoff, including the
        first XInput hotplug after startup, and must be seeded before consumers
        can mistake a held control for a new edge.
        """

        return previous is None or previous != current

    def bindings(self, player: str, action: str) -> list[dict[str, Any]]:
        return self.config.get("players", {}).get(player, {}).get(action, [])

    def assigned_xinput(self, player: str) -> int | None:
        value = self.config.get("devices", {}).get(player)
        return value if isinstance(value, int) and 0 <= value <= 3 else None

    def assigned_sdl(self, player: str) -> dict[str, Any] | None:
        value = normalize_device_assignment(self.config.get("devices", {}).get(player))
        return value if isinstance(value, dict) else None

    def assigned_gamepad(self, player: str) -> int | dict[str, Any] | None:
        return normalize_device_assignment(self.config.get("devices", {}).get(player))

    def sdl_assignment_for_player(self, player: str) -> dict[str, Any] | None:
        direct = self.assigned_sdl(player)
        if direct is not None:
            return direct
        return aliased_sdl_assignment(self.config, self.assigned_xinput(player))

    def effective_gamepad_assignment(
        self,
        player: str,
    ) -> int | dict[str, Any] | None:
        return effective_gamepad_assignment(
            self.config,
            player,
            self._states,
            self._sdl_states,
        )

    def sdl_xinput_gamepad(self, player: str) -> bytes | None:
        """Return the assigned SDL pad as a complete XINPUT_GAMEPAD payload.

        A configured but temporarily unavailable SDL controller intentionally
        yields an idle payload. This keeps the virtual P1 slot reserved and
        prevents a different physical XInput device from taking over Player 1.
        """

        assignment = self.effective_gamepad_assignment(player)
        if not isinstance(assignment, dict):
            return None
        state = (
            resolve_sdl_state(self._sdl_states, assignment) if self._enabled else None
        )
        if state is not None and len(state.xinput_gamepad) == 12:
            return state.xinput_gamepad
        strengths = state.strengths if state is not None else {}
        return pack_xinput_gamepad_from_strengths(strengths)

    def _gamepad_control_strength(self, player: str, control: str) -> float:
        return self._gamepad_control_strength_from(
            player,
            control,
            self._states,
            self._sdl_states,
        )

    def _gamepad_control_strength_from(
        self,
        player: str,
        control: str,
        states: list[XINPUT_STATE | None],
        sdl_states: tuple[SDLControllerState, ...],
    ) -> float:
        assignment = effective_gamepad_assignment(
            self.config,
            player,
            states,
            sdl_states,
        )
        if isinstance(assignment, int):
            return xinput_control_strength(states[assignment], control)
        if not isinstance(assignment, dict):
            return 0.0
        state = resolve_sdl_state(sdl_states, assignment)
        return sdl_control_strength(state, control)

    def _gamepad_action_strength_from(
        self,
        player: str,
        action: str,
        states: list[XINPUT_STATE | None],
        sdl_states: tuple[SDLControllerState, ...],
        *,
        swap_face_buttons: bool = False,
    ) -> float:
        value = 0.0
        for binding in self.bindings(player, action):
            if binding.get("device") != "xinput":
                continue
            control = str(binding["control"])
            if swap_face_buttons:
                control = {"A": "B", "B": "A"}.get(control, control)
            value = max(
                value,
                self._gamepad_control_strength_from(
                    player,
                    control,
                    states,
                    sdl_states,
                ),
            )
        return value

    def _event_strength(
        self,
        player: str,
        action: str,
        *,
        swap_face_buttons: bool = False,
        excluded_keyboard_vks: Iterable[int] = (),
    ) -> float:
        """Return action strength without keys inherited across a focus resume."""

        excluded_vks = frozenset(int(vk) for vk in excluded_keyboard_vks)
        key = (player, action, swap_face_buttons, excluded_vks)
        if key in self._event_strength_cache:
            return self._event_strength_cache[key]
        value = 0.0
        if self._enabled:
            for binding in self.bindings(player, action):
                if binding.get("device") == "keyboard":
                    if not self.desktop_input_enabled(player):
                        continue
                    vk = int(binding["vk"])
                    if vk in excluded_vks:
                        continue
                    if (player, vk) in self._resumed_keyboard_holds:
                        continue
                    if _user32.GetAsyncKeyState(vk) & 0x8000:
                        value = 1.0
                        break
                elif binding.get("device") == "xinput":
                    if self.effective_gamepad_assignment(player) is None:
                        continue
                    control = str(binding["control"])
                    if swap_face_buttons:
                        control = {"A": "B", "B": "A"}.get(control, control)
                    value = max(
                        value,
                        self._gamepad_control_strength(player, control),
                    )
        self._event_strength_cache[key] = value
        return value

    def xinput_control_down(
        self, player: str, control: str, *, threshold: float = 0.5
    ) -> bool:
        assignment = self.effective_gamepad_assignment(player)
        if not self._enabled or assignment is None:
            return False
        return self._gamepad_control_strength(player, control) > threshold

    def xinput_control_edge(self, player: str, control: str) -> bool:
        control = str(control).upper()
        key = (player, f"xinput:{control}", "edge")
        current = self.xinput_control_down(player, control)
        prior = self._previous.get(key, False)
        self._previous[key] = current
        return current and not prior

    def native_left_stick_axes(self, player: str) -> tuple[float, float]:
        if not self._enabled or self.effective_gamepad_assignment(player) is None:
            return 0.0, 0.0
        forward = self._gamepad_control_strength(
            player, "LS_UP"
        ) - self._gamepad_control_strength(player, "LS_DOWN")
        right = self._gamepad_control_strength(
            player, "LS_RIGHT"
        ) - self._gamepad_control_strength(player, "LS_LEFT")
        return forward, right

    def _source_down(self, player: str, device: str) -> bool:
        if not self._enabled:
            return False
        actions = self.config.get("players", {}).get(player, {})
        for bindings in actions.values():
            for binding in bindings:
                if binding.get("device") != device:
                    continue
                if device == "keyboard":
                    if not self.desktop_input_enabled(player):
                        continue
                    if _user32.GetAsyncKeyState(int(binding["vk"])) & 0x8000:
                        return True
                elif self._gamepad_control_strength(player, binding["control"]) > 0.5:
                    return True
        return False

    def _update_active_devices(self) -> None:
        for player in PLAYER_IDS:
            for device in ("keyboard", "xinput"):
                key = (player, device)
                current = self._source_down(player, device)
                if current and not self._source_was_down.get(key, False):
                    self._active_device[player] = device
                self._source_was_down[key] = current

    def _keyboard_action_down(self, player: str, action: str) -> bool:
        if not self.desktop_input_enabled(player):
            return False
        return any(
            binding.get("device") == "keyboard"
            and bool(_user32.GetAsyncKeyState(int(binding["vk"])) & 0x8000)
            for binding in self.bindings(player, action)
        )

    def _release_resumed_keyboard_holds(self) -> None:
        retained = {
            (player, vk)
            for player, vk in self._resumed_keyboard_holds
            if self.desktop_input_enabled(player)
            and bool(_user32.GetAsyncKeyState(vk) & 0x8000)
        }
        if retained != self._resumed_keyboard_holds:
            self._resumed_keyboard_holds = retained
            self._event_strength_cache.clear()

    def _seed_keyboard_history(
        self,
        now: float,
        previous_states: list[XINPUT_STATE | None],
        previous_sdl_states: tuple[SDLControllerState, ...],
    ) -> None:
        """Absorb desktop keys held while focus belonged to another process.

        Controller polling deliberately continues in the background, so this
        method suppresses only inherited keyboard holds. Logical history is
        restored to the previous gamepad sample so a fresh controller edge on
        the foreground frame remains observable.
        """

        self._resumed_keyboard_holds.clear()
        for player, actions in PLAYER_ACTIONS.items():
            for action, _label in actions:
                held_vks = {
                    int(binding["vk"])
                    for binding in self.bindings(player, action)
                    if binding.get("device") == "keyboard"
                    and self.desktop_input_enabled(player)
                    and bool(
                        _user32.GetAsyncKeyState(int(binding["vk"])) & 0x8000
                    )
                }
                if not held_vks:
                    continue
                self._resumed_keyboard_holds.update(
                    (player, vk) for vk in held_vks
                )
                previous_pad_down = bool(
                    self._gamepad_action_strength_from(
                        player,
                        action,
                        previous_states,
                        previous_sdl_states,
                    )
                    > 0.5
                )
                previous_native_pad_down = bool(
                    previous_pad_down
                    or self._gamepad_action_strength_from(
                        player,
                        action,
                        previous_states,
                        previous_sdl_states,
                        swap_face_buttons=True,
                    )
                    > 0.5
                )
                self._previous[(player, action, "edge")] = previous_pad_down
                self._previous[(player, action, "repeat")] = previous_pad_down
                self._previous[(player, action, "native_edge")] = (
                    previous_native_pad_down
                )
                timer_key = (player, action)
                if previous_pad_down:
                    self._next_repeat.setdefault(
                        timer_key,
                        now + INITIAL_REPEAT_DELAY,
                    )
                else:
                    self._next_repeat.pop(timer_key, None)
        self._event_strength_cache.clear()

    def _seed_player_input_history(self, now: float, player: str) -> None:
        """Absorb the first sample after one player's backend handoff."""

        actions = PLAYER_ACTIONS[player]
        for action, _label in actions:
            current = self.down(player, action)
            self._previous[(player, action, "edge")] = current
            self._previous[(player, action, "repeat")] = current
            timer_key = (player, action)
            if current:
                self._next_repeat[timer_key] = now + INITIAL_REPEAT_DELAY
            else:
                self._next_repeat.pop(timer_key, None)
        for control in GAMEPAD_CONTROLS:
            current = self.xinput_control_down(player, control)
            self._previous[(player, f"xinput:{control}", "edge")] = current
        for action, _label in actions:
            current = bool(
                self._strength(player, action)
                or self._strength(player, action, swap_face_buttons=True)
            )
            self._previous[(player, action, "native_edge")] = current

    def _seed_input_history(self, now: float) -> None:
        """Absorb controls held at startup or across a live config replacement."""

        self._resumed_keyboard_holds.clear()
        self._event_strength_cache.clear()
        self._next_repeat.clear()
        for player in PLAYER_IDS:
            self._seed_player_input_history(now, player)

    def active_device(self, player: str) -> str:
        active = self._active_device.get(player)
        if active == "keyboard" and not self.owns_desktop(player):
            active = None
        if active in ("keyboard", "xinput"):
            return active
        return self._default_active_device(player)

    @staticmethod
    def _short_prompt_label(binding: dict[str, Any] | None) -> str:
        if not binding:
            return "?"
        if binding.get("device") == "keyboard":
            vk = int(binding.get("vk", 0))
            if vk == 0x25:
                return "<"
            if vk == 0x27:
                return ">"
            label = VK_NAMES.get(vk, f"{vk:#x}")
            return {"Space": "SPC", "Backspace": "BSP", "Enter": "ENT"}.get(
                label, label
            )
        control = str(binding.get("control", "?"))
        return {
            "DPAD_LEFT": "<",
            "DPAD_RIGHT": ">",
            "DPAD_UP": "UP",
            "DPAD_DOWN": "DN",
        }.get(control, control)

    def _device_bindings(
        self, player: str, action: str, device: str
    ) -> list[dict[str, Any]]:
        if device == "keyboard" and not self.owns_desktop(player):
            return []
        return [
            binding
            for binding in self.bindings(player, action)
            if binding.get("device") == device
        ]

    def action_prompt_source(
        self,
        player: str,
        action: str,
        *,
        swap_face_buttons: bool = False,
    ) -> tuple[str, str]:
        device = self.active_device(player)
        bindings = self._device_bindings(player, action, device)
        if not bindings:
            device = "keyboard" if device == "xinput" else "xinput"
            bindings = self._device_bindings(player, action, device)
        label = self._short_prompt_label(bindings[0] if bindings else None)
        if device == "xinput" and swap_face_buttons:
            label = {"A": "B", "B": "A"}.get(label, label)
        return device, label

    def action_prompt_label(
        self,
        player: str,
        action: str,
        *,
        swap_face_buttons: bool = False,
    ) -> str:
        return self.action_prompt_source(
            player,
            action,
            swap_face_buttons=swap_face_buttons,
        )[1]

    def target_prompt_pair(self, player: str) -> tuple[str, str]:
        device = self.active_device(player)
        previous = self._device_bindings(player, "target_previous", device)
        following = self._device_bindings(player, "target_next", device)
        if previous and following:
            return (
                self._short_prompt_label(previous[0]),
                self._short_prompt_label(following[0]),
            )

        left = self._device_bindings(player, "menu_left", device)
        right = self._device_bindings(player, "menu_right", device)
        if device == "xinput":
            left.sort(key=lambda item: item.get("control") != "LB")
            right.sort(key=lambda item: item.get("control") != "RB")
        if left and right:
            return (
                self._short_prompt_label(left[0]),
                self._short_prompt_label(right[0]),
            )
        return ("Q", "E") if device == "keyboard" else ("LB", "RB")

    def _strength(
        self,
        player: str,
        action: str,
        *,
        swap_face_buttons: bool = False,
    ) -> float:
        key = (player, action, swap_face_buttons)
        if key in self._strength_cache:
            return self._strength_cache[key]
        value = 0.0
        if self._enabled:
            for binding in self.bindings(player, action):
                if binding.get("device") == "keyboard":
                    if not self.desktop_input_enabled(player):
                        continue
                    if _user32.GetAsyncKeyState(int(binding["vk"])) & 0x8000:
                        value = 1.0
                        break
                elif binding.get("device") == "xinput":
                    if self.effective_gamepad_assignment(player) is None:
                        continue
                    control = str(binding["control"])
                    if swap_face_buttons:
                        control = {"A": "B", "B": "A"}.get(control, control)
                    value = max(
                        value,
                        self._gamepad_control_strength(player, control),
                    )
        self._strength_cache[key] = value
        return value

    def strength(self, player: str, action: str) -> float:
        return self._strength(player, action)

    def native_action_down(
        self,
        player: str,
        action: str,
        circle_decides: bool,
    ) -> bool:
        return (
            self._strength(
                player,
                action,
                swap_face_buttons=circle_decides,
            )
            > 0.5
        )

    def native_action_edge(
        self,
        player: str,
        action: str,
        circle_decides: bool,
        *,
        excluded_keyboard_vks: Iterable[int] = (),
    ) -> bool:
        excluded_vks = frozenset(int(vk) for vk in excluded_keyboard_vks)
        key = (
            (player, action, "native_edge")
            if not excluded_vks
            else (player, action, "native_edge", excluded_vks)
        )
        current = (
            self._event_strength(
                player,
                action,
                swap_face_buttons=circle_decides,
                excluded_keyboard_vks=excluded_vks,
            )
            > 0.5
        )
        prior = self._previous.get(key, False)
        self._previous[key] = current
        return current and not prior

    def down(self, player: str, action: str) -> bool:
        return self.strength(player, action) > 0.5

    def edge(self, player: str, action: str) -> bool:
        key = (player, action, "edge")
        current = self._event_strength(player, action) > 0.5
        prior = self._previous.get(key, False)
        self._previous[key] = current
        return current and not prior

    def repeat(self, player: str, action: str) -> bool:
        now = time.monotonic()
        key = (player, action, "repeat")
        current = self._event_strength(player, action) > 0.5
        prior = self._previous.get(key, False)
        self._previous[key] = current
        timer_key = (player, action)
        if current and not prior:
            self._next_repeat[timer_key] = now + INITIAL_REPEAT_DELAY
            return True
        if current and now >= self._next_repeat.get(timer_key, math.inf):
            self._next_repeat[timer_key] = now + REPEAT_INTERVAL
            return True
        if not current:
            self._next_repeat.pop(timer_key, None)
        return False

    def signed_axis(
        self, player: str, positive_action: str, negative_action: str
    ) -> float:
        return self.strength(player, positive_action) - self.strength(
            player, negative_action
        )
