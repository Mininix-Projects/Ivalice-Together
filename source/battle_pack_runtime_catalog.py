from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import struct
from typing import TYPE_CHECKING, Mapping

from headless_menu_model import (
    EntryKind,
    MenuEntry,
    PageDefinition,
    REVIVAL_ACTION_IDS,
    TargetGroup,
)

if TYPE_CHECKING:
    from native_action_catalog import NativeActionCatalog


HERE = Path(__file__).resolve().parent
DEFAULT_BATTLE_PACK = (
    HERE
    / "extracted_ui_assets/ps2data/image/ff12/test_battle/us/binaryfile"
    / "battle_pack.bin"
)


MAIN_SAVE_OWNER_GLOBAL = 0x2092758
BATTLE_SAVE_BASE_GLOBAL = 0x2EBF190
GLOBAL_BATTLE_INFO_OFFSET = 0x2200
PARTY_MEMBER_BASE_OFFSET = 0x08
PARTY_MEMBER_STRIDE = 0x1C8
PENELO_PARTY_MEMBER_ID = 5
PARTY_LICENSES_OFFSET = 0x194
PARTY_LICENSES_SIZE = 46
MAGICK_LICENSE_FLAGS_OFFSET = 0x1AA
MAGICK_LICENSE_FLAGS_SIZE = 0x17
MENU_FLAGS_OFFSET = 0x04

COMMAND_RESTRICTIONS_GLOBAL = 0x22C314C
ATTACK_COMMAND_RESTRICTION = 0x04
MAGICK_COMMAND_RESTRICTION = 0x08
TECHNICK_COMMAND_RESTRICTION = 0x10
ITEM_COMMAND_RESTRICTION = 0x20

PERMANENT_STATUS_EFFECTS_OFFSET = 0x3C
TEMPORARY_STATUS_EFFECTS_OFFSET = 0x64
BERSERK_STATUS_MASK = 1 << 27
HARD_COMMAND_BLOCKING_STATUS_MASK = sum(1 << bit for bit in (0, 1, 3, 4, 5, 13, 31))
SILENCE_STATUS_MASK = 1 << 9

ITEM_COUNTERS_OFFSET = 0x4748
ITEM_COUNTER_COUNT = 64

MAGICK_CONTENT_BASE = 0x3000
TECHNICK_CONTENT_BASE = 0x4000
FIXED_ACTION_PARTY_MEMBER_IDS = frozenset(range(6, 15))

NATIVE_RESOURCE_BASE_GLOBAL = 0x01F83530
NATIVE_ITEM_ORDER_POINTER_GLOBAL = 0x02092764
NATIVE_SORT_OWNER_GLOBAL = 0x020927C0
NATIVE_ITEM_ORDER_COUNT_OFFSET = 0x47B8
MAGICK_OWNERSHIP_OFFSET = 0x4F10
MAGICK_OWNERSHIP_SIZE = 12
TECHNICK_OWNERSHIP_OFFSET = 0x4F1C
TECHNICK_OWNERSHIP_SIZE = 4
MIST_OWNERSHIP_OFFSET = 0x4F80
MIST_OWNERSHIP_SIZE = 4

MAGICK_CATEGORY_NAMES = (
    "White Magicks",
    "Black Magicks",
    "Time Magicks",
    "Green Magicks",
    "Arcane Magicks",
)
MAGICK_PAGE_KEYS = tuple(
    "magick:" + name.casefold().replace(" ", "_") for name in MAGICK_CATEGORY_NAMES
)


MAGICK_LICENSE_NODE_BY_FLAG: Mapping[int, int] = {
    **dict(
        zip(
            (6, 7, 8, 9, 10, 11, 12, 13, 179, 32, 33, 34, 35),
            (182, 183, 184, 185, 186, 187, 188, 189, 355, 211, 209, 210, 208),
        )
    ),
    **dict(
        zip(
            (14, 15, 16, 17, 18, 19, 20, 21, 180, 182, 39, 40, 41),
            (190, 191, 192, 193, 194, 195, 196, 197, 356, 358, 216, 215, 217),
        )
    ),
    **dict(
        zip(
            (22, 23, 24, 25, 26, 27, 28, 181, 42, 183),
            (198, 199, 200, 201, 202, 203, 204, 357, 218, 359),
        )
    ),
    **dict(zip((29, 30, 31), (205, 206, 207))),
    **dict(zip((36, 37, 38), (212, 213, 214))),
}


