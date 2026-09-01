from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import atomic_files
import runtime_paths


CONFIG_VERSION = 1
CONFIG_PATH = runtime_paths.config_path(__file__, "session_config.json")

BATTLE_INFO_GLOBAL = 0x02EBF190
BATTLE_PARTY_LAYOUT_OFFSET = 0x5A48
BATTLE_PARTY_LAYOUT_STRIDE = 0x12
BATTLE_DEFAULT_LAYOUT = 0
BATTLE_CURRENT_LAYOUT = 1
BATTLE_UI_LAYOUT = 2
BATTLE_MENU_LAYOUT = 3
BATTLE_LEADER_OFFSET = 0x5AA4
BATTLE_MODE_FLAGS_OFFSET = 0x5B04
BATTLE_CHOCOBO_MODE_BIT = 0x02


USER_CONTROL_GLOBAL = 0x02099D70
USER_CONTROL_ENABLED_OFFSET = 0x14


NATIVE_COMMAND_ACTOR_GLOBAL = 0x02092730
NATIVE_COMMAND_FLAG_GLOBAL = 0x02092734


NATIVE_MENU_STATE = NATIVE_COMMAND_ACTOR_GLOBAL
LOCATION_SAVE_GLOBAL = 0x02092758
LOCATION_CURRENT_OFFSET = 0x1244
LOCATION_UI_OFFSET = 0x125C
LOCATION_FLAGS_OFFSET = 0x1262
LOCATION_TOWN_FLAG = 0x01
LOCATION_INFO_GLOBAL = 0x02ADD100


UI_MANAGER_GLOBAL = 0x0209BE80
NATIVE_MODAL_MANAGER_FLAGS_OFFSET = 0x10F68
NATIVE_MODAL_MANAGER_OBJECT_OFFSET = 0x10F80
NATIVE_MODAL_OBJECT_FLAGS_OFFSET = 0x1D7A

EMPTY_PARTY_ID = 0xFFFF
MAIN_PARTY_MEMBER_IDS = frozenset(range(6))
ESPER_PARTY_MEMBER_IDS = frozenset(range(27, 40))
PARTY_NAMES = {
    0: "Vaan",
    1: "Ashe",
    2: "Fran",
    3: "Balthier",
    4: "Basch",
    5: "Penelo",
    6: "Reks",
    7: "Ashe",
    8: "Basch",
    9: "Basch",
    10: "Lamont",
    11: "Vossler",
    12: "Vossler",
    13: "Larsa",
    14: "Reddas",
    26: "Chocobo",
    27: "Belias",
    28: "Mateus",
    29: "Adrammelech",
    30: "Hashmal",
    31: "Cuchulainn",
    32: "Famfrit",
    33: "Zalera",
    34: "Shemhazai",
    35: "Chaos",
    36: "Zeromus",
    37: "Exodus",
    38: "Ultima",
    39: "Zodiark",
}


def default_config() -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "players": {
            "p1": {"party_member_id": 0},
            "p2": {"party_member_id": 5},
        },
    }


def normalize_config(raw: Any) -> dict[str, Any]:
    result = default_config()
    if not isinstance(raw, dict):
        return result
    players = raw.get("players")
    if not isinstance(players, dict):
        return result
    configured_player_count = 3 if "p3" in players else 2
    try:
        configured_player_count = int(raw.get("player_count", configured_player_count))
    except (TypeError, ValueError):
        configured_player_count = 2
    configured_player_count = 3 if configured_player_count >= 3 else 2
    result["player_count"] = configured_player_count

    values: dict[str, int | None] = {}
    third = players.get("p3")
    if configured_player_count >= 3:
        third_id = None
        if isinstance(third, dict):
            try:
                third_id = int(third.get("party_member_id"))
            except (TypeError, ValueError):
                third_id = None
        result["players"]["p3"] = {
            "party_member_id": (
                third_id if third_id in MAIN_PARTY_MEMBER_IDS else None
            )
        }
    for player in ("p1", "p2"):
        source = players.get(player)
        if not isinstance(source, dict):
            continue
        try:
            raw_member_id = source.get("party_member_id")
            member_id = None if raw_member_id is None else int(raw_member_id)
        except (TypeError, ValueError):
            continue
        if player == "p1" and member_id not in MAIN_PARTY_MEMBER_IDS:
            continue
        if (
            player == "p2"
            and member_id is not None
            and member_id not in MAIN_PARTY_MEMBER_IDS
        ):
            continue
        if member_id is None or member_id in MAIN_PARTY_MEMBER_IDS:
            values[player] = member_id
    for player, member_id in values.items():
        result["players"][player]["party_member_id"] = member_id

    if (
        result["players"]["p1"]["party_member_id"]
        == result["players"]["p2"]["party_member_id"]
    ):
        result["players"]["p2"]["party_member_id"] = None
    third = result["players"].get("p3")
    if third is not None and third.get("party_member_id") in {
        result["players"]["p1"]["party_member_id"],
        result["players"]["p2"]["party_member_id"],
    }:
        third["party_member_id"] = None
    return result


