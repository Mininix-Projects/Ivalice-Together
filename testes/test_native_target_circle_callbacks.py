from __future__ import annotations

import math
import struct

import pytest

unicorn = pytest.importorskip("unicorn")
from unicorn import UC_ARCH_X86, UC_MODE_64, Uc  # noqa: E402
from unicorn.x86_const import (  # noqa: E402
    UC_X86_REG_EFLAGS,
    UC_X86_REG_R8,
    UC_X86_REG_R9,
    UC_X86_REG_R10,
    UC_X86_REG_R11,
    UC_X86_REG_R12,
    UC_X86_REG_R13,
    UC_X86_REG_R14,
    UC_X86_REG_R15,
    UC_X86_REG_RAX,
    UC_X86_REG_RBP,
    UC_X86_REG_RBX,
    UC_X86_REG_RCX,
    UC_X86_REG_RDI,
    UC_X86_REG_RDX,
    UC_X86_REG_RIP,
    UC_X86_REG_RSI,
    UC_X86_REG_RSP,
)

import native_area_target_circle_controller as area_circle  # noqa: E402
import native_target_circle_router as target_router  # noqa: E402


STACK_BASE = 0x70000000
MANAGER = 0x00300000
NODE = 0x00400000
SECOND_NODE = 0x00408000
OTHER_NODE = 0x00410000
ALLOCATOR = 0x00320000
NATIVE_PALETTE_TABLE = 0x00500000
PALETTE_OBSERVER = 0x00501000
RESOURCE_BASE = 0x12345000


def _map(machine: Uc, address: int, size: int) -> None:
    page = int(address) & ~0xFFF
    end = (int(address) + int(size) + 0xFFF) & ~0xFFF
    machine.mem_map(page, end - page)


def _machine(cave: int, state: int, code: bytes, mailbox: bytes) -> Uc:
    machine = Uc(UC_ARCH_X86, UC_MODE_64)
    allocation = int(cave) & ~0xFFFF
    machine.mem_map(allocation, 0x10000)
    _map(machine, target_router.TARGET_UI_MANAGER_GLOBAL, 8)
    _map(machine, MANAGER, 0x12000)
    _map(machine, NODE, 0x20000)
    _map(machine, NATIVE_PALETTE_TABLE, 0x2000)
    machine.mem_map(STACK_BASE, 0x10000)
    machine.mem_write(cave, code)
    machine.mem_write(state, bytes(mailbox))
    _map(machine, target_router.TARGET_CIRCLE_PALETTE_COMMAND, 0x20)
    # Model the real 0x271650 encoding: command+0x20 receives
    # resource[0x30] + (palette << 38), and command+0x28 receives 6.
    machine.mem_write(
        target_router.TARGET_CIRCLE_PALETTE_COMMAND,
        (
            b"\x44\x89\xc8"
            b"\x48\xc1\xe0\x26"
            b"\x48\x03\x42\x30"
            b"\x48\x89\x41\x20"
            b"\x48\xc7\x41\x28\x06\x00\x00\x00"
            b"\xc3"
        ),
    )
    machine.mem_write(NATIVE_PALETTE_TABLE, b"\x04\x00\x01")
    for node in (NODE, SECOND_NODE, OTHER_NODE):
        resource = node + 0x2000
        machine.mem_write(node + 0xD00, struct.pack("<Q", resource))
        machine.mem_write(resource + 0x30, struct.pack("<Q", RESOURCE_BASE))
    if cave == 0x60000300:
        service = cave - (target_router.CODE_OFFSET - target_router.SERVICE_OFFSET)
        machine.mem_write(
            service,
            target_router.build_target_palette_hooks(service, state),
        )
        selector = service + target_router.TARGET_PALETTE_SELECTOR_OFFSET
        machine.mem_write(
            selector,
            target_router.build_target_palette_selector(selector, state),
        )
    stack = STACK_BASE + 0x8000
    sentinel = allocation + 0xF000
    machine.mem_write(stack, struct.pack("<Q", sentinel))
    machine.reg_write(UC_X86_REG_RSP, stack)
    return machine


def _run(machine: Uc, cave: int) -> None:
    sentinel = (int(cave) & ~0xFFFF) + 0xF000
    machine.emu_start(cave, sentinel, count=10_000)
    assert machine.reg_read(UC_X86_REG_RIP) == sentinel


def _u32(machine: Uc, address: int) -> int:
    return struct.unpack("<I", machine.mem_read(address, 4))[0]


def _u64(machine: Uc, address: int) -> int:
    return struct.unpack("<Q", machine.mem_read(address, 8))[0]


def _ownership(
    *,
    owner_tag: int = target_router.P1_TARGET_OWNER_TAG,
    manager: int = MANAGER,
    allocator: int = ALLOCATOR,
    node: int = NODE,
    target: int = 0x100012,
    palette: int = 4,
    token: int = 7,
) -> target_router.TargetCircleOwnership:
    return target_router.TargetCircleOwnership(
        owner_tag=owner_tag,
        token=token,
        manager=manager,
        allocator=allocator,
        node=node,
        target=target,
        palette=palette,
    )


def _target_mailbox(
    *,
    operation: int,
    ownership: target_router.TargetCircleOwnership | None = None,
    target: int = 0x100008,
    palette: int = 4,
) -> bytearray:
    mailbox = bytearray(
        target_router.NativeTargetCircleRetargeter._initial_state_image()
    )
    if operation == target_router.TARGET_OPERATION_CREATE:
        struct.pack_into(
            "<I", mailbox, target_router.TARGET_OPERATION_OFFSET, operation
        )
        struct.pack_into(
            "<I", mailbox, target_router.TARGET_HANDLE_OFFSET, target
        )
        struct.pack_into("<I", mailbox, target_router.TARGET_VARIANT_OFFSET, 1)
        struct.pack_into(
            "<I",
            mailbox,
            target_router.TARGET_OWNER_OFFSET,
            target_router.P1_TARGET_OWNER_TAG,
        )
        struct.pack_into(
            "<I",
            mailbox,
            target_router.TARGET_DESIRED_PALETTE_OFFSET,
            palette,
        )
    else:
        ownership = ownership or _ownership()
        payload = target_router.NativeTargetCircleRetargeter._owned_payload(
            ownership,
            operation=operation,
            target=target,
            palette=palette,
        )
        mailbox[: len(payload)] = payload
        slot = target_router.TARGET_OWNER_TAGS.index(ownership.owner_tag)
        offset = (
            target_router.TARGET_OWNERSHIP_RECORDS_OFFSET
            + slot * target_router.TARGET_OWNERSHIP_RECORD_SIZE
        )
        mailbox[offset : offset + target_router.TARGET_OWNERSHIP_RECORD_SIZE] = (
            target_router._pack_ownership_record(ownership)
        )
    struct.pack_into("<I", mailbox, 0, 1)
    struct.pack_into("<I", mailbox, target_router.TARGET_SEQUENCE_OFFSET, 17)
    return mailbox


