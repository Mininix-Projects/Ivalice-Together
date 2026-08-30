import struct
import unittest
from unittest import mock

import story_party_patch as story
from story_party_patch import (
    BALTHIER_ID,
    EMPTY,
    build_extended_records,
    normalize_story_party,
)


def _memory_process(chunks: dict[int, bytes]) -> mock.Mock:
    process = mock.Mock()
    memory: dict[int, int] = {}
    for address, payload in chunks.items():
        for offset, value in enumerate(payload):
            memory[address + offset] = value

    def read(address, size):
        try:
            return bytes(memory[address + offset] for offset in range(size))
        except KeyError as error:
            raise OSError(f"unexpected read {address:#x}+{size}") from error

    def write(address, data):
        for offset, value in enumerate(bytes(data)):
            memory[address + offset] = value

    process.read.side_effect = read
    process.write.side_effect = write
    process.patch_code.side_effect = write
    process.memory = memory
    return process


def test_first_vaan_record_unlocks_companions_without_activating_them():
    assert normalize_story_party(
        bytes((0,) + (EMPTY,) * 8), members=(5, BALTHIER_ID)
    ) == bytes((0, EMPTY, EMPTY, EMPTY, 5, BALTHIER_ID, EMPTY, EMPTY, EMPTY))


def test_first_vaan_record_can_add_all_five_permanent_companions():
    members = (5, BALTHIER_ID, 2, 4, 1)
    assert normalize_story_party(bytes((0,) + (EMPTY,) * 8), members=members) == bytes(
        (0, EMPTY, EMPTY, EMPTY, 5, BALTHIER_ID, 2, 4, 1)
    )


def test_reks_record_is_untouched():
    original = bytes((6,) + (EMPTY,) * 8)
    assert normalize_story_party(original) == original


def test_existing_penelo_and_native_balthier_placements_are_preserved():
    original = bytes((0, 1, 2, EMPTY, 3, 4, 5, EMPTY, EMPTY))
    assert normalize_story_party(original, members=(5, BALTHIER_ID)) == original


def test_native_guest_slot_is_preserved_and_missing_companion_uses_reserve():
    original = bytes((0, 2, 3, 8, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY))
    assert normalize_story_party(original, members=(5, BALTHIER_ID)) == bytes(
        (0, 2, 3, 8, 5, EMPTY, EMPTY, EMPTY, EMPTY)
    )


def test_patch_is_idempotent_and_removes_duplicate():
    original = bytes((0, 5, 2, EMPTY, 5, 3, EMPTY, EMPTY, EMPTY))
    expected = bytes((0, 5, 2, EMPTY, EMPTY, 3, EMPTY, EMPTY, EMPTY))
    assert normalize_story_party(original, members=(5, BALTHIER_ID)) == expected
    assert normalize_story_party(expected, members=(5, BALTHIER_ID)) == expected


def test_patch_collapses_a_duplicated_guest_preserving_first_position():
    original = bytes((0, 8, 2, EMPTY, 8, 5, 3, EMPTY, EMPTY))
    expected = bytes((0, 8, 2, EMPTY, EMPTY, 5, 3, EMPTY, EMPTY))
    assert normalize_story_party(original, members=(5, BALTHIER_ID)) == expected
    assert normalize_story_party(expected, members=(5, BALTHIER_ID)) == expected


def test_patch_collapses_native_guest_duplicate_without_moving_slot_three():
    original = bytes((0, 2, 3, 11, 11, 5, EMPTY, EMPTY, EMPTY))
    expected = bytes((0, 2, 3, 11, EMPTY, 5, EMPTY, EMPTY, EMPTY))
    assert normalize_story_party(original, members=(5, BALTHIER_ID)) == expected
    assert normalize_story_party(expected, members=(5, BALTHIER_ID)) == expected


def test_extended_table_adds_lowest_level_native_penelo_to_link_one():
    vaan = bytes((2, 0, 1)) + bytes(41)
    late_penelo = bytes((20, 5, 40)) + bytes(41)
    early_penelo = bytes((25, 5, 2)) + bytes(range(3, 44))
    early_balthier = bytes((30, BALTHIER_ID, 3)) + bytes(range(3, 44))
    with mock.patch.object(story, "INITIAL_COOP_MEMBERS", (5, BALTHIER_ID)):
        expanded = build_extended_records(
            [vaan, late_penelo, early_penelo, early_balthier]
        )
    assert len(expanded) == 6
    assert expanded[-2][0] == 1
    assert expanded[-2][1] == 5
    assert expanded[-2][2:] == early_penelo[2:]
    assert expanded[-1][0] == 1
    assert expanded[-1][1] == BALTHIER_ID
    assert expanded[-1][2:] == early_balthier[2:]


