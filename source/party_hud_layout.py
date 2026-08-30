from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass


MRP_SECTION_TABLE = 0x0209AC60
FIELD_SECTION_ID = 1
LIVE_MRP_MAGIC = 0x0150524D
PARTY_HUD_GROUP_IDS = tuple(range(40, 52))
PARTY_HUD_ROOT_GROUP_IDS = (40, 49, 50, 51)
PARTY_SELECTION_PANEL_GROUP_ID = 21
PARTY_HUD_SCALE = 0.75
ACTION_PANEL_GROUP_ID = 50
ACTION_PANEL_SCALE = 0.70
ACTION_PANEL_SCALE_NUMERATOR = 7
ACTION_PANEL_SCALE_DENOMINATOR = 10


PARTY_HUD_ANCHOR_X = 1402
PARTY_HUD_ANCHOR_Y = 286
VANILLA_FINGERPRINT = "24e93218b194875235248869b51c8ca710d297df3f0cf32a8cd204cc2b3dc71c"
LEGACY_REDUCED_FINGERPRINT = (
    "0b98c574900bccd62e906532294519d778f081f517f320d33b9bacf708d4a00f"
)
PREVIOUS_REDUCED_FINGERPRINT = (
    "6f722237d6db2164ff8db08514628eeeb29223119ed5325afb320d4099c2ab2e"
)
INTERMEDIATE_REDUCED_FINGERPRINT = (
    "3cc94810baa5e600acfbf82d3b5506973ea21a018de660b354c75fdbb77035a7"
)
RESTORED_OVERLAYS_FINGERPRINT = (
    "fded6d50e955000f62fb777c3ba5463a0654e81dd1189522bd8d941402efa367"
)
PRE_ALIGNED_REDUCED_FINGERPRINT = (
    "6956e476c0300c78beb7e3bc7171282573d6636736ef27edeea85f27a27a5212"
)
PRE_NUMERIC_REDUCED_FINGERPRINT = (
    "8248d88814537af038f150f782227ba2d7e7317a836b020a7571014cab9bcba5"
)
PRE_NUMERIC_Y_REDUCED_FINGERPRINT = (
    "648c49af89670a3ffebf055664d0ca02c685eee48653520d3549638c6b532404"
)
PRE_COLLISION_REDUCED_FINGERPRINT = (
    "1f18fe5fc1dc79098d7d33075c62c3362545a58701e855971d5aef722df3d601"
)
PRE_HUD_REFINEMENT_FINGERPRINT = (
    "9ab27465c6a5351fedbf3b50e4a912ea77d2d0a3fb302c7aad6d726981535582"
)
LEGACY_PRE_ACTION_BAR_POSITION_FINGERPRINT = (
    "5fad935b7712885b4afdfb6a44378645cca69767f9ba033e9fb245a8dea0cfcc"
)
LEGACY_PRE_ACTION_BAR_SECOND_POSITION_FINGERPRINT = (
    "9c62b61d42f0d53c6abf08f680931918600abfedb33e6a984f3aafe64e814cac"
)
PRE_ACTION_BAR_POSITION_PRE_ESPER_Y_FINGERPRINT = (
    "c292e92aa88ea57aafd189a18572583f15b8169153a4ca10e6f31ed162116478"
)
PRE_ACTION_BAR_SECOND_POSITION_PRE_ESPER_Y_FINGERPRINT = (
    "c66357ca93b8cb8848a18f1d1056035fdece939c2cae661906f05590c7779239"
)
PRE_ACTION_BAR_POSITION_FINGERPRINT = (
    "7017b0043202c1bea11e2135202588870c231ab26d900d6e81b32a6a593a6875"
)
PRE_ACTION_BAR_SECOND_POSITION_FINGERPRINT = (
    "24a5c698ba2cc61fecdcf9af0f548fb3f9406923bb3ff984cabe2276bdfb7703"
)
PRE_ACTION_BAR_POSITION_POST_PARTY_NAME_FINGERPRINT = (
    "fcded32113ff2c06c93608d70e4859809815ecd9123a946769c92d6405cb1412"
)
PRE_ACTION_BAR_SECOND_POSITION_POST_PARTY_NAME_FINGERPRINT = (
    "6244b735e7692174ecd0c9b9d2b9c85941e83f72fe4f6aec2aa76e4b80924018"
)
PRE_ESPER_NAME_SCALE_FINGERPRINT = (
    "88cfaf563e2e7874ad42f1c32a172fb95cfc2024cd7bd8447d2ef3fedd11a062"
)
PRE_ESPER_NAME_SECOND_SCALE_FINGERPRINT = (
    "f2cf7dff126ab8c87d028b20e1358eab031570037bb64675f327f51841bb4a90"
)
PRE_ESPER_NAME_POSITION_FINGERPRINT = (
    "69b9ebcefc56ee53ca6b5a421c3cb3a39e7498cda6ec204752dead933a101eb4"
)
PRE_PARTY_NAME_POSITION_FINGERPRINT = (
    "f94258723573c9d9b5ed29a6d1f3bdae0ac39e78358fb4a79be67638e1915ef3"
)
REDUCED_FINGERPRINT = "3c2d721e5fc305d28272ce5b61848d79eadd1a5983c56e68c67812d736641a81"