def _seed_owned_list(
    machine: Uc,
    ownership: target_router.TargetCircleOwnership,
    *,
    lifecycle: int = 2,
    head: int | None = None,
    current: int | None = None,
) -> None:
    head = ownership.node if head is None else head
    current = ownership.node if current is None else current
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", ownership.manager),
    )
    machine.mem_write(
        ownership.manager + target_router.TARGET_CIRCLE_ALLOCATOR_OFFSET,
        struct.pack("<Q", ownership.allocator),
    )
    machine.mem_write(ownership.manager + 0x10F00, struct.pack("<Q", head))
    machine.mem_write(ownership.manager + 0x10EF0, struct.pack("<Q", current))
    machine.mem_write(ownership.node + 0xD68, b"\x00" * 8)
    machine.mem_write(ownership.node + 0xD70, b"\x00" * 8)
    machine.mem_write(ownership.node + 0xD54, struct.pack("<H", lifecycle))
    machine.mem_write(
        ownership.node + 0xD2C, struct.pack("<I", ownership.target)
    )
    machine.mem_write(
        ownership.node + 0xD7C, struct.pack("<I", ownership.palette)
    )
    machine.mem_write(
        ownership.node + 0xD00,
        struct.pack("<Q", ownership.node + 0x2000),
    )
    machine.mem_write(
        ownership.node + 0x2000 + 0x30,
        struct.pack("<Q", RESOURCE_BASE),
    )
    machine.mem_write(
        ownership.node + 0xBD0,
        struct.pack("<Q", RESOURCE_BASE + (ownership.palette << 38)),
    )
    machine.mem_write(ownership.node + 0xBD8, struct.pack("<Q", 6))


def _area_mailbox(
    *,
    owner: int,
    radius: float = 8.0,
) -> bytearray:
    mailbox = bytearray(area_circle.STATE_SIZE)
    struct.pack_into("<I", mailbox, 0, 1)
    struct.pack_into("<I", mailbox, area_circle.AREA_SEQUENCE_OFFSET, 19)
    struct.pack_into("<I", mailbox, area_circle.AREA_TARGET_OFFSET, 0x100008)
    struct.pack_into("<I", mailbox, area_circle.AREA_PALETTE_OFFSET, 4)
    struct.pack_into("<f", mailbox, area_circle.AREA_RADIUS_OFFSET, radius)
    struct.pack_into("<I", mailbox, area_circle.AREA_OWNER_TAG_OFFSET, owner)
    mailbox[
        area_circle.AREA_STATE_MAGIC_OFFSET : area_circle.AREA_STATE_MAGIC_OFFSET
        + len(area_circle.AREA_STATE_MAGIC)
    ] = area_circle.AREA_STATE_MAGIC
    return mailbox


@pytest.mark.parametrize(
    "operation",
    (
        target_router.TARGET_OPERATION_CREATE,
        target_router.TARGET_OPERATION_RETARGET,
    ),
)
def test_target_callback_loading_without_manager_fails_closed(
    operation: int,
) -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(cave, state, code, _target_mailbox(operation=operation))
    machine.mem_write(target_router.TARGET_UI_MANAGER_GLOBAL, b"\x00" * 8)

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, state + target_router.TARGET_ACK_SEQUENCE_OFFSET) == 17


def _target_factory_stub(
    node: int,
    *,
    append_to: int | None = None,
    mutate_existing: tuple[int, ...] = (),
) -> bytes:
    """Model CREATE's palette gate/command/store before publishing the node."""

    service = 0x60000200
    create_hook = service + target_router.TARGET_PALETTE_CREATE_HOOK_OFFSET
    code = bytearray(b"\x55\x53\x41\x54\x41\x57\x41\x55\x41\x56")
    code += b"\x48\x83\xec\x28"
    code += b"\x49\x89\xcd\x41\x89\xd6\x89\xd5"  # manager/target
    code += b"\x41\x83\xa5\x68\x0f\x01\x00\xf7"  # manager flag &= ~8
    for existing in mutate_existing:
        code += b"\x49\xba" + struct.pack("<Q", int(existing))
        code += b"\x41\x83\xa2\x4c\x0d\x00\x00\xf7"  # D4C &= ~8
        code += b"\x41\xc7\x82\x30\x0d\x00\x00\x00\x00\x00\x00"
        code += b"\x66\x41\xc7\x82\x54\x0d\x00\x00\x03\x00"
    code += b"\x48\xbb" + struct.pack("<Q", int(node))
    code += b"\x49\xbc" + struct.pack("<Q", NATIVE_PALETTE_TABLE)
    code += b"\x49\xc7\xc7\x01\x00\x00\x00"
    code += b"\x89\xab\x2c\x0d\x00\x00"  # candidate target
    code += b"\x49\x89\x9d\xf0\x0e\x01\x00"  # manager current=candidate
    code += b"\x48\xb8" + struct.pack("<Q", create_hook) + b"\xff\xd0"
    code += b"\x45\x89\xc1"  # native constructor consumes palette in r8/r9
    code += b"\x48\x8b\x93\x00\x0d\x00\x00"
    code += b"\x48\x8d\x8b\xb0\x0b\x00\x00\x45\x33\xc0"
    code += b"\xc7\x44\x24\x20\x01\x00\x00\x00"
    code += (
        b"\x48\xb8"
        + struct.pack("<Q", target_router.TARGET_CIRCLE_PALETTE_COMMAND)
        + b"\xff\xd0"
    )
    code += b"\x44\x89\x8b\x7c\x0d\x00\x00"
    code += b"\x4c\x8b\x93\xd0\x0b\x00\x00"
    code += b"\x49\xbb" + struct.pack("<Q", PALETTE_OBSERVER)
    code += b"\x4d\x89\x13"
    code += b"\x44\x8b\x93\x7c\x0d\x00\x00\x45\x89\x53\x08"
    code += b"\x66\xc7\x83\x54\x0d\x00\x00\x01\x00"
    if append_to is None:
        code += b"\x49\x89\x9d\x00\x0f\x01\x00"  # empty-list head
    else:
        code += b"\x49\xba" + struct.pack("<Q", int(append_to))
        code += b"\x49\x89\x9a\x70\x0d\x00\x00"  # old tail -> new
        code += b"\x4c\x89\x93\x68\x0d\x00\x00"  # new -> old tail
    code += b"\x48\x8b\xc3\x48\x83\xc4\x28"
    code += b"\x41\x5e\x41\x5d\x41\x5f\x41\x5c\x5b\x5d\xc3"
    return bytes(code)