def _load_config_file(path: Path) -> dict[str, Any]:
    return normalize_config(json.loads(path.read_text(encoding="utf-8")))


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        return _load_config_file(path)
    except (OSError, ValueError, TypeError):
        return default_config()


def save_identity(identity: "SessionIdentity", path: Path = CONFIG_PATH) -> None:
    config = {
        "version": CONFIG_VERSION,
        "player_count": identity.player_count,
        "players": {
            "p1": {"party_member_id": int(identity.p1_party_member_id)},
            "p2": {
                "party_member_id": (
                    None
                    if identity.p2_party_member_id is None
                    else int(identity.p2_party_member_id)
                )
            },
        },
    }
    if identity.player_count >= 3:
        config["players"]["p3"] = {
            "party_member_id": (
                None
                if identity.p3_party_member_id is None
                else int(identity.p3_party_member_id)
            )
        }
    normalized = normalize_config(config)
    payload = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    atomic_files.write_text(path, payload, encoding="utf-8")


@dataclass(frozen=True)
class SessionIdentity:
    p1_party_member_id: int = 0
    p2_party_member_id: int | None = 5
    p3_party_member_id: int | None = None
    seat_count: int = 2

    def __post_init__(self) -> None:
        normalized = (
            3
            if int(self.seat_count) >= 3 or self.p3_party_member_id is not None
            else 2
        )
        object.__setattr__(self, "seat_count", normalized)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "SessionIdentity":
        players = config["players"]
        third = players.get("p3")
        p3 = None
        if isinstance(third, dict) and third.get("party_member_id") is not None:
            p3 = int(third["party_member_id"])
        raw_p2 = players["p2"].get("party_member_id")
        player_count = (
            3
            if int(config.get("player_count", 3 if third is not None else 2)) >= 3
            else 2
        )
        return cls(
            int(players["p1"]["party_member_id"]),
            None if raw_p2 is None else int(raw_p2),
            p3,
            player_count,
        )

    @property
    def player_count(self) -> int:
        return self.seat_count

    def member_id(self, player: str) -> int | None:
        return {
            "p1": self.p1_party_member_id,
            "p2": self.p2_party_member_id,
            "p3": self.p3_party_member_id,
        }.get(player)

    def name(self, player: str) -> str:
        member_id = self.member_id(player)
        if member_id is None:
            return "None"
        return PARTY_NAMES.get(member_id, f"Party {member_id}")


def ko_promoted_follower_role(
    identity: SessionIdentity,
    native_leader_member_id: int | None,
    p1_alive: bool | None,
    *,
    p1_present: bool = True,
) -> str | None:
    if not p1_present or p1_alive is True:
        return None
    if (
        identity.p2_party_member_id is not None
        and native_leader_member_id == identity.p2_party_member_id
    ):
        return "p2"
    if (
        identity.p3_party_member_id is not None
        and native_leader_member_id == identity.p3_party_member_id
    ):
        return "p3"
    return None


def p1_controlled_party_member_id(
    identity: SessionIdentity,
    current_layout: Sequence[int],
    ui_layout: Sequence[int],
    native_leader_member_id: int | None,
) -> int:
    leader = native_leader_member_id
    current_members = {value for value in current_layout[:4] if value != EMPTY_PARTY_ID}
    ui_members = {value for value in ui_layout[:4] if value != EMPTY_PARTY_ID}
    if (
        leader in PARTY_NAMES
        and leader not in ESPER_PARTY_MEMBER_IDS
        and leader in current_members
        and leader in ui_members
    ):
        if leader == identity.p1_party_member_id:
            return int(leader)
        follower_owners = {
            identity.p2_party_member_id,
            identity.p3_party_member_id,
        }
        if leader not in follower_owners:
            return int(leader)
    return int(identity.p1_party_member_id)


