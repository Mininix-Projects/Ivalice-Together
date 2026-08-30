from __future__ import annotations

import struct
import time

import coop_roster
import coop_runtime as coop
import helper_lease
import runtime_shutdown
from penelo_story_guard import (
    BATTLE_INFO_GLOBAL,
    JOINED_ONCE_FLAG,
    PARTY_MEMBER_OFFSET,
    PARTY_MEMBER_STRIDE,
)


BATTLEPACK_GLOBAL = 0x208E680
MENU_INFO_GLOBAL = 0x20927C0
STORY_SECTION_ID = 60
STORY_EXTENDED_SECTION_ID = 59
ST2E_MAGIC = b"st2e"
STORY_SLOT_COUNT = 9
LIVE_PARTY_SLOT_COUNT = 9
LIVE_PARTY_LAYOUT_BASE = 0x5A48
LIVE_PARTY_LAYOUT_STRIDE = 0x12
CURRENT_PARTY_LAYOUT_ID = 1
DEFAULT_PARTY_LAYOUT_ID = 0
MENU_PARTY_LAYOUT_ID = 3


MENU_SELECTABLE_FLAGS_OFFSET = 0x3249
MENU_PARTY_SELECTABLE_BIT = 0x02
MENU_GAMBITS_SELECTABLE_BIT = 0x20
MENU_START_SELECTABLE_MASK = MENU_PARTY_SELECTABLE_BIT | MENU_GAMBITS_SELECTABLE_BIT


MENU_FEATURE_GATE_SITE = 0x32A8BB
MENU_FEATURE_GATE_ORIGINAL = bytes.fromhex("E9 B0 D0 04 00")
MENU_FEATURE_GATE_NATIVE = 0x377970
MENU_FEATURE_GATE_CONTEXT_SITE = 0x32A8A0
MENU_FEATURE_GATE_CONTEXT = bytes.fromhex(
    "40 57 48 83 EC 20 8B F9 81 E7 FF 3F 00 00 0F BA E1 0E 73 0C 8B CF 48 83 C4 20 5F"
)
MENU_FEATURE_PARTY_ID = 8
MENU_FEATURE_GAMBITS_ID = 9


MENU_FEATURE_GATE_CAVE_OFFSET = 0x1400
MENU_FEATURE_GATE_CAVE_SIZE = 0x100
MENU_FEATURE_GATE_STATE_OFFSET = 0x14F0
MENU_FEATURE_GATE_STATIC_SIZE = (
    MENU_FEATURE_GATE_STATE_OFFSET - MENU_FEATURE_GATE_CAVE_OFFSET
)
ACTIVE_STORAGE_SLOT_COUNT = 4
COOP_ACTIVE_SLOT_COUNT = 3
VAAN_ID = 0
PENELO_ID = 5
ASHE_ID = 1
BALTHIER_ID = 3


INITIAL_COOP_ROSTER = coop_roster.load()
START_WITH_ALL = INITIAL_COOP_ROSTER.start_with_all
INITIAL_COOP_MEMBERS = INITIAL_COOP_ROSTER.members
EMPTY = 0xFF
POLL_SECONDS = 0.25
EXTENDED_ALLOCATION_SIZE = coop.STORY_EXTENDED_WORKSPACE_SIZE


def build_early_menu_feature_gate(cave: int, state: int) -> bytes:
    body = bytearray()

    body += b"\x80\x3d" + coop.rel32(cave, 7, state) + b"\x00"
    body += b"\x74\x0a"
    body += b"\x83\xf9" + bytes((MENU_FEATURE_PARTY_ID,))
    body += b"\x74\x0a"
    body += b"\x83\xf9" + bytes((MENU_FEATURE_GAMBITS_ID,))
    body += b"\x74\x05"
    native_jump = cave + len(body)
    body += b"\xe9" + coop.rel32(native_jump, 5, MENU_FEATURE_GATE_NATIVE)
    body += b"\xb8\x01\x00\x00\x00\xc3"
    available = state - cave
    if available <= 0 or len(body) > available:
        raise RuntimeError("Party/Gambits feature gate overlaps its state")
    return bytes(body)