def _target_native_decision_stub(
    node: int,
    *,
    append_to: int,
    mutate_existing: tuple[int, ...] = (),
) -> bytes:
    """Reuse current like 0x2917E0, otherwise enter the allocating stub."""

    code = bytearray(b"\x48\x8b\x81\xf0\x0e\x01\x00")
    code += b"\x48\x85\xc0"
    code += b"\x0f\x84\x07\x00\x00\x00"
    code += b"\x89\x90\x2c\x0d\x00\x00\xc3"
    code += _target_factory_stub(
        node,
        append_to=append_to,
        mutate_existing=mutate_existing,
    )
    return bytes(code)


def _target_retarget_stub(*, final_target: int | None = None) -> bytes:
    """Mirror 0x2917E0's exact command-before-target-before-field order."""

    service = 0x60000200
    command_hook = service + target_router.TARGET_PALETTE_COMMAND_HOOK_OFFSET
    field_hook = service + target_router.TARGET_PALETTE_FIELD_HOOK_OFFSET
    code = bytearray(b"\x55\x53\x56\x41\x54\x41\x57\x48\x83\xec\x30")
    code += b"\x89\xd5"
    code += b"\x48\x8b\x99\xf0\x0e\x01\x00\x48\x8b\xf3"
    code += b"\x49\xbc" + struct.pack("<Q", NATIVE_PALETTE_TABLE)
    code += b"\x49\xc7\xc7\x01\x00\x00\x00"
    # 0x291858: old D2C is still present.  The command gate must nevertheless
    # return the custom palette, and the observer samples BD0 immediately.
    code += b"\x48\xb8" + struct.pack("<Q", command_hook) + b"\xff\xd0"
    code += b"\x48\x8b\x93\x00\x0d\x00\x00"
    code += b"\x48\x8d\x8b\xb0\x0b\x00\x00\x45\x33\xc0"
    code += b"\xc7\x44\x24\x20\x01\x00\x00\x00"
    code += (
        b"\x48\xb8"
        + struct.pack("<Q", target_router.TARGET_CIRCLE_PALETTE_COMMAND)
        + b"\xff\xd0"
    )
    code += b"\x4c\x8b\x93\xd0\x0b\x00\x00"
    code += b"\x49\xbb" + struct.pack("<Q", PALETTE_OBSERVER)
    code += b"\x4d\x89\x13"
    # Native target store (0x29187B), then 0x291893's durable field gate.
    code += b"\x89\xab\x2c\x0d\x00\x00"
    code += b"\x48\xb8" + struct.pack("<Q", field_hook) + b"\xff\xd0"
    code += b"\x89\x83\x7c\x0d\x00\x00"
    code += b"\x41\x89\x43\x08"
    if final_target is not None:
        code += b"\xc7\x83\x2c\x0d\x00\x00" + struct.pack(
            "<I", int(final_target)
        )
    code += b"\x66\xc7\x83\x54\x0d\x00\x00\x01\x00"
    code += b"\x48\x83\xc4\x30\x41\x5f\x41\x5c\x5e\x5b\x5d\xc3"
    return bytes(code)


_HOOK_GPRS = (
    UC_X86_REG_RAX,
    UC_X86_REG_RBX,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_RBP,
    UC_X86_REG_RSI,
    UC_X86_REG_RDI,
    UC_X86_REG_R8,
    UC_X86_REG_R9,
    UC_X86_REG_R10,
    UC_X86_REG_R11,
    UC_X86_REG_R12,
    UC_X86_REG_R13,
    UC_X86_REG_R14,
    UC_X86_REG_R15,
)


def _palette_hook_machine(
    phase: str,
    *,
    custom: bool,
) -> tuple[Uc, int, dict[int, int], int]:
    cave = 0x60000300
    state = 0x60000E70
    old_target = 0x100012
    new_target = 0x100008
    mailbox = bytearray(
        target_router.NativeTargetCircleRetargeter._initial_state_image()
    )
    operation = (
        target_router.TARGET_OPERATION_CREATE
        if phase == "create"
        else target_router.TARGET_OPERATION_RETARGET
    )
    struct.pack_into(
        "<I", mailbox, target_router.TARGET_OPERATION_OFFSET, operation
    )
    struct.pack_into(
        "<I", mailbox, target_router.TARGET_HANDLE_OFFSET, new_target
    )
    struct.pack_into(
        "<I", mailbox, target_router.TARGET_DESIRED_PALETTE_OFFSET, 2
    )
    struct.pack_into(
        "<I", mailbox, target_router.TARGET_EXPECTED_TARGET_OFFSET, old_target
    )
    if phase != "create":
        struct.pack_into("<Q", mailbox, target_router.TARGET_NODE_OFFSET, NODE)
    if custom:
        struct.pack_into("<I", mailbox, 0, 2)
        struct.pack_into(
            "<I",
            mailbox,
            target_router.TARGET_REQUEST_RESERVED_OFFSET,
            target_router.TARGET_PALETTE_OVERRIDE_COOKIE,
        )

    machine = _machine(cave, state, b"\xc3", mailbox)
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL, struct.pack("<Q", MANAGER)
    )
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", NODE))
    machine.mem_write(
        NODE + 0xD2C,
        struct.pack("<I", old_target if phase == "command" else new_target),
    )
    if not custom:
        machine.mem_write(NATIVE_PALETTE_TABLE, b"\xfb")

    service = cave - (target_router.CODE_OFFSET - target_router.SERVICE_OFFSET)
    hook = service + {
        "command": target_router.TARGET_PALETTE_COMMAND_HOOK_OFFSET,
        "field": target_router.TARGET_PALETTE_FIELD_HOOK_OFFSET,
        "create": target_router.TARGET_PALETTE_CREATE_HOOK_OFFSET,
    }[phase]
    initial = {
        UC_X86_REG_RAX: 0x1111111122222222,
        UC_X86_REG_RBX: NODE,
        UC_X86_REG_RCX: MANAGER,
        UC_X86_REG_RDX: new_target,
        UC_X86_REG_RBP: new_target,
        UC_X86_REG_RSI: NODE,
        UC_X86_REG_RDI: 0x7777777788888888,
        UC_X86_REG_R8: 0x8888888899999999,
        UC_X86_REG_R9: 0x99999999AAAAAAAA,
        UC_X86_REG_R10: 0xAAAAAAAA55555555,
        UC_X86_REG_R11: 0xBBBBBBBB66666666,
        UC_X86_REG_R12: NATIVE_PALETTE_TABLE,
        UC_X86_REG_R13: 0xDDDDDDDD88888888,
        UC_X86_REG_R14: 0xEEEEEEEE99999999,
        UC_X86_REG_R15: 0,
    }
    for register, value in initial.items():
        machine.reg_write(register, value)
    entry_stack = STACK_BASE + 0x8008
    sentinel = (cave & ~0xFFFF) + 0xF000
    machine.mem_write(entry_stack, struct.pack("<Q", sentinel))
    machine.reg_write(UC_X86_REG_RSP, entry_stack)
    machine.reg_write(UC_X86_REG_EFLAGS, 0x246)
    return machine, hook, initial, entry_stack