def test_extended_table_does_not_duplicate_existing_link():
    penelo = bytes((1, 5, 2)) + bytes(41)
    balthier = bytes((1, BALTHIER_ID, 2)) + bytes(41)
    with mock.patch.object(story, "INITIAL_COOP_MEMBERS", (5, BALTHIER_ID)):
        assert build_extended_records([penelo, balthier]) == [penelo, balthier]


def test_extended_table_can_create_initial_links_for_all_five_members():
    native = [
        bytes((20 + member_id, member_id, member_id + 1)) + bytes(41)
        for member_id in range(1, 6)
    ]
    members = (5, BALTHIER_ID, 2, 4, 1)
    with mock.patch.object(story, "INITIAL_COOP_MEMBERS", members):
        expanded = build_extended_records(native)
    initial_links = [
        record[1] for record in expanded if record[0] == 1 and record[1] in members
    ]
    assert initial_links == list(members)


def test_early_menu_feature_gate_only_overrides_party_and_gambits():
    cave = 0x10001400
    state = 0x100014F0
    body = story.build_early_menu_feature_gate(cave, state)

    assert len(body) == 30
    assert len(body) <= (
        story.MENU_FEATURE_GATE_STATE_OFFSET - story.MENU_FEATURE_GATE_CAVE_OFFSET
    )
    assert body[:2] == b"\x80\x3d"
    state_target = cave + 7 + struct.unpack("<i", body[2:6])[0]
    assert state_target == state
    assert body[7:19] == bytes.fromhex("74 0A 83 F9 08 74 0A 83 F9 09 74 05")
    assert body[19] == 0xE9
    native_target = cave + 24 + struct.unpack("<i", body[20:24])[0]
    assert native_target == story.MENU_FEATURE_GATE_NATIVE
    assert body[24:] == bytes.fromhex("B8 01 00 00 00 C3")

    try:
        story.build_early_menu_feature_gate(cave, cave + len(body) - 1)
    except RuntimeError as error:
        assert "overlaps its state" in str(error)
    else:
        raise AssertionError("feature-gate body must not overlap its state")


def test_early_menu_feature_gate_installs_fail_closed_and_disarmed():
    base = 0x10000000
    process = _memory_process({
        story.MENU_FEATURE_GATE_CONTEXT_SITE: story.MENU_FEATURE_GATE_CONTEXT,
        story.MENU_FEATURE_GATE_SITE: story.MENU_FEATURE_GATE_ORIGINAL,
        base + story.MENU_FEATURE_GATE_CAVE_OFFSET: (
            b"\x00" * story.MENU_FEATURE_GATE_CAVE_SIZE
        ),
    })
    gate = story.EarlyMenuFeatureGate(process, base)
    gate.install()

    assert process.read(gate.cave, story.MENU_FEATURE_GATE_STATIC_SIZE) == (
        gate.static_image
    )
    assert process.read(gate.state, 1) == b"\x00"
    assert process.read(story.MENU_FEATURE_GATE_SITE, 5) == gate.patch
    assert gate.set_enabled(True)
    assert process.read(gate.state, 1) == b"\x01"
    assert not gate.set_enabled(True)
    assert gate.set_enabled(False)
    assert process.read(gate.state, 1) == b"\x00"


def test_early_menu_feature_gate_adopts_owned_body_without_repatching():
    base = 0x10000000
    cave = base + story.MENU_FEATURE_GATE_CAVE_OFFSET
    state = base + story.MENU_FEATURE_GATE_STATE_OFFSET
    body = story.build_early_menu_feature_gate(cave, state)
    static_image = body.ljust(story.MENU_FEATURE_GATE_STATIC_SIZE, b"\x00")
    patch = b"\xe9" + story.coop.rel32(story.MENU_FEATURE_GATE_SITE, 5, cave)
    process = _memory_process({
        story.MENU_FEATURE_GATE_CONTEXT_SITE: story.MENU_FEATURE_GATE_CONTEXT,
        story.MENU_FEATURE_GATE_SITE: patch,
        cave: static_image + b"\x01" + b"\x00" * 15,
    })
    gate = story.EarlyMenuFeatureGate(process, base)
    gate.install()

    assert process.read(gate.state, 1) == b"\x00"
    process.write.assert_called_once_with(gate.state, b"\x00")
    process.patch_code.assert_not_called()