class EarlyMenuFeatureGate:
    def __init__(self, process: coop.RemoteProcess, cave_base: int):
        self.process = process
        self.cave = cave_base + MENU_FEATURE_GATE_CAVE_OFFSET
        self.state = cave_base + MENU_FEATURE_GATE_STATE_OFFSET
        self.body = build_early_menu_feature_gate(self.cave, self.state)
        self.static_image = self.body.ljust(MENU_FEATURE_GATE_STATIC_SIZE, b"\x00")
        self.patch = b"\xe9" + coop.rel32(MENU_FEATURE_GATE_SITE, 5, self.cave)

    def _read_slot(self) -> tuple[bytes, bytes, bytes]:
        static = self.process.read(self.cave, MENU_FEATURE_GATE_STATIC_SIZE)
        state = self.process.read(self.state, 1)
        tail_size = MENU_FEATURE_GATE_CAVE_SIZE - MENU_FEATURE_GATE_STATIC_SIZE - 1
        tail = self.process.read(self.state + 1, tail_size)
        return static, state, tail

    def install(self) -> None:
        context = self.process.read(
            MENU_FEATURE_GATE_CONTEXT_SITE,
            len(MENU_FEATURE_GATE_CONTEXT),
        )
        if context != MENU_FEATURE_GATE_CONTEXT:
            raise RuntimeError(
                "Party/Gambits feature gate has an incompatible ABI context"
            )
        current = self.process.read(
            MENU_FEATURE_GATE_SITE, len(MENU_FEATURE_GATE_ORIGINAL)
        )
        static, state, tail = self._read_slot()
        zero_static = b"\x00" * MENU_FEATURE_GATE_STATIC_SIZE
        zero_tail = b"\x00" * len(tail)
        if current == MENU_FEATURE_GATE_ORIGINAL:
            fresh = static == zero_static and state == b"\x00" and tail == zero_tail
            prepared = (
                static == self.static_image
                and state == b"\x00"
                and tail == zero_tail
            )
            if not fresh and not prepared:
                raise RuntimeError(
                    "Party/Gambits feature gate has a non-empty or partial cave"
                )
            if fresh:
                # The whole immutable span is published before the executable
                # root.  A failure at this point is safely resumable only when
                # the next installer observes this exact prepared image.
                self.process.patch_code(self.cave, self.static_image)

            # Revalidate every dependency immediately before the one-way root
            # commit.  Never repair, roll back or free executable memory after
            # the root may have become visible to another game thread.
            if (
                self.process.read(
                    MENU_FEATURE_GATE_CONTEXT_SITE,
                    len(MENU_FEATURE_GATE_CONTEXT),
                )
                != MENU_FEATURE_GATE_CONTEXT
                or self.process.read(
                    MENU_FEATURE_GATE_SITE,
                    len(MENU_FEATURE_GATE_ORIGINAL),
                )
                != MENU_FEATURE_GATE_ORIGINAL
                or self._read_slot()
                != (self.static_image, b"\x00", zero_tail)
            ):
                raise RuntimeError(
                    "Party/Gambits feature gate changed before root commit"
                )
            self.process.patch_code(MENU_FEATURE_GATE_SITE, self.patch)
            return
        if current == self.patch:
            if (
                static != self.static_image
                or state not in (b"\x00", b"\x01")
                or tail != zero_tail
            ):
                raise RuntimeError(
                    "Party/Gambits feature gate points at an unknown cave"
                )

            # Adoption is read-only for code and the root.  Only the dynamic
            # fail-closed contract is disarmed when a prior owner left it set.
            if state != b"\x00":
                self.process.write(self.state, b"\x00")
            return
        raise RuntimeError("Party/Gambits feature gate has unexpected executable bytes")

    def set_enabled(self, enabled: bool) -> bool:
        desired = bytes((int(bool(enabled)),))
        if self.process.read(self.state, 1) == desired:
            return False
        self.process.write(self.state, desired)
        return True