@pytest.mark.parametrize("phase", ("command", "field", "create"))
@pytest.mark.parametrize("custom", (False, True))
def test_palette_load_wrappers_preserve_exact_native_abi(
    phase: str,
    custom: bool,
) -> None:
    machine, hook, before, entry_stack = _palette_hook_machine(
        phase,
        custom=custom,
    )
    flags = machine.reg_read(UC_X86_REG_EFLAGS)

    _run(machine, hook)

    destination = {
        "command": UC_X86_REG_R9,
        "field": UC_X86_REG_RAX,
        "create": UC_X86_REG_R8,
    }[phase]
    for register in _HOOK_GPRS:
        if register != destination:
            assert machine.reg_read(register) == before[register]
    if custom:
        expected = 2
    elif phase == "command":
        expected = 0xFFFFFFFFFFFFFFFB
    else:
        expected = 0xFFFFFFFB
    assert machine.reg_read(destination) == expected
    assert machine.reg_read(UC_X86_REG_EFLAGS) == flags
    assert machine.reg_read(UC_X86_REG_RSP) == entry_stack + 8


@pytest.mark.parametrize(
    "damage",
    ("status", "cookie", "operation", "node", "handle", "old_target", "current"),
)
def test_command_palette_gate_rejects_every_partial_context(damage: str) -> None:
    machine, hook, _before, _stack = _palette_hook_machine(
        "command",
        custom=True,
    )
    state = 0x60000E70
    if damage == "status":
        machine.mem_write(state, b"\x00" * 4)
    elif damage == "cookie":
        machine.mem_write(
            state + target_router.TARGET_REQUEST_RESERVED_OFFSET,
            b"\x00" * 4,
        )
    elif damage == "operation":
        machine.mem_write(
            state + target_router.TARGET_OPERATION_OFFSET,
            struct.pack("<I", target_router.TARGET_OPERATION_CREATE),
        )
    elif damage == "node":
        machine.mem_write(
            state + target_router.TARGET_NODE_OFFSET,
            struct.pack("<Q", OTHER_NODE),
        )
    elif damage == "handle":
        machine.mem_write(
            state + target_router.TARGET_HANDLE_OFFSET,
            struct.pack("<I", 0x100099),
        )
    elif damage == "old_target":
        machine.mem_write(
            state + target_router.TARGET_EXPECTED_TARGET_OFFSET,
            struct.pack("<I", 0x100099),
        )
    else:
        machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", OTHER_NODE))

    _run(machine, hook)

    assert machine.reg_read(UC_X86_REG_R9) == 4


def test_target_create_bootstraps_and_preserves_an_empty_native_list() -> None:
    cave = 0x60000300
    state = 0x60000E70
    target = 0x100008
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(operation=target_router.TARGET_OPERATION_CREATE),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 0x40)
    machine.mem_write(
        target_router.TARGET_CIRCLE_RETARGET,
        _target_factory_stub(NODE),
    )
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(MANAGER + 0x10F00, b"\x00" * 8)
    machine.mem_write(MANAGER + 0x10EF0, b"\x00" * 8)
    machine.mem_write(MANAGER + 0x10FC0, struct.pack("<Q", ALLOCATOR))

    _run(machine, cave)

    assert _u32(machine, state) == 3
    assert _u32(machine, state + target_router.TARGET_ACK_SEQUENCE_OFFSET) == 17
    assert _u64(machine, state + target_router.TARGET_NODE_OFFSET) == NODE
    assert _u32(machine, state + target_router.TARGET_OBSERVED_OFFSET) == target
    assert _u64(machine, MANAGER + 0x10F00) == NODE
    assert _u64(machine, MANAGER + 0x10EF0) == NODE
    assert _u64(machine, PALETTE_OBSERVER) == RESOURCE_BASE + (4 << 38)
    assert _u32(machine, PALETTE_OBSERVER + 8) == 4
    assert _u32(machine, state + target_router.TARGET_FACTORY_ENTERED_OFFSET) == 1
    assert _u64(machine, state + target_router.TARGET_RESULT_MANAGER_OFFSET) == MANAGER
    assert (
        _u64(machine, state + target_router.TARGET_RESULT_ALLOCATOR_OFFSET)
        == ALLOCATOR
    )


def test_target_create_variant_one_rejects_invalid_palette_before_factory() -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(
            operation=target_router.TARGET_OPERATION_CREATE,
            palette=7,
        ),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 0x200)
    machine.mem_write(
        target_router.TARGET_CIRCLE_RETARGET,
        _target_factory_stub(NODE),
    )
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL, struct.pack("<Q", MANAGER)
    )
    machine.mem_write(MANAGER + 0x10F00, b"\x00" * 8)
    machine.mem_write(MANAGER + 0x10EF0, b"\x00" * 8)
    machine.mem_write(MANAGER + 0x10FC0, struct.pack("<Q", ALLOCATOR))

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, state + target_router.TARGET_FACTORY_ENTERED_OFFSET) == 0
    assert _u64(machine, MANAGER + 0x10F00) == 0
    assert _u64(machine, PALETTE_OBSERVER) == 0