CHARACTER_QUICKENING_RANGES: Mapping[int, tuple[int, int, int]] = {
    0: (0xD0, 0xD1, 0xD2),
    1: (0xDC, 0xDD, 0xDE),
    2: (0xD3, 0xD4, 0xD5),
    3: (0xD6, 0xD7, 0xD8),
    4: (0xD9, 0xDA, 0xDB),
    5: (0xDF, 0xE0, 0xE1),
}
CHARACTER_QUICKENING_ACTION_IDS = frozenset(
    action_id
    for action_ids in CHARACTER_QUICKENING_RANGES.values()
    for action_id in action_ids
)


def _bit(raw: bytes, index: int) -> bool:
    return 0 <= index < len(raw) * 8 and bool(raw[index // 8] & (1 << (index % 8)))


@dataclass(frozen=True)
class ContentRecord:
    content_index: int
    action_id: int
    sort_order: int

    display_action_id: int | None = None

    @property
    def presentation_action_id(self) -> int:
        return (
            self.action_id if self.display_action_id is None else self.display_action_id
        )


@dataclass(frozen=True)
class LicenseNode:
    index: int
    node_type: int
    action_ids: tuple[int, ...]


@dataclass(frozen=True)
class ActionRules:
    action_id: int
    mp_cost: int
    area_radius: float
    possible_ally: bool
    possible_foe: bool
    sole_self: bool
    initial_target: int
    menu_category: int
    magick_category: int
    required_content: int
    no_license_requirement: bool
    possible_ko_party: bool
    possible_stone_party: bool
    possible_reserve_party: bool

    @property
    def target_groups(self) -> tuple[TargetGroup, ...]:
        if self.sole_self:
            return (TargetGroup.PARTY,)
        possible_party = (
            self.possible_ally or self.possible_ko_party or self.possible_stone_party
        )

        allowed: set[TargetGroup] = set()
        if possible_party:
            allowed.add(TargetGroup.PARTY)
            allowed.add(TargetGroup.NEUTRALS)
        if self.possible_foe:
            allowed.add(TargetGroup.FOES)
        if self.possible_reserve_party:
            allowed.add(TargetGroup.RESERVE)

        preferred = (
            (
                TargetGroup.PARTY,
                TargetGroup.NEUTRALS,
                TargetGroup.FOES,
                TargetGroup.RESERVE,
            )
            if self.initial_target in (1, 2)
            else (
                TargetGroup.FOES,
                TargetGroup.NEUTRALS,
                TargetGroup.PARTY,
                TargetGroup.RESERVE,
            )
        )
        return tuple(group for group in preferred if group in allowed)

    @property
    def default_group(self) -> TargetGroup:
        groups = self.target_groups
        if self.initial_target == 4 and TargetGroup.FOES in groups:
            return TargetGroup.FOES
        if self.initial_target in (1, 2) and TargetGroup.PARTY in groups:
            return TargetGroup.PARTY
        if groups:
            return groups[0]
        return TargetGroup.PARTY


@dataclass(frozen=True)
class LiveCatalogState:
    menu_flags: int
    acquired_license_bits: bytes
    item_counts: Mapping[int, int]
    magick_bits: bytes
    technick_bits: bytes
    mist_bits: bytes
    signature: bytes

    magick_license_flags: bytes | None = None
    gambits_enabled: bool = True

    current_mp: int | None = None

    item_order: tuple[int, ...] | None = None

    status_mask: int = 0
    command_restrictions: int = 0


@dataclass(frozen=True)
class SharedCatalogState:
    owner: int
    battle: int
    menu_flags: int
    item_counts: Mapping[int, int]
    magick_bits: bytes
    technick_bits: bytes
    mist_bits: bytes
    signature: bytes
    item_order: tuple[int, ...] | None = None
    command_restrictions: int = 0


def read_native_item_order(process) -> tuple[int, ...] | None:
    try:
        owner = process.u64(NATIVE_SORT_OWNER_GLOBAL)
        relative = process.u32(NATIVE_ITEM_ORDER_POINTER_GLOBAL)
        resource_base = process.u64(NATIVE_RESOURCE_BASE_GLOBAL)
        if not owner or not relative:
            return None
        count = process.u32(owner + NATIVE_ITEM_ORDER_COUNT_OFFSET)
        if not 0 <= count <= ITEM_COUNTER_COUNT:
            return None
        raw = process.read(resource_base + relative, count * 2)
        if process.u64(NATIVE_SORT_OWNER_GLOBAL) != owner:
            return None
        if len(raw) != count * 2:
            return None
        return tuple(struct.unpack(f"<{count}H", raw)) if count else ()
    except (
        AttributeError,
        OSError,
        RuntimeError,
        IndexError,
        struct.error,
        ValueError,
    ):
        return None


def records_in_native_item_order(
    records: tuple[ContentRecord, ...] | list[ContentRecord],
    item_order: tuple[int, ...] | None,
) -> tuple[ContentRecord, ...]:
    fallback = tuple(records)
    if item_order is None:
        return fallback
    by_content = {record.content_index: record for record in fallback}
    ordered = tuple(
        by_content[content_index]
        for content_index in item_order
        if content_index in by_content
    )
    if len(ordered) != len(fallback):
        return fallback
    if len({record.content_index for record in ordered}) != len(ordered):
        return fallback
    return ordered


class BattlePackGameplayCatalog:
    def __init__(self, path: Path = DEFAULT_BATTLE_PACK) -> None:
        self.path = Path(path)
        raw = self.path.read_bytes()
        if len(raw) < 8:
            raise ValueError(f"truncated battle pack: {self.path}")
        section_count = struct.unpack_from("<I", raw, 0)[0]
        offsets = struct.unpack_from(f"<{section_count}I", raw, 4)

        def st2e(section_id: int) -> tuple[bytes, ...]:
            if not 0 <= section_id < section_count:
                raise ValueError(f"battle pack has no section {section_id}")
            base = offsets[section_id]
            if raw[base : base + 4] != b"st2e":
                raise ValueError(f"battle pack section {section_id} is not st2e")
            count = struct.unpack_from("<I", raw, base + 4)[0]
            entry_size = struct.unpack_from("<H", raw, base + 8)[0]
            entries_offset = struct.unpack_from("<I", raw, base + 0x0C)[0]
            start = base + entries_offset
            end = start + count * entry_size
            if end > len(raw):
                raise ValueError(f"truncated battle pack section {section_id}")
            return tuple(
                raw[start + index * entry_size : start + (index + 1) * entry_size]
                for index in range(count)
            )

        action_entries = st2e(14)
        self.action_rules = tuple(
            self._decode_action(index, entry)
            for index, entry in enumerate(action_entries)
        )
        self.license_nodes = tuple(
            self._decode_license(index, entry) for index, entry in enumerate(st2e(12))
        )
        self.items = tuple(
            ContentRecord(
                index,
                entry[7],
                entry[0],
                struct.unpack_from("<H", entry, 8)[0],
            )
            for index, entry in enumerate(st2e(18))
        )
        self.magicks = tuple(
            ContentRecord(index, entry[2], entry[6])
            for index, entry in enumerate(st2e(29))
        )
        self.technicks = tuple(
            ContentRecord(index, entry[2], entry[6])
            for index, entry in enumerate(st2e(30))
        )
        (
            self.fixed_magicks_by_member,
            self.fixed_technicks_by_member,
        ) = self._decode_fixed_member_actions(st2e(16))

        if (
            len(self.items) != 64
            or len(self.magicks) != 81
            or len(self.technicks) != 24
        ):
            raise ValueError(
                "unexpected Steam catalog sizes: "
                f"items={len(self.items)} magicks={len(self.magicks)} "
                f"technicks={len(self.technicks)}"
            )

    def _decode_fixed_member_actions(
        self,
        entries: tuple[bytes, ...],
    ) -> tuple[dict[int, tuple[int, ...]], dict[int, tuple[int, ...]]]:
        magicks_by_content = {
            MAGICK_CONTENT_BASE + record.content_index: record.action_id
            for record in self.magicks
        }
        technicks_by_content = {
            TECHNICK_CONTENT_BASE + record.content_index: record.action_id
            for record in self.technicks
        }
        magicks_by_member: dict[int, tuple[int, ...]] = {}
        technicks_by_member: dict[int, tuple[int, ...]] = {}

        for member_id in FIXED_ACTION_PARTY_MEMBER_IDS:
            if member_id >= len(entries) or len(entries[member_id]) < 0x6C:
                raise ValueError("truncated fixed party-member action inventory")
            contents = struct.unpack_from("<10H", entries[member_id], 0x58)
            magicks_by_member[member_id] = tuple(
                dict.fromkeys(
                    magicks_by_content[content]
                    for content in contents
                    if content in magicks_by_content
                )
            )
            technicks_by_member[member_id] = tuple(
                dict.fromkeys(
                    technicks_by_content[content]
                    for content in contents
                    if content in technicks_by_content
                )
            )

        return magicks_by_member, technicks_by_member

    def fixed_member_actions(
        self,
        party_member_id: int,
    ) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
        if party_member_id not in FIXED_ACTION_PARTY_MEMBER_IDS:
            return None
        return (
            self.fixed_magicks_by_member[party_member_id],
            self.fixed_technicks_by_member[party_member_id],
        )

    @staticmethod
    def _decode_license(index: int, entry: bytes) -> LicenseNode:
        if len(entry) < 0x18:
            raise ValueError("short license-node entry")
        node_type = entry[5]

        actions: tuple[int, ...] = ()
        if 9 <= node_type <= 14 or node_type == 20:
            values = struct.unpack_from("<8H", entry, 8)
            actions = tuple(value for value in values if value != 0xFFFF)
        return LicenseNode(index, node_type, actions)

    @staticmethod
    def _decode_action(index: int, entry: bytes) -> ActionRules:
        if len(entry) < 0x3C:
            raise ValueError("short action entry")
        first = entry[0x0C]
        second = struct.unpack_from("<H", entry, 0x2C)[0]
        target_flags = struct.unpack_from("<H", entry, 0x36)[0]
        return ActionRules(
            action_id=index,
            mp_cost=entry[0x0A],
            area_radius=float(entry[0x06]),
            sole_self=bool(first & 0x01),
            possible_ally=bool(first & 0x04),
            possible_foe=bool(first & 0x08),
            initial_target=(first >> 5) & 0x07,
            menu_category=entry[0x1E],
            magick_category=second & 0x07,
            required_content=struct.unpack_from("<H", entry, 0x22)[0],
            possible_ko_party=bool(second & 0x40),
            possible_stone_party=bool(second & 0x80),
            possible_reserve_party=bool(target_flags & 0x20),
            no_license_requirement=bool(entry[0x2D] & 0x20),
        )

    def rules(self, action_id: int) -> ActionRules:
        if not 0 <= action_id < len(self.action_rules):
            raise KeyError(f"unknown action ID 0x{action_id:X}")
        return self.action_rules[action_id]


class BattlePackRuntimeCatalog:
    def __init__(
        self,
        action_catalog: "NativeActionCatalog",
        *,
        party_member_id: int = PENELO_PARTY_MEMBER_ID,
        battle_pack: Path = DEFAULT_BATTLE_PACK,
        gameplay_catalog: BattlePackGameplayCatalog | None = None,
    ) -> None:
        self.action_catalog = action_catalog
        self.party_member_id = party_member_id
        self.static = gameplay_catalog or BattlePackGameplayCatalog(battle_pack)

    def read_shared_state(self, process) -> SharedCatalogState | None:
        item_order = read_native_item_order(process)
        try:
            owner = process.u64(MAIN_SAVE_OWNER_GLOBAL)
            battle = process.u64(BATTLE_SAVE_BASE_GLOBAL)
            if not owner or not battle:
                return None
            info = owner + GLOBAL_BATTLE_INFO_OFFSET
            menu_raw = process.read(info + MENU_FLAGS_OFFSET, 4)
            counters = process.read(
                battle + ITEM_COUNTERS_OFFSET, ITEM_COUNTER_COUNT * 2
            )
            magicks = process.read(
                battle + MAGICK_OWNERSHIP_OFFSET, MAGICK_OWNERSHIP_SIZE
            )
            technicks = process.read(
                battle + TECHNICK_OWNERSHIP_OFFSET, TECHNICK_OWNERSHIP_SIZE
            )
            mist = process.read(battle + MIST_OWNERSHIP_OFFSET, MIST_OWNERSHIP_SIZE)
            command_restrictions = process.read(COMMAND_RESTRICTIONS_GLOBAL, 1)[0]
        except (OSError, RuntimeError, IndexError, struct.error):
            return None

        try:
            if (
                process.u64(MAIN_SAVE_OWNER_GLOBAL) != owner
                or process.u64(BATTLE_SAVE_BASE_GLOBAL) != battle
            ):
                return None
        except (OSError, RuntimeError, IndexError, struct.error):
            return None

        item_counts: dict[int, int] = {}
        for record in self.static.items:
            packed = struct.unpack_from("<H", counters, record.content_index * 2)[0]
            quantity = packed & 0x03FF
            if quantity:
                item_counts[record.action_id] = quantity
        signature = (
            menu_raw
            + counters
            + magicks
            + technicks
            + mist
            + bytes((command_restrictions,))
            + (
                b"\x00"
                if item_order is None
                else b"\x01" + struct.pack(f"<{len(item_order)}H", *item_order)
            )
        )
        return SharedCatalogState(
            owner=owner,
            battle=battle,
            menu_flags=struct.unpack("<I", menu_raw)[0],
            item_counts=item_counts,
            magick_bits=magicks,
            technick_bits=technicks,
            mist_bits=mist,
            signature=signature,
            item_order=item_order,
            command_restrictions=command_restrictions,
        )

    def read_player_state(
        self,
        process,
        shared: SharedCatalogState,
        *,
        live_actor: int = 0,
    ) -> LiveCatalogState | None:
        info = shared.owner + GLOBAL_BATTLE_INFO_OFFSET
        member = (
            info + PARTY_MEMBER_BASE_OFFSET + self.party_member_id * PARTY_MEMBER_STRIDE
        )
        try:
            licenses = process.read(member + PARTY_LICENSES_OFFSET, PARTY_LICENSES_SIZE)
            member_state = process.read(member, 1)[0]
            current_mp = struct.unpack("<H", process.read(member + 0x4C, 2))[0]
            magick_license_flags = process.read(
                member + MAGICK_LICENSE_FLAGS_OFFSET,
                MAGICK_LICENSE_FLAGS_SIZE,
            )
            status_mask = 0
        except (OSError, RuntimeError, IndexError, struct.error):
            return None

        if live_actor:
            try:
                try:
                    live_actor_handle = struct.unpack(
                        "<I", process.read(live_actor + 0x08, 4)
                    )[0]
                except (OSError, RuntimeError, IndexError, struct.error):
                    live_actor_handle = None
                battle_keep = process.u64(live_actor + 0x698)
                if battle_keep:
                    live_licenses = process.read(
                        battle_keep + PARTY_LICENSES_OFFSET,
                        PARTY_LICENSES_SIZE,
                    )
                    live_member_state = process.read(battle_keep, 1)[0]
                    live_current_mp = struct.unpack(
                        "<H", process.read(battle_keep + 0x4C, 2)
                    )[0]
                    live_magick_license_flags = process.read(
                        battle_keep + MAGICK_LICENSE_FLAGS_OFFSET,
                        MAGICK_LICENSE_FLAGS_SIZE,
                    )
                    temporary_status = struct.unpack(
                        "<I",
                        process.read(battle_keep + TEMPORARY_STATUS_EFFECTS_OFFSET, 4),
                    )[0]
                    permanent_status = struct.unpack(
                        "<I",
                        process.read(battle_keep + PERMANENT_STATUS_EFFECTS_OFFSET, 4),
                    )[0]
                    if (
                        len(live_licenses) != PARTY_LICENSES_SIZE
                        or len(live_magick_license_flags) != MAGICK_LICENSE_FLAGS_SIZE
                    ):
                        raise struct.error("short live battle-keep snapshot")
                    final_battle_keep = process.u64(live_actor + 0x698)
                    if final_battle_keep != battle_keep:
                        return None
                    if live_actor_handle is not None:
                        final_actor_handle = struct.unpack(
                            "<I", process.read(live_actor + 0x08, 4)
                        )[0]
                        if final_actor_handle != live_actor_handle:
                            return None

                    (
                        licenses,
                        member_state,
                        current_mp,
                        magick_license_flags,
                    ) = (
                        live_licenses,
                        live_member_state,
                        live_current_mp,
                        live_magick_license_flags,
                    )
                    status_mask = temporary_status | permanent_status
            except (OSError, RuntimeError, IndexError, struct.error):
                return None

        try:
            if (
                process.u64(MAIN_SAVE_OWNER_GLOBAL) != shared.owner
                or process.u64(BATTLE_SAVE_BASE_GLOBAL) != shared.battle
            ):
                return None
        except (OSError, RuntimeError, IndexError, struct.error):
            return None

        signature = (
            shared.signature
            + licenses
            + bytes((member_state,))
            + struct.pack("<H", current_mp)
            + magick_license_flags
            + struct.pack("<I", status_mask)
        )
        return LiveCatalogState(
            menu_flags=shared.menu_flags,
            acquired_license_bits=licenses,
            item_counts=shared.item_counts,
            magick_bits=shared.magick_bits,
            technick_bits=shared.technick_bits,
            mist_bits=shared.mist_bits,
            signature=signature,
            magick_license_flags=magick_license_flags,
            gambits_enabled=bool(member_state & 0x04),
            current_mp=current_mp,
            item_order=shared.item_order,
            status_mask=status_mask,
            command_restrictions=shared.command_restrictions,
        )

    def read_state(self, process, *, live_actor: int = 0) -> LiveCatalogState | None:
        shared = self.read_shared_state(process)
        if shared is None:
            return None
        return self.read_player_state(process, shared, live_actor=live_actor)

    def _licensed_actions(self, state: LiveCatalogState) -> set[int]:
        result: set[int] = set()
        for node in self.static.license_nodes:
            if _bit(state.acquired_license_bits, node.index):
                result.update(node.action_ids)
        return result

    def _licensed_magick_actions(self, state: LiveCatalogState) -> set[int]:
        if state.magick_license_flags is None:
            return self._licensed_actions(state)
        result: set[int] = set()
        for flag_index, node_index in MAGICK_LICENSE_NODE_BY_FLAG.items():
            if not _bit(state.magick_license_flags, flag_index):
                continue
            result.update(self.static.license_nodes[node_index].action_ids)
        return result

    def _definition(
        self,
        action_id: int,
        *,
        enabled: bool = True,
        quantity: int | None = None,
        display_action_id: int | None = None,
    ):
        presentation_id = (
            action_id if display_action_id is None else int(display_action_id)
        )
        base = self.action_catalog.definition(presentation_id)
        rules = self.static.rules(action_id)
        return replace(
            base,
            action_id=action_id,
            target_groups=rules.target_groups,
            default_group=rules.default_group,
            self_only=rules.sole_self,
            enabled=enabled,
            quantity=quantity,
            mp_cost=rules.mp_cost,
            revive_only=action_id in REVIVAL_ACTION_IDS,
            area_radius=(
                0.0
                if action_id in CHARACTER_QUICKENING_ACTION_IDS
                else rules.area_radius
            ),
        )

    @staticmethod
    def _empty_page(key: str, title: str) -> PageDefinition:
        return PageDefinition(
            key,
            title,
            (MenuEntry("No actions available", "", EntryKind.LABEL, enabled=False),),
        )

    @staticmethod
    def _disable_entry(entry: MenuEntry) -> MenuEntry:
        action = replace(entry.action, enabled=False) if entry.action else None
        return replace(entry, action=action, enabled=False)

    @staticmethod
    def _page_has_enabled_entry(page: PageDefinition) -> bool:
        return any(
            entry.enabled and entry.kind is not EntryKind.LABEL
            for entry in page.entries
        )

    def _apply_command_restrictions(
        self,
        pages: dict[str, PageDefinition],
        state: LiveCatalogState,
    ) -> dict[str, PageDefinition]:
        blocks_everything = bool(state.status_mask & HARD_COMMAND_BLOCKING_STATUS_MASK)
        berserk_attack_only = bool(state.status_mask & BERSERK_STATUS_MASK)
        blocks_non_attack = blocks_everything or berserk_attack_only
        magicks_blocked = blocks_non_attack or bool(
            state.status_mask & SILENCE_STATUS_MASK
            or state.command_restrictions & MAGICK_COMMAND_RESTRICTION
        )

        blocked_pages = set()
        if blocks_non_attack:
            blocked_pages.update(("technicks", "quickenings", "summons"))
        elif state.command_restrictions & TECHNICK_COMMAND_RESTRICTION:
            blocked_pages.add("technicks")
        if magicks_blocked:
            blocked_pages.update(MAGICK_PAGE_KEYS)
        if blocks_non_attack or state.command_restrictions & ITEM_COMMAND_RESTRICTION:
            blocked_pages.add("items")

        for key in blocked_pages:
            page = pages.get(key)
            if page is not None:
                pages[key] = replace(
                    page,
                    entries=tuple(self._disable_entry(entry) for entry in page.entries),
                )

        for key in ("magicks", "mist"):
            page = pages.get(key)
            if page is None:
                continue
            entries = []
            for entry in page.entries:
                enabled = entry.enabled
                if entry.page_key in blocked_pages:
                    enabled = False
                entries.append(replace(entry, enabled=enabled))
            pages[key] = replace(page, entries=tuple(entries))

        root = pages["root"]
        root_entries = []
        for entry in root.entries:
            enabled = entry.enabled
            if blocks_everything:
                enabled = False
            elif berserk_attack_only:
                enabled = bool(
                    entry.kind is EntryKind.ACTION
                    and entry.action is not None
                    and entry.action.category == "Attack"
                    and not (state.command_restrictions & ATTACK_COMMAND_RESTRICTION)
                )
            elif (
                entry.kind is EntryKind.ACTION
                and entry.action is not None
                and entry.action.category == "Attack"
            ):
                enabled = enabled and not bool(
                    state.command_restrictions & ATTACK_COMMAND_RESTRICTION
                )
            elif entry.kind is EntryKind.PAGE:
                if entry.page_key == "magicks":
                    restrictions_apply = bool(
                        blocks_everything
                        or state.status_mask & SILENCE_STATUS_MASK
                        or state.command_restrictions
                        & (MAGICK_COMMAND_RESTRICTION | TECHNICK_COMMAND_RESTRICTION)
                    )
                    if restrictions_apply:
                        enabled = enabled and self._page_has_enabled_entry(
                            pages["magicks"]
                        )
                elif entry.page_key == "items":
                    enabled = enabled and entry.page_key not in blocked_pages
                elif blocks_everything:
                    enabled = False
            root_entries.append(self._disable_entry(entry) if not enabled else entry)
        pages["root"] = replace(root, entries=tuple(root_entries))
        return pages

    def _action_page(
        self,
        key: str,
        title: str,
        action_ids: tuple[int, ...],
        *,
        quantities: Mapping[int, int] | None = None,
        display_action_ids: Mapping[int, int] | None = None,
        current_mp: int | None = None,
    ) -> PageDefinition:
        entries: list[MenuEntry] = []
        for action_id in action_ids:
            quantity = quantities.get(action_id) if quantities is not None else None
            display_action_id = (
                display_action_ids.get(action_id)
                if display_action_ids is not None
                else None
            )
            action = self._definition(
                action_id,
                quantity=quantity,
                display_action_id=display_action_id,
            )
            enabled = action.enabled

            if (
                key.startswith("magick:")
                and current_mp is not None
                and action.mp_cost is not None
            ):
                enabled = enabled and current_mp >= action.mp_cost
                action = replace(action, enabled=enabled)
            entries.append(
                MenuEntry(
                    action.name,
                    action.description,
                    EntryKind.ACTION,
                    action=action,
                    enabled=enabled,
                )
            )
        if not entries:
            return self._empty_page(key, title)
        return PageDefinition(key, title, tuple(entries))

    def build_pages(
        self,
        state: LiveCatalogState,
        *,
        summon_active: bool = False,
    ) -> dict[str, PageDefinition]:
        fixed_actions = self.static.fixed_member_actions(self.party_member_id)
        if fixed_actions is None:
            licensed = self._licensed_actions(state)
            licensed_magicks = self._licensed_magick_actions(state)
            fixed_magicks: frozenset[int] | None = None
            fixed_technicks: frozenset[int] | None = None
        else:
            fixed_magicks = frozenset(fixed_actions[0])
            fixed_technicks = frozenset(fixed_actions[1])
            licensed = set()
            licensed_magicks = set()
        pages: dict[str, PageDefinition] = {}

        item_records = sorted(
            (
                record
                for record in self.static.items
                if state.item_counts.get(record.action_id, 0) > 0
            ),
            key=lambda record: (record.sort_order, record.content_index),
        )
        item_records = records_in_native_item_order(
            item_records,
            state.item_order,
        )
        item_ids = tuple(record.action_id for record in item_records)
        pages["items"] = self._action_page(
            "items",
            "Items",
            item_ids,
            quantities=state.item_counts,
            display_action_ids={
                record.action_id: record.presentation_action_id
                for record in item_records
            },
        )

        category_rows: list[MenuEntry] = []
        for category, (name, key) in enumerate(
            zip(MAGICK_CATEGORY_NAMES, MAGICK_PAGE_KEYS)
        ):
            records = sorted(
                (
                    record
                    for record in self.static.magicks
                    if self.static.rules(record.action_id).magick_category == category
                    and (
                        record.action_id in fixed_magicks
                        if fixed_magicks is not None
                        else _bit(state.magick_bits, record.content_index)
                    )
                ),
                key=lambda record: (record.sort_order, record.content_index),
            )
            available = tuple(
                record.action_id
                for record in records
                if fixed_magicks is not None
                or record.action_id in licensed_magicks
                or self.static.rules(record.action_id).no_license_requirement
            )
            pages[key] = self._action_page(
                key, name, available, current_mp=state.current_mp
            )
            category_rows.append(
                MenuEntry(
                    name,
                    f"Use {name.casefold()}.",
                    EntryKind.PAGE,
                    page_key=key,
                    enabled=bool(available),
                )
            )

        records = sorted(
            (
                record
                for record in self.static.technicks
                if (
                    record.action_id in fixed_technicks
                    if fixed_technicks is not None
                    else _bit(state.technick_bits, record.content_index)
                )
            ),
            key=lambda record: (record.sort_order, record.content_index),
        )
        available = tuple(
            record.action_id
            for record in records
            if fixed_technicks is not None
            or record.action_id in licensed
            or self.static.rules(record.action_id).no_license_requirement
        )
        pages["technicks"] = self._action_page("technicks", "Technicks", available)
        category_rows.append(
            MenuEntry(
                "Technicks",
                "Use technicks.",
                EntryKind.PAGE,
                page_key="technicks",
                enabled=bool(available),
            )
        )
        if category_rows:
            pages["magicks"] = PageDefinition(
                "magicks", "Magicks & Technicks", tuple(category_rows)
            )

        character_quickenings = (
            ()
            if fixed_actions is not None
            else CHARACTER_QUICKENING_RANGES.get(self.party_member_id, ())
        )
        global_quickening_ids = tuple(
            action_id
            for action_id in character_quickenings
            if _bit(state.mist_bits, action_id - 0xD0)
        )
        licensed_quickening_count = min(
            len(character_quickenings),
            sum(
                1
                for node in self.static.license_nodes
                if node.node_type == 19
                and _bit(state.acquired_license_bits, node.index)
            ),
        )
        quickening_ids = global_quickening_ids
        if licensed_quickening_count > len(global_quickening_ids):
            quickening_ids = character_quickenings[:licensed_quickening_count]
        esper_ids: list[int] = []
        if fixed_actions is None:
            for bit_index in range(18, 31):
                action_id = 0x106 + (bit_index - 18)
                node_index = bit_index
                if _bit(state.mist_bits, bit_index) and _bit(
                    state.acquired_license_bits, node_index
                ):
                    esper_ids.append(action_id)

        mist_rows: list[MenuEntry] = []
        if quickening_ids:
            pages["quickenings"] = self._action_page(
                "quickenings", "Quickening", quickening_ids
            )
            mist_rows.append(
                MenuEntry(
                    "Quickening",
                    "Use a Quickening.",
                    EntryKind.PAGE,
                    page_key="quickenings",
                )
            )
        if esper_ids:
            pages["summons"] = self._action_page("summons", "Summon", tuple(esper_ids))
            mist_rows.append(
                MenuEntry(
                    "Summon",
                    "Summon an Esper.",
                    EntryKind.PAGE,
                    page_key="summons",
                )
            )
        mist_menu_unlocked = bool(mist_rows or state.menu_flags & ((1 << 7) | (1 << 8)))
        if mist_menu_unlocked:
            pages["mist"] = PageDefinition("mist", "Mist", tuple(mist_rows))

        attack = self._definition(0x96)
        root: list[MenuEntry] = [
            MenuEntry(
                attack.name,
                attack.description,
                EntryKind.ACTION,
                action=attack,
            )
        ]
        if state.menu_flags & 0x01 or any(fixed_actions or ()):
            root.append(
                MenuEntry(
                    "Magicks & Technicks",
                    "Use magicks and technicks.",
                    EntryKind.PAGE,
                    page_key="magicks",
                    enabled=bool(category_rows),
                )
            )
            pages.setdefault(
                "magicks", self._empty_page("magicks", "Magicks & Technicks")
            )
        if summon_active:
            dismiss = replace(
                self._definition(0x113),
                description="Dismiss the summoned Esper.",
                category="Mist",
                target_groups=(),
                default_group=TargetGroup.PARTY,
                self_only=True,
            )
            root.append(
                MenuEntry(
                    dismiss.name,
                    dismiss.description,
                    EntryKind.ACTION,
                    action=dismiss,
                )
            )
        elif mist_menu_unlocked:
            root.append(
                MenuEntry(
                    "Mist",
                    "Use Mist abilities.",
                    EntryKind.PAGE,
                    page_key="mist",
                    enabled=bool(mist_rows),
                )
            )
        root.append(MenuEntry("Gambits", "Toggle gambits.", EntryKind.TOGGLE))
        root.append(
            MenuEntry(
                "Items",
                "Use items.",
                EntryKind.PAGE,
                page_key="items",
                enabled=bool(item_ids),
            )
        )
        pages["root"] = PageDefinition("root", "", tuple(root))
        return self._apply_command_restrictions(pages, state)


__all__ = [
    "ActionRules",
    "BattlePackGameplayCatalog",
    "ContentRecord",
    "BattlePackRuntimeCatalog",
    "LicenseNode",
    "LiveCatalogState",
    "SharedCatalogState",
]