def available_identity(
    configured: SessionIdentity,
    current_layout: Sequence[int],
    ui_layout: Sequence[int],
    leader: int | None,
) -> SessionIdentity | None:
    ui_members = {value for value in ui_layout[:4] if value in MAIN_PARTY_MEMBER_IDS}
    members = tuple(
        value
        for value in current_layout[:4]
        if value in MAIN_PARTY_MEMBER_IDS and value in ui_members
    )
    if not members:
        return None
    p1 = leader if leader in members else configured.p1_party_member_id
    if p1 not in members:
        p1 = members[0]

    configured_roles = [configured.p1_party_member_id, configured.p2_party_member_id]
    if configured.player_count >= 3:
        configured_roles.append(configured.p3_party_member_id)
    resolved: list[int | None] = [p1] + [None] * (len(configured_roles) - 1)
    occupied = {p1}

    # A leader change that takes an already-owned character is a two-seat swap.
    # Preserve every unrelated player role instead of compacting followers in
    # native party order, which can silently rotate P2 and P3.
    displaced_role = next(
        (
            role_index
            for role_index in range(1, len(configured_roles))
            if configured_roles[role_index] == p1
        ),
        None,
    )
    for role_index in range(1, len(configured_roles)):
        member_id = configured_roles[role_index]
        if member_id in members and member_id not in occupied:
            resolved[role_index] = member_id
            occupied.add(member_id)

    if displaced_role is not None:
        former_p1 = configured.p1_party_member_id
        if former_p1 in members and former_p1 not in occupied:
            resolved[displaced_role] = former_p1
            occupied.add(former_p1)

    remaining = [member_id for member_id in members if member_id not in occupied]
    for role_index in range(1, len(resolved)):
        if resolved[role_index] is None and remaining:
            member_id = remaining.pop(0)
            resolved[role_index] = member_id
            occupied.add(member_id)

    p2 = resolved[1]
    p3 = resolved[2] if len(resolved) >= 3 else None
    return SessionIdentity(
        int(p1),
        None if p2 is None else int(p2),
        None if p3 is None else int(p3),
        configured.player_count,
    )


def active_main_party_members(layout: Sequence[int]) -> frozenset[int]:
    return frozenset(value for value in layout[:4] if value in MAIN_PARTY_MEMBER_IDS)


def active_esper_party_member_id(
    current_layout: Sequence[int],
    ui_layout: Sequence[int],
) -> int | None:
    """Return the coherent temporary Esper materialized by both live layouts."""

    current_espers = {
        value for value in current_layout[:4] if value in ESPER_PARTY_MEMBER_IDS
    }
    ui_espers = {value for value in ui_layout[:4] if value in ESPER_PARTY_MEMBER_IDS}
    if len(current_espers) != 1 or current_espers != ui_espers:
        return None
    return int(next(iter(current_espers)))


def active_summon_owner_party_member_id(
    identity: SessionIdentity,
    current_layout: Sequence[int],
    ui_layout: Sequence[int],
    native_leader_member_id: int | None,
) -> int | None:
    """Resolve the main-party owner of a coherent temporary Esper formation."""

    if active_esper_party_member_id(current_layout, ui_layout) is None:
        return None
    current_main = {
        value for value in current_layout[:4] if value in MAIN_PARTY_MEMBER_IDS
    }
    ui_main = {value for value in ui_layout[:4] if value in MAIN_PARTY_MEMBER_IDS}
    common_main = current_main & ui_main
    assigned = {
        identity.p1_party_member_id,
        identity.p2_party_member_id,
        identity.p3_party_member_id,
    }
    assigned.discard(None)
    candidates = common_main & assigned
    if native_leader_member_id in candidates:
        return int(native_leader_member_id)
    if len(candidates) == 1:
        return int(next(iter(candidates)))
    return None


def active_summon_owner_role(
    identity: SessionIdentity,
    current_layout: Sequence[int],
    ui_layout: Sequence[int],
    native_leader_member_id: int | None,
) -> str | None:
    owner = active_summon_owner_party_member_id(
        identity,
        current_layout,
        ui_layout,
        native_leader_member_id,
    )
    if owner == identity.p1_party_member_id:
        return "p1"
    if identity.p2_party_member_id is not None and owner == identity.p2_party_member_id:
        return "p2"
    if identity.p3_party_member_id is not None and owner == identity.p3_party_member_id:
        return "p3"
    return None