def test_target_create_restores_a_preexisting_manager_view() -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(operation=target_router.TARGET_OPERATION_CREATE),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 0x40)
    machine.mem_write(
        target_router.TARGET_CIRCLE_RETARGET,
        _target_factory_stub(
            OTHER_NODE,
            append_to=NODE,
            mutate_existing=(NODE,),
        ),
    )
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(MANAGER + 0x10F00, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10FC0, struct.pack("<Q", ALLOCATOR))
    machine.mem_write(NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(NODE + 0xD54, struct.pack("<H", 2))
    machine.mem_write(NODE + 0xD30, struct.pack("<I", 0x12345678))
    machine.mem_write(NODE + 0xD4C, struct.pack("<I", 0xABCDEF8F))
    machine.mem_write(MANAGER + 0x10F68, struct.pack("<I", 0x1F))
    old_node_before = bytes(machine.mem_read(NODE + 0xD30, 0x28))

    _run(machine, cave)

    assert _u32(machine, state) == 3
    assert _u64(machine, state + target_router.TARGET_NODE_OFFSET) == OTHER_NODE
    assert _u64(machine, MANAGER + 0x10F00) == NODE
    assert _u64(machine, MANAGER + 0x10EF0) == NODE
    assert _u64(machine, NODE + 0xD70) == OTHER_NODE
    assert _u64(machine, OTHER_NODE + 0xD68) == NODE
    assert _u64(machine, OTHER_NODE + 0xD70) == 0
    assert bytes(machine.mem_read(NODE + 0xD30, 0x28)) == old_node_before
    assert _u32(machine, MANAGER + 0x10F68) == 0x1F
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == 2


def test_target_create_hides_current_from_native_retarget_decision() -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    target = 0x100005
    old_target = 0x100006
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(
            operation=target_router.TARGET_OPERATION_CREATE,
            target=target,
        ),
    )
    native_stub = _target_native_decision_stub(
        OTHER_NODE,
        append_to=NODE,
        mutate_existing=(NODE,),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, len(native_stub))
    machine.mem_write(target_router.TARGET_CIRCLE_RETARGET, native_stub)
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(MANAGER + 0x10F00, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10FC0, struct.pack("<Q", ALLOCATOR))
    machine.mem_write(NODE + 0xD2C, struct.pack("<I", old_target))
    machine.mem_write(NODE + 0xD68, b"\x00" * 8)
    machine.mem_write(NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(NODE + 0xD54, struct.pack("<H", 2))
    machine.mem_write(NODE + 0xD30, struct.pack("<I", 0x12345678))
    machine.mem_write(NODE + 0xD4C, struct.pack("<I", 0xABCDEF8F))

    _run(machine, cave)

    assert _u32(machine, state) == 3
    assert _u64(machine, state + target_router.TARGET_NODE_OFFSET) == OTHER_NODE
    assert _u32(machine, NODE + 0xD2C) == old_target
    assert _u64(machine, MANAGER + 0x10F00) == NODE
    assert _u64(machine, MANAGER + 0x10EF0) == NODE
    assert _u64(machine, NODE + 0xD70) == OTHER_NODE
    assert _u64(machine, OTHER_NODE + 0xD68) == NODE
    assert _u32(machine, OTHER_NODE + 0xD2C) == target


def test_target_create_preserves_and_appends_after_two_exact_live_nodes() -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(operation=target_router.TARGET_OPERATION_CREATE),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 0x40)
    machine.mem_write(
        target_router.TARGET_CIRCLE_RETARGET,
        _target_factory_stub(
            OTHER_NODE,
            append_to=SECOND_NODE,
            mutate_existing=(NODE, SECOND_NODE),
        ),
    )
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(MANAGER + 0x10F00, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", SECOND_NODE))
    machine.mem_write(MANAGER + 0x10FC0, struct.pack("<Q", ALLOCATOR))
    machine.mem_write(NODE + 0xD68, b"\x00" * 8)
    machine.mem_write(NODE + 0xD70, struct.pack("<Q", SECOND_NODE))
    machine.mem_write(NODE + 0xD54, struct.pack("<H", 2))
    machine.mem_write(SECOND_NODE + 0xD68, struct.pack("<Q", NODE))
    machine.mem_write(SECOND_NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(SECOND_NODE + 0xD54, struct.pack("<H", 2))
    machine.mem_write(NODE + 0xD30, struct.pack("<I", 0x11223344))
    machine.mem_write(NODE + 0xD4C, struct.pack("<I", 0x5566778F))
    machine.mem_write(SECOND_NODE + 0xD30, struct.pack("<I", 0x99AABBCC))
    machine.mem_write(SECOND_NODE + 0xD4C, struct.pack("<I", 0xDDEEFF8F))
    machine.mem_write(MANAGER + 0x10F68, struct.pack("<I", 0xF))
    old_nodes_before = (
        bytes(machine.mem_read(NODE + 0xD30, 0x28)),
        bytes(machine.mem_read(SECOND_NODE + 0xD30, 0x28)),
    )

    _run(machine, cave)

    assert _u32(machine, state) == 3
    assert _u64(machine, MANAGER + 0x10F00) == NODE
    assert _u64(machine, MANAGER + 0x10EF0) == SECOND_NODE
    assert _u64(machine, NODE + 0xD70) == SECOND_NODE
    assert _u64(machine, SECOND_NODE + 0xD68) == NODE
    assert _u64(machine, SECOND_NODE + 0xD70) == OTHER_NODE
    assert _u64(machine, OTHER_NODE + 0xD68) == SECOND_NODE
    assert _u64(machine, OTHER_NODE + 0xD70) == 0
    assert bytes(machine.mem_read(NODE + 0xD30, 0x28)) == old_nodes_before[0]
    assert (
        bytes(machine.mem_read(SECOND_NODE + 0xD30, 0x28))
        == old_nodes_before[1]
    )
    assert _u32(machine, MANAGER + 0x10F68) == 0xF
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == 2
    assert (
        struct.unpack("<H", machine.mem_read(SECOND_NODE + 0xD54, 2))[0]
        == 2
    )


def test_target_create_rejects_inconsistent_previous_links_before_factory() -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(operation=target_router.TARGET_OPERATION_CREATE),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 0x40)
    machine.mem_write(
        target_router.TARGET_CIRCLE_RETARGET,
        _target_factory_stub(OTHER_NODE, append_to=SECOND_NODE),
    )
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(MANAGER + 0x10F00, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", SECOND_NODE))
    machine.mem_write(MANAGER + 0x10FC0, struct.pack("<Q", ALLOCATOR))
    machine.mem_write(NODE + 0xD68, b"\x00" * 8)
    machine.mem_write(NODE + 0xD70, struct.pack("<Q", SECOND_NODE))
    machine.mem_write(NODE + 0xD54, struct.pack("<H", 2))
    machine.mem_write(SECOND_NODE + 0xD68, b"\x00" * 8)
    machine.mem_write(SECOND_NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(SECOND_NODE + 0xD54, struct.pack("<H", 2))

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, state + target_router.TARGET_FACTORY_ENTERED_OFFSET) == 0
    assert _u64(machine, SECOND_NODE + 0xD70) == 0


@pytest.mark.parametrize(
    ("head", "current", "lifecycle"),
    (
        (0, NODE, 2),
        (NODE, 0, 2),
        (NODE, NODE, 5),
    ),
)
def test_target_create_rejects_torn_or_retiring_native_views_without_factory(
    head: int,
    current: int,
    lifecycle: int,
) -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(operation=target_router.TARGET_OPERATION_CREATE),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 0x40)
    machine.mem_write(
        target_router.TARGET_CIRCLE_RETARGET,
        _target_factory_stub(OTHER_NODE),
    )
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(MANAGER + 0x10F00, struct.pack("<Q", head))
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", current))
    machine.mem_write(MANAGER + 0x10FC0, struct.pack("<Q", ALLOCATOR))
    machine.mem_write(NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(NODE + 0xD54, struct.pack("<H", lifecycle))

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, state + target_router.TARGET_FACTORY_ENTERED_OFFSET) == 0
    assert _u64(machine, state + target_router.TARGET_NODE_OFFSET) == 0
    assert _u64(machine, MANAGER + 0x10F00) == head
    assert _u64(machine, MANAGER + 0x10EF0) == current
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == lifecycle


def test_target_create_factory_failure_is_terminal_and_never_claims_old_current(
) -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(operation=target_router.TARGET_OPERATION_CREATE),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 1)
    machine.mem_write(target_router.TARGET_CIRCLE_RETARGET, b"\xC3")
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(MANAGER + 0x10F00, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10FC0, struct.pack("<Q", ALLOCATOR))
    machine.mem_write(NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(NODE + 0xD54, struct.pack("<H", 2))

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, state + target_router.TARGET_FACTORY_ENTERED_OFFSET) == 1
    assert _u64(machine, state + target_router.TARGET_NODE_OFFSET) == 0
    assert _u64(machine, state + target_router.TARGET_RESULT_MANAGER_OFFSET) == MANAGER
    assert (
        _u64(machine, state + target_router.TARGET_RESULT_ALLOCATOR_OFFSET)
        == ALLOCATOR
    )
    assert _u64(machine, MANAGER + 0x10F00) == NODE
    assert _u64(machine, MANAGER + 0x10EF0) == NODE
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == 2