def prepare_early_menu_feature_gate(
    process: coop.RemoteProcess,
    *,
    install_new: bool,
) -> EarlyMenuFeatureGate | None:
    current = process.read(MENU_FEATURE_GATE_SITE, len(MENU_FEATURE_GATE_ORIGINAL))
    if current == MENU_FEATURE_GATE_ORIGINAL:
        if not install_new:
            return None
        context = process.read(
            MENU_FEATURE_GATE_CONTEXT_SITE,
            len(MENU_FEATURE_GATE_CONTEXT),
        )
        if context != MENU_FEATURE_GATE_CONTEXT:
            raise RuntimeError(
                "Party/Gambits feature gate has an incompatible ABI context"
            )
        cave_base = process.ensure_cave_allocation()
    else:
        if len(current) != 5 or current[0] != 0xE9:
            raise RuntimeError(
                "Party/Gambits feature gate has unexpected executable bytes"
            )
        target = MENU_FEATURE_GATE_SITE + 5 + struct.unpack("<i", current[1:])[0]
        cave_base = target - MENU_FEATURE_GATE_CAVE_OFFSET
        if cave_base <= 0:
            raise RuntimeError("Party/Gambits feature gate has an invalid cave target")
        if process.read(cave_base, len(coop.CAVE_MAGIC)) != coop.CAVE_MAGIC:
            raise RuntimeError(
                "Party/Gambits feature gate points outside the co-op cave"
            )

    gate = EarlyMenuFeatureGate(process, cave_base)
    gate.install()
    return gate


def initialize_early_menu_feature_gate(
    process: coop.RemoteProcess,
    *,
    install_new: bool,
) -> EarlyMenuFeatureGate | None:
    try:
        return prepare_early_menu_feature_gate(
            process,
            install_new=install_new,
        )
    except (OSError, RuntimeError, TypeError, ValueError, struct.error) as error:
        print(
            "STORY_PARTY_PATCH party_gambits_gate unavailable "
            f"{type(error).__name__}: {error}",
            flush=True,
        )
        return None


def enable_party_menu_for_start_all(
    process: coop.RemoteProcess,
    battle_info: int,
    *,
    enabled: bool | None = None,
    feature_gate: EarlyMenuFeatureGate | None = None,
) -> bool:
    def disarm_feature_gate() -> None:
        if feature_gate is None:
            return
        try:
            feature_gate.set_enabled(False)
        except (OSError, RuntimeError, TypeError, ValueError, struct.error):
            pass

    if enabled is None:
        enabled = START_WITH_ALL
    if not enabled or not battle_info:
        disarm_feature_gate()
        return False

    current_layout = (
        battle_info
        + LIVE_PARTY_LAYOUT_BASE
        + CURRENT_PARTY_LAYOUT_ID * LIVE_PARTY_LAYOUT_STRIDE
    )
    try:
        raw_slots = process.read(current_layout, LIVE_PARTY_SLOT_COUNT * 2)
        slots = struct.unpack("<9H", raw_slots)
        if VAAN_ID not in slots:
            active_main = tuple(
                member
                for member in slots[:ACTIVE_STORAGE_SLOT_COUNT]
                if 0 <= member <= PENELO_ID
            )
            if not active_main or len(active_main) != len(set(active_main)):
                disarm_feature_gate()
                return False
            history_address = (
                battle_info + PARTY_MEMBER_OFFSET + PENELO_ID * PARTY_MEMBER_STRIDE
            )
            history = process.read(history_address, 1)
            persistent_history = bool(
                len(history) == 1 and history[0] & JOINED_ONCE_FLAG
            )
            roster_complete = False
            if not persistent_history:
                complete_layouts = []
                for layout_id in (DEFAULT_PARTY_LAYOUT_ID, MENU_PARTY_LAYOUT_ID):
                    address = (
                        battle_info
                        + LIVE_PARTY_LAYOUT_BASE
                        + layout_id * LIVE_PARTY_LAYOUT_STRIDE
                    )
                    complete_layouts.append(
                        struct.unpack(
                            "<9H",
                            process.read(address, LIVE_PARTY_SLOT_COUNT * 2),
                        )
                    )
                roster_complete = bool(INITIAL_COOP_MEMBERS) and all(
                    all(
                        layout.count(member_id) == 1
                        for member_id in INITIAL_COOP_MEMBERS
                    )
                    for layout in complete_layouts
                )
            if not roster_complete and not persistent_history:
                disarm_feature_gate()
                return False

        menu_info = process.u64(MENU_INFO_GLOBAL)
        if not menu_info:
            disarm_feature_gate()
            return False
        flag_address = menu_info + MENU_SELECTABLE_FLAGS_OFFSET
        raw_flag = process.read(flag_address, 1)
        if len(raw_flag) != 1:
            disarm_feature_gate()
            return False
        current_flag = raw_flag[0]
        desired_flag = current_flag | MENU_START_SELECTABLE_MASK
        selectability_changed = desired_flag != current_flag
        if selectability_changed:
            process.write(flag_address, bytes((desired_flag,)))

        gate_changed = False
        if feature_gate is not None:
            gate_changed = feature_gate.set_enabled(True)
    except (OSError, RuntimeError, TypeError, ValueError, struct.error):
        disarm_feature_gate()
        return False
    return selectability_changed or gate_changed