NUMERIC_SPRITE_ENTRY_IDS = {
    40: frozenset((*range(7, 10), *range(11, 14), *range(15, 18))),
    44: frozenset(range(4)),
    45: frozenset(range(6)),
    46: frozenset(range(8)),
    47: frozenset(range(1, 4)),
    48: frozenset(range(5)),
}
NUMERIC_SCALE_NUMERATOR = 85
NUMERIC_SCALE_DENOMINATOR = 100
NUMERIC_Y_OFFSET = 4
PARTY_HUD_ROW_GAP = 42
EMPTY_PARTY_MEMBER_ID = 0xFFFF
PARTY_HUD_BASE_TOP = 790.0
PARTY_HUD_BASE_ROW_PITCH = 60.0
PARTY_HUD_CURSOR_X = 1184.0
PARTY_HUD_CURSOR_Y_IN_ROW = 5.0
# The hand's authored transparent top makes its visible fingertip sit a few
# pixels above the native name centre.  Keep this screen-space correction out
# of the MRP transform, and add a little more clearance when the Esper row is
# present below the party.
PARTY_HUD_CURSOR_DOWN_SHIFT = 3.0
PARTY_HUD_CURSOR_ESPER_EXTRA_DOWN_SHIFT = 3.0
PARTY_HUD_REDUCED_ANCHOR_X = 1972.0
PARTY_HUD_REDUCED_ANCHOR_Y = 1027.0
ESPER_NAME_GROUP_ID = 47
ESPER_NAME_ENTRY_ID = 0
# Only the Esper-name field needs correction: retain its previously approved
# size and lower that field inside the fourth row without touching party names.
ESPER_NAME_SCALE_VALUE = 61
ESPER_NAME_Y = 11
PARTY_NAME_GROUP_ID = 40
PARTY_NAME_ENTRY_ID = 19
PARTY_NAME_Y = 3
FIRST_ACTION_LABEL_LEFT_SHIFT = 28
SECOND_ACTION_LABEL_LEFT_SHIFT = 36
ACTION_GAUGE_LEFT_SHIFT = 21


def party_hud_character_order(member_id: int) -> int:
    """Mirror the renderer's stable story-character ordering exactly."""

    member_id = int(member_id)
    return {
        0: 0,
        3: 1,
        2: 2,
        4: 3,
        1: 4,
        5: 5,
    }.get(member_id, 0x100 + member_id)


def party_hud_visual_row(current_layout, party_slot: int) -> int | None:
    """Return the bottom-anchored native HUD row for one active party slot."""

    party_slot = int(party_slot)
    layout = tuple(int(value) for value in current_layout[:4])
    if (
        party_slot < 0
        or party_slot >= len(layout)
        or layout[party_slot] == EMPTY_PARTY_MEMBER_ID
    ):
        return None
    target_member = layout[party_slot]
    target_order = party_hud_character_order(target_member)
    active = [member for member in layout if member != EMPTY_PARTY_MEMBER_ID]
    canonical_rank = sum(
        1
        for member in active
        if party_hud_character_order(member) < target_order
        or (
            party_hud_character_order(member) == target_order and member < target_member
        )
    )
    if not active or canonical_rank >= len(active):
        return None
    return 4 - len(active) + canonical_rank


def party_hud_member_row(current_layout, member_id: int) -> int | None:
    """Resolve a live member id to the row occupied by its native HUD entry."""

    member_id = int(member_id)
    for party_slot, candidate in enumerate(tuple(current_layout[:4])):
        if int(candidate) == member_id:
            return party_hud_visual_row(current_layout, party_slot)
    return None


def party_hud_cursor_position(
    visual_row: int,
    *,
    reduced: bool,
    jab: float = 0.0,
    esper_active: bool = False,
) -> tuple[float, float]:
    """Place the cursor tip on the same geometry used by the native HUD."""

    visual_row = int(visual_row)
    if not 0 <= visual_row <= 3:
        raise ValueError(f"invalid party HUD row: {visual_row}")
    x = PARTY_HUD_CURSOR_X + float(jab)
    y = (
        PARTY_HUD_BASE_TOP
        + PARTY_HUD_BASE_ROW_PITCH * visual_row
        + PARTY_HUD_CURSOR_Y_IN_ROW
    )
    if reduced:
        x = (
            PARTY_HUD_REDUCED_ANCHOR_X
            + (x - PARTY_HUD_REDUCED_ANCHOR_X) * PARTY_HUD_SCALE
        )
        y = (
            PARTY_HUD_REDUCED_ANCHOR_Y
            + (y - PARTY_HUD_REDUCED_ANCHOR_Y) * PARTY_HUD_SCALE
        )
    y += PARTY_HUD_CURSOR_DOWN_SHIFT
    if esper_active:
        y += PARTY_HUD_CURSOR_ESPER_EXTRA_DOWN_SHIFT
    return x, y