def test_target_callback_rejects_a_node_removed_by_ko_or_area_rebuild() -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    owned = _ownership(node=OTHER_NODE)
    mailbox = _target_mailbox(
        operation=target_router.TARGET_OPERATION_RETARGET,
        ownership=owned,
    )
    machine = _machine(cave, state, code, mailbox)
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(
        MANAGER + target_router.TARGET_CIRCLE_ALLOCATOR_OFFSET,
        struct.pack("<Q", ALLOCATOR),
    )
    machine.mem_write(MANAGER + 0x10F00, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", NODE))
    machine.mem_write(NODE + 0xD68, b"\x00" * 8)
    machine.mem_write(NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(NODE + 0xD54, struct.pack("<H", 2))
    machine.mem_write(OTHER_NODE + 0xD54, struct.pack("<H", 2))
    machine.mem_write(OTHER_NODE + 0xD2C, struct.pack("<I", owned.target))
    machine.mem_write(OTHER_NODE + 0xD7C, struct.pack("<I", owned.palette))

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, state + target_router.TARGET_ACK_SEQUENCE_OFFSET) == 17
    assert _u64(machine, MANAGER + 0x10F00) == NODE
    assert _u64(machine, MANAGER + 0x10EF0) == NODE
    assert _u32(machine, OTHER_NODE + 0xD2C) == owned.target
    assert _u32(machine, OTHER_NODE + 0xD7C) == owned.palette


@pytest.mark.parametrize("lifecycle", (1, 3, 4, 5))
def test_target_retarget_rejects_every_nonlive_lifecycle(
    lifecycle: int,
) -> None:
    cave = 0x60000300
    state = 0x60000E70
    code = target_router.build_target_dispatcher(cave, state)
    owned = _ownership()
    mailbox = _target_mailbox(
        operation=target_router.TARGET_OPERATION_RETARGET,
        ownership=owned,
    )
    machine = _machine(cave, state, code, mailbox)
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 1)
    machine.mem_write(target_router.TARGET_CIRCLE_RETARGET, b"\xC3")
    _seed_owned_list(machine, owned, lifecycle=lifecycle)

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, state + target_router.TARGET_ACK_SEQUENCE_OFFSET) == 17
    assert _u64(machine, MANAGER + 0x10F00) == NODE
    assert _u64(machine, MANAGER + 0x10EF0) == NODE
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == lifecycle


def test_target_update_waits_for_every_foreign_list_member_to_settle() -> None:
    cave = 0x60000300
    state = 0x60000E70
    owned = _ownership()
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(
            operation=target_router.TARGET_OPERATION_RETARGET,
            ownership=owned,
            target=owned.target,
            palette=1,
        ),
    )
    _seed_owned_list(machine, owned)
    machine.mem_write(NODE + 0xD70, struct.pack("<Q", SECOND_NODE))
    machine.mem_write(SECOND_NODE + 0xD68, struct.pack("<Q", NODE))
    machine.mem_write(SECOND_NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(SECOND_NODE + 0xD54, struct.pack("<H", 5))
    before_record = bytes(
        machine.mem_read(
            state + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET,
            target_router.TARGET_OWNERSHIP_RECORD_SIZE,
        )
    )

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, NODE + 0xD7C) == owned.palette
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == 2
    assert struct.unpack("<H", machine.mem_read(SECOND_NODE + 0xD54, 2))[0] == 5
    assert (
        bytes(
            machine.mem_read(
                state + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET,
                target_router.TARGET_OWNERSHIP_RECORD_SIZE,
            )
        )
        == before_record
    )


def test_target_callback_restores_manager_links_before_terminal_success() -> None:
    cave = 0x60000300
    state = 0x60000E70
    target = 0x100008
    code = target_router.build_target_dispatcher(cave, state)
    owned = _ownership()
    mailbox = _target_mailbox(
        operation=target_router.TARGET_OPERATION_RETARGET,
        ownership=owned,
        target=target,
        palette=2,
    )
    machine = _machine(cave, state, code, mailbox)
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 0x40)
    machine.mem_write(target_router.TARGET_CIRCLE_RETARGET, _target_retarget_stub())
    _seed_owned_list(machine, owned)
    machine.mem_write(NODE + 0xD70, struct.pack("<Q", SECOND_NODE))
    machine.mem_write(SECOND_NODE + 0xD68, struct.pack("<Q", NODE))
    machine.mem_write(SECOND_NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(SECOND_NODE + 0xD54, struct.pack("<H", 2))
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", OTHER_NODE))
    # Current is permitted to identify another native node; use SECOND_NODE.
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", SECOND_NODE))
    foreign_before = bytes(machine.mem_read(SECOND_NODE, 0xE00))

    _run(machine, cave)

    assert _u32(machine, state) == 3
    assert _u32(machine, state + target_router.TARGET_ACK_SEQUENCE_OFFSET) == 17
    assert _u32(machine, state + target_router.TARGET_OBSERVED_OFFSET) == target
    assert _u32(machine, state + target_router.TARGET_OBSERVED_PALETTE_OFFSET) == 2
    assert _u64(machine, MANAGER + 0x10F00) == NODE
    assert _u64(machine, MANAGER + 0x10EF0) == SECOND_NODE
    assert bytes(machine.mem_read(SECOND_NODE, 0xE00)) == foreign_before
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == 1
    assert _u32(machine, NODE + 0xD2C) == target
    assert _u32(machine, NODE + 0xD7C) == 2
    assert _u64(machine, NODE + 0xBD0) == RESOURCE_BASE + (2 << 38)
    state_image = bytes(machine.mem_read(state, target_router.STATE_SIZE))
    updated = target_router._unpack_ownership_record(state_image, 0)
    assert updated is not None
    assert updated.token == owned.token
    assert updated.target == target
    assert updated.palette == 2


def test_retarget_finishes_custom_render_command_before_returning_to_ui_tick() -> None:
    cave = 0x60000300
    state = 0x60000E70
    target = 0x100008
    desired_palette = 2
    code = target_router.build_target_dispatcher(cave, state)
    owned = _ownership(palette=4)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(
            operation=target_router.TARGET_OPERATION_RETARGET,
            ownership=owned,
            target=target,
            palette=desired_palette,
        ),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 0x80)
    machine.mem_write(
        target_router.TARGET_CIRCLE_RETARGET,
        _target_retarget_stub(),
    )
    _seed_owned_list(machine, owned)

    _run(machine, cave)

    # The observer runs immediately after 0x291858's helper, before the native
    # target/field stores and before callback post-validation.  It must already
    # be custom: a terminal-only assertion cannot catch a one-frame blue flash.
    assert _u32(machine, state) == 3
    assert _u64(machine, PALETTE_OBSERVER) == RESOURCE_BASE + (
        desired_palette << 38
    )
    assert _u32(machine, PALETTE_OBSERVER + 8) == desired_palette
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == 1
    assert _u32(machine, NODE + 0xD2C) == target
    assert _u32(machine, NODE + 0xD7C) == desired_palette
    assert _u64(machine, NODE + 0xBD0) == RESOURCE_BASE + (
        desired_palette << 38
    )
    updated = target_router._unpack_ownership_record(
        bytes(machine.mem_read(state, target_router.STATE_SIZE)),
        0,
    )
    assert updated is not None
    assert updated.target == target
    assert updated.palette == desired_palette