def test_early_menu_feature_gate_resumes_exact_prepared_image_root_last():
    base = 0x10000000
    cave = base + story.MENU_FEATURE_GATE_CAVE_OFFSET
    state = base + story.MENU_FEATURE_GATE_STATE_OFFSET
    body = story.build_early_menu_feature_gate(cave, state)
    static_image = body.ljust(story.MENU_FEATURE_GATE_STATIC_SIZE, b"\x00")
    process = _memory_process({
        story.MENU_FEATURE_GATE_CONTEXT_SITE: story.MENU_FEATURE_GATE_CONTEXT,
        story.MENU_FEATURE_GATE_SITE: story.MENU_FEATURE_GATE_ORIGINAL,
        cave: static_image + b"\x00" + b"\x00" * 15,
    })
    gate = story.EarlyMenuFeatureGate(process, base)

    gate.install()

    process.patch_code.assert_called_once_with(
        story.MENU_FEATURE_GATE_SITE,
        gate.patch,
    )
    assert process.read(cave, story.MENU_FEATURE_GATE_STATIC_SIZE) == static_image


def test_early_menu_feature_gate_rejects_partial_prepared_cave_without_writes():
    base = 0x10000000
    cave = base + story.MENU_FEATURE_GATE_CAVE_OFFSET
    process = _memory_process({
        story.MENU_FEATURE_GATE_CONTEXT_SITE: story.MENU_FEATURE_GATE_CONTEXT,
        story.MENU_FEATURE_GATE_SITE: story.MENU_FEATURE_GATE_ORIGINAL,
        cave: b"\x90" + b"\x00" * (story.MENU_FEATURE_GATE_CAVE_SIZE - 1),
    })
    gate = story.EarlyMenuFeatureGate(process, base)

    with unittest.TestCase().assertRaisesRegex(RuntimeError, "non-empty or partial"):
        gate.install()

    process.write.assert_not_called()
    process.patch_code.assert_not_called()


def test_early_menu_feature_gate_failure_boundaries_are_retry_safe():
    for failed_address in (
        "cave_before",
        "cave_after",
        "root_before",
        "root_after",
    ):
        base = 0x10000000
        cave = base + story.MENU_FEATURE_GATE_CAVE_OFFSET
        process = _memory_process({
            story.MENU_FEATURE_GATE_CONTEXT_SITE: story.MENU_FEATURE_GATE_CONTEXT,
            story.MENU_FEATURE_GATE_SITE: story.MENU_FEATURE_GATE_ORIGINAL,
            cave: b"\x00" * story.MENU_FEATURE_GATE_CAVE_SIZE,
        })
        gate = story.EarlyMenuFeatureGate(process, base)
        backing_write = process.patch_code.side_effect
        failed = False

        def injected_patch(address, payload):
            nonlocal failed
            stage = "cave" if address == gate.cave else "root"
            target_stage, timing = failed_address.split("_")
            if not failed and stage == target_stage:
                failed = True
                if timing == "after":
                    backing_write(address, payload)
                raise OSError(f"injected {failed_address}")
            backing_write(address, payload)

        process.patch_code.side_effect = injected_patch
        with unittest.TestCase().assertRaisesRegex(OSError, failed_address):
            gate.install()

        process.patch_code.side_effect = backing_write
        gate.install()
        assert process.read(story.MENU_FEATURE_GATE_SITE, 5) == gate.patch
        assert process.read(gate.cave, story.MENU_FEATURE_GATE_STATIC_SIZE) == (
            gate.static_image
        )
        assert process.read(gate.state, 1) == b"\x00"


def test_early_menu_feature_gate_rejects_foreign_site_and_cave_without_writes():
    base = 0x10000000
    foreign = _memory_process({
        story.MENU_FEATURE_GATE_CONTEXT_SITE: story.MENU_FEATURE_GATE_CONTEXT,
        story.MENU_FEATURE_GATE_SITE: b"\x90" * 5,
        base + story.MENU_FEATURE_GATE_CAVE_OFFSET: (
            b"\x00" * story.MENU_FEATURE_GATE_CAVE_SIZE
        ),
    })

    foreign_gate = story.EarlyMenuFeatureGate(foreign, base)
    try:
        foreign_gate.install()
    except RuntimeError as error:
        assert "unexpected executable bytes" in str(error)
    else:
        raise AssertionError("foreign feature-gate bytes must fail closed")
    foreign.write.assert_not_called()
    foreign.patch_code.assert_not_called()

    cave = base + story.MENU_FEATURE_GATE_CAVE_OFFSET
    patch = b"\xe9" + story.coop.rel32(story.MENU_FEATURE_GATE_SITE, 5, cave)
    foreign_cave = _memory_process({
        story.MENU_FEATURE_GATE_CONTEXT_SITE: story.MENU_FEATURE_GATE_CONTEXT,
        story.MENU_FEATURE_GATE_SITE: patch,
        cave: b"\x90" * story.MENU_FEATURE_GATE_CAVE_SIZE,
    })
    cave_gate = story.EarlyMenuFeatureGate(foreign_cave, base)
    try:
        cave_gate.install()
    except RuntimeError as error:
        assert "unknown cave" in str(error)
    else:
        raise AssertionError("foreign cave body must fail closed")
    foreign_cave.write.assert_not_called()
    foreign_cave.patch_code.assert_not_called()