def party_hud_is_reduced(process) -> bool:
    """Read the live HUD root twice and fail closed while its MRP reloads."""

    base = process.u64(MRP_SECTION_TABLE + FIELD_SECTION_ID * 8)
    if not base or process.u32(base) != LIVE_MRP_MAGIC:
        return False
    group = _group_rect_address(process, base, 40) - 4
    first = process.read(group + 4, 12)
    second = process.read(group + 4, 12)
    if first != second:
        return False
    rect = struct.unpack_from("<hhhh", first, 0)
    gap = struct.unpack_from("<i", first, 8)[0]
    return rect == (122, 242, 900, 44) and gap == PARTY_HUD_ROW_GAP


PARTY_SELECTION_PANEL_VANILLA_RECT = (570, 741, 1800, 304)
PARTY_SELECTION_PANEL_REDUCED_RECT = (950, 741, 1800, 304)
REVIEWED_ROOT_X = {
    40: 122,
    49: 317,
    50: 78,
    51: 470,
}


_ACTION_PANEL_PREVIOUS_VALUES = (
    struct.pack("<hhhh", 478, 244, 408, 48),
    struct.pack("<i", 39),
    struct.pack("<hhhh", 219, 22, 150, 18),
    struct.pack("<hhhh", 177, 0, 183, 27),
    bytes((60,)),
    struct.pack("<hhhh", 259, 6, 77, 35),
    struct.pack("<hhhh", 128, 0, 101, 36),
    struct.pack("<hhhh", 236, 18, 122, 27),
)

_ACTION_PANEL_INTERMEDIATE_VALUES = (
    struct.pack("<hhhh", 458, 244, 381, 45),
    struct.pack("<i", 39),
    struct.pack("<hhhh", 204, 20, 140, 17),
    struct.pack("<hhhh", 165, 0, 171, 25),
    bytes((56,)),
    struct.pack("<hhhh", 242, 6, 71, 32),
    struct.pack("<hhhh", 119, 0, 94, 34),
    struct.pack("<hhhh", 220, 17, 113, 25),
)

_ACTION_PANEL_REFINED_VALUES = (
    struct.pack("<hhhh", 458, 244, 408, 48),
    *_ACTION_PANEL_INTERMEDIATE_VALUES[1:],
)

_ACTION_PANEL_ALIGNED_VALUES = (
    struct.pack("<hhhh", REVIEWED_ROOT_X[ACTION_PANEL_GROUP_ID], 244, 408, 48),
    *_ACTION_PANEL_REFINED_VALUES[1:],
)

# Retained solely as a migration fingerprint for the build that shortened the
# wrong authored entries.  New layouts restore their original widths below.
_ACTION_PANEL_COLLISION_SAFE_VALUES = (
    _ACTION_PANEL_REFINED_VALUES[0],
    _ACTION_PANEL_REFINED_VALUES[1],
    struct.pack("<hhhh", 204, 20, 120, 17),
    *_ACTION_PANEL_REFINED_VALUES[3:7],
    struct.pack("<hhhh", 220, 17, 93, 25),
)
_ACTION_PANEL_COLLISION_SAFE_ALIGNED_VALUES = (
    struct.pack("<hhhh", REVIEWED_ROOT_X[ACTION_PANEL_GROUP_ID], 244, 408, 48),
    *_ACTION_PANEL_COLLISION_SAFE_VALUES[1:],
)

# Entry 0 is the authored type-7 action gauge. Move that widget roughly 15%
# of its rendered width to the left. Entries 3 and 4 are type-2 sprites; the
# earlier attempts moved those and visibly displaced the ACTION label instead.
# Restore every label/name/icon to its reviewed position and alter only E0.x.
_ACTION_PANEL_POSITIONED_VALUES = (
    _ACTION_PANEL_REFINED_VALUES[0],
    _ACTION_PANEL_REFINED_VALUES[1],
    struct.pack("<hhhh", 204 - ACTION_GAUGE_LEFT_SHIFT, 20, 140, 17),
    *_ACTION_PANEL_REFINED_VALUES[3:],
)

_ACTION_PANEL_PREVIOUS_POSITIONED_VALUES = (
    _ACTION_PANEL_ALIGNED_VALUES[0],
    _ACTION_PANEL_REFINED_VALUES[1],
    *_ACTION_PANEL_REFINED_VALUES[2:6],
    struct.pack("<hhhh", 119 - FIRST_ACTION_LABEL_LEFT_SHIFT, 0, 94, 34),
    struct.pack("<hhhh", 220 - FIRST_ACTION_LABEL_LEFT_SHIFT, 17, 113, 25),
)

_ACTION_PANEL_SECOND_POSITIONED_VALUES = (
    _ACTION_PANEL_ALIGNED_VALUES[0],
    _ACTION_PANEL_REFINED_VALUES[1],
    *_ACTION_PANEL_REFINED_VALUES[2:6],
    struct.pack("<hhhh", 119 - SECOND_ACTION_LABEL_LEFT_SHIFT, 0, 94, 34),
    struct.pack("<hhhh", 220 - SECOND_ACTION_LABEL_LEFT_SHIFT, 17, 113, 25),
)


@dataclass(frozen=True)
class MutableField:
    address: int
    value: bytes
    kind: str
    group_id: int
    entry_id: int = -1