def normalize_story_party(slots: bytes, members: tuple[int, ...] = None) -> bytes:
    if len(slots) != STORY_SLOT_COUNT:
        raise ValueError("story party must contain nine byte-sized slots")
    if members is None:
        members = INITIAL_COOP_MEMBERS
    if not members:
        return slots

    active = tuple(value for value in slots[:COOP_ACTIVE_SLOT_COUNT] if value != EMPTY)
    if VAAN_ID not in active:
        return slots
    return bytes(
        coop_roster.normalize_party_layout(
            slots,
            members,
            empty=EMPTY,
            prefer_active=False,
        )
    )


def story_table(process: coop.RemoteProcess) -> tuple[int, int, int]:
    battlepack = process.u64(BATTLEPACK_GLOBAL)
    if not battlepack:
        return 0, 0, 0
    section = process.u32(battlepack + 4 + STORY_SECTION_ID * 4)
    if not section or process.read(section, 4) != ST2E_MAGIC:
        return 0, 0, 0
    count = process.u32(section + 4)
    stride = struct.unpack("<H", process.read(section + 8, 2))[0]
    entries = process.u32(section + 0x0C)
    if count < 2 or count > 0x1000 or stride < 0x0B or not entries:
        return 0, 0, 0
    process.read(entries, min(stride, 0x10))
    return entries, count, stride


def st2e_table(
    process: coop.RemoteProcess, section_id: int
) -> tuple[int, int, int, int, int]:
    battlepack = process.u64(BATTLEPACK_GLOBAL)
    if not battlepack:
        return 0, 0, 0, 0, 0
    directory_entry = battlepack + 4 + section_id * 4
    section = process.u32(directory_entry)
    if not section or process.read(section, 4) != ST2E_MAGIC:
        return 0, 0, 0, 0, 0
    count = process.u32(section + 4)
    stride = struct.unpack("<H", process.read(section + 8, 2))[0]
    entries = process.u32(section + 0x0C)
    if not count or count > 0x10000 or not stride or not entries:
        return 0, 0, 0, 0, 0
    process.read(entries, min(stride, 0x10))
    return directory_entry, section, entries, count, stride


def choose_initial_member_template(
    records: list[bytes],
    member_id: int,
) -> bytes:
    candidates = [record for record in records if record[1] == member_id]
    if not candidates:
        raise RuntimeError(
            f"section 59 contains no native template for member {member_id}"
        )

    template = bytearray(min(candidates, key=lambda record: record[2]))
    template[0] = 1
    template[1] = member_id
    return bytes(template)


def build_extended_records(records: list[bytes]) -> list[bytes]:
    expanded = list(records)
    for member_id in INITIAL_COOP_MEMBERS:
        if any(record[0] == 1 and record[1] == member_id for record in expanded):
            continue
        expanded.append(choose_initial_member_template(records, member_id))
    return expanded


def allocate_low(process: coop.RemoteProcess) -> int:
    base = process.ensure_damage_number_theme_allocation()
    return base + coop.STORY_EXTENDED_WORKSPACE_OFFSET