@dataclass(frozen=True)
class SessionSnapshot:
    identity: SessionIdentity
    battle_info: int
    current_layout: tuple[int, ...]
    ui_layout: tuple[int, ...]
    leader_party_member_id: int | None
    current_location: int | None
    ui_location: int | None
    user_control_enabled: bool
    native_menu_open: bool
    native_modal_active: bool
    town: bool
    loading: bool
    cutscene: bool
    p1_gameplay_ready: bool
    gameplay_ready: bool
    p1_is_leader: bool
    p2_is_leader: bool
    p1_menu_allowed: bool
    p2_menu_allowed: bool
    movement_allowed: bool
    camera_allowed: bool
    reason: str
    p1_owner_override: int | None = None
    chocobo_mode_active: bool = False

    @property
    def p1_owner_party_member_id(self) -> int:
        if self.p1_owner_override is not None:
            return int(self.p1_owner_override)
        return p1_controlled_party_member_id(
            self.identity,
            self.current_layout,
            self.ui_layout,
            self.leader_party_member_id,
        )

    @property
    def p1_reserved_party_member_id(self) -> int:
        return int(self.identity.p1_party_member_id)

    @property
    def bridge_ownership_allowed(self) -> bool:
        return (
            self.p1_gameplay_ready
            and not self.native_menu_open
            and not self.chocobo_mode_active
        )

    @property
    def coherent_world_signature(self) -> tuple[int, int] | None:
        """Stable input for runtime-owned world epochs.

        The epoch itself is deliberately not persisted in session identity.  A
        worker increments it after every invalid/loading boundary or coherent
        location change, preventing a reused actor pointer from validating an
        old handoff request.
        """

        if (
            not self.battle_info
            or self.current_location is None
            or self.ui_location is None
            or self.current_location == 0xFFFFFFFF
            or self.current_location != self.ui_location
        ):
            return None
        return int(self.battle_info), int(self.current_location)


def with_solo_leader_ownership(snapshot: SessionSnapshot) -> SessionSnapshot:
    """Give P1 menu ownership to the native leader without changing the session."""
    leader = snapshot.leader_party_member_id
    if leader not in PARTY_NAMES or leader in ESPER_PARTY_MEMBER_IDS:
        return snapshot
    if (
        leader not in snapshot.current_layout[:4]
        or leader not in snapshot.ui_layout[:4]
    ):
        return snapshot

    leader_ready = bool(
        snapshot.battle_info
        and snapshot.current_location is not None
        and snapshot.ui_location is not None
        and snapshot.current_location != 0xFFFFFFFF
        and snapshot.current_location == snapshot.ui_location
    )
    p1_menu_allowed = bool(
        leader_ready
        and snapshot.user_control_enabled
        and not snapshot.native_menu_open
        and not snapshot.native_modal_active
        and not snapshot.town
        and not snapshot.chocobo_mode_active
    )
    reason = snapshot.reason
    if leader_ready:
        if snapshot.native_menu_open:
            reason = "native fullscreen menu"
        elif snapshot.native_modal_active:
            reason = "native modal interaction"
        elif snapshot.town:
            reason = "town"
        elif not snapshot.user_control_enabled:
            reason = "cutscene/dialogue control lock"
        else:
            reason = "field"
    return replace(
        snapshot,
        loading=not leader_ready,
        cutscene=bool(
            leader_ready
            and not snapshot.user_control_enabled
            and not snapshot.native_menu_open
            and not snapshot.native_modal_active
        ),
        gameplay_ready=False,
        p1_gameplay_ready=leader_ready,
        p1_is_leader=True,
        p2_is_leader=False,
        p1_menu_allowed=p1_menu_allowed,
        p2_menu_allowed=False,
        movement_allowed=False,
        camera_allowed=False,
        reason=reason,
        p1_owner_override=int(leader),
    )