def test_early_menu_feature_gate_rejects_incompatible_abi_context():
    base = 0x10000000
    process = _memory_process({
        story.MENU_FEATURE_GATE_CONTEXT_SITE: b"\x90" * len(
            story.MENU_FEATURE_GATE_CONTEXT
        ),
        story.MENU_FEATURE_GATE_SITE: story.MENU_FEATURE_GATE_ORIGINAL,
        base + story.MENU_FEATURE_GATE_CAVE_OFFSET: (
            b"\x00" * story.MENU_FEATURE_GATE_CAVE_SIZE
        ),
    })
    gate = story.EarlyMenuFeatureGate(process, base)
    try:
        gate.install()
    except RuntimeError as error:
        assert "incompatible ABI context" in str(error)
    else:
        raise AssertionError("an incompatible native ABI must fail closed")
    process.write.assert_not_called()
    process.patch_code.assert_not_called()


def test_prepare_early_menu_gate_leaves_stock_site_alone_when_disabled():
    process = mock.Mock()
    process.read.return_value = story.MENU_FEATURE_GATE_ORIGINAL
    assert (
        story.prepare_early_menu_feature_gate(
            process,
            install_new=False,
        )
        is None
    )
    process.ensure_cave_allocation.assert_not_called()
    process.write.assert_not_called()
    process.patch_code.assert_not_called()


def test_prepare_early_menu_gate_validates_abi_before_allocating():
    process = mock.Mock()

    def read(address, size):
        if address == story.MENU_FEATURE_GATE_SITE:
            return story.MENU_FEATURE_GATE_ORIGINAL
        if address == story.MENU_FEATURE_GATE_CONTEXT_SITE:
            return b"\x90" * size
        raise OSError(f"unexpected read {address:#x}+{size}")

    process.read.side_effect = read
    with mock.patch("builtins.print"):
        assert (
            story.initialize_early_menu_feature_gate(
                process,
                install_new=True,
            )
            is None
        )
    process.ensure_cave_allocation.assert_not_called()
    process.write.assert_not_called()
    process.patch_code.assert_not_called()


def test_prepare_early_menu_gate_adopts_the_current_jump_target():
    owned_base = 0x18000000
    unrelated_base = 0x10000000
    cave = owned_base + story.MENU_FEATURE_GATE_CAVE_OFFSET
    state = owned_base + story.MENU_FEATURE_GATE_STATE_OFFSET
    body = story.build_early_menu_feature_gate(cave, state)
    static_image = body.ljust(story.MENU_FEATURE_GATE_STATIC_SIZE, b"\x00")
    patch = b"\xe9" + story.coop.rel32(story.MENU_FEATURE_GATE_SITE, 5, cave)
    process = _memory_process({
        story.MENU_FEATURE_GATE_CONTEXT_SITE: story.MENU_FEATURE_GATE_CONTEXT,
        story.MENU_FEATURE_GATE_SITE: patch,
        owned_base: story.coop.CAVE_MAGIC,
        cave: static_image + b"\x01" + b"\x00" * 15,
    })
    gate = story.EarlyMenuFeatureGate(process, owned_base)
    process.ensure_cave_allocation.return_value = unrelated_base
    adopted = story.prepare_early_menu_feature_gate(
        process,
        install_new=False,
    )
    assert adopted is not None
    assert adopted.cave == gate.cave
    assert process.read(gate.state, 1) == b"\x00"
    process.ensure_cave_allocation.assert_not_called()
    process.patch_code.assert_not_called()


def test_initialize_early_menu_gate_degrades_only_the_optional_feature():
    process = mock.Mock()
    process.read.return_value = b"\x90" * 5
    with mock.patch("builtins.print") as output:
        assert (
            story.initialize_early_menu_feature_gate(
                process,
                install_new=True,
            )
            is None
        )
    output.assert_called_once()
    assert "party_gambits_gate unavailable" in output.call_args.args[0]
    process.write.assert_not_called()
    process.patch_code.assert_not_called()