def ensure_initial_coop_extended_records(
    process: coop.RemoteProcess,
) -> tuple[bool, int]:
    directory, section, entries, count, stride = st2e_table(
        process, STORY_EXTENDED_SECTION_ID
    )
    if not section or stride < 2:
        return False, 0
    records = [process.read(entries + index * stride, stride) for index in range(count)]
    expanded = build_extended_records(records)
    if len(expanded) == len(records):
        return False, section

    new_section = allocate_low(process)
    new_entries = new_section + 0x20
    required = 0x20 + len(expanded) * stride
    if required > EXTENDED_ALLOCATION_SIZE:
        raise RuntimeError("expanded story section exceeds private allocation")
    header = bytearray(process.read(section, 0x20))
    struct.pack_into("<I", header, 4, len(expanded))
    struct.pack_into("<I", header, 0x0C, new_entries)
    process.write(new_section, bytes(header) + b"".join(expanded))
    process.write(directory, struct.pack("<I", new_section))
    return True, new_section


def patch_story_records(process: coop.RemoteProcess) -> tuple[int, int]:
    entries, count, stride = story_table(process)
    if not entries:
        return 0, 0
    eligible = 0
    changed = 0
    for record_id in range(count):
        address = entries + record_id * stride
        current = process.read(address, STORY_SLOT_COUNT)
        desired = normalize_story_party(current)
        if desired == current:
            if VAAN_ID in current[:COOP_ACTIVE_SLOT_COUNT]:
                eligible += 1
            continue
        eligible += 1
        process.write(address, desired)
        changed += 1
    return eligible, changed


def main() -> int:
    pid = coop.find_pid()
    lease = helper_lease.HelperLease(pid, "story_party")
    try:
        process = coop.RemoteProcess(pid)
    except BaseException:
        try:
            lease.close()
        except BaseException:
            pass
        raise
    menu_feature_gate = None
    last_table = None
    last_extended = None
    try:
        menu_feature_gate = initialize_early_menu_feature_gate(
            process,
            install_new=START_WITH_ALL,
        )
        print(f"STORY_PARTY_PATCH ready pid={pid}", flush=True)
        while True:
            if runtime_shutdown.requested():
                break
            try:
                battle_info = process.u64(BATTLE_INFO_GLOBAL)
                if enable_party_menu_for_start_all(
                    process,
                    battle_info,
                    feature_gate=menu_feature_gate,
                ):
                    print(
                        "STORY_PARTY_PATCH party_gambits_menus unlocked",
                        flush=True,
                    )

                extended = st2e_table(process, STORY_EXTENDED_SECTION_ID)
                if extended[1] and extended[1] != last_extended:
                    added, section = ensure_initial_coop_extended_records(process)
                    last_extended = section
                    print(
                        "STORY_PARTY_PATCH section59 "
                        f"initial_companions_added={int(added)} section={section:#x}",
                        flush=True,
                    )

                table = story_table(process)
                if table[0] and table != last_table:
                    eligible, changed = patch_story_records(process)
                    print(
                        "STORY_PARTY_PATCH section60 "
                        f"eligible={eligible} changed={changed}",
                        flush=True,
                    )
                    last_table = table
                elif table[0]:
                    _eligible, changed = patch_story_records(process)
                    if changed:
                        print(
                            f"STORY_PARTY_PATCH restored={changed}",
                            flush=True,
                        )

            except OSError:
                if not coop.movement_runtime_process_alive(process):
                    break
                last_table = None
                last_extended = None
            except struct.error:
                # A torn ST2E header during loading is retryable.  RuntimeError
                # is intentionally absent: allocation/layout incompatibility
                # must leave the loop and let the supervisor report/restart it.
                if not coop.movement_runtime_process_alive(process):
                    break
                last_table = None
                last_extended = None
            time.sleep(POLL_SECONDS)
    finally:
        try:
            if menu_feature_gate is not None:
                try:
                    # Only the mutable policy byte is disarmed.  The exact root
                    # and cave are process-lifetime resources for adoption.
                    menu_feature_gate.set_enabled(False)
                except BaseException:
                    pass
        finally:
            try:
                process.close()
            except BaseException:
                pass
            try:
                lease.close()
            except BaseException:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