def test_target_palette_only_update_is_one_authenticated_callback_transaction() -> None:
    cave = 0x60000300
    state = 0x60000E70
    owned = _ownership()
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(
            operation=target_router.TARGET_OPERATION_RETARGET,
            ownership=owned,
            target=owned.target,
            palette=1,
        ),
    )
    _seed_owned_list(machine, owned)

    _run(machine, cave)

    assert _u32(machine, state) == 3
    assert _u32(machine, NODE + 0xD2C) == owned.target
    assert _u32(machine, NODE + 0xD7C) == 1
    state_image = bytes(machine.mem_read(state, target_router.STATE_SIZE))
    updated = target_router._unpack_ownership_record(state_image, 0)
    assert updated is not None
    assert updated.target == owned.target
    assert updated.palette == 1


def test_target_update_rolls_back_every_native_partial_byte_before_error() -> None:
    cave = 0x60000300
    state = 0x60000E70
    owned = _ownership()
    requested_target = 0x100008
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(
            operation=target_router.TARGET_OPERATION_RETARGET,
            ownership=owned,
            target=requested_target,
            palette=1,
        ),
    )
    _map(machine, target_router.TARGET_CIRCLE_RETARGET, 0x200)
    machine.mem_write(
        target_router.TARGET_CIRCLE_RETARGET,
        _target_retarget_stub(final_target=owned.target),
    )
    _seed_owned_list(machine, owned)
    machine.mem_write(MANAGER + 0x10F68, struct.pack("<I", 0xA5A5A5A5))
    rollback_before = tuple(
        bytes(machine.mem_read(NODE + offset, size))
        for offset, size in ((0xBB0, 0x30), (0xC80, 0x30), (0xD10, 0x70))
    )
    before_record = bytes(
        machine.mem_read(
            state + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET,
            target_router.TARGET_OWNERSHIP_RECORD_SIZE,
        )
    )

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert tuple(
        bytes(machine.mem_read(NODE + offset, size))
        for offset, size in ((0xBB0, 0x30), (0xC80, 0x30), (0xD10, 0x70))
    ) == rollback_before
    assert _u32(machine, MANAGER + 0x10F68) == 0xA5A5A5A5
    assert _u64(machine, PALETTE_OBSERVER) == RESOURCE_BASE + (1 << 38)
    assert _u32(machine, NODE + 0xD2C) == owned.target
    assert _u32(machine, NODE + 0xD7C) == owned.palette
    assert (
        bytes(
            machine.mem_read(
                state + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET,
                target_router.TARGET_OWNERSHIP_RECORD_SIZE,
            )
        )
        == before_record
    )


def test_target_reconcile_updates_only_authenticated_live_metadata() -> None:
    cave = 0x60000300
    state = 0x60000E70
    owned = _ownership()
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(
            operation=target_router.TARGET_OPERATION_RECONCILE,
            ownership=owned,
        ),
    )
    _seed_owned_list(machine, owned)
    machine.mem_write(NODE + 0xD2C, struct.pack("<I", 0x100044))
    machine.mem_write(NODE + 0xD7C, struct.pack("<I", 2))

    _run(machine, cave)

    assert _u32(machine, state) == 3
    assert _u32(machine, state + target_router.TARGET_OBSERVED_OFFSET) == 0x100044
    assert _u32(machine, state + target_router.TARGET_OBSERVED_PALETTE_OFFSET) == 2
    state_image = bytes(machine.mem_read(state, target_router.STATE_SIZE))
    reconciled = target_router._unpack_ownership_record(state_image, 0)
    assert reconciled is not None
    assert reconciled.token == owned.token
    assert reconciled.manager == owned.manager
    assert reconciled.allocator == owned.allocator
    assert reconciled.node == owned.node
    assert reconciled.target == 0x100044
    assert reconciled.palette == 2


@pytest.mark.parametrize("damage", ("token", "checksum", "membership"))
def test_owned_callback_rejects_auth_or_membership_damage_without_mutation(
    damage: str,
) -> None:
    cave = 0x60000300
    state = 0x60000E70
    owned = _ownership()
    code = target_router.build_target_dispatcher(cave, state)
    mailbox = _target_mailbox(
        operation=target_router.TARGET_OPERATION_RETIRE,
        ownership=owned,
    )
    if damage == "token":
        struct.pack_into(
            "<I",
            mailbox,
            target_router.TARGET_AUTH_TOKEN_OFFSET,
            owned.token + 1,
        )
    elif damage == "checksum":
        mailbox[
            target_router.TARGET_OWNERSHIP_RECORDS_OFFSET + 0x28
        ] ^= 0x01
    machine = _machine(cave, state, code, mailbox)
    _seed_owned_list(machine, owned)
    if damage == "membership":
        machine.mem_write(NODE + 0xD68, struct.pack("<Q", OTHER_NODE))
    before_record = bytes(
        machine.mem_read(
            state + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET,
            target_router.TARGET_OWNERSHIP_RECORD_SIZE,
        )
    )

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == 2
    assert _u32(machine, NODE + 0xD2C) == owned.target
    assert _u32(machine, NODE + 0xD7C) == owned.palette
    assert (
        bytes(
            machine.mem_read(
                state + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET,
                target_router.TARGET_OWNERSHIP_RECORD_SIZE,
            )
        )
        == before_record
    )