def _scaled(value: int) -> int:
    magnitude = int(abs(value) * PARTY_HUD_SCALE + 0.5)
    return -magnitude if value < 0 else magnitude


def _scaled_field(field: MutableField) -> bytes:
    if field.kind == "group_rect":
        x, y, width, height = struct.unpack("<hhhh", field.value)
        new_width = _scaled(width)
        new_height = _scaled(height)
        if field.group_id in PARTY_HUD_ROOT_GROUP_IDS:
            x = PARTY_HUD_ANCHOR_X + _scaled(x - PARTY_HUD_ANCHOR_X)
            y = PARTY_HUD_ANCHOR_Y + _scaled(y - PARTY_HUD_ANCHOR_Y)
        else:
            x = _scaled(x)
            y = _scaled(y)
        return struct.pack("<hhhh", x, y, new_width, new_height)
    if field.kind == "group_gap":
        return struct.pack("<i", _scaled(struct.unpack("<i", field.value)[0]))
    if field.kind == "entry_rect":
        return struct.pack(
            "<hhhh",
            *(_scaled(value) for value in struct.unpack("<hhhh", field.value)),
        )
    if field.kind == "text_scale":
        return bytes((max(1, min(255, _scaled(field.value[0]))),))
    raise ValueError(f"unknown party-HUD field kind: {field.kind}")


def _action_panel_field(field: MutableField) -> bytes:
    if field.kind == "group_rect":
        return _ACTION_PANEL_REFINED_VALUES[0]
    if field.kind == "group_gap":
        return _ACTION_PANEL_REFINED_VALUES[1]
    if field.kind == "entry_rect":
        values = struct.unpack("<hhhh", field.value)
        return struct.pack(
            "<hhhh",
            *(
                (
                    (
                        abs(value) * ACTION_PANEL_SCALE_NUMERATOR
                        + ACTION_PANEL_SCALE_DENOMINATOR // 2
                    )
                    // ACTION_PANEL_SCALE_DENOMINATOR
                )
                * (-1 if value < 0 else 1)
                for value in values
            ),
        )
    if field.kind == "text_scale":
        return bytes(
            (
                max(
                    1,
                    min(
                        255,
                        (
                            field.value[0] * ACTION_PANEL_SCALE_NUMERATOR
                            + ACTION_PANEL_SCALE_DENOMINATOR // 2
                        )
                        // ACTION_PANEL_SCALE_DENOMINATOR,
                    ),
                ),
            )
        )
    raise ValueError(f"unknown action-panel field kind: {field.kind}")


def _reviewed_root_x(field: MutableField, value: bytes) -> bytes:
    if field.kind != "group_rect" or field.group_id not in REVIEWED_ROOT_X:
        return value
    _x, y, width, height = struct.unpack("<hhhh", value)
    return struct.pack("<hhhh", REVIEWED_ROOT_X[field.group_id], y, width, height)


def _reviewed_party_row_gap(field: MutableField, value: bytes) -> bytes:
    if field.kind == "group_gap" and field.group_id in (40, 49):
        return struct.pack("<i", PARTY_HUD_ROW_GAP)
    return value


def _scaled_esper_name(field: MutableField, value: bytes) -> bytes:
    if not (
        field.kind == "text_scale"
        and field.group_id == ESPER_NAME_GROUP_ID
        and field.entry_id == ESPER_NAME_ENTRY_ID
    ):
        return value
    return bytes((ESPER_NAME_SCALE_VALUE,))


def _positioned_esper_name(field: MutableField, value: bytes) -> bytes:
    if not (
        field.kind == "entry_rect"
        and field.group_id == ESPER_NAME_GROUP_ID
        and field.entry_id == ESPER_NAME_ENTRY_ID
    ):
        return value
    x, _y, width, height = struct.unpack("<hhhh", value)
    return struct.pack("<hhhh", x, ESPER_NAME_Y, width, height)


def _esper_name_layout(field: MutableField, value: bytes) -> bytes:
    return _scaled_esper_name(field, _positioned_esper_name(field, value))


def _positioned_party_name(field: MutableField, value: bytes) -> bytes:
    if not (
        field.kind == "entry_rect"
        and field.group_id == PARTY_NAME_GROUP_ID
        and field.entry_id == PARTY_NAME_ENTRY_ID
    ):
        return value
    x, _y, width, height = struct.unpack("<hhhh", value)
    return struct.pack("<hhhh", x, PARTY_NAME_Y, width, height)


def _hud_name_layout(field: MutableField, value: bytes) -> bytes:
    return _positioned_party_name(field, _esper_name_layout(field, value))