def replacement_identity_for_live_party(
    snapshot: SessionSnapshot,
) -> SessionIdentity | None:
    if active_esper_party_member_id(
        snapshot.current_layout,
        snapshot.ui_layout,
    ) is not None:
        # Summoning temporarily dematerializes the other player actors. It is
        # never a persistent party edit and must not rotate configured seats.
        return None
    current_members = tuple(
        value for value in snapshot.current_layout[:4] if value != EMPTY_PARTY_ID
    )
    ui_members = tuple(
        value for value in snapshot.ui_layout[:4] if value != EMPTY_PARTY_ID
    )
    if (
        snapshot.battle_info == 0
        # Story scripts can publish a temporary roster for several seconds. It
        # becomes persistent identity only after the Current and UI layouts converge.
        or not snapshot.user_control_enabled
        or snapshot.native_menu_open
        or snapshot.native_modal_active
        or snapshot.current_location is None
        or snapshot.current_location == 0xFFFFFFFF
        or snapshot.current_location != snapshot.ui_location
        or len(set(current_members)) < 1
        or len(current_members) != len(set(current_members))
        or len(ui_members) != len(set(ui_members))
        or set(current_members) != set(ui_members)
        or (
            snapshot.leader_party_member_id is not None
            and snapshot.leader_party_member_id not in MAIN_PARTY_MEMBER_IDS
        )
    ):
        return None
    replacement = available_identity(
        snapshot.identity,
        current_members,
        ui_members,
        snapshot.leader_party_member_id,
    )
    if replacement == snapshot.identity:
        return None
    return replacement


def _layout(process: Any, battle_info: int, layout_id: int) -> tuple[int, ...]:
    return struct.unpack(
        "<9H",
        process.read(
            battle_info
            + BATTLE_PARTY_LAYOUT_OFFSET
            + layout_id * BATTLE_PARTY_LAYOUT_STRIDE,
            BATTLE_PARTY_LAYOUT_STRIDE,
        ),
    )


def _metadata_marks_town(process: Any, location_id: int) -> bool:
    table = process.u64(LOCATION_INFO_GLOBAL)
    if not table:
        return False
    count = process.u32(table + 4)
    if location_id < 0 or location_id >= count:
        return False

    return bool(process.read(table + 0x0A + location_id * 3, 1)[0] & 1)


def _native_modal_active(process: Any) -> bool:
    try:
        manager = process.u64(UI_MANAGER_GLOBAL)
        if not manager:
            return False
        manager_flags = process.u32(manager + NATIVE_MODAL_MANAGER_FLAGS_OFFSET)
        ui_object = process.u64(manager + NATIVE_MODAL_MANAGER_OBJECT_OFFSET)
        if not ui_object:
            return False
        object_flags = int.from_bytes(
            process.read(ui_object + NATIVE_MODAL_OBJECT_FLAGS_OFFSET, 2),
            "little",
        )
        return bool((manager_flags & 0x40) and (object_flags & 0x08))
    except (OSError, RuntimeError, struct.error, ValueError):
        return False


def _native_command_menu_open(process: Any) -> bool:
    return bool(
        process.u32(NATIVE_COMMAND_ACTOR_GLOBAL)
        and process.u32(NATIVE_COMMAND_FLAG_GLOBAL) == 1
    )


def _chocobo_mode_active(process: Any, battle_info: int) -> bool:
    """Read the native getchocobomode bit used while the party is mounted."""

    if not battle_info:
        return False
    try:
        flags = process.u32(battle_info + BATTLE_MODE_FLAGS_OFFSET)
    except (AttributeError, OSError, RuntimeError, struct.error, ValueError):
        return False
    return bool(flags & BATTLE_CHOCOBO_MODE_BIT)