def _menu_process(
    slots,
    flag=0x00,
    menu_info=0x51000000,
    *,
    penelo_flags=0,
):
    process = mock.Mock()
    battle_info = 0x41000000
    current_layout = (
        battle_info
        + story.LIVE_PARTY_LAYOUT_BASE
        + story.CURRENT_PARTY_LAYOUT_ID * story.LIVE_PARTY_LAYOUT_STRIDE
    )
    # Keep this native contract independent from the production constants so
    # an incorrect address or width cannot make the test pass circularly.
    flag_address = menu_info + 0x3249
    penelo_flag_address = (
        battle_info
        + story.PARTY_MEMBER_OFFSET
        + story.PENELO_ID * story.PARTY_MEMBER_STRIDE
    )
    state = {"flag": flag}

    def read(address, size):
        if (address, size) == (current_layout, 18):
            return struct.pack("<9H", *slots)
        if (address, size) == (flag_address, 1):
            return bytes((state["flag"],))
        if (address, size) == (penelo_flag_address, 1):
            return bytes((penelo_flags,))
        raise OSError(f"unexpected read {address:#x}+{size}")

    def write(address, data):
        if address != flag_address or len(data) != 1:
            raise OSError("unexpected write")
        state["flag"] = data[0]

    process.read.side_effect = read
    process.write.side_effect = write
    process.u64.return_value = menu_info
    return process, battle_info, flag_address, state


def test_party_menu_unlock_is_disabled_without_start_all():
    process = mock.Mock()
    assert not story.enable_party_menu_for_start_all(process, 0x41000000, enabled=False)
    process.read.assert_not_called()
    process.u64.assert_not_called()
    process.write.assert_not_called()


def test_party_menu_unlock_requires_vaan_or_persistent_coop_history():
    no_vaan = (2, 1, 5, 0xFFFF, *([0xFFFF] * 5))
    process, battle_info, _flag_address, _state = _menu_process(no_vaan)
    assert not story.enable_party_menu_for_start_all(process, battle_info, enabled=True)
    process.u64.assert_not_called()
    process.write.assert_not_called()

    # Once Vaan's handoff added the co-op roster, removing him from the live
    # formation must not hide Party/Gambits again. JOINED_ONCE is save-owned
    # evidence and also survives a helper/gameplay restart without a latch.
    process, battle_info, _flag_address, state = _menu_process(
        no_vaan,
        penelo_flags=story.JOINED_ONCE_FLAG,
    )
    assert story.enable_party_menu_for_start_all(process, battle_info, enabled=True)
    assert state["flag"] == 0x22
    process.u64.assert_called_once_with(story.MENU_INFO_GLOBAL)


def test_full_start_all_layouts_rearm_menu_without_vaan_or_join_history():
    process = mock.Mock()
    battle_info = 0x41000000
    menu_info = 0x51000000
    full_layout = (5, 3, 2, 0xFFFF, 4, 1, 0xFFFF, 0xFFFF, 0xFFFF)
    current_layout = (5, 3, 2, 0xFFFF, *([0xFFFF] * 5))
    layouts = {
        story.CURRENT_PARTY_LAYOUT_ID: current_layout,
        story.DEFAULT_PARTY_LAYOUT_ID: full_layout,
        story.MENU_PARTY_LAYOUT_ID: full_layout,
    }
    penelo_flag_address = (
        battle_info
        + story.PARTY_MEMBER_OFFSET
        + story.PENELO_ID * story.PARTY_MEMBER_STRIDE
    )
    flag_address = menu_info + story.MENU_SELECTABLE_FLAGS_OFFSET
    state = {"flag": 0}

    def read(address, size):
        for layout_id, slots in layouts.items():
            layout_address = (
                battle_info
                + story.LIVE_PARTY_LAYOUT_BASE
                + layout_id * story.LIVE_PARTY_LAYOUT_STRIDE
            )
            if (address, size) == (layout_address, 18):
                return struct.pack("<9H", *slots)
        if (address, size) == (penelo_flag_address, 1):
            return b"\x00"
        if (address, size) == (flag_address, 1):
            return bytes((state["flag"],))
        raise OSError(f"unexpected read {address:#x}+{size}")

    def write(address, data):
        assert address == flag_address and len(data) == 1
        state["flag"] = data[0]

    process.read.side_effect = read
    process.write.side_effect = write
    process.u64.return_value = menu_info
    with mock.patch.object(story, "INITIAL_COOP_MEMBERS", (5, 3, 2, 4, 1)):
        assert story.enable_party_menu_for_start_all(
            process,
            battle_info,
            enabled=True,
        )
    assert state["flag"] == story.MENU_START_SELECTABLE_MASK

    vaan_in_last_slot = (6, 1, 2, 13, 3, 4, 5, 0xFFFF, 0)
    process, battle_info, _flag_address, state = _menu_process(vaan_in_last_slot)
    with mock.patch.object(story, "START_WITH_ALL", True):
        assert story.enable_party_menu_for_start_all(process, battle_info)
    assert state["flag"] == 0x22
    assert state["flag"] & 0x02  # Party: native selectability bit 0x29.
    assert state["flag"] & 0x20  # Gambits: native selectability bit 0x2D.
    process.u64.assert_called_once_with(story.MENU_INFO_GLOBAL)