def _numeric_sprite_size(field: MutableField, value: bytes) -> bytes:
    if field.kind != "entry_rect" or field.entry_id not in NUMERIC_SPRITE_ENTRY_IDS.get(
        field.group_id, ()
    ):
        return value
    x, y, width, height = struct.unpack("<hhhh", value)
    width = max(
        1,
        (width * NUMERIC_SCALE_NUMERATOR + NUMERIC_SCALE_DENOMINATOR // 2)
        // NUMERIC_SCALE_DENOMINATOR,
    )
    height = max(
        1,
        (height * NUMERIC_SCALE_NUMERATOR + NUMERIC_SCALE_DENOMINATOR // 2)
        // NUMERIC_SCALE_DENOMINATOR,
    )
    return struct.pack("<hhhh", x, y, width, height)


def _numeric_sprite_y(field: MutableField, value: bytes) -> bytes:
    if field.kind != "entry_rect" or field.entry_id not in NUMERIC_SPRITE_ENTRY_IDS.get(
        field.group_id, ()
    ):
        return value
    x, y, width, height = struct.unpack("<hhhh", value)
    return struct.pack("<hhhh", x, y + NUMERIC_Y_OFFSET, width, height)


def _fingerprint(fields: list[MutableField]) -> str:
    return hashlib.sha256(b"".join(field.value for field in fields)).hexdigest()


def _collect_fields(process, base: int) -> list[MutableField]:
    if process.u32(base) != LIVE_MRP_MAGIC:
        raise RuntimeError("field MRP magic is not live")
    group_count = process.u32(base + 0x1C)
    group_list = process.u32(base + 0x20)
    if group_count <= max(PARTY_HUD_GROUP_IDS) or not group_list:
        raise RuntimeError("field MRP has no native party-HUD groups")

    fields: list[MutableField] = []
    for group_id in PARTY_HUD_GROUP_IDS:
        group = group_list + group_id * 0x14
        record = process.read(group, 0x14)
        entry_count = record[0]
        entries = struct.unpack_from("<I", record, 0x10)[0]
        if entry_count > 96 or (entry_count and not entries):
            raise RuntimeError(f"invalid MRP group {group_id}")
        fields.append(MutableField(group + 4, record[4:12], "group_rect", group_id))
        fields.append(
            MutableField(group + 0x0C, record[0x0C:0x10], "group_gap", group_id)
        )

        entry = entries
        for entry_id in range(entry_count):
            header = process.read(entry, 0x18)
            size, entry_type = header[0], header[1]
            if size < 0x0C or size > 0x80:
                raise RuntimeError(f"invalid MRP entry G{group_id} E{entry_id}: {size}")
            fields.append(
                MutableField(
                    entry + 4,
                    header[4:12],
                    "entry_rect",
                    group_id,
                    entry_id,
                )
            )
            if entry_type == 5 and size >= 0x18:
                fields.append(
                    MutableField(
                        entry + 0x10,
                        header[0x10:0x11],
                        "text_scale",
                        group_id,
                        entry_id,
                    )
                )
            entry += size
    return fields


def _group_rect_address(process, base: int, group_id: int) -> int:
    group_count = process.u32(base + 0x1C)
    group_list = process.u32(base + 0x20)
    if group_count <= group_id or not group_list:
        raise RuntimeError(f"field MRP has no group {group_id}")
    return group_list + group_id * 0x14 + 4


def _snapshot_still_current(
    process,
    base: int,
    panel_address: int,
    panel_rect: tuple[int, int, int, int],
    fields: list[MutableField],
) -> bool:
    """Revalidate every pointer and source byte immediately before mutation.

    Field MRP storage is replaced during loading.  A settle delay alone cannot
    prove that the allocation observed at the beginning of ``tick`` is still
    the live allocation at commit time.  Comparing the section-table owner,
    derived panel address, panel value and complete mutable-field snapshot makes
    a transition race fail closed before the first remote write.
    """

    if process.u64(MRP_SECTION_TABLE + FIELD_SECTION_ID * 8) != int(base):
        return False
    if _group_rect_address(process, base, PARTY_SELECTION_PANEL_GROUP_ID) != int(
        panel_address
    ):
        return False
    if struct.unpack("<hhhh", process.read(panel_address, 8)) != tuple(panel_rect):
        return False
    return _collect_fields(process, base) == fields


class PartyHudLayoutScaler:
    def __init__(
        self,
        retry_seconds: float = 0.5,
        settle_seconds: float = 0.0,
    ) -> None:
        self.retry_seconds = max(0.05, float(retry_seconds))
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.next_check = 0.0
        self.last_base = 0
        self.last_fingerprint = ""
        self.last_result = "unloaded"
        self.guard_address = 0
        self.guard_value = b""
        self.candidate_key: tuple[int, str, tuple[int, int, int, int]] | None = None
        self.candidate_since = 0.0

    def tick(self, process, now: float | None = None) -> str:
        if now is None:
            now = time.monotonic()
        if now < self.next_check:
            return self.last_result
        self.next_check = now + self.retry_seconds

        base = process.u64(MRP_SECTION_TABLE + FIELD_SECTION_ID * 8)
        if not base:
            self.last_base = 0
            self.last_fingerprint = ""
            self.last_result = "unloaded"
            self.guard_address = 0
            self.guard_value = b""
            self.candidate_key = None
            return self.last_result

        panel_address = _group_rect_address(
            process, base, PARTY_SELECTION_PANEL_GROUP_ID
        )
        panel_rect = struct.unpack("<hhhh", process.read(panel_address, 8))
        if (
            base == self.last_base
            and self.last_result == "reduced"
            and panel_rect == PARTY_SELECTION_PANEL_REDUCED_RECT
            and self.guard_address
            and process.read(self.guard_address, len(self.guard_value))
            == self.guard_value
        ):
            return self.last_result

        fields = _collect_fields(process, base)
        fingerprint = _fingerprint(fields)
        self.last_base = base
        self.last_fingerprint = fingerprint
        if (
            fingerprint == REDUCED_FINGERPRINT
            and panel_rect == PARTY_SELECTION_PANEL_REDUCED_RECT
        ):
            self.guard_address = fields[0].address
            self.guard_value = fields[0].value
            self.candidate_key = None
            self.last_result = "reduced"
            return self.last_result
        if fingerprint == REDUCED_FINGERPRINT:
            if panel_rect != PARTY_SELECTION_PANEL_VANILLA_RECT:
                self.last_result = "foreign"
                self.candidate_key = None
                return self.last_result
            candidate_key = (base, fingerprint, panel_rect)
            if self.settle_seconds:
                if candidate_key != self.candidate_key:
                    self.candidate_key = candidate_key
                    self.candidate_since = now
                    self.last_result = "stabilizing"
                    return self.last_result
                if now - self.candidate_since < self.settle_seconds:
                    self.last_result = "stabilizing"
                    return self.last_result
            if not _snapshot_still_current(
                process,
                base,
                panel_address,
                panel_rect,
                fields,
            ):
                self.candidate_key = None
                self.last_result = "stabilizing"
                return self.last_result
            process.write(
                panel_address,
                struct.pack("<hhhh", *PARTY_SELECTION_PANEL_REDUCED_RECT),
            )
            if struct.unpack("<hhhh", process.read(panel_address, 8)) != (
                PARTY_SELECTION_PANEL_REDUCED_RECT
            ):
                raise RuntimeError("party-selection panel write did not verify")
            self.last_result = "applied"
            self.candidate_key = None
            return self.last_result
        if fingerprint not in (
            VANILLA_FINGERPRINT,
            LEGACY_REDUCED_FINGERPRINT,
            PREVIOUS_REDUCED_FINGERPRINT,
            INTERMEDIATE_REDUCED_FINGERPRINT,
            RESTORED_OVERLAYS_FINGERPRINT,
            PRE_ALIGNED_REDUCED_FINGERPRINT,
            PRE_NUMERIC_REDUCED_FINGERPRINT,
            PRE_NUMERIC_Y_REDUCED_FINGERPRINT,
            PRE_COLLISION_REDUCED_FINGERPRINT,
            PRE_HUD_REFINEMENT_FINGERPRINT,
            PRE_ACTION_BAR_POSITION_FINGERPRINT,
            PRE_ACTION_BAR_SECOND_POSITION_FINGERPRINT,
            PRE_ACTION_BAR_POSITION_POST_PARTY_NAME_FINGERPRINT,
            PRE_ACTION_BAR_SECOND_POSITION_POST_PARTY_NAME_FINGERPRINT,
            LEGACY_PRE_ACTION_BAR_POSITION_FINGERPRINT,
            LEGACY_PRE_ACTION_BAR_SECOND_POSITION_FINGERPRINT,
            PRE_ACTION_BAR_POSITION_PRE_ESPER_Y_FINGERPRINT,
            PRE_ACTION_BAR_SECOND_POSITION_PRE_ESPER_Y_FINGERPRINT,
            PRE_ESPER_NAME_SCALE_FINGERPRINT,
            PRE_ESPER_NAME_SECOND_SCALE_FINGERPRINT,
            PRE_ESPER_NAME_POSITION_FINGERPRINT,
            PRE_PARTY_NAME_POSITION_FINGERPRINT,
        ):
            self.last_result = "foreign"
            self.candidate_key = None
            return self.last_result
        if panel_rect not in (
            PARTY_SELECTION_PANEL_VANILLA_RECT,
            PARTY_SELECTION_PANEL_REDUCED_RECT,
        ):
            self.last_result = "foreign"
            self.candidate_key = None
            return self.last_result

        candidate_key = (base, fingerprint, panel_rect)
        if self.settle_seconds:
            if candidate_key != self.candidate_key:
                self.candidate_key = candidate_key
                self.candidate_since = now
                self.last_result = "stabilizing"
                return self.last_result
            if now - self.candidate_since < self.settle_seconds:
                self.last_result = "stabilizing"
                return self.last_result

        replacements: list[bytes] = []
        action_index = 0
        for field in fields:
            if fingerprint == PRE_PARTY_NAME_POSITION_FINGERPRINT:
                replacements.append(_positioned_party_name(field, field.value))
                continue
            if fingerprint in (
                PRE_ESPER_NAME_SCALE_FINGERPRINT,
                PRE_ESPER_NAME_SECOND_SCALE_FINGERPRINT,
                PRE_ESPER_NAME_POSITION_FINGERPRINT,
            ):
                replacements.append(_hud_name_layout(field, field.value))
                continue
            if (
                fingerprint
                in (
                    PRE_ACTION_BAR_POSITION_FINGERPRINT,
                    PRE_ACTION_BAR_SECOND_POSITION_FINGERPRINT,
                    PRE_ACTION_BAR_POSITION_POST_PARTY_NAME_FINGERPRINT,
                    PRE_ACTION_BAR_SECOND_POSITION_POST_PARTY_NAME_FINGERPRINT,
                    LEGACY_PRE_ACTION_BAR_POSITION_FINGERPRINT,
                    LEGACY_PRE_ACTION_BAR_SECOND_POSITION_FINGERPRINT,
                    PRE_ACTION_BAR_POSITION_PRE_ESPER_Y_FINGERPRINT,
                    PRE_ACTION_BAR_SECOND_POSITION_PRE_ESPER_Y_FINGERPRINT,
                )
                and field.group_id != ACTION_PANEL_GROUP_ID
            ):
                replacements.append(_hud_name_layout(field, field.value))
                continue
            if field.group_id <= 48:
                replacement = (
                    _scaled_field(field)
                    if fingerprint == VANILLA_FINGERPRINT
                    else field.value
                )
                replacement = _reviewed_root_x(field, replacement)
                replacement = _reviewed_party_row_gap(field, replacement)
                if fingerprint not in (
                    PRE_NUMERIC_Y_REDUCED_FINGERPRINT,
                    PRE_COLLISION_REDUCED_FINGERPRINT,
                    PRE_HUD_REFINEMENT_FINGERPRINT,
                ):
                    replacement = _numeric_sprite_size(field, replacement)
                if fingerprint not in (
                    PRE_COLLISION_REDUCED_FINGERPRINT,
                    PRE_HUD_REFINEMENT_FINGERPRINT,
                ):
                    replacement = _numeric_sprite_y(field, replacement)
                replacements.append(_hud_name_layout(field, replacement))
                continue

            if field.group_id in (49, 51):
                if fingerprint in (
                    VANILLA_FINGERPRINT,
                    LEGACY_REDUCED_FINGERPRINT,
                    RESTORED_OVERLAYS_FINGERPRINT,
                ):
                    replacement = _scaled_field(field)
                else:
                    replacement = field.value
                replacement = _reviewed_root_x(field, replacement)
                replacements.append(_reviewed_party_row_gap(field, replacement))
                continue

            if field.group_id == ACTION_PANEL_GROUP_ID:
                if fingerprint in (
                    VANILLA_FINGERPRINT,
                    LEGACY_REDUCED_FINGERPRINT,
                ):
                    replacement = _ACTION_PANEL_POSITIONED_VALUES[action_index]
                    action_index += 1
                    replacements.append(_reviewed_root_x(field, replacement))
                    continue
                if action_index >= len(_ACTION_PANEL_PREVIOUS_VALUES):
                    raise RuntimeError("unexpected extra action-panel field")
                if fingerprint == PREVIOUS_REDUCED_FINGERPRINT:
                    previous_values = _ACTION_PANEL_PREVIOUS_VALUES
                elif fingerprint == PRE_ALIGNED_REDUCED_FINGERPRINT:
                    previous_values = _ACTION_PANEL_REFINED_VALUES
                elif fingerprint in (
                    PRE_NUMERIC_REDUCED_FINGERPRINT,
                    PRE_NUMERIC_Y_REDUCED_FINGERPRINT,
                    PRE_COLLISION_REDUCED_FINGERPRINT,
                ):
                    previous_values = _ACTION_PANEL_ALIGNED_VALUES
                elif fingerprint == PRE_HUD_REFINEMENT_FINGERPRINT:
                    previous_values = _ACTION_PANEL_COLLISION_SAFE_ALIGNED_VALUES
                elif fingerprint in (
                    PRE_ACTION_BAR_POSITION_FINGERPRINT,
                    PRE_ACTION_BAR_POSITION_POST_PARTY_NAME_FINGERPRINT,
                ):
                    previous_values = _ACTION_PANEL_PREVIOUS_POSITIONED_VALUES
                elif fingerprint in (
                    PRE_ACTION_BAR_SECOND_POSITION_FINGERPRINT,
                    PRE_ACTION_BAR_SECOND_POSITION_POST_PARTY_NAME_FINGERPRINT,
                ):
                    previous_values = _ACTION_PANEL_SECOND_POSITIONED_VALUES
                else:
                    previous_values = _ACTION_PANEL_INTERMEDIATE_VALUES
                expected_previous = previous_values[action_index]
                replacement = _ACTION_PANEL_POSITIONED_VALUES[action_index]
                action_index += 1
                if field.value != expected_previous:
                    raise RuntimeError("unexpected previous action-panel layout")
                replacements.append(_reviewed_root_x(field, replacement))
                continue

            raise RuntimeError(f"unexpected party-HUD group {field.group_id}")
        expected = hashlib.sha256(b"".join(replacements)).hexdigest()
        if expected != REDUCED_FINGERPRINT:
            raise RuntimeError(
                "party-HUD transform does not match its reviewed fingerprint: "
                f"{expected}"
            )
        if not _snapshot_still_current(
            process,
            base,
            panel_address,
            panel_rect,
            fields,
        ):
            self.candidate_key = None
            self.last_result = "stabilizing"
            return self.last_result
        for field, replacement in zip(fields, replacements):
            if replacement != field.value:
                if (
                    process.u64(MRP_SECTION_TABLE + FIELD_SECTION_ID * 8) != base
                    or process.read(field.address, len(field.value)) != field.value
                ):
                    raise RuntimeError("party-HUD MRP changed during commit")
                process.write(field.address, replacement)
        if panel_rect != PARTY_SELECTION_PANEL_REDUCED_RECT:
            if (
                process.u64(MRP_SECTION_TABLE + FIELD_SECTION_ID * 8) != base
                or struct.unpack("<hhhh", process.read(panel_address, 8))
                != panel_rect
            ):
                raise RuntimeError("party-HUD panel changed during commit")
            process.write(
                panel_address,
                struct.pack("<hhhh", *PARTY_SELECTION_PANEL_REDUCED_RECT),
            )

        verified_fields = _collect_fields(process, base)
        verified = _fingerprint(verified_fields)
        verified_panel = struct.unpack("<hhhh", process.read(panel_address, 8))
        if (
            verified != REDUCED_FINGERPRINT
            or verified_panel != PARTY_SELECTION_PANEL_REDUCED_RECT
        ):
            raise RuntimeError("party-HUD MRP write did not verify")
        self.last_fingerprint = verified
        self.guard_address = verified_fields[0].address
        self.guard_value = verified_fields[0].value
        self.last_result = "reduced"
        self.candidate_key = None
        return "applied"


__all__ = [
    "FIELD_SECTION_ID",
    "LIVE_MRP_MAGIC",
    "MRP_SECTION_TABLE",
    "ACTION_PANEL_GROUP_ID",
    "ACTION_PANEL_SCALE",
    "PARTY_HUD_ANCHOR_X",
    "PARTY_HUD_ANCHOR_Y",
    "PARTY_HUD_GROUP_IDS",
    "PARTY_HUD_ROOT_GROUP_IDS",
    "PARTY_HUD_SCALE",
    "PARTY_HUD_ROW_GAP",
    "EMPTY_PARTY_MEMBER_ID",
    "PARTY_HUD_BASE_TOP",
    "PARTY_HUD_BASE_ROW_PITCH",
    "PARTY_HUD_CURSOR_X",
    "PARTY_HUD_CURSOR_Y_IN_ROW",
    "PARTY_HUD_CURSOR_DOWN_SHIFT",
    "PARTY_HUD_CURSOR_ESPER_EXTRA_DOWN_SHIFT",
    "PARTY_HUD_REDUCED_ANCHOR_X",
    "PARTY_HUD_REDUCED_ANCHOR_Y",
    "NUMERIC_SCALE_DENOMINATOR",
    "NUMERIC_SCALE_NUMERATOR",
    "NUMERIC_SPRITE_ENTRY_IDS",
    "NUMERIC_Y_OFFSET",
    "PARTY_SELECTION_PANEL_GROUP_ID",
    "PARTY_SELECTION_PANEL_REDUCED_RECT",
    "PARTY_SELECTION_PANEL_VANILLA_RECT",
    "INTERMEDIATE_REDUCED_FINGERPRINT",
    "LEGACY_REDUCED_FINGERPRINT",
    "PRE_ALIGNED_REDUCED_FINGERPRINT",
    "PRE_NUMERIC_REDUCED_FINGERPRINT",
    "PRE_NUMERIC_Y_REDUCED_FINGERPRINT",
    "PRE_COLLISION_REDUCED_FINGERPRINT",
    "PRE_ACTION_BAR_POSITION_FINGERPRINT",
    "PRE_ACTION_BAR_SECOND_POSITION_FINGERPRINT",
    "PRE_ACTION_BAR_POSITION_POST_PARTY_NAME_FINGERPRINT",
    "PRE_ACTION_BAR_SECOND_POSITION_POST_PARTY_NAME_FINGERPRINT",
    "LEGACY_PRE_ACTION_BAR_POSITION_FINGERPRINT",
    "LEGACY_PRE_ACTION_BAR_SECOND_POSITION_FINGERPRINT",
    "PRE_ACTION_BAR_POSITION_PRE_ESPER_Y_FINGERPRINT",
    "PRE_ACTION_BAR_SECOND_POSITION_PRE_ESPER_Y_FINGERPRINT",
    "PRE_ESPER_NAME_SCALE_FINGERPRINT",
    "PRE_ESPER_NAME_SECOND_SCALE_FINGERPRINT",
    "PRE_ESPER_NAME_POSITION_FINGERPRINT",
    "PRE_PARTY_NAME_POSITION_FINGERPRINT",
    "ESPER_NAME_SCALE_VALUE",
    "ESPER_NAME_Y",
    "PARTY_NAME_GROUP_ID",
    "PARTY_NAME_ENTRY_ID",
    "PARTY_NAME_Y",
    "PREVIOUS_REDUCED_FINGERPRINT",
    "RESTORED_OVERLAYS_FINGERPRINT",
    "PartyHudLayoutScaler",
    "party_hud_character_order",
    "party_hud_cursor_position",
    "party_hud_is_reduced",
    "party_hud_member_row",
    "party_hud_visual_row",
    "REDUCED_FINGERPRINT",
    "VANILLA_FINGERPRINT",
]