def read_snapshot(
    process: Any,
    identity: SessionIdentity,
    *,
    materialized_party_count: int | None = None,
) -> SessionSnapshot:
    battle_info = 0
    current_layout = (EMPTY_PARTY_ID,) * 9
    ui_layout = current_layout
    leader: int | None = None
    current_location: int | None = None
    ui_location: int | None = None
    control_enabled = False
    native_menu_open = False
    native_modal_active = False
    chocobo_mode_active = False
    town = False
    structural_ready = False
    summon_structural_ready = False
    summon_owner_role: str | None = None
    reason = "loading: unavailable live structures"

    try:
        native_menu_open = _native_command_menu_open(process)
        native_modal_active = _native_modal_active(process)
        battle_info = process.u64(BATTLE_INFO_GLOBAL)
        location_base = process.u64(LOCATION_SAVE_GLOBAL)
        control = process.u64(USER_CONTROL_GLOBAL)
        if not battle_info or not location_base or not control:
            raise RuntimeError("missing gameplay root")

        chocobo_mode_active = _chocobo_mode_active(process, battle_info)

        current_layout = _layout(process, battle_info, BATTLE_CURRENT_LAYOUT)
        ui_layout = _layout(process, battle_info, BATTLE_UI_LAYOUT)
        leader_value = process.u32(battle_info + BATTLE_LEADER_OFFSET)
        leader = None if leader_value == EMPTY_PARTY_ID else leader_value
        current_location = process.u32(location_base + LOCATION_CURRENT_OFFSET)
        ui_location = process.u32(location_base + LOCATION_UI_OFFSET)
        control_enabled = bool(
            process.read(control + USER_CONTROL_ENABLED_OFFSET, 1)[0]
        )
        runtime_town = bool(
            process.read(location_base + LOCATION_FLAGS_OFFSET, 1)[0]
            & LOCATION_TOWN_FLAG
        )
        town = runtime_town or _metadata_marks_town(process, current_location)

        current_members = {
            value for value in current_layout[:4] if value != EMPTY_PARTY_ID
        }
        ui_members = {value for value in ui_layout[:4] if value != EMPTY_PARTY_ID}
        p1_owner_member_id = p1_controlled_party_member_id(
            identity,
            current_layout,
            ui_layout,
            leader,
        )
        p1_role_present = (
            p1_owner_member_id in current_members and p1_owner_member_id in ui_members
        )
        follower_members = tuple(
            member_id
            for member_id in (
                identity.p2_party_member_id,
                identity.p3_party_member_id,
            )
            if member_id is not None
        )
        roles_present = bool(
            follower_members
            and p1_role_present
            and all(
                member_id in current_members and member_id in ui_members
                for member_id in follower_members
            )
        )
        p1_actor_present = (
            True if materialized_party_count is None else materialized_party_count >= 1
        )
        actors_present = bool(
            materialized_party_count is None
            or materialized_party_count >= 1 + len(follower_members)
        )
        location_coherent = (
            current_location != 0xFFFFFFFF
            and ui_location != 0xFFFFFFFF
            and current_location == ui_location
        )
        summon_owner_role = active_summon_owner_role(
            identity,
            current_layout,
            ui_layout,
            leader,
        )
        summon_owner_member_id = active_summon_owner_party_member_id(
            identity,
            current_layout,
            ui_layout,
            leader,
        )
        summon_actor_present = bool(
            materialized_party_count is None or materialized_party_count >= 1
        )
        summon_structural_ready = bool(
            summon_owner_role is not None
            and summon_owner_member_id in current_members
            and summon_owner_member_id in ui_members
            and summon_actor_present
            and location_coherent
        )
        p1_structural_ready = p1_role_present and p1_actor_present and location_coherent
        structural_ready = bool(
            (p1_structural_ready and roles_present and actors_present)
            or summon_structural_ready
        )
        if summon_structural_ready:
            reason = f"field: active summon owned by {summon_owner_role.upper()}"
        elif not p1_role_present:
            reason = "loading: controlled P1 is not in Current/UI party"
        elif not p1_actor_present:
            reason = "loading: P1 actor is not materialized"
        elif not location_coherent:
            reason = "loading: location generations disagree"
        elif not roles_present or not actors_present:
            reason = "field: no active follower"
        else:
            reason = "field"
    except (OSError, RuntimeError, struct.error, ValueError):
        p1_structural_ready = False
        structural_ready = False

    loading = not (p1_structural_ready or summon_structural_ready)
    cutscene = bool(
        p1_structural_ready
        and not control_enabled
        and not native_menu_open
        and not native_modal_active
    )
    p1_gameplay_ready = bool(
        (p1_structural_ready or summon_owner_role == "p1") and not loading
    )
    gameplay_ready = bool(structural_ready and not loading)
    p1_is_leader = leader == p1_controlled_party_member_id(
        identity, current_layout, ui_layout, leader
    )
    p2_is_leader = bool(
        identity.p2_party_member_id is not None
        and leader == identity.p2_party_member_id
    )

    if not loading:
        if native_menu_open:
            reason = "native fullscreen menu"
        elif native_modal_active:
            reason = "native world interaction/modal"
        elif town:
            reason = "town"
        elif chocobo_mode_active:
            reason = "chocobo"
        elif cutscene:
            reason = "cutscene/dialogue control lock"
        elif summon_owner_role is not None:
            reason = f"field: active summon owned by {summon_owner_role.upper()}"
        elif not p1_is_leader:
            reason = "P1 is not the party leader"

    p1_menu_allowed = bool(
        p1_gameplay_ready
        and control_enabled
        and not native_menu_open
        and not native_modal_active
        and not town
        and p1_is_leader
        and summon_owner_role in (None, "p1")
        and not chocobo_mode_active
    )
    p2_menu_allowed = bool(
        gameplay_ready
        and identity.p2_party_member_id is not None
        and control_enabled
        and not native_menu_open
        and not native_modal_active
        and not town
        and summon_owner_role in (None, "p2")
        and not chocobo_mode_active
    )
    return SessionSnapshot(
        identity=identity,
        battle_info=battle_info,
        current_layout=current_layout,
        ui_layout=ui_layout,
        leader_party_member_id=leader,
        current_location=current_location,
        ui_location=ui_location,
        user_control_enabled=control_enabled,
        native_menu_open=native_menu_open,
        native_modal_active=native_modal_active,
        town=town,
        loading=loading,
        cutscene=cutscene,
        p1_gameplay_ready=p1_gameplay_ready,
        gameplay_ready=gameplay_ready,
        p1_is_leader=p1_is_leader,
        p2_is_leader=p2_is_leader,
        p1_menu_allowed=p1_menu_allowed,
        p2_menu_allowed=p2_menu_allowed,
        movement_allowed=bool(
            gameplay_ready
            and control_enabled
            and not native_menu_open
            and not native_modal_active
        ),
        camera_allowed=bool(gameplay_ready and not cutscene),
        reason=reason,
        chocobo_mode_active=chocobo_mode_active,
    )