def test_party_and_gambits_feature_gate_is_armed_only_after_vaan():
    gate = mock.Mock()
    gate.set_enabled.return_value = True
    no_vaan = (6,) + (0xFFFF,) * 8
    process, battle_info, _flag_address, _state = _menu_process(no_vaan)
    assert not story.enable_party_menu_for_start_all(
        process,
        battle_info,
        enabled=True,
        feature_gate=gate,
    )
    gate.set_enabled.assert_called_once_with(False)
    process.u64.assert_not_called()

    gate.reset_mock()
    slots = (0, 5, 3, 0xFFFF, 2, 4, 1, 0xFFFF, 0xFFFF)
    process, battle_info, _flag_address, state = _menu_process(slots, flag=0x22)
    assert story.enable_party_menu_for_start_all(
        process,
        battle_info,
        enabled=True,
        feature_gate=gate,
    )
    gate.set_enabled.assert_called_once_with(True)
    assert state["flag"] == 0x22
    process.write.assert_not_called()


def test_feature_gate_arms_after_successful_selectability_write():
    events = []
    slots = (0, 5, 3, 0xFFFF, 2, 4, 1, 0xFFFF, 0xFFFF)
    process, battle_info, flag_address, state = _menu_process(slots)
    native_read = process.read.side_effect
    native_write = process.write.side_effect

    def record_read(address, size):
        result = native_read(address, size)
        if (address, size) == (flag_address, 1):
            events.append(("read", address, result))
        return result

    def record_write(address, data):
        events.append(("write", address, bytes(data)))
        native_write(address, data)

    gate = mock.Mock()

    def record_gate(enabled):
        events.append(("gate", enabled))
        return True

    process.read.side_effect = record_read
    process.write.side_effect = record_write
    gate.set_enabled.side_effect = record_gate
    assert story.enable_party_menu_for_start_all(
        process,
        battle_info,
        enabled=True,
        feature_gate=gate,
    )
    assert state["flag"] == 0x22
    assert events == [
        ("read", flag_address, b"\x00"),
        ("write", flag_address, b"\x22"),
        ("gate", True),
    ]


def test_feature_gate_arms_last_when_selectability_is_already_idempotent():
    events = []
    slots = (0, 5, 3, 0xFFFF, 2, 4, 1, 0xFFFF, 0xFFFF)
    process, battle_info, _flag_address, state = _menu_process(slots, flag=0x22)
    flag_address = 0x51003249
    native_read = process.read.side_effect
    gate = mock.Mock()

    def record_read(address, size):
        result = native_read(address, size)
        if (address, size) == (flag_address, 1):
            events.append(("read", address, result))
        return result

    def record_gate(enabled):
        events.append(("gate", enabled))
        return True

    process.read.side_effect = record_read
    gate.set_enabled.side_effect = record_gate
    assert story.enable_party_menu_for_start_all(
        process,
        battle_info,
        enabled=True,
        feature_gate=gate,
    )
    assert state["flag"] == 0x22
    process.write.assert_not_called()
    assert events == [
        ("read", flag_address, b"\x22"),
        ("gate", True),
    ]


def test_feature_gate_fails_closed_when_menu_info_is_unavailable():
    slots = (0, 5, 3, 0xFFFF, 2, 4, 1, 0xFFFF, 0xFFFF)
    process, battle_info, _flag_address, _state = _menu_process(slots, menu_info=0)
    gate = mock.Mock()
    gate.set_enabled.return_value = True
    assert not story.enable_party_menu_for_start_all(
        process,
        battle_info,
        enabled=True,
        feature_gate=gate,
    )
    gate.set_enabled.assert_called_once_with(False)
    process.write.assert_not_called()


def test_feature_gate_fails_closed_on_short_selectability_read():
    slots = (0, 5, 3, 0xFFFF, 2, 4, 1, 0xFFFF, 0xFFFF)
    process, battle_info, flag_address, _state = _menu_process(slots)
    native_read = process.read.side_effect

    def short_flag(address, size):
        if (address, size) == (flag_address, 1):
            return b""
        return native_read(address, size)

    process.read.side_effect = short_flag
    gate = mock.Mock()
    gate.set_enabled.return_value = True
    assert not story.enable_party_menu_for_start_all(
        process,
        battle_info,
        enabled=True,
        feature_gate=gate,
    )
    gate.set_enabled.assert_called_once_with(False)
    process.write.assert_not_called()