@pytest.mark.parametrize("owner_tag", target_router.TARGET_OWNER_TAGS)
def test_target_retire_marks_native_node_before_clearing_exact_sidecar(
    owner_tag: int,
) -> None:
    cave = 0x60000300
    state = 0x60000E70
    owned = _ownership(owner_tag=owner_tag)
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(
            operation=target_router.TARGET_OPERATION_RETIRE,
            ownership=owned,
        ),
    )
    _seed_owned_list(machine, owned)

    _run(machine, cave)

    assert _u32(machine, state) == 3
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == 5
    assert _u32(machine, state + target_router.TARGET_OBSERVED_OFFSET) == owned.target
    assert (
        _u32(machine, state + target_router.TARGET_OBSERVED_PALETTE_OFFSET)
        == owned.palette
    )
    slot = target_router.TARGET_OWNER_TAGS.index(owner_tag)
    record = machine.mem_read(
        state
        + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET
        + slot * target_router.TARGET_OWNERSHIP_RECORD_SIZE,
        target_router.TARGET_OWNERSHIP_RECORD_SIZE,
    )
    assert bytes(record) == b"\x00" * target_router.TARGET_OWNERSHIP_RECORD_SIZE


@pytest.mark.parametrize(
    "operation",
    (
        target_router.TARGET_OPERATION_RETARGET,
        target_router.TARGET_OPERATION_RETIRE,
        target_router.TARGET_OPERATION_RECONCILE,
    ),
)
def test_owned_callback_rejects_same_address_reused_by_a_new_allocator(
    operation: int,
) -> None:
    """A flip after Python proof must not mutate a same-address stock node."""

    cave = 0x60000300
    state = 0x60000E70
    owned = _ownership()
    code = target_router.build_target_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _target_mailbox(
            operation=operation,
            ownership=owned,
            target=0x100008,
            palette=2,
        ),
    )
    replacement_allocator = ALLOCATOR + 0x1000
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(
        MANAGER + target_router.TARGET_CIRCLE_ALLOCATOR_OFFSET,
        struct.pack("<Q", replacement_allocator),
    )
    machine.mem_write(MANAGER + 0x10F00, struct.pack("<Q", NODE))
    machine.mem_write(MANAGER + 0x10EF0, struct.pack("<Q", NODE))
    machine.mem_write(NODE + 0xD68, b"\x00" * 8)
    machine.mem_write(NODE + 0xD70, b"\x00" * 8)
    machine.mem_write(NODE + 0xD54, struct.pack("<H", 2))
    machine.mem_write(NODE + 0xD2C, struct.pack("<I", 0x100055))
    machine.mem_write(NODE + 0xD7C, struct.pack("<I", 6))
    before_record = bytes(
        machine.mem_read(
            state + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET,
            target_router.TARGET_OWNERSHIP_RECORD_SIZE,
        )
    )

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert struct.unpack("<H", machine.mem_read(NODE + 0xD54, 2))[0] == 2
    assert _u32(machine, NODE + 0xD2C) == 0x100055
    assert _u32(machine, NODE + 0xD7C) == 6
    assert (
        bytes(
            machine.mem_read(
                state + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET,
                target_router.TARGET_OWNERSHIP_RECORD_SIZE,
            )
        )
        == before_record
    )


def test_area_callback_loading_without_manager_fails_closed() -> None:
    cave = 0x61000100
    state = 0x61000800
    code = area_circle.build_create_dispatcher(cave, state)
    mailbox = _area_mailbox(owner=area_circle.P2_OWNER_TAG)
    machine = _machine(cave, state, code, mailbox)
    machine.mem_write(target_router.TARGET_UI_MANAGER_GLOBAL, b"\x00" * 8)

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, state + area_circle.AREA_ACK_SEQUENCE_OFFSET) == 19


@pytest.mark.parametrize("radius", (0.0, -1.0, math.inf, math.nan))
def test_area_callback_rejects_invalid_radius_without_detaching_native_list(
    radius: float,
) -> None:
    cave = 0x61000100
    state = 0x61000800
    code = area_circle.build_create_dispatcher(cave, state)
    mailbox = _area_mailbox(owner=area_circle.P1_OWNER_TAG, radius=radius)
    machine = _machine(cave, state, code, mailbox)
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(
        MANAGER + area_circle.AREA_CIRCLE_LIST_OFFSET,
        struct.pack("<Q", NODE),
    )

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u64(machine, MANAGER + area_circle.AREA_CIRCLE_LIST_OFFSET) == NODE


@pytest.mark.parametrize(
    "owner",
    (
        area_circle.P1_OWNER_TAG,
        area_circle.P2_OWNER_TAG,
        area_circle.P3_OWNER_TAG,
    ),
)
def test_area_callback_reads_owner_from_mailbox_and_publishes_one_static_body(
    owner: int,
) -> None:
    cave = 0x61000100
    state = 0x61000800
    code = area_circle.build_create_dispatcher(cave, state)
    machine = _machine(cave, state, code, _area_mailbox(owner=owner))
    _map(machine, area_circle.AREA_CIRCLE_FACTORY, 16)
    machine.mem_write(
        area_circle.AREA_CIRCLE_FACTORY,
        b"\x48\xB8" + struct.pack("<Q", NODE) + b"\xC3",
    )
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(
        MANAGER + area_circle.AREA_CIRCLE_LIST_OFFSET,
        b"\x00" * 8,
    )

    _run(machine, cave)

    assert _u32(machine, state) == 3
    assert _u32(machine, state + area_circle.AREA_ACK_SEQUENCE_OFFSET) == 19
    assert _u32(machine, NODE + 0x54) == owner
    assert _u32(machine, NODE + 0x48) == 4
    assert _u64(machine, MANAGER + area_circle.AREA_CIRCLE_LIST_OFFSET) == NODE


def test_area_callback_restores_old_list_when_native_append_fails() -> None:
    cave = 0x61000100
    state = 0x61000800
    code = area_circle.build_create_dispatcher(cave, state)
    machine = _machine(
        cave,
        state,
        code,
        _area_mailbox(owner=area_circle.P2_OWNER_TAG),
    )
    _map(machine, area_circle.AREA_CIRCLE_FACTORY, 16)
    machine.mem_write(
        area_circle.AREA_CIRCLE_FACTORY,
        b"\x48\xB8" + struct.pack("<Q", NODE) + b"\xC3",
    )
    _map(machine, area_circle.AREA_CIRCLE_APPEND, 4)
    machine.mem_write(area_circle.AREA_CIRCLE_APPEND, b"\x31\xC0\xC3")
    machine.mem_write(
        target_router.TARGET_UI_MANAGER_GLOBAL,
        struct.pack("<Q", MANAGER),
    )
    machine.mem_write(
        MANAGER + area_circle.AREA_CIRCLE_LIST_OFFSET,
        struct.pack("<Q", OTHER_NODE),
    )

    _run(machine, cave)

    assert _u32(machine, state) == 4
    assert _u32(machine, state + area_circle.AREA_ACK_SEQUENCE_OFFSET) == 19
    assert _u64(machine, MANAGER + area_circle.AREA_CIRCLE_LIST_OFFSET) == (
        OTHER_NODE
    )