def map_party_slot_actors(
    snapshot: SessionSnapshot,
    party_actors_by_slot: Mapping[int, Any],
) -> dict[int, Any]:
    result = {}
    for party_slot, actor in party_actors_by_slot.items():
        if not 0 <= party_slot < 4:
            continue
        member_id = snapshot.current_layout[party_slot]
        if member_id != EMPTY_PARTY_ID:
            result[member_id] = actor
    return result


def party_slot_for_member(
    snapshot: SessionSnapshot,
    party_member_id: int,
) -> int | None:
    wanted = int(party_member_id)
    for slot, member_id in enumerate(snapshot.current_layout[:4]):
        if member_id == wanted:
            return slot
    return None


def mask_owned_command_shadow(
    snapshot: SessionSnapshot, *, owned: bool
) -> SessionSnapshot:
    if not owned or not snapshot.native_menu_open:
        return snapshot
    common_menu_allowed = bool(
        snapshot.p1_gameplay_ready
        and snapshot.user_control_enabled
        and not snapshot.town
        and not snapshot.chocobo_mode_active
    )
    p2_menu_allowed = bool(
        snapshot.gameplay_ready
        and snapshot.user_control_enabled
        and not snapshot.town
        and not snapshot.chocobo_mode_active
    )
    return replace(
        snapshot,
        native_menu_open=False,
        p1_menu_allowed=common_menu_allowed and snapshot.p1_is_leader,
        p2_menu_allowed=p2_menu_allowed,
        movement_allowed=False,
        reason="custom Wait command menu",
    )


class SessionTracker:
    def __init__(
        self,
        path: Path = CONFIG_PATH,
        poll_seconds: float = 0.25,
        initial_identity: SessionIdentity | None = None,
    ) -> None:
        self.path = path
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._stamp: tuple[int, int] | None = None
        self._next_poll = 0.0
        self.identity = initial_identity or SessionIdentity()
        self.reload(force=True)

    def reload(self, *, force: bool = False) -> SessionIdentity:
        now = time.monotonic()
        if not force and now < self._next_poll:
            return self.identity
        self._next_poll = now + self.poll_seconds
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if force or stamp != self._stamp:
                candidate = SessionIdentity.from_config(
                    _load_config_file(self.path)
                )
                self.identity = candidate
                self._stamp = stamp
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            # Keep the last coherent role assignment while a VFS-backed file is
            # being replaced.  The next poll retries the unread signature.
            pass
        return self.identity

    def snapshot(
        self,
        process: Any,
        *,
        materialized_party_count: int | None = None,
    ) -> SessionSnapshot:
        self.reload()
        return read_snapshot(
            process,
            self.identity,
            materialized_party_count=materialized_party_count,
        )