def test_feature_gate_fails_closed_on_selectability_write_error():
    slots = (0, 5, 3, 0xFFFF, 2, 4, 1, 0xFFFF, 0xFFFF)
    process, battle_info, _flag_address, state = _menu_process(slots)
    process.write.side_effect = OSError("unavailable generation")
    gate = mock.Mock()
    gate.set_enabled.return_value = True
    assert not story.enable_party_menu_for_start_all(
        process,
        battle_info,
        enabled=True,
        feature_gate=gate,
    )
    assert state["flag"] == 0
    gate.set_enabled.assert_called_once_with(False)


def test_party_and_gambits_selectability_uses_native_contract_and_preserves_bits():
    slots = (0, 5, 3, 0xFFFF, 2, 4, 1, 0xFFFF, 0xFFFF)
    process, battle_info, flag_address, state = _menu_process(slots, flag=0xD1)
    assert story.enable_party_menu_for_start_all(process, battle_info, enabled=True)
    assert flag_address == 0x51003249
    assert state["flag"] == 0xF3
    assert state["flag"] & 0x02
    assert state["flag"] & 0x20
    assert (state["flag"] & ~0x22) == (0xD1 & ~0x22)
    process.write.assert_called_once_with(0x51003249, b"\xf3")

    assert not story.enable_party_menu_for_start_all(process, battle_info, enabled=True)
    process.write.assert_called_once()


def test_party_menu_unlock_fails_closed_on_unavailable_structures():
    process = mock.Mock()
    process.read.return_value = b"\x00"  # Short Current-layout read.
    assert not story.enable_party_menu_for_start_all(process, 0x41000000, enabled=True)
    process.u64.assert_not_called()
    process.write.assert_not_called()

    slots = (0,) + (0xFFFF,) * 8
    process, battle_info, _flag_address, _state = _menu_process(slots, menu_info=0)
    assert not story.enable_party_menu_for_start_all(process, battle_info, enabled=True)
    process.write.assert_not_called()


def test_party_menu_unlock_fails_closed_on_short_flag_or_failed_write():
    slots = (0,) + (0xFFFF,) * 8
    process, battle_info, flag_address, _state = _menu_process(slots)
    valid_read = process.read.side_effect

    def short_flag(address, size):
        if (address, size) == (flag_address, 1):
            return b""
        return valid_read(address, size)

    process.read.side_effect = short_flag
    assert not story.enable_party_menu_for_start_all(process, battle_info, enabled=True)
    process.write.assert_not_called()

    process, battle_info, _flag_address, state = _menu_process(slots)
    process.write.side_effect = OSError("unavailable generation")
    assert not story.enable_party_menu_for_start_all(process, battle_info, enabled=True)
    assert state["flag"] == 0


class PartyGambitsRuntimeTests(unittest.TestCase):
    """Executable unittest coverage for the live menu-visibility contract."""

    def test_start_all_off_disarms_the_optional_feature_gate(self):
        gate = mock.Mock()
        process = mock.Mock()
        self.assertFalse(
            story.enable_party_menu_for_start_all(
                process,
                0x41000000,
                enabled=False,
                feature_gate=gate,
            )
        )
        gate.set_enabled.assert_called_once_with(False)
        process.read.assert_not_called()

    def test_initial_vaan_generation_unlocks_party_and_gambits(self):
        slots = (0, 5, 3, 0xFFFF, 2, 4, 1, 0xFFFF, 0xFFFF)
        process, battle_info, _flag_address, state = _menu_process(slots)
        gate = mock.Mock()
        gate.set_enabled.return_value = True
        self.assertTrue(
            story.enable_party_menu_for_start_all(
                process,
                battle_info,
                enabled=True,
                feature_gate=gate,
            )
        )
        self.assertEqual(state["flag"], 0x22)
        gate.set_enabled.assert_called_once_with(True)

    def test_joined_history_rearms_without_vaan_in_live_layout(self):
        # Fran/Ashe/Penelo is a valid late-game active party. Penelo's
        # save-owned JOINED_ONCE bit proves Vaan's initial handoff happened,
        # even though Current no longer contains him at all.
        slots = (2, 1, 5, 0xFFFF, *([0xFFFF] * 5))
        process, battle_info, _flag_address, state = _menu_process(
            slots,
            penelo_flags=story.JOINED_ONCE_FLAG,
        )
        gate = mock.Mock()
        gate.set_enabled.return_value = True
        self.assertTrue(
            story.enable_party_menu_for_start_all(
                process,
                battle_info,
                enabled=True,
                feature_gate=gate,
            )
        )
        self.assertEqual(state["flag"], 0x22)
        gate.set_enabled.assert_called_once_with(True)

    def test_joined_history_reasserts_bits_and_preserves_unrelated_bits(self):
        slots = (4, 3, 1, 0xFFFF, *([0xFFFF] * 5))
        process, battle_info, flag_address, state = _menu_process(
            slots,
            flag=0xD1,
            penelo_flags=story.JOINED_ONCE_FLAG,
        )
        self.assertTrue(
            story.enable_party_menu_for_start_all(
                process,
                battle_info,
                enabled=True,
            )
        )
        self.assertEqual(flag_address, 0x51003249)
        self.assertEqual(state["flag"], 0xF3)
        self.assertEqual(state["flag"] & ~0x22, 0xD1 & ~0x22)

    def test_fresh_reks_or_empty_generation_stays_native(self):
        for slots in (
            (6, *([0xFFFF] * 8)),
            tuple([0xFFFF] * 9),
        ):
            with self.subTest(slots=slots):
                process, battle_info, _flag_address, state = _menu_process(
                    slots,
                    penelo_flags=story.JOINED_ONCE_FLAG,
                )
                gate = mock.Mock()
                self.assertFalse(
                    story.enable_party_menu_for_start_all(
                        process,
                        battle_info,
                        enabled=True,
                        feature_gate=gate,
                    )
                )
                self.assertEqual(state["flag"], 0)
                gate.set_enabled.assert_called_once_with(False)
                process.u64.assert_not_called()


class StoryWorkerLifetimeTests(unittest.TestCase):
    def test_remote_process_open_failure_releases_story_lease(self):
        lease = mock.Mock()
        with (
            mock.patch.object(story.coop, "find_pid", return_value=1234),
            mock.patch.object(story.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(
                story.coop,
                "RemoteProcess",
                side_effect=OSError("OpenProcess failed"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "OpenProcess"):
                story.main()

        lease.close.assert_called_once_with()

    def test_structural_frame_error_escapes_and_cleanup_remains_complete(self):
        process = mock.Mock()
        lease = mock.Mock()
        gate = mock.Mock()
        process.close.side_effect = OSError("injected process close")
        lease.close.side_effect = RuntimeError("injected lease close")
        with (
            mock.patch.object(story.coop, "find_pid", return_value=1234),
            mock.patch.object(story.coop, "RemoteProcess", return_value=process),
            mock.patch.object(story.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(
                story,
                "initialize_early_menu_feature_gate",
                return_value=gate,
            ),
            mock.patch.object(story, "enable_party_menu_for_start_all", return_value=False),
            mock.patch.object(
                story,
                "st2e_table",
                side_effect=RuntimeError("incompatible section 59"),
            ),
            mock.patch.object(story.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(story.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "incompatible section"):
                story.main()

        sleep.assert_not_called()
        gate.set_enabled.assert_called_once_with(False)
        process.close.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_transient_io_retries_only_while_exact_process_handle_is_alive(self):
        process = mock.Mock()
        lease = mock.Mock()
        with (
            mock.patch.object(story.coop, "find_pid", return_value=1234),
            mock.patch.object(story.coop, "RemoteProcess", return_value=process),
            mock.patch.object(story.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(
                story,
                "initialize_early_menu_feature_gate",
                return_value=None,
            ),
            mock.patch.object(story, "enable_party_menu_for_start_all", return_value=False),
            mock.patch.object(
                story,
                "st2e_table",
                side_effect=OSError("loading replaced ST2E"),
            ),
            mock.patch.object(
                story.coop,
                "movement_runtime_process_alive",
                return_value=True,
            ) as alive,
            mock.patch.object(
                story.runtime_shutdown,
                "requested",
                side_effect=(False, True),
            ),
            mock.patch.object(story.time, "sleep") as sleep,
        ):
            self.assertEqual(story.main(), 0)

        alive.assert_called_once_with(process)
        sleep.assert_called_once_with(story.POLL_SECONDS)
        process.close.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_one_gate_cleanup_failure_cannot_strand_process_or_lease(self):
        process = mock.Mock()
        lease = mock.Mock()
        gate = mock.Mock()
        gate.set_enabled.side_effect = RuntimeError("state write failed")
        with (
            mock.patch.object(story.coop, "find_pid", return_value=1234),
            mock.patch.object(story.coop, "RemoteProcess", return_value=process),
            mock.patch.object(story.helper_lease, "HelperLease", return_value=lease),
            mock.patch.object(
                story,
                "initialize_early_menu_feature_gate",
                return_value=gate,
            ),
            mock.patch.object(story.runtime_shutdown, "requested", return_value=True),
        ):
            self.assertEqual(story.main(), 0)

        gate.set_enabled.assert_called_once_with(False)
        process.close.assert_called_once_with()
        lease.close.assert_called_once_with()
